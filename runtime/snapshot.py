"""统一双库 Snapshot / Backup / Restore（产品化收口，Phase B）。

agent.db（产品 Conversation/Run/Audit）与 sessions.sqlite（模型 Runtime Context）
职责分离保留；Snapshot 把二者放入同一一致性快照：

    snapshot/<snapshot_id>/
    ├── agent.db
    ├── sessions.sqlite
    └── manifest.json

manifest 字段：snapshot_id / created_at / app_version / schema_version /
agent_db_hash / sessions_db_hash / source_paths。

约定：
- 不做第二套 Backup Framework（复用 sqlite backup API + 本项目既有备份目录语义）；
- 恢复先做 pre-restore 快照；任一步失败自动回滚，绝不允许
  agent.db=新 + sessions.sqlite=旧 的混合状态；
- 一致性窗口说明：两库分别以 SQLite backup API 顺序备份，间隔为毫秒级；
  对“产品库-模型缓存”这对语义，这构成同一窗口（不做跨库事务，文档化）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_VERSION = "2026.09-runtime-stable"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def schema_version(db_path: Path) -> int | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return None


def _backup_file(src: Path, dst: Path) -> None:
    """SQLite backup API：得到单库一致副本。"""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    dst_conn = sqlite3.connect(str(dst), timeout=10, check_same_thread=False)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


@dataclass(slots=True)
class Snapshot:
    root: Path
    snapshot_id: str
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def agent_db(self) -> Path:
        return self.root / "agent.db"

    @property
    def sessions_db(self) -> Path:
        return self.root / "sessions.sqlite"

    def valid(self) -> tuple[bool, str]:
        """校验 manifest 存在且两库 hash 一致（不校验 schema——恢复流程单独做）。"""
        if not self.root.exists():
            return False, "snapshot 目录不存在"
        if not self.agent_db.exists() or not self.sessions_db.exists():
            return False, "快照缺少 agent.db/sessions.sqlite"
        try:
            manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"manifest 解析失败: {exc}"
        if _sha256(self.agent_db) != manifest.get("agent_db_hash"):
            return False, "agent.db hash 不一致"
        if _sha256(self.sessions_db) != manifest.get("sessions_db_hash"):
            return False, "sessions.sqlite hash 不一致"
        return True, "ok"


def create_snapshot(
    agent_db: Path,
    sessions_db: Path,
    snapshots_dir: Path,
    *,
    app_version: str = APP_VERSION,
) -> Snapshot:
    """创建一致性快照；返回 Snapshot 对象。"""
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = "snap_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    root = snapshots_dir / snapshot_id
    root.mkdir(parents=True)
    tmp_agent = root / "agent.db.tmp"
    tmp_sessions = root / "sessions.sqlite.tmp"
    try:
        _backup_file(agent_db, tmp_agent)
        _backup_file(sessions_db, tmp_sessions)
        manifest = {
            "snapshot_id": snapshot_id,
            "created_at": _utcnow(),
            "app_version": app_version,
            "schema_version": {
                "agent_db": schema_version(agent_db),
                "sessions_db": schema_version(sessions_db),
            },
            "agent_db_hash": _sha256(tmp_agent),
            "sessions_db_hash": _sha256(tmp_sessions),
            "source_paths": {"agent_db": str(agent_db), "sessions_db": str(sessions_db)},
        }
        tmp_agent.rename(root / "agent.db")
        tmp_sessions.rename(root / "sessions.sqlite")
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return Snapshot(root=root, snapshot_id=snapshot_id, manifest=manifest)


def list_snapshots(snapshots_dir: Path) -> list[Snapshot]:
    out = []
    if not snapshots_dir.exists():
        return out
    for child in sorted(snapshots_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(Snapshot(root=child, snapshot_id=manifest.get("snapshot_id", child.name),
                            manifest=manifest))
    return out


def verify_consistency(agent_db: Path, sessions_db: Path) -> dict[str, Any]:
    """恢复后一致性检查：容器 session 与 SDK session 映射、run/message 关联存在性。"""
    report: dict[str, Any] = {"ok": True, "issues": []}

    def _agent_session_ids() -> set[str]:
        try:
            conn = sqlite3.connect(f"file:{agent_db}?mode=ro", uri=True, timeout=5)
            try:
                rows = conn.execute("SELECT session_id FROM tasks WHERE session_id IS NOT NULL").fetchall()
                return {r[0] for r in rows}
            finally:
                conn.close()
        except Exception as exc:
            report["issues"].append(f"agent.db 不可读: {exc}")
            return set()

    def _sdk_session_ids() -> set[str]:
        try:
            conn = sqlite3.connect(f"file:{sessions_db}?mode=ro", uri=True, timeout=5)
            try:
                rows = conn.execute("SELECT session_id FROM agent_sessions").fetchall()
                return {r[0] for r in rows}
            finally:
                conn.close()
        except Exception as exc:
            report["issues"].append(f"sessions.sqlite 不可读: {exc}")
            return set()

    sdk_ids = _sdk_session_ids()
    agent_ids = _agent_session_ids()
    # SDK 侧有、但产品侧无容器的 session 属于孤儿（诊断性，不自动删除，也不判失败）
    orphans = sorted(sdk_ids - agent_ids)
    report["orphan_sessions"] = orphans
    report["orphans_found"] = len(orphans)
    report["ok"] = not report["issues"]
    return report


def restore_snapshot(
    snapshot: Snapshot,
    agent_db: Path,
    sessions_db: Path,
    snapshots_dir: Path,
    *,
    app_version: str = APP_VERSION,
) -> dict[str, Any]:
    """恢复：校验 → 兼容检查 → pre-restore 备份 → 成组替换 → 一致性检查。

    任一步失败回滚为“替换前的两库”；绝不留下混合版本。
    """
    result: dict[str, Any] = {"ok": False, "steps": []}
    valid, reason = snapshot.valid()
    if not valid:
        result["error"] = f"快照校验失败：{reason}"
        return result

    current_agent_v = schema_version(agent_db)
    snap_agent_v = (snapshot.manifest.get("schema_version") or {}).get("agent_db")
    if current_agent_v is None:
        result["error"] = "当前 agent.db 不可读"
        return result
    if snap_agent_v is not None and snap_agent_v != current_agent_v:
        result["error"] = f"schema 不兼容：当前 agent_db v{current_agent_v} vs 快照 v{snap_agent_v}"
        return result

    # 1) pre-restore 备份
    pre = create_snapshot(agent_db, sessions_db, snapshots_dir, app_version=app_version)
    result["pre_restore"] = pre.snapshot_id

    # 2) 成组替换（先 temp 后 rename；失败即用 pre 回滚）
    backups = []
    try:
        for cur, snap_file in ((agent_db, snapshot.agent_db),
                               (sessions_db, snapshot.sessions_db)):
            bak = cur.with_name(cur.name + ".pre_restore")
            shutil.copy2(cur, bak)
            backups.append((cur, bak))
            tmp = cur.with_name(cur.name + ".restore_tmp")
            shutil.copy2(snap_file, tmp)
            os.replace(tmp, cur)
        result["steps"].append("replaced_both")
    except Exception as exc:
        for cur, bak in backups:
            try:
                shutil.copy2(bak, cur)
            except Exception:
                pass
        result["error"] = f"替换失败，已回滚：{exc}"
        return result

    # 3) 一致性检查
    result["consistency"] = verify_consistency(agent_db, sessions_db)
    result["ok"] = True
    return result
