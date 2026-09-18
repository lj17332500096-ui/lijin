"""沙箱快照与回滚：把 code_sandbox/<project> 的文件树拍成快照，失败可一键回滚。

- 快照存到 logs/sandbox_snapshots/<project>/<stamp>/（manifest.json + files 镜像）；
- 单文件 ≤2MB、单次快照总量 ≤50MB，跳过 __pycache__/.git/缓存后缀；
- 每项目最多保留 5 份快照，超出自动删最旧；
- 回滚 = 清空项目目录后按快照还原，支持 dry_run 预览（列出增/删/改清单）。
"""

import hashlib
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from agents import function_tool

from code_exec import SANDBOX_ROOT, _project_dir

BASE_DIR = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = BASE_DIR / "logs" / "sandbox_snapshots"

MAX_SNAPSHOTS = 5
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
SKIP_DIRS = {"__pycache__", ".git", ".venv"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def _snap_base(project: str) -> Path:
    return SNAPSHOT_ROOT / project


def _snap_dir(project: str, snapshot_id: str) -> Path:
    return _snap_base(project) / snapshot_id


def _valid_id(snapshot_id: str) -> bool:
    return bool(snapshot_id) and set(snapshot_id) <= set("0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-")


def _scan_files(project_dir: Path) -> dict[str, dict]:
    """遍历项目目录，返回 {relpath: {sha256, size}}。"""
    result: dict[str, dict] = {}
    total = 0
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(project_dir).parts[:-1]):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            continue
        total += size
        rel = path.relative_to(project_dir).as_posix()
        result[rel] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": size,
        }
    return result


def _prune(project: str) -> None:
    base = _snap_base(project)
    if not base.exists():
        return
    entries = sorted(
        (p for p in base.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in entries[MAX_SNAPSHOTS:]:
        shutil.rmtree(old, ignore_errors=True)


def snapshot_impl(project: str, note: str = "") -> str:
    """给沙箱项目拍快照，返回结果说明。"""
    project = (project or "").strip()
    project_dir = _project_dir(project)
    if project_dir is None:
        return "错误：项目名只能包含字母、数字、下划线和连字符。"
    if not project_dir.exists():
        return "错误：项目还不存在，先用 write_code_file 写第一个文件。"
    files = _scan_files(project_dir)
    if not files:
        return "错误：项目里没有可快照的文件。"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest_files = _snap_dir(project, stamp) / "files"
    dest_files.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created": time.time(),
        "note": (note or "").strip()[:200],
        "files": files,
    }
    (_snap_dir(project, stamp) / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for rel in files:
        src = project_dir / rel
        dst = dest_files / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    _prune(project)
    return (
        f"已拍快照 {stamp}：{len(files)} 个文件"
        f"（总 {sum(f['size'] for f in files.values())} 字节）{('，备注：' + manifest['note']) if manifest['note'] else ''}。"
        f"想回滚：sandbox_rollback(project='{project}')；每项目最多保留 {MAX_SNAPSHOTS} 份。"
    )


def list_snapshots_impl(project: str) -> str:
    """列出项目的全部快照。"""
    project = (project or "").strip()
    if _project_dir(project) is None:
        return "错误：项目名只能包含字母、数字、下划线和连字符。"
    base = _snap_base(project)
    if not base.exists() or not any(base.iterdir()):
        return "该项目还没有快照（用 sandbox_snapshot 拍一份）。"
    lines = []
    for entry in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        note = manifest.get("note", "")
        created = datetime.fromtimestamp(manifest.get("created", 0)).strftime("%m-%d %H:%M:%S")
        lines.append(f"- {entry.name}（{created}，{len(manifest.get('files', {}))} 个文件）{('：' + note) if note else ''}")
    if not lines:
        return "该项目还没有快照。"
    return "\n".join(lines)


def _safe_rel(rel: str) -> bool:
    p = Path(rel)
    return not p.is_absolute() and ".." not in p.parts


def rollback_impl(project: str, snapshot_id: str = "", dry_run: bool = False) -> str:
    """回滚到指定（默认最新）快照；dry_run 只列变更不执行。"""
    project = (project or "").strip()
    project_dir = _project_dir(project)
    if project_dir is None:
        return "错误：项目名只能包含字母、数字、下划线和连字符。"
    base = _snap_base(project)
    if snapshot_id:
        if not _valid_id(snapshot_id):
            return "错误：快照 ID 不合法。"
        snap_dir = base / snapshot_id
    else:
        candidates = (
            sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
            if base.exists()
            else []
        )
        snap_dir = candidates[0] if candidates else None
    if snap_dir is None or not (snap_dir / "manifest.json").exists():
        return "错误：没有可用的快照（先 sandbox_snapshot）。"
    try:
        manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return f"错误：快照损坏（{exc}）。"
    snap_files: dict[str, dict] = manifest.get("files", {})
    if not all(_safe_rel(rel) for rel in snap_files):
        return "错误：快照内容含非法路径，拒绝回滚。"
    current = _scan_files(project_dir) if project_dir.exists() else {}
    changed, added, deleted = [], [], []
    for rel, meta in snap_files.items():
        cur = current.get(rel)
        if cur is None:
            added.append(rel)
        elif cur["sha256"] != meta["sha256"]:
            changed.append(rel)
    for rel in current:
        if rel not in snap_files:
            deleted.append(rel)

    preview = (
        f"快照 {snap_dir.name} 回滚预览：新增 {len(added)}、修改 {len(changed)}、删除 {len(deleted)} 个文件。"
    )
    def _short(items, n=20):
        s = ", ".join(items[:n])
        return s + (" …" if len(items) > n else "")

    if dry_run:
        parts = [preview]
        if added:
            parts.append(f"新增：{_short(added)}")
        if changed:
            parts.append(f"修改：{_short(changed)}")
        if deleted:
            parts.append(f"将删除：{_short(deleted)}")
        parts.append("（dry_run=true，未做任何改动；去掉 dry_run 后执行回滚。）")
        return "\n".join(parts)

    files_dir = snap_dir / "files"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    restored = 0
    for rel in snap_files:
        src = files_dir / rel
        if not src.exists():
            continue
        dst = project_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored += 1
    parts = [f"已回滚到快照 {snap_dir.name}：恢复 {restored} 个文件；{preview.split('：', 1)[1]}"]
    if changed:
        parts.append(f"已还原修改：{_short(changed)}")
    if added:
        parts.append(f"已补回新增：{_short(added)}")
    if deleted:
        parts.append(f"已清除后写入的文件：{_short(deleted)}")
    return "\n".join(parts)


@function_tool
def sandbox_snapshot(project: str, note: str = "") -> str:
    """给代码沙箱项目拍快照（写文件前/动手前建议先拍）。project 是 code_sandbox 里的
    项目名；note 可写一句原因。快照保留最近 5 份，回滚用 sandbox_rollback。"""
    try:
        return snapshot_impl(project, note)
    except Exception as exc:
        return f"快照失败：{type(exc).__name__}: {str(exc)[:300]}"


@function_tool
def sandbox_rollback(project: str, snapshot_id: str = "", dry_run: bool = False) -> str:
    """把沙箱项目回滚到某份快照（默认最新）。snapshot_id 可从 list_sandbox_snapshots 拿到；
    dry_run=true 时只列出增/删/改清单不动文件。回滚会清空项目目录后按快照还原，不可撤销。"""
    try:
        return rollback_impl(project, snapshot_id, dry_run)
    except Exception as exc:
        return f"回滚失败：{type(exc).__name__}: {str(exc)[:300]}"


@function_tool
def list_sandbox_snapshots(project: str) -> str:
    """列出沙箱项目的全部快照（时间、文件数、备注），用于选 snapshot_id 回滚。"""
    try:
        return list_snapshots_impl(project)
    except Exception as exc:
        return f"查询失败：{type(exc).__name__}: {str(exc)[:300]}"
