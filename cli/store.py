"""CLI 的会话视图层。

数据来源是 Runtime 的 TaskManager（`agent.db`）：
  · `tasks` 表里 `session_id` 非空、未归档的行 = 一个「会话容器」（id 形如 tk_xxxxxxxx）
  · `messages` 表按 `task_id` 挂在容器上
所以「切换会话」等价于「切换容器 + 切换 LLM 上下文会话名」，历史展示与命令操作都走这里，
不在 CLI 里直接写 SQL（唯一的写操作是清空历史，也封装在 TaskManager.clear_container_history）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionRow:
    """列表里的一行会话。"""

    container_id: str
    session_id: str
    title: str
    updated_at: str
    pinned: bool = False
    messages: int = 0
    runs: int = 0
    latest_run: dict[str, Any] = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        return self.title.strip() or "(未命名会话)"


class SessionStore:
    """对 Runtime 容器/消息/产物的只读视图 + 少量受控写操作。"""

    def __init__(self, manager) -> None:
        self.mgr = manager

    # ── 列表与定位 ──────────────────────────────────────────────
    def list(self, limit: int = 50) -> list[SessionRow]:
        rows: list[SessionRow] = []
        for raw in self.mgr.list_containers(limit=max(1, min(int(limit), 500))):
            stat = {}
            try:
                stat = self.mgr.container_stat(raw["id"]) or {}
            except Exception:
                stat = {}
            rows.append(
                SessionRow(
                    container_id=str(raw.get("id") or ""),
                    session_id=str(raw.get("session_id") or ""),
                    title=str(raw.get("title") or ""),
                    updated_at=str(raw.get("updated_at") or ""),
                    pinned=bool(raw.get("pinned")),
                    messages=int(stat.get("messages") or 0),
                    runs=int(stat.get("runs") or 0),
                    latest_run=dict(stat.get("latest_run") or {}),
                )
            )
        return rows

    def resolve(self, ref: str | int | None, *, rows: list[SessionRow] | None = None) -> str | None:
        """把用户输入解析成容器 id。

        接受三种写法：列表序号（1 基）、容器 id（tk_xxx）、底层会话名（session_id）。
        """
        if ref is None or (isinstance(ref, str) and not ref.strip()):
            return None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            index = int(ref)
            candidates = rows if rows is not None else self.list()
            if 1 <= index <= len(candidates):
                return candidates[index - 1].container_id
            return None
        text = str(ref).strip()
        if text.startswith("tk_"):
            return text if self.mgr.get_container(text) else None
        for row in (rows if rows is not None else self.list()):
            if row.session_id == text or row.container_id == text:
                return row.container_id
        return None

    def get(self, container_id: str) -> SessionRow | None:
        raw = self.mgr.get_container(container_id)
        if raw is None:
            return None
        stat = {}
        try:
            stat = self.mgr.container_stat(container_id) or {}
        except Exception:
            pass
        return SessionRow(
            container_id=str(raw.get("id") or ""),
            session_id=str(raw.get("session_id") or ""),
            title=str(raw.get("title") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            pinned=bool(raw.get("pinned")),
            messages=int(stat.get("messages") or 0),
            runs=int(stat.get("runs") or 0),
            latest_run=dict(stat.get("latest_run") or {}),
        )

    def find_by_session_id(self, session_id: str) -> SessionRow | None:
        for row in self.list():
            if row.session_id == session_id:
                return row
        return None

    # ── 消息 ────────────────────────────────────────────────────
    def messages(self, container_id: str, limit: int = 20) -> list[dict]:
        items = self.mgr.list_messages(container_id, limit=2000)
        if limit and limit > 0:
            items = items[-limit:]
        return items

    def rename(self, container_id: str, title: str) -> bool:
        return bool(self.mgr.set_container_title(container_id, str(title)[:120]))

    def clear(self, container_id: str) -> dict:
        return self.mgr.clear_container_history(container_id)

    # ── 产物 / 诊断 ─────────────────────────────────────────────
    def artifacts(self, session_id: str | None = None, limit: int = 20) -> list[dict]:
        try:
            return self.mgr.list_artifacts(session_id=session_id, limit=limit)
        except Exception:
            return []

    def diagnose(self, container_id: str, *, event_limit: int = 8, attempt_limit: int = 5) -> dict:
        """聚合一个会话的诊断信息（最近一次 Run 的状态、事件、provider 尝试）。"""
        stat = {}
        try:
            stat = self.mgr.container_stat(container_id) or {}
        except Exception:
            pass
        latest = dict(stat.get("latest_run") or {})
        run_id = str(latest.get("id") or "")
        result: dict[str, Any] = {
            "container_id": container_id,
            "runs": int(stat.get("runs") or 0),
            "messages": int(stat.get("messages") or 0),
            "latest_run": latest,
            "events": [],
            "provider_attempts": [],
            "artifacts": [],
        }
        if not run_id:
            return result
        try:
            result["events"] = [
                {
                    "type": getattr(event, "event_type", "") or "",
                    "at": getattr(event, "created_at", "") or "",
                    "payload": getattr(event, "payload", {}) or {},
                }
                for event in (self.mgr.list_events(run_id, limit=event_limit) or [])
            ]
        except Exception:
            result["events"] = []
        try:
            result["provider_attempts"] = [
                dict(row) for row in (self.mgr.list_provider_attempts(run_id, limit=attempt_limit) or [])
            ]
        except Exception:
            result["provider_attempts"] = []
        try:
            result["artifacts"] = [
                {"id": a.get("id"), "name": a.get("name"), "kind": a.get("kind")}
                for a in (self.mgr.list_artifacts(task_id=run_id, limit=10) or [])
            ]
        except Exception:
            result["artifacts"] = []
        return result
