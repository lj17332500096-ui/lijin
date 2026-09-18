"""Context / Session Preparation：Run 前对 SDK Session 历史做有界化处理。

解决 P0：Web / Project 对话历史在 sessions.sqlite 无限增长、每轮全量送模型。

职责（只做 Session History，不做 Project Context / Prompt / Tool Router）：
1. 廉价统计（直接 SQL 数行/量字节，普通短对话零模型成本）；
2. Soft Limit：复用 compact.py 的 AUTO_SUMMARY_* 判定，真正调用同一套 compact
   （compact_history 支持超长历史分块摘要，不重造第二套 compact）；
3. Hard Limit：compact 失败/未触发但历史仍超硬边界时，Runtime 硬窗口
   （按完整逻辑 Turn 切割，保证 tool_call ↔ tool_result 成对，不依赖模型）；
4. 记录 context 指标/事件（compaction/window 是否发生、前后规模），供开发者审计。

设计约束：
- 普通 3~10 轮短对话：只做一次 SQL 计数，绝不调用摘要模型；
- Current User Message 永远不在本次被压缩（本轮消息由 SDK 在 Run 开始时才写入 Session）；
- Project Context（说明/来源/项目记忆/工作位置）不进 Conversation Summary；
- Global Memory 不参与摘要输入；Compact 只消费 Conversation History。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import compact

#: 新配置（默认值即保守档；全部可被 .env 覆盖）
DEFAULT_HARD_CHARS = 260_000   # 硬字符/粗略 token 上限（应不低于软阈值 AUTO_SUMMARY_TRIGGER_CHARS）
DEFAULT_HARD_MESSAGES = 500    # 硬条数上限（防海量短消息撑爆）

#: 事件名（task_events append-only 存储）
EV_COMPACTION_STARTED = "context.compaction.started"
EV_COMPACTION_COMPLETED = "context.compaction.completed"
EV_COMPACTION_FAILED = "context.compaction.failed"
EV_WINDOWED = "context.history_windowed"
EV_METRICS = "context.metrics"

ProgressCb = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class SessionStats:
    """一次会话历史的规模指标（与 compact.py 同口径的粗略 token≈字符估算）。"""

    messages: int = 0
    chars: int = 0
    est_tokens: int = 0
    user_turns: int = 0
    has_previous_summary: bool = False
    fast: bool = False  # True = 由 SQL 快路径估算（chars 为字节量，仅用于提前判定）


@dataclass(slots=True)
class PrepResult:
    """一次 Session Preparation 的结果与指标（metrics 进事件/审计，不打扰 UI）。"""

    action: str = "none"  # none | compacted | windowed | compact_failed | window_failed
    reason: str = ""
    before: SessionStats = field(default_factory=SessionStats)
    after: SessionStats = field(default_factory=SessionStats)
    summary_chars: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def guard_enabled() -> bool:
    return os.getenv("FORGE_CONTEXT_GUARD", "on").strip().lower() not in ("off", "false", "0")


def hard_limits() -> tuple[int, int]:
    """返回 (hard_chars, hard_messages)。hard_chars 至少不低于软阈值字符数。"""
    compact._refresh_thresholds()
    hard_chars = _env_int("FORGE_HISTORY_HARD_CHARS", DEFAULT_HARD_CHARS)
    hard_msgs = _env_int("FORGE_HISTORY_HARD_MESSAGES", DEFAULT_HARD_MESSAGES)
    return max(hard_chars, compact.TRIGGER_CHARS), max(1, hard_msgs)


# ---------------------------------------------------------------------------
# 快路径统计（只做 SQL，不构造 item）
# ---------------------------------------------------------------------------


async def quick_stats(session: Any) -> SessionStats | None:
    """直接读 sessions.sqlite 的行数与字节量做廉价的“会不会接近阈值”判定。

    session 缺少 db_path（内存库/测试替身）时返回 None，由调用方走精确路径。
    """
    db_path = getattr(session, "db_path", None)
    table = getattr(session, "messages_table", "agent_messages")
    if not db_path or str(db_path) == ":memory:":
        return None

    def _sync() -> SessionStats | None:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute(
                    f"SELECT COUNT(*), COALESCE(SUM(LENGTH(message_data)), 0) FROM {table} "
                    f"WHERE session_id = ?",
                    (session.session_id,),
                ).fetchone()
                approx_users = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id = ? AND "
                    f"(message_data LIKE '%\"role\": \"user\"%' "
                    f"OR message_data LIKE '%\"role\":\"user\"%')",
                    (session.session_id,),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return None
        if row is None:
            return None
        return SessionStats(messages=int(row[0]), chars=int(row[1]),
                            est_tokens=int(row[1]),
                            user_turns=int(approx_users[0] if approx_users else 0),
                            fast=True)

    return await asyncio.to_thread(_sync)


# ---------------------------------------------------------------------------
# 精确统计（构造 item 列表，与 compact.py 同口径）
# ---------------------------------------------------------------------------


def precise_stats(items: list[Any]) -> SessionStats:
    stats = SessionStats(messages=len(items))
    stats.chars = sum(compact._item_rough_chars(item) for item in items)
    stats.est_tokens = stats.chars
    stats.user_turns = sum(1 for item in items if compact._is_user_message(item))
    stats.has_previous_summary = any(compact._is_previous_summary(item) for item in items)
    return stats


# ---------------------------------------------------------------------------
# Hard Window（纯函数，可离线测试；按完整逻辑 Turn 切割）
# ---------------------------------------------------------------------------


def window_history(
    items: list[Any],
    *,
    keep_turns: int,
    max_chars: int,
    max_messages: int,
) -> list[Any]:
    """Runtime 硬窗口：保留最近 keep_turns 个用户轮次（完整 Turn，含 tool 成对），
    头部已有的 system 摘要继续保留；仍超硬预算时从旧到新丢弃整轮。

    切割只在「用户消息起点」进行：绝不会留下孤立的 tool_call/tool_result。
    """
    if not items:
        return []

    user_indices = [i for i, item in enumerate(items) if compact._is_user_message(item)]
    if not user_indices:
        # 没有用户消息（纯 system/工具遗留）：保守保留最近一条
        return items[-1:]

    # 头部保留：只保留第一个用户消息之前出现的旧摘要（少量 system 行）
    first_user = user_indices[0]
    head = [item for i, item in enumerate(items) if i < first_user and compact._is_previous_summary(item)]
    head_chars = sum(compact._item_rough_chars(item) for item in head)

    # 候选起点：最近 keep_turns 个用户轮次的起点（升序）
    candidates = user_indices[-keep_turns:] if len(user_indices) > keep_turns else user_indices

    chosen: list[Any] | None = None
    # 在预算内尽量保留更多轮：从最早的候选起点开始尝试（tail 最大）
    for start in candidates:
        tail = items[start:]
        tail_chars = sum(compact._item_rough_chars(item) for item in tail)
        if head_chars + tail_chars <= max_chars and len(head) + len(tail) <= max_messages:
            chosen = head + tail
            break
    if chosen is None:
        # 极端情况：连最近一轮都超预算 → 只保留最近一轮，最多 max_messages 条
        last_turn = items[user_indices[-1]:]
        chosen = head + (last_turn[-max_messages:] if len(last_turn) > max_messages else last_turn)
    return chosen


# ---------------------------------------------------------------------------
# Context Overflow 强制压缩（Provider 返回 context_length_exceeded 时的兜底：只一次）
# ---------------------------------------------------------------------------


async def force_compact(session: Any, *, progress: ProgressCb | None = None) -> tuple[str, str]:
    """Provider 报上下文溢出时调用：锁内强制 compact 一次（不依赖软阈值命中）。

    返回 (status, summary_text)；失败时调用方应按普通失败收口（不再重试压缩）。
    """
    session_id = str(getattr(session, "session_id", "")) or "?"
    if session is None or not hasattr(session, "get_items"):
        return "skipped", ""
    async with _lock_for(session_id):
        try:
            items = await session.get_items(limit=10_000_000)
            if not items:
                return "skipped", ""
            stats = precise_stats(items)
            if progress is not None:
                try:
                    await progress(EV_COMPACTION_STARTED, {
                        "messages_before": stats.messages, "chars_before": stats.chars,
                        "reason": "provider_context_overflow",
                    })
                except Exception:
                    pass
            summary, status = await compact.compact_history(session)
            if status == "compacted":
                if progress is not None:
                    try:
                        await progress(EV_COMPACTION_COMPLETED, {
                            "messages_before": stats.messages, "summary_chars": len(summary or ""),
                            "reason": "provider_context_overflow",
                        })
                    except Exception:
                        pass
                return "compacted", summary or ""
            if progress is not None:
                try:
                    await progress(EV_COMPACTION_FAILED, {
                        "messages_before": stats.messages, "status": status,
                        "reason": "provider_context_overflow",
                        "provider_kind": compact.last_summary_error().get("kind"),
                    })
                except Exception:
                    pass
            return "compact_failed", ""
        except Exception:
            return "error", ""


# ---------------------------------------------------------------------------
# Compact 并发控制（Phase 2）：摘要模型在线程执行；per-session 锁防同会话重复压缩
# ---------------------------------------------------------------------------

_COMPACT_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _COMPACT_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _COMPACT_LOCKS[session_id] = lock
    return lock


def _compact_eligible(stats: SessionStats, soft_turns: int, soft_chars: int,
                      min_turns: int, keep_turns: int) -> bool:
    return (
        stats.user_turns >= min_turns
        and stats.user_turns > keep_turns
        and (stats.user_turns >= soft_turns or stats.chars >= soft_chars)
    )


async def _soft_compact_once(session: Any, emit: ProgressCb) -> tuple[str, str]:
    """在 per-session 锁内执行一次真实判断与 compact；返回 (status, summary_text)。

    锁内会重新读取并复核（并发请求可能已把历史压缩），不会同会话并行 compact 两次。
    """
    session_id = str(getattr(session, "session_id", "")) or "?"
    async with _lock_for(session_id):
        items = await session.get_items(limit=10_000_000)
        if not items:
            return "skipped", ""
        stats = precise_stats(items)
        compact._refresh_thresholds()
        eligible = _compact_eligible(
            stats, compact.TRIGGER_USER_TURNS, compact.TRIGGER_CHARS,
            compact.MIN_USER_TURNS, compact.KEEP_USER_TURNS,
        )
        if not eligible:
            return "skipped", ""
        await emit(EV_COMPACTION_STARTED, {
            "messages_before": stats.messages,
            "chars_before": stats.chars,
            "est_tokens_before": stats.est_tokens,
            "user_turns_before": stats.user_turns,
            "reason": "turns" if stats.user_turns >= compact.TRIGGER_USER_TURNS else "chars",
        })
        summary, status = await compact.compact_history(session)
        if status == "compacted":
            after_items = await session.get_items(limit=10_000_000)
            after = precise_stats(after_items)
            await emit(EV_COMPACTION_COMPLETED, {
                "messages_before": stats.messages,
                "messages_after": after.messages,
                "chars_after": after.chars,
                "est_tokens_after": after.est_tokens,
                "user_turns_after": after.user_turns,
                "summary_chars": len(summary or ""),
                "hard_window_applied": False,
            })
            return "compacted", summary or ""
        await emit(EV_COMPACTION_FAILED, {
            "messages_before": stats.messages, "status": status,
            "provider_kind": compact.last_summary_error().get("kind"),
        })
        return "compact_failed", ""


# ---------------------------------------------------------------------------
# Session History 生命周期一致性（Phase 5）
# 职责语义：
# - agent.db.messages = 产品原始 Conversation Record（UI/审计；Compact 永不删除它）
# - sessions.sqlite    = 模型 Runtime Context History / Cache（可被 Compact 改写）
# 二者允许内容不同，但生命周期必须一致（删除/重置联动）。
# ---------------------------------------------------------------------------


def delete_session_history(session_id: str, db_path: str) -> None:
    """删除某个 SDK Session 的模型历史（Project/容器删除时调用）。"""
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
        try:
            conn.execute("DELETE FROM agent_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def session_ids(db_path: str) -> list[str]:
    """列出 sessions.sqlite 中全部 session key。"""
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT session_id FROM agent_sessions").fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def orphan_session_ids(db_path: str, known_session_ids: list[str]) -> list[str]:
    """诊断：没有对应 Project/容器 的 SDK Session（不自动删除，只识别统计）。"""
    known = set(known_session_ids)
    return [sid for sid in session_ids(db_path) if sid not in known]


# ---------------------------------------------------------------------------
# Session Preparation（主入口）
# ---------------------------------------------------------------------------


async def prepare_session_context(
    session: Any,
    *,
    progress: ProgressCb | None = None,
) -> PrepResult:
    """Run 前调用一次：检查并收敛该会话历史；任何失败都不影响本轮继续。

    session: SDK SQLiteSession（或实现了 get_items/clear_session/add_items 的替身）。
    progress: 可选异步回调(action, payload)，用于向 run/task 事件流广播进度。
    """
    result = PrepResult()
    if session is None or not guard_enabled() or not hasattr(session, "get_items"):
        return result

    compact._refresh_thresholds()
    soft_chars = compact.TRIGGER_CHARS
    soft_turns = compact.TRIGGER_USER_TURNS
    min_turns = compact.MIN_USER_TURNS
    keep_turns = compact.KEEP_USER_TURNS
    hard_chars, hard_msgs = hard_limits()

    async def _emit(action: str, payload: dict[str, Any]) -> None:
        result.events.append({"action": action, "payload": payload})
        if progress is not None:
            try:
                await progress(action, payload)
            except Exception:
                pass

    def _metrics() -> dict[str, Any]:
        return {
            "action": result.action,
            "reason": result.reason,
            "messages_before": result.before.messages,
            "chars_before": result.before.chars,
            "est_tokens_before": result.before.est_tokens,
            "user_turns_before": result.before.user_turns,
            "has_summary_before": result.before.has_previous_summary,
            "messages_after": result.after.messages,
            "chars_after": result.after.chars,
            "est_tokens_after": result.after.est_tokens,
            "user_turns_after": result.after.user_turns,
            "summary_chars": result.summary_chars,
            "hard_window_applied": result.action == "windowed",
        }

    # ---- 1) 快路径：轮数/字节都远低于阈值时零成本放行（只留一条轻量 metrics） ----
    quick = await quick_stats(session)
    if quick is not None:
        quick_bytes = quick.chars
        if (
            quick.user_turns < soft_turns
            and quick_bytes < soft_chars
            and quick_bytes < hard_chars
            and quick.messages < hard_msgs
        ):
            result.before = quick
            result.after = quick
            await _emit(EV_METRICS, _metrics())
            return result

    # ---- 2) 精确路径 ----
    try:
        items = await session.get_items(limit=10_000_000)
    except Exception:
        result.reason = "session read failed"
        return result
    if not items:
        await _emit(EV_METRICS, _metrics())
        return result

    before = precise_stats(items)
    result.before = before
    result.after = before

    # ---- 3) Soft Limit：进同一套 compact（异步化 + per-session 锁，防同会话并发双压缩） ----
    items_now = items
    if _compact_eligible(before, soft_turns, soft_chars, min_turns, keep_turns):
        status, summary_text = await _soft_compact_once(session, _emit)
        if status == "compacted":
            result.action = "compacted"
            result.reason = "auto_summary"
            result.summary_chars = len(summary_text or "")
            items_now = await session.get_items(limit=10_000_000)
            result.after = precise_stats(items_now)
        elif status == "compact_failed":
            result.action = "compact_failed"
            result.reason = "compact failed"
        else:
            # 锁内复查发现已不满足（并发请求刚完成 compact）→ 无操作
            result.action = "none"
            result.reason = "already_compacted"

    # ---- 4) Hard Limit：历史（compact 后仍）超硬边界 → Runtime 硬窗口（不依赖模型） ----
    current = precise_stats(items_now)
    if current.chars >= hard_chars or current.messages >= hard_msgs:
        windowed_items = window_history(
            items_now, keep_turns=keep_turns, max_chars=hard_chars, max_messages=hard_msgs,
        )
        if len(windowed_items) < len(items_now):
            ok = await compact._atomic_replace(session, items_now, windowed_items)
            after_items = await session.get_items(limit=10_000_000)
            result.after = precise_stats(after_items)
            if ok:
                result.action = "windowed"
                result.summary_chars = 0
                await _emit(EV_WINDOWED, _metrics())
            else:
                result.action = "window_failed"
                result.reason = "hard window replace failed"
                await _emit(EV_METRICS, _metrics())
        elif result.action == "compacted":
            await _emit(EV_METRICS, _metrics())
        else:
            await _emit(EV_METRICS, _metrics())
    else:
        await _emit(EV_METRICS, _metrics())

    return result
