"""网页聊天界面：本地浏览器里和「全能助手」对话。

运行：python webapp.py          # 默认 http://127.0.0.1:8765
      python webapp.py --port 9000 --open

使用 Starlette（已随环境安装），SSE 推送工具调用与最终回复；
会话历史存同一份 sessions.sqlite，与终端入口互通。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, AsyncGenerator

import uvicorn
from agents import (
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
)
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.memory import SQLiteSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import compact
import main as main_module
from agent import assistant_agent
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from schemas import AgentReply

# llama-ui 前端适配层（OpenAI 协议 → AgentRuntime）
import llama_bridge

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
MATERIALS_DIR = BASE_DIR / "materials"
MATERIAL_EXTS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".md",
    ".csv", ".json", ".yaml", ".yml", ".png", ".jpg", ".jpeg", ".html",
    ".css", ".js", ".py", ".zip",
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


async def api_material_upload(request: Request) -> JSONResponse:
    """POST /api/artifacts/upload?name=file.pdf —— 上传一份资料到 materials/ 并登记产物。

    文件体直接作为请求体；保存后登记 artifact(kind=material)，供「资料」页展示与下载。
    """
    name = (request.query_params.get("name") or "").strip()
    body = await request.body()
    if not name or not body:
        return JSONResponse({"error": "需要 name 与文件内容"}, status_code=400)
    name = Path(name).name  # 仅取 basename，防路径穿越
    if Path(name).suffix.lower() not in MATERIAL_EXTS:
        return JSONResponse({"error": "不支持的文件类型：" + (Path(name).suffix or "(无后缀)")}, status_code=400)
    if len(body) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "文件超过 20MB 上限"}, status_code=400)
    try:
        MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
        dest = MATERIALS_DIR / name
        dest.write_bytes(body)
        runtime = AgentRuntime.get_default()
        runtime._ensure()
        artifact = runtime.tasks.register_artifact(
            task_id=None,
            session_id=None,
            name=name,
            kind="material",
            storage_path=str(dest),
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            metadata={"origin": "upload"},
        )
    except Exception as exc:
        return JSONResponse({"error": f"保存失败：{type(exc).__name__}: {exc}"}, status_code=500)
    return JSONResponse({"ok": True, "artifact": artifact})


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered)):
        return "🛡️ " + main_module._guardrail_reason(exc)
    if isinstance(exc, MaxTurnsExceeded):
        return f"本轮超过最大循环次数（{exc}）。可调大 max_turns，或把任务拆小再试。"
    if isinstance(exc, ModelBehaviorError) and "not found in agent" in str(exc):
        match = re.search(r"Tool (.+?) not found in agent", str(exc))
        tool = match.group(1) if match else "未知工具"
        return f"模型调用了一个不存在的工具“{tool}”（幻觉调用），已提示模型纠正，请再发一次。"
    message = str(exc) or type(exc).__name__
    if main_module._looks_like_api_or_network(message):
        return f"{message}\n（如果与 API Key / 网络有关，请检查 .env 配置）"
    return message


def _reply_payload(reply: object) -> dict:
    if isinstance(reply, AgentReply):
        payload = reply.model_dump()
        payload["role"] = "assistant"
        return payload
    return {
        "role": "assistant",
        "kind": "raw",
        "summary": "",
        "content": str(reply)[:4000],
        "questions": [],
        "saved_file": None,
        "next_step": None,
    }


def _list_sessions_json() -> list[dict]:
    path = main_module.SESSIONS_DB
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            """
            SELECT s.session_id, COUNT(m.id) AS msg_count
            FROM agent_sessions s
            LEFT JOIN agent_messages m ON m.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [{"id": sid, "messages": count} for sid, count in rows]


async def _history_items(session_name: str) -> list[dict]:
    session = SQLiteSession(session_name, db_path=str(main_module.SESSIONS_DB))
    try:
        items = await session.get_items(limit=200)
    finally:
        session.close()
    history: list[dict] = []
    for item in items:
        role = compact._item_role(item)
        text = compact._item_text(item).strip()
        if not text:
            continue
        if role == "user":
            history.append({"role": "user", "content": text[:3000]})
        elif role == "assistant":
            reply = main_module.coerce_reply(text)
            payload = _reply_payload(reply)
            payload["content"] = (payload.get("content") or "")[:6000]
            history.append(payload)
        elif role == "system" and "自动摘要" in text:
            history.append({"role": "system", "content": text[:2000]})
    return history


async def index_page(_request: Request) -> HTMLResponse:
    """根路径 → llama-ui 前端（OpenAI 协议，经 llama_bridge 接 AgentRuntime）。"""
    path = BASE_DIR / "web" / "llama-ui" / "index.html"
    html = path.read_text(encoding="utf-8") if path.exists() else "<h1>web/llama-ui/index.html 缺失</h1>"
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def runtime_page(_request: Request) -> HTMLResponse:
    """/runtime → 旧版 v2 Runtime UI（保留为回滚入口；根路径已切到 llama-ui）。"""
    path = BASE_DIR / "web" / "runtime.html"
    html = path.read_text(encoding="utf-8") if path.exists() else "<h1>web/runtime.html 缺失</h1>"
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def legacy_chat_page(_request: Request) -> RedirectResponse:
    """旧版聊天页已下线：统一收敛到当前正式主界面（品牌统一）。"""
    return RedirectResponse("/", status_code=301)


async def api_sessions(_request: Request) -> JSONResponse:
    return JSONResponse({"sessions": _list_sessions_json()})


async def api_history(request: Request) -> JSONResponse:
    session_name = request.query_params.get("session", "personal").strip()
    if not session_name:
        return JSONResponse({"error": "session 不能为空"}, status_code=400)
    return JSONResponse({"messages": await _history_items(session_name)})


# ===========================================================================
# Run 生命周期与 SSE 解耦（2026-09 生产收口）
# - Run 创建/消息/执行启动 = POST（幂等 client_message_id；同容器单 Active Run）；
# - GET stream / events 只读订阅：SSE 断开 ≠ Run 取消，断线后 Run 继续执行；
# - 取消 = POST cancel（真实中断在飞 asyncio 任务，由 run_turn 收口 CANCELLED）；
# - GET 端点不再因 message/query 产生任何数据库写入。
# ===========================================================================

_MAX_MESSAGE_CHARS = 20000


class _LiveRunSession:
    """一个在飞（或刚结束、短窗内仍可重订）Run 的进程内广播会话。"""

    __slots__ = ("run_id", "session", "task", "_subs", "history", "finished", "closed_at")

    def __init__(self, run_id: str, session: Any) -> None:
        self.run_id = run_id
        self.session = session  # SQLiteSession（run 结束并广播完后再关闭）
        self.task: asyncio.Task | None = None
        self._subs: set[asyncio.Queue] = set()
        self.history: list[tuple[str, dict]] = []
        self.finished = False
        self.closed_at = 0.0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def feed(self, name: str, payload: dict) -> None:
        from runtime.public_activity import public_wire
        wire = public_wire(self.run_id, name, payload)
        if wire is None:
            return
        name, payload = wire
        self.history.append((name, payload))
        if len(self.history) > 3000:
            del self.history[: max(0, len(self.history) - 3000)]
        for q in list(self._subs):
            try:
                if q.qsize() > 5000:
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                q.put_nowait((name, payload))
            except Exception:
                pass


_LIVE_RUNS: dict[str, _LiveRunSession] = {}
_LIVE_LOCK = asyncio.Lock()
_STARTING_RUNS: set[str] = set()  # 启动单飞：同一 run 的重复 resume/创建只启动一次


def _start_run_once(run_id: str) -> _LiveRunSession | None:
    """单飞启动后台执行：并发 resume 只有第一个生效（其余返回已启动会话）。"""
    if run_id in _STARTING_RUNS:
        return _LIVE_RUNS.get(run_id)
    _STARTING_RUNS.add(run_id)
    try:
        live = _start_run_session(run_id)
        return live
    finally:
        _STARTING_RUNS.discard(run_id)


def _live_get(run_id: str) -> _LiveRunSession | None:
    _prune_live()
    return _LIVE_RUNS.get(run_id)


def _prune_live() -> None:
    """清理超过 15 分钟的已完成会话（历史回放窗口）。"""
    now = time.time()
    stale = [rid for rid, s in _LIVE_RUNS.items()
             if s.finished and s.closed_at and now - s.closed_at > 900]
    for rid in stale:
        s = _LIVE_RUNS.pop(rid, None)
        if s is not None:
            try:
                s.session.close()
            except Exception:
                pass


def _approval_payload_pub(run_id: str) -> dict:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    approvals = runtime.tasks.list_pending_approvals(run_id)
    return {
        "task_id": run_id,
        "approvals": [__import__("runtime.public_activity", fromlist=["public_approval"]).public_approval(a) for a in approvals],
    }


def _terminal_sse_events(run_id: str) -> list[tuple[str, dict]]:
    """Run 已终态/无在飞任务时，从 DB 合成给订阅者的回放事件（只读，无副作用）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    run = runtime.tasks.get_task(run_id)
    if run is None:
        return [("error", {"message": f"run not found: {run_id}"}), ("done", {})]
    state = run.state.value
    container_id = runtime.tasks.get_run_container_id(run_id)
    if state == "waiting_approval":
        return [("approval", _approval_payload_pub(run_id)), ("done", {})]
    if state == "waiting_user":
        return [("run.waiting_for_user", {"run_id": run_id}), ("done", {})]
    if state == "cancelled":
        evs: list[tuple[str, dict]] = [
            ("run.started", {"run_id": run_id, "task_id": container_id, "state": "running"}),
            ("run.cancelled", {"run_id": run_id, "task_id": container_id}),
            ("task.cancelled", {"task_id": run_id, "state": "cancelled"}),
        ]
        evs.append(("done", {}))
        return evs
    if state == "failed":
        err = (run.error_message or "任务失败")[:300]
        return [
            ("run.failed", {"run_id": run_id, "message": err}),
            ("task.failed", {"task_id": run_id, "state": "failed", "message": err}),
            ("done", {}),
        ]
    if state in ("submitted", "running", "waiting_approval", "paused"):
        return [("error", {
            "message": "该 Run 当前未在执行（请用 POST /api/runs/{run_id}/resume 恢复后订阅，"
                       "或等待执行完成）",
        }), ("done", {})]
    # completed
    evs = [("run.completed", {"run_id": run_id})]
    try:
        container_id = runtime.tasks.get_run_container_id(run_id)
        if container_id:
            for msg in reversed(runtime.tasks.list_messages(container_id, limit=500)):
                if msg.get("run_id") == run_id and msg.get("role") == "assistant":
                    meta = msg.get("meta") or {}
                    evs.insert(0, ("reply", {
                        "role": "assistant",
                        "kind": meta.get("kind") or "raw",
                        "content": (msg.get("content") or "")[:4000],
                        "summary": "",
                        "questions": [],
                        "saved_file": None,
                        "next_step": None,
                    }))
                    break
    except Exception:
        pass
    evs.append(("done", {}))
    return evs


async def _synthesize_run_end(run_id: str, session_obj: _LiveRunSession) -> None:
    """Run 执行任务结束后：把终态转成 SSE 事件广播给所有订阅者（只读 DB）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    run = runtime.tasks.get_task(run_id)
    if run is None:
        session_obj.feed("error", {"message": f"run not found: {run_id}"})
        session_obj.feed("done", {})
        session_obj.finished = True
        session_obj.closed_at = time.time()
        return
    state = run.state.value
    if state == "waiting_user":
        session_obj.feed("run.waiting_for_user", {"run_id": run_id})
        session_obj.feed("done", {})
        session_obj.finished = True
        session_obj.closed_at = time.time()
        session_obj.session.close()
        return
    if state == "waiting_approval":
        session_obj.feed("approval", _approval_payload_pub(run_id))
        session_obj.feed("run.waiting_approval", {"run_id": run_id})
    elif state == "cancelled":
        session_obj.feed("run.cancelled", {"run_id": run_id})
        session_obj.feed("task.cancelled", {"task_id": run_id, "state": "cancelled"})
    elif state == "failed":
        err = (run.error_message or "任务失败")[:300]
        session_obj.feed("run.failed", {"run_id": run_id, "message": err})
        session_obj.feed("task.failed", {"task_id": run_id, "state": "failed", "message": err})
    else:  # completed
        try:
            container_id = runtime.tasks.get_run_container_id(run_id)
            if container_id:
                for msg in reversed(runtime.tasks.list_messages(container_id, limit=500)):
                    if msg.get("run_id") == run_id and msg.get("role") == "assistant":
                        meta = msg.get("meta") or {}
                        session_obj.feed("reply", {
                            "role": "assistant",
                            "kind": meta.get("kind") or "raw",
                            "content": (msg.get("content") or ""),
                            "summary": "",
                            "questions": [],
                            "saved_file": None,
                            "next_step": None,
                        })
                        break
        except Exception:
            pass
        session_obj.feed("run.completed", {"run_id": run_id})
    session_obj.feed("done", {})
    session_obj.finished = True
    session_obj.closed_at = time.time()
    try:
        session_obj.session.close()
    except Exception:
        pass


def _start_run_session(run_id: str) -> _LiveRunSession | None:
    """开始 Run 的后台执行（resume 语义：SUBSCRIBED→RUNNING），返回可订阅会话。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    existing = runtime.tasks.get_task(run_id)
    if existing is None:
        return None
    container_id = runtime.tasks.get_run_container_id(run_id) or existing.metadata.get("container_id")
    container_row = runtime.tasks.get_container(container_id) if container_id else None
    session_id = existing.session_id or (container_row or {}).get("session_id") or "personal"
    session = SQLiteSession(session_id, db_path=str(main_module.SESSIONS_DB))
    live = _LiveRunSession(run_id, session)
    _LIVE_RUNS[run_id] = live
    live.feed("run.started", {"run_id": run_id, "task_id": container_id, "state": "running",
                              "resumed": True})

    def _cb(name: str, payload: dict) -> None:
        live.feed(name, payload)

    try:
        max_turns = 20
        meta = dict(existing.metadata or {})
        try:
            max_turns = max(5, min(int(meta.get("max_turns") or 20), 100))
        except (TypeError, ValueError):
            max_turns = 20
        coro = runtime.run_turn(
              existing.goal,
              session=session,
              session_id=session_id,
              mode="stream",
              max_turns=max_turns,
              metadata={"channel": "web"},
              stream_events_cb=_cb,
          )
        live.task = runtime.spawn_run_task(run_id, coro)

        async def _watch() -> None:
            try:
                await live.task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            await _synthesize_run_end(run_id, live)

        asyncio.create_task(_watch(), name=f"watch-{run_id}")
    except Exception:
        try:
            runtime.finalize_cancelled(run_id, reason="start failed")
            live.feed("error", {"message": "Run 启动失败"})
            live.feed("done", {})
            live.finished = True
            live.closed_at = time.time()
        except Exception:
            pass
    return live


def _sse_deprecated_create(message: str = "") -> str:
    return _sse("error", {
        "message": "GET 不再创建/执行 Run（消息也不得出现在 URL）。"
                   f"请改用 POST /api/tasks/{{容器id}}/runs 提交消息，"
                   "随后用返回的 run_id 订阅 GET /api/runs/{run_id}/stream。"
    })


async def _subscribe_generator(run_id: str) -> AsyncGenerator[str, None]:
    """订阅一个 Run：历史回放（含终态合成）→ 实时事件；纯只读。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    run = runtime.tasks.get_task(run_id)
    if run is None:
        yield _sse("error", {"message": f"run not found: {run_id}"})
        yield _sse("done", {})
        return
    live = _live_get(run_id)
    if live is None:
        from runtime.public_activity import stored_public_events, public_wire
        persisted = stored_public_events(runtime.tasks, run_id)
        for payload in persisted:
            yield _sse(payload["channel"], payload)
        for name, payload in _terminal_sse_events(run_id):
            if persisted and name not in {"done", "approval"}:
                continue
            wire = public_wire(run_id, name, payload)
            if wire:
                yield _sse(*wire)
        return
    q = live.subscribe()
    # 历史先回放（含此前的终态事件），再追实时队列 → 订阅者只见一份完整序列
    snap = list(live.history)
    for name, payload in snap:
        yield _sse(name, payload)
    if live.finished:
        live.unsubscribe(q)
        return
    try:
        while True:
            try:
                name, payload = q.get_nowait()
                yield _sse(name, payload)
                if payload.get("type") == "runtime.done":
                    return
            except asyncio.QueueEmpty:
                pass
            if live.finished and q.empty():
                # 兜底：done 已在历史快照中放过，且无新事件 → 结束
                return
            await asyncio.sleep(0.05)
    finally:
        # 断连 ≠ 取消：这里不做任何 DB 状态变更
        live.unsubscribe(q)


async def api_stream(request: Request) -> StreamingResponse:
    """SSE 订阅流（只读）。参数 resume_task=<run_id> 表示订阅某 Run（不得再创建/执行）。

    GET 中携带 message 的旧用法已废弃（不会写库，直接返回错误指引）。
    """
    message = request.query_params.get("message", "").strip()
    resume_task = (request.query_params.get("resume_task", "") or "").strip()
    if message and not resume_task:
        return StreamingResponse(iter([_sse_deprecated_create()]), media_type="text/event-stream")
    if resume_task:
        return StreamingResponse(_subscribe_generator(resume_task), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(iter([_sse("error", {
        "message": "需要 resume_task=<run_id>（消息请通过 POST /api/tasks/{容器id}/runs 提交）",
    })]), media_type="text/event-stream")


async def api_run_stream(request: Request) -> StreamingResponse:
    """GET /api/runs/{run_id}/stream —— 只读订阅 Run 事件（不创建、不启动执行）。"""
    run_id = request.path_params.get("run_id", "")
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    run = runtime.tasks.get_task(run_id)
    if run is None:
        return StreamingResponse(iter([_sse("error", {"message": f"run not found: {run_id}"})]),
                                 media_type="text/event-stream")
    return StreamingResponse(_subscribe_generator(run_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _validate_message(content: str) -> str | None:
    content = (content or "").strip()
    if not content:
        return "message 不能为空"
    if len(content) > _MAX_MESSAGE_CHARS:
        return f"message 过长（上限 {_MAX_MESSAGE_CHARS} 字符，当前 {len(content)}）"
    return None


async def _create_run_and_start(container: dict, body: dict) -> tuple[dict | None, int]:
    """POST 建 Run 唯一入口：幂等 → 单 Active Run → 建 Run/消息 → 后台启动执行。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    container_id = container["id"]
    content = str(body.get("message") or body.get("content") or "").strip()
    error = _validate_message(content)
    if error:
        return {"error": error}, 400
    client_message_id = str(body.get("client_message_id") or "").strip()
    # 1) 幂等：同一容器 + client_message_id 命中 → 返回既有 run，不做任何新写入
    if client_message_id:
        existing_run_id = runtime.tasks.find_run_by_client_message(container_id, client_message_id)
        if existing_run_id is not None:
            run = runtime.tasks.get_task(existing_run_id)
            if run is not None:
                return {
                    "ok": True, "idempotent": True, "task_id": container_id, "run_id": run.id,
                    "run": _run_payload_short(run),
                    "stream": f"/api/runs/{run.id}/stream",
                }, 200
    # 2) 同容器单 Active Run
    active = runtime.tasks.find_active_run(container_id)
    if active is not None:
        return {"error": (
            f"当前任务仍在处理中（Run {active.id}，状态 {active.state.value}），"
            "请等待完成、取消或处理待确认操作后再继续。"
        ), "active_run_id": active.id}, 409
    # 3) 建 Run + user Message（唯一索引兜底并发双发）
    try:
        run = runtime.tasks.create_task(
            session_id=container.get("session_id") or "personal",
            goal=content,
            metadata={"channel": "web", "source": "run-create"},
            thread_id=container_id,
        )
    except Exception as exc:
        if "已有正在处理" in str(exc):
            return {"error": str(exc)}, 409
        return {"error": f"创建 Run 失败：{exc}"}, 400
    message = runtime.tasks.add_message(container_id, "user", content, run_id=run.id)
    bound = 0
    attachment_ids = body.get("attachments") or []
    if isinstance(attachment_ids, list) and attachment_ids:
        try:
            bound = runtime.tasks.bind_message_attachments(
                [str(a) for a in attachment_ids if str(a).startswith("att_")], message["id"], run.id
            )
        except Exception:
            bound = 0
    if client_message_id:
        runtime.tasks.record_client_message(container_id, client_message_id, run.id)
    runtime.tasks.touch_container(container_id)
    # 4) 后台启动执行（SSE 不再持有 Run 生命周期）
    _start_run_once(run.id)
    return {
        "ok": True, "idempotent": False, "task_id": container_id, "run_id": run.id,
        "message": message, "run": _run_payload_short(run), "bound_attachments": bound,
        "stream": f"/api/runs/{run.id}/stream",
    }, 202


async def api_task_runs_create(request: Request) -> JSONResponse:
    """POST /api/tasks/{task_id}/runs —— 提交消息并后台启动执行（生产主入口）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    container = runtime.tasks.get_container(request.path_params.get("task_id", ""))
    if container is None:
        return JSONResponse({"error": "task container not found"}, status_code=404)
    payload, code = await _create_run_and_start(container, body)
    return JSONResponse(payload, status_code=code)


async def api_project_runs_create(request: Request) -> JSONResponse:
    """POST /api/projects/{project_id}/runs —— 项目消息提交与后台执行（生产主入口）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    container = runtime.tasks.get_container(request.path_params.get("project_id", ""))
    if container is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    payload, code = await _create_run_and_start(container, body)
    return JSONResponse(payload, status_code=code)


async def api_approval(request: Request) -> JSONResponse:
    """审批决定：POST {"id": approval_id, "decision": "approved"|"denied"}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    approval_id = str(body.get("id") or "")
    decision = str(body.get("decision") or "")
    if not approval_id or decision not in ("approved", "denied"):
        return JSONResponse({"error": "id/decision 不合法"}, status_code=400)
    try:
        runtime = AgentRuntime.get_default()
        runtime._ensure()
        info = runtime.tasks.decide_approval(approval_id, decision, actor="web-user")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "id": info["id"], "status": info["status"]})


async def api_artifact_download(request: Request) -> FileResponse | JSONResponse:
    """按 artifact_id 下载已登记产物（路径来自 Artifact Registry，非模型自报）。"""
    artifact_id = request.path_params.get("artifact_id", "")
    try:
        runtime = AgentRuntime.get_default()
        runtime._ensure()
        artifact = runtime.tasks.get_artifact(artifact_id)
    except Exception:
        artifact = None
    if artifact is None:
        return JSONResponse({"error": "artifact not found"}, status_code=404)
    path = Path(artifact["storage_path"])
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "artifact 文件已不存在"}, status_code=410)
    return FileResponse(str(path), filename=artifact["name"])


def _task_payload(task: object, manager: object, *, with_details: bool = True) -> dict:
    """Task → API JSON（含用量/checkpoint/计数摘要）。"""
    usage = task.usage
    payload = {
        "id": task.id,
        "session_id": task.session_id,
        "goal": task.goal,
        "state": task.state.value,
        "agent_name": task.agent_name,
        "metadata": task.metadata,
        "error": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "usage": {
            "turns": usage.turns,
            "tool_calls": usage.tool_calls,
            "failures": usage.failures,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
        },
    }
    if with_details:
        payload["artifacts"] = manager.list_artifacts(task_id=task.id)
        payload["approvals_pending"] = len(manager.list_pending_approvals(task.id))
        payload["model_calls"] = len(manager.list_model_calls(task.id))
        payload["tool_calls_total"] = len(manager.list_tool_calls(task.id))
        latest = manager.read_latest_checkpoint(task.id)
        if latest:
            snap = latest.get("snapshot") or {}
            payload["checkpoint"] = {
                "schema_version": latest.get("schema_version"),
                "created_at": latest.get("created_at"),
                "summary": snap.get("final_summary") or snap.get("error") or "",
            }
    return payload





async def api_task_events(request: Request) -> JSONResponse:
    """GET /api/tasks/{task_id}/events —— 事件流回放。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    task = runtime.tasks.get_task(request.path_params.get("task_id", ""))
    if task is None:
        return JSONResponse({"error": "task not found"}, status_code=404)
    events = runtime.tasks.list_events(task.id)
    return JSONResponse(
        {
            "events": [
                {"type": e.event_type, "payload": e.payload, "created_at": e.created_at} for e in events
            ]
        }
    )


async def _task_action(request: Request) -> JSONResponse:
    """POST /api/tasks/{id}/pause|resume|cancel —— 真实状态机转换。"""
    from runtime.errors import AgentError
    from runtime.task import TaskState

    path = request.url.path
    action = "pause" if path.endswith("/pause") else "resume" if path.endswith("/resume") else "cancel"
    task_id = request.path_params.get("task_id", "")
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    task = runtime.tasks.get_task(task_id)
    if task is None:
        return JSONResponse({"error": "task not found"}, status_code=404)
    try:
        if action == "pause":
            updated = runtime.tasks.transition(task.id, TaskState.PAUSED, reason="user pause")
        elif action == "resume":
            if not runtime.tasks.is_resumable(task):
                return JSONResponse({"error": f"task 不可恢复（当前 {task.state.value}）"}, status_code=409)
            updated = runtime.tasks.transition(task.id, TaskState.RUNNING, reason="user resume")
        else:
            updated = runtime.tasks.transition(task.id, TaskState.CANCELLED, reason="user cancel")
    except AgentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "task": _task_payload(updated, runtime.tasks, with_details=False)})


async def api_approvals_list(request: Request) -> JSONResponse:
    """GET /api/approvals?state=&task_id= —— 审批列表/审批中心。"""
    state = request.query_params.get("state") or None
    task_id = request.query_params.get("task_id") or None
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    rows = runtime.tasks.list_approvals(task_id=task_id, state=state)
    if request.query_params.get("debug") != "1":
        from runtime.public_activity import public_approval
        rows = [public_approval(row) for row in rows]
    return JSONResponse({"approvals": rows, "total": len(rows)})


async def api_artifacts_list(request: Request) -> JSONResponse:
    """GET /api/artifacts?task_id=&session_id= —— 产物列表（真实 Registry 数据）。"""
    task_id = request.query_params.get("task_id") or None
    session_id = request.query_params.get("session_id") or None
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    rows = runtime.tasks.list_artifacts(task_id=task_id, session_id=session_id)
    return JSONResponse({"artifacts": rows, "total": len(rows)})


async def api_artifact_meta(request: Request) -> JSONResponse:
    """GET /api/artifacts/{id} —— 产物元数据（供 ArtifactCard 数据驱动）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    artifact = runtime.tasks.get_artifact(request.path_params.get("artifact_id", ""))
    if artifact is None:
        return JSONResponse({"error": "artifact not found"}, status_code=404)
    return JSONResponse(artifact)


async def api_tools(request: Request) -> JSONResponse:
    """GET /api/tools —— Runtime 工具注册表元数据（Inspector/Tools 页）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    from runtime.capability_introspection import collect_tool_entries

    caps = {e["tool_id"]: e for e in collect_tool_entries()}
    return JSONResponse(
        {
            "tools": [
                {
                    "name": b.spec.name,
                    "description": b.spec.description[:400],
                    "category": b.spec.category,
                    "risk": b.spec.risk,
                    "side_effect": b.spec.side_effect,
                    "destructive": b.spec.destructive,
                    "idempotent": b.spec.idempotent,
                    "source": b.spec.source,
                    "origin": caps.get(b.spec.name, {}).get("origin")
                              or (b.spec.source if b.spec.source != "native" else "builtin"),
                    "display_name": caps.get(b.spec.name, {}).get("display_name")
                                    or b.spec.name,
                    "enabled": caps.get(b.spec.name, {}).get("enabled", True),
                    "available": caps.get(b.spec.name, {}).get("available", True),
                    "connected": caps.get(b.spec.name, {}).get("connected", None),
                    "server_id": caps.get(b.spec.name, {}).get("server_id", None),
                }
                for b in runtime.list_tools()
            ]
        }
    )


async def api_runtime_status(request: Request) -> JSONResponse:
    """GET /api/runtime/status —— Runtime 卡数据（db/MCP/工具数/只读配置，无密钥）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    from runtime.task_manager import DEFAULT_DB_PATH

    db_ok = DEFAULT_DB_PATH.exists()
    mcp_names = []
    mcp_policy: dict[str, int] = {}
    try:
        import mcp_bridge

        mcp_names = list(mcp_bridge._connected_names)
        mcp_policy = dict(mcp_bridge._policy_summary)
    except Exception:
        pass
    from code_exec import SANDBOX_ROOT as _SANDBOX_ROOT

    backups_dir = BASE_DIR / "logs" / "backups"
    provider_text = getattr(main_module, "PROVIDER_TEXT", "") or "未测试"
    build_config = {
        "model": os.getenv("AGENT_MODEL") or "agnes-2.5-flash（门户默认）",
        "provider_text": provider_text,
        "model_pref_default": os.getenv("FORGE_MODEL_PREF", "gateway"),
        "local_model": os.getenv("FORGE_LOCAL_MODEL_NAME", "") or "",
        "local_model_url": os.getenv("FORGE_LOCAL_MODEL_BASE_URL", "") or "",
        "code_exec_enabled": _env_flag("ALLOW_CODE_EXEC"),
        "project_edit_enabled": _env_flag("ALLOW_PROJECT_EDIT"),
        "approval_enabled": os.getenv("APPROVAL", "").strip().lower() not in ("off", "false", "0"),
        "sandbox_root": str(_SANDBOX_ROOT),
        "materials_dir": str(MATERIALS_DIR),
        "backups_count": len(list(backups_dir.glob("*"))) if backups_dir.exists() else 0,
        "db_path": str(DEFAULT_DB_PATH),
        "db_size_mb": round(DEFAULT_DB_PATH.stat().st_size / 1048576, 1) if db_ok else 0,
    }
    return JSONResponse(
        {
            "ok": True,
            "db": {"path": str(DEFAULT_DB_PATH), "wal": db_ok},
            "mcp_servers": mcp_names,
            "mcp_policy": mcp_policy,
            "tools_count": len(runtime.list_tools()),
            "service": "全能助手 Agent Runtime",
            "config": build_config,
        }
    )


async def api_schedules_list(request: Request) -> JSONResponse:
    """GET /api/schedules —— 计划任务镜像（schedules 表）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    return JSONResponse({"schedules": runtime.tasks.list_schedules()})



async def api_memory_delete(request: Request) -> JSONResponse:
    """POST /api/memories/delete {id} —— 删除单条长期记忆。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    ok = runtime.tasks.delete_memory_row(str(body.get("id") or ""))
    return JSONResponse({"ok": ok} if ok else {"error": "memory not found"}, status_code=200 if ok else 404)


async def api_memory_setting(request: Request) -> JSONResponse:
    """POST /api/settings/memory {enabled} —— 长期记忆总开关（作用于当前进程工具门控）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    enabled = bool(body.get("enabled", True))
    os.environ["FORGE_MEMORY_ENABLED"] = "1" if enabled else "0"
    return JSONResponse({"ok": True, "enabled": enabled})


async def api_memories_list(request: Request) -> JSONResponse:
    """GET /api/memories —— 长期记忆（memories 表）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    return JSONResponse({"memories": runtime.tasks.memory_rows()})


async def api_search(request: Request) -> JSONResponse:
    """GET /api/search?q= —— 全局搜索（任务/运行/消息/资料），按组返回，限制各 20 条。"""
    q = (request.query_params.get("q") or "").strip()[:100]
    if not q:
        return JSONResponse({"q": q, "groups": []})
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    mgr = runtime.tasks
    return JSONResponse({
        "q": q,
        "groups": [
            {"kind": "task", "label": "任务", "items": mgr.search_containers(q)},
            {"kind": "run", "label": "运行记录", "items": mgr.search_runs(q)},
            {"kind": "message", "label": "消息", "items": mgr.search_messages(q)},
            {"kind": "artifact", "label": "资料与文件", "items": mgr.search_artifacts(q)},
        ],
    })


async def api_notifications(request: Request) -> JSONResponse:
    """GET /api/notifications —— 通知中心（普通用户可感知的事件：
    需要你确认（审批）、遇到问题（本轮失败）、已完成（本轮完成）；各近 48h，最多 50 条。"""
    import datetime as _dt

    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)).isoformat(timespec="seconds")
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    mgr = runtime.tasks
    items: list[dict] = []
    # 等待确认：pending approvals 挂的 run → 容器
    for ap in mgr.list_pending_approvals_all(limit=50):
        run = mgr.get_task(ap["task_id"]) if ap.get("task_id") else None
        cid = mgr.get_run_container_id(ap["task_id"]) if ap.get("task_id") else None
        if not run or not cid:
            continue
        items.append({
            "id": "notif_ap_" + ap["id"], "type": "approval",
            "title": "等你确认", "subtitle": f"{ap['tool_name'] or '一个操作'} · 需要你决定",
            "container_id": cid, "run_id": run.id, "created_at": ap.get("created_at"),
        })
    if items:
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    # 最近 48h 的失败与完成（用户可感知的“遇到问题/已完成”）
    recents: list[dict] = []
    with runtime.tasks._connect() as conn:
        rows = conn.execute(
            "SELECT id, task_id, state, goal, error_message, created_at FROM runs "
            "WHERE state IN ('failed','completed','cancelled') AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 40",
            (cutoff,),
        ).fetchall()
    for r in rows:
        cid = r["task_id"]
        if not cid:
            continue
        if r["state"] == "failed":
            recents.append({
                "id": "notif_fail_" + r["id"], "type": "failed",
                "title": "遇到一个问题", "subtitle": (r["error_message"] or "本轮未完成")[:80],
                "container_id": cid, "run_id": r["id"], "created_at": r["created_at"],
            })
        elif r["state"] == "completed":
            recents.append({
                "id": "notif_done_" + r["id"], "type": "done",
                "title": "已完成", "subtitle": (r["goal"] or "")[:80],
                "container_id": cid, "run_id": r["id"], "created_at": r["created_at"],
            })
    items = (items + recents)
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return JSONResponse({
        "items": items[:50],
        "unread": sum(1 for x in items if x["type"] == "approval" or x["type"] == "failed"),
    })


async def api_container_archive(request: Request) -> JSONResponse:
    """POST /api/tasks/{task_id}/archive —— 归档（进回收站，可恢复）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    ok = runtime.tasks.archive_container(request.path_params.get("task_id", ""))
    if not ok:
        return JSONResponse({"error": "task not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_container_pin(request: Request) -> JSONResponse:
    """POST /api/tasks/{task_id}/pin {pinned: true|false} —— 收藏/取消收藏（左栏置顶）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    pinned = bool(body.get("pinned", False))
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    ok = runtime.tasks.set_container_pinned(request.path_params.get("task_id", ""), pinned)
    if not ok:
        return JSONResponse({"error": "task not found"}, status_code=404)
    return JSONResponse({"ok": True, "pinned": pinned})


async def api_container_delete(request: Request) -> JSONResponse:
    """DELETE /api/tasks/{task_id} — 回收站里彻底清除（不可恢复）."""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    task_id = request.path_params.get("task_id", "")
    container = runtime.tasks.get_container(task_id)
    sid = container.get("session_id") if container else None
    ok = runtime.tasks.delete_container(task_id)
    if not ok:
        return JSONResponse({"error": "task not found"}, status_code=404)
    if sid:
        try:
            from runtime.context import delete_session_history

            delete_session_history(sid, str(main_module.SESSIONS_DB))
        except Exception:
            pass
    return JSONResponse({"ok": True})


async def api_container_restore(request: Request) -> JSONResponse:
    """POST /api/tasks/{task_id}/restore —— 从回收站恢复。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    ok = runtime.tasks.restore_container(request.path_params.get("task_id", ""))
    if not ok:
        return JSONResponse({"error": "task not found"}, status_code=404)
    return JSONResponse({"ok": True})


# ---------- 新模型 REST（Task 容器 / Message / Run） ----------


def _container_payload(container: dict, manager: object) -> dict:
    stat = manager.container_stat(container["id"])
    return {
        "id": container["id"],
        "project_id": container.get("project_id"),
        "session_id": container.get("session_id"),
        "title": container.get("title") or "",
        "summary": container.get("summary") or "",
        "status": container.get("status") or "active",
        "pinned": bool(container.get("pinned")),
        "instructions": container.get("instructions") or "",
        "memory_scope": container.get("memory_scope") or "project_only",
        "model_pref": container.get("model_pref") or "",
        "work_location_id": container.get("work_location_id"),
        "created_at": container.get("created_at"),
        "updated_at": container.get("updated_at"),
        "archived_at": container.get("archived_at"),
        "work_location": manager.get_work_location(container["work_location_id"]) if container.get("work_location_id") else None,
        "stat": stat,
    }


def _run_payload_short(run: object) -> dict:
    return {
        "id": run.id,
        "state": run.state.value,
        "goal": (run.goal or "")[:2000],
        "error": run.error_message,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


async def api_projects_list(_request: Request) -> JSONResponse:
    """GET /api/projects —— Project（用户级）列表（容器，含 sources 数）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    containers = runtime.tasks.list_containers(limit=200)
    out = []
    for c in containers:
        item = _container_payload(c, runtime.tasks)
        item["sources_count"] = len(runtime.tasks.list_project_sources(c["id"]))
        out.append(item)
    return JSONResponse({"projects": out, "total": len(out)})


async def api_projects_create(request: Request) -> JSONResponse:
    """POST /api/projects/create —— 新建 Project（无需 WorkLocation；默认仅此项目记忆）。"""
    import uuid as _uuid

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    name = str(body.get("name") or "").strip()[:120]
    if not name:
        return JSONResponse({"error": "name 不能为空"}, status_code=400)
    scope = body.get("memory_scope")
    container = runtime.tasks.get_or_create_container(
        "proj-" + _uuid.uuid4().hex[:8],
        title=name,
    )
    if scope not in ("project_only", "global"):
        scope = "project_only"
    runtime.tasks.update_project(container["id"],
                                 instructions=body.get("instructions"),
                                 memory_scope=scope,
                                 work_location_id=body.get("work_location_id"))
    created = runtime.tasks.get_container(container["id"])
    return JSONResponse({"ok": True, "project": _container_payload(created, runtime.tasks)}, status_code=201)


async def api_project_detail(request: Request) -> JSONResponse:
    """GET /api/projects/{project_id} —— Project 详情（messages+runs+sources 概要+artifacts 计数）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    container = runtime.tasks.get_container(pid)
    if container is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    payload = _container_payload(container, runtime.tasks)
    payload["instructions"] = container.get("instructions") or ""
    payload["memory_scope"] = container.get("memory_scope") or "project_only"
    payload["work_location"] = None
    if container.get("work_location_id"):
        payload["work_location"] = runtime.tasks.get_work_location(container["work_location_id"])
    payload["messages"] = runtime.tasks.list_messages(pid)
    # 每条消息附带其附件（作用域仅本次使用 / 项目来源）
    all_att = runtime.tasks.list_message_attachments(task_id=pid)
    by_msg: dict = {}
    for a in all_att:
        if a.get("message_id") is not None:
            by_msg.setdefault(str(a["message_id"]), []).append(a)
    for m in payload["messages"]:
        m["attachments"] = by_msg.get(str(m["id"]), [])
    runs = runtime.tasks.list_tasks(container_id=pid, limit=100)
    payload["runs"] = [_run_payload_short(r) for r in runs]
    payload["sources"] = runtime.tasks.list_project_sources(pid)
    payload["artifacts_count"] = len(runtime.tasks.list_project_artifacts(pid, limit=500))
    return JSONResponse(payload)


async def api_project_update(request: Request) -> JSONResponse:
    """PATCH /api/projects/{id} —— 名称/项目说明/记忆范围/工作位置。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    updated = runtime.tasks.update_project(
        pid,
        name=body.get("name"),
        instructions=body.get("instructions"),
        memory_scope=body.get("memory_scope"),
        work_location_id=body.get("work_location_id"),
        detach_work_location=bool(body.get("detach_work_location")),
    )
    if updated is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"ok": True, "project": _container_payload(updated, runtime.tasks)})


async def api_project_reset(request: Request) -> JSONResponse:
    """POST /api/projects/{id}/reset — 清空对话 + 模型 Runtime Context（保留项目/文件/执行记录）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    container = runtime.tasks.get_container(pid)
    if container is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    cleared = runtime.tasks.clear_container_history(pid)
    sid = container.get("session_id")
    if sid:
        try:
            from runtime.context import delete_session_history

            delete_session_history(sid, str(main_module.SESSIONS_DB))
        except Exception:
            pass
    runs = runtime.tasks.list_tasks(container_id=pid, limit=500)
    return JSONResponse({
        "ok": True,
        "cleared_messages": cleared,
        "kept": {"runs": len(runs), "project": pid, "session_id": sid},
        "hint": "清空的是对话与 AI 上下文；项目文件与历史执行记录保留。",
    })


async def api_project_delete(request: Request) -> JSONResponse:
    """POST /api/projects/{id}/delete — 彻底删除项目（级联，回收站语义由前端控制）."""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    container = runtime.tasks.get_container(pid)
    sid = container.get("session_id") if container else None
    ok = runtime.tasks.delete_container(pid)
    if ok and sid:
        try:
            from runtime.context import delete_session_history

            delete_session_history(sid, str(main_module.SESSIONS_DB))
        except Exception:
            pass
    return JSONResponse({"ok": ok} if ok else {"error": "project not found"}, status_code=200 if ok else 404)


async def api_project_messages_list(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    if runtime.tasks.get_container(pid) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"messages": runtime.tasks.list_messages(pid)})


async def api_project_messages_create(request: Request) -> JSONResponse:
    """POST /api/projects/{id}/messages —— 提交消息并后台启动 Run（幂等，生产主入口兼容）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    container = runtime.tasks.get_container(pid)
    if container is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    if "message" not in body and "content" in body:
        body["message"] = body.get("content")
    payload, code = await _create_run_and_start(container, body)
    return JSONResponse(payload, status_code=code)


async def api_project_stream(request: Request) -> StreamingResponse:
    """GET /api/projects/{id}/stream —— 只读订阅 Run（message 创建已废弃，改走 POST runs）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    container = runtime.tasks.get_container(pid)
    if container is None:
        return StreamingResponse(iter([_sse("error", {"message": "project not found"})]),
                                 media_type="text/event-stream")
    message = request.query_params.get("message", "").strip()
    run_id = (request.query_params.get("run_id", "") or "").strip()
    if message and not run_id:
        return StreamingResponse(iter([_sse_deprecated_create()]),
                                 media_type="text/event-stream")
    if not run_id:
        return StreamingResponse(iter([_sse("error", {"message": "需要 run_id 参数（消息请通过 POST 提交）"})]),
                                 media_type="text/event-stream")
    if runtime.tasks.get_run_container_id(run_id) != pid:
        return StreamingResponse(iter([_sse("error", {"message": f"run {run_id} 不属于该项目"})]),
                                 media_type="text/event-stream")
    return StreamingResponse(_subscribe_generator(run_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def api_project_sources_list(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    if runtime.tasks.get_container(pid) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"sources": runtime.tasks.list_project_sources(pid)})


async def api_project_sources_upload(request: Request) -> JSONResponse:
    """POST /api/projects/{id}/sources/upload?name= —— 上传来源文件（本地持久化，与附件同安全白名单）。"""
    import mimetypes as _mimetypes

    from runtime.task_manager import project_sources_dir

    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    if runtime.tasks.get_container(pid) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    name = (request.query_params.get("name") or "").strip()
    body = await request.body()
    base = _safe_upload_name(name)
    if base is None:
        return JSONResponse({"error": "文件类型不受支持或文件名不合法"}, status_code=400)
    if not body or len(body) > _MAX_UPLOAD:
        return JSONResponse({"error": "文件为空或超过 50MB 上限"}, status_code=400)
    sha = hashlib.sha256(body).hexdigest()
    existing = [s for s in runtime.tasks.list_project_sources(pid) if s.get("sha256") == sha]
    if existing:
        return JSONResponse({"ok": True, "source": existing[0], "duplicate": True})
    dest_dir = project_sources_dir(pid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / base
    if dest.exists():
        import uuid as _uuid

        dest = dest_dir / (_uuid.uuid4().hex[:6] + "_" + base)
    dest.write_bytes(body)
    mime = _mimetypes.guess_type(base)[0] or "application/octet-stream"
    row = runtime.tasks.add_project_source(
        task_id=pid, display_name=base, stored_path=str(dest), mime_type=mime,
        size_bytes=len(body), sha256=sha, parse_status="pending",
    )
    # Sources RAG：后台异步 parse → chunk → FTS/向量（无 Worker 系统；失败只置状态不打断上传）
    try:
        from sources.indexer import index_source_async

        index_source_async(pid, row)
    except Exception:
        pass
    return JSONResponse({"ok": True, "source": row}, status_code=201)


async def api_project_sources_delete(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    source_id = request.path_params.get("source_id", "")
    # Sources RAG：先清索引（chunks/FTS/向量），再删记录与原文件（不删用户磁盘原文件之外的任何东西）
    try:
        from sources.store import delete_source_index

        delete_source_index(pid, source_id)
    except Exception:
        pass
    row = runtime.tasks.delete_project_source(source_id)
    if row is None or row.get("task_id") != pid:
        return JSONResponse({"error": "source not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_project_artifacts_list(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    if runtime.tasks.get_container(pid) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"artifacts": runtime.tasks.list_project_artifacts(pid)})


async def api_work_locations_list(_request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    return JSONResponse({"work_locations": runtime.tasks.list_work_locations()})


async def api_work_locations_create(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    wl = runtime.tasks.create_work_location(
        str(body.get("name") or "").strip()[:120],
        body.get("local_path"),
        str(body.get("permission_profile") or "read-write")[:40],
    )
    return JSONResponse({"ok": True, "work_location": wl}, status_code=201)


async def api_work_locations_pick(_request: Request) -> JSONResponse:
    """弹出系统文件夹选择框（服务器本机），返回用户选择的绝对路径。

    浏览器原生 file input 拿不到绝对路径，故本机应用由服务端弹对话框。
    Windows 用 PowerShell FolderBrowserDialog；mac/Linux 用 zenity。
    """
    import platform
    import subprocess

    def _run_dialog():
        if platform.system() == "Windows":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                "$d.Description='选择工作文件夹';"
                "$d.ShowNewFolderButton=$true;"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True, text=True, timeout=240,
            )
            return (r.stdout or "").strip() or None
        r = subprocess.run(
            ["zenity", "--directory", "--title=选择工作文件夹"],
            capture_output=True, text=True, timeout=240,
        )
        return (r.stdout or "").strip() or None

    try:
        from starlette.concurrency import run_in_threadpool
        selected = await run_in_threadpool(_run_dialog)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"无法打开文件夹选择器：{type(e).__name__}: {e}"})
    if not selected:
        return JSONResponse({"ok": True, "canceled": True})
    return JSONResponse({"ok": True, "path": selected})


# 附件/Sources 公共白名单（防脚本/可执行/压缩包/危险扩展）
_SAFE_UPLOAD_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf",
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".html", ".htm",
    ".sql", ".log", ".ini", ".cfg", ".toml",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".odt", ".ods", ".odp",
}
_MAX_UPLOAD = 50 * 1024 * 1024


def _safe_upload_name(name: str) -> str | None:
    base = Path(name).name
    if not base or base in (".", ".."):
        return None
    if Path(base).suffix.lower() not in _SAFE_UPLOAD_EXTS:
        return None
    if any(c in base for c in ("\\", "/", "\x00")):
        return None
    return base


def _store_upload_bytes(pid: str, kind: str, name: str, body: bytes, existing_rows: callable) -> Path | None:
    """kind in ('attachments','sources')：落盘到 forge_data/projects/{pid}/{kind}/（重名加前缀防覆盖）。"""
    from runtime.task_manager import FORGE_DATA_DIR

    if not body:
        return None
    base = _safe_upload_name(name)
    if base is None:
        return None
    d = FORGE_DATA_DIR / "projects" / pid / kind
    d.mkdir(parents=True, exist_ok=True)
    dest = d / base
    if dest.exists():
        dest = d / (__import__("uuid").uuid4().hex[:6] + "_" + base)
    dest.write_bytes(body)
    return dest


async def api_project_attachments_upload(request: Request) -> JSONResponse:
    """POST /api/projects/{id}/attachments/upload?name= —— 当前消息临时附件（默认仅本次使用）。"""
    import mimetypes as _mimetypes
    import uuid as _uuid

    from runtime.task_manager import FORGE_DATA_DIR

    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    if runtime.tasks.get_container(pid) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    name = (request.query_params.get("name") or "").strip()
    body = await request.body()
    base = _safe_upload_name(name)
    if base is None:
        return JSONResponse({"error": "文件类型不受支持或文件名不合法"}, status_code=400)
    if not body or len(body) > _MAX_UPLOAD:
        return JSONResponse({"error": "文件为空或超过 50MB 上限"}, status_code=400)
    sha = hashlib.sha256(body).hexdigest()
    # 同项目同 hash 已有 → 复用（不重复落盘）
    dup = [a for a in runtime.tasks.list_message_attachments(task_id=pid) if a.get("sha256") == sha and a.get("message_id") is None]
    if dup:
        return JSONResponse({"ok": True, "attachment": dup[0], "duplicate": True})
    d = FORGE_DATA_DIR / "projects" / pid / "attachments" / _uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    dest = d / base
    dest.write_bytes(body)
    mime = _mimetypes.guess_type(base)[0] or "application/octet-stream"
    row = runtime.tasks.add_message_attachment(
        task_id=pid, display_name=base, stored_path=str(dest), mime_type=mime,
        size_bytes=len(body), sha256=sha, scope="message_only",
    )
    return JSONResponse({"ok": True, "attachment": row}, status_code=201)


async def api_project_attachments_list(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    if runtime.tasks.get_container(pid) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"attachments": runtime.tasks.list_message_attachments(task_id=pid)})


async def api_project_attachment_delete(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    row = runtime.tasks.delete_message_attachment(request.path_params.get("attachment_id", ""))
    if row is None:
        return JSONResponse({"error": "attachment not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_project_attachment_promote(request: Request) -> JSONResponse:
    """临时附件 → 加入项目来源（move/reference/dedupe）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    try:
        row = runtime.tasks.promote_attachment_to_source(request.path_params.get("attachment_id", ""))
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=400)
    return JSONResponse({"ok": True, "attachment": row})


async def api_project_attachment_refs(request: Request) -> JSONResponse:
    """引用已有项目来源为待发送附件（不复制文件、不重复索引）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    rows = runtime.tasks.create_source_reference_attachments(pid, body.get("source_ids") or [])
    return JSONResponse({"ok": True, "attachments": rows})


def _attach_messages(runtime: object, messages: list[dict], pid: str) -> None:
    """为消息列表补 attachments（作用域仅本次使用/项目来源）。"""
    try:
        all_att = runtime.tasks.list_message_attachments(task_id=pid)
    except Exception:
        return
    by_msg: dict = {}
    for a in all_att:
        if a.get("message_id") is not None:
            by_msg.setdefault(str(a["message_id"]), []).append(a)
    for m in messages:
        m["attachments"] = by_msg.get(str(m.get("id")), [])


async def api_project_memories_list(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    pid = request.path_params.get("project_id", "")
    if runtime.tasks.get_container(pid) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"memories": runtime.tasks.list_project_memories(pid)})


async def api_project_memory_delete(request: Request) -> JSONResponse:
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    ok = runtime.tasks.delete_project_memory(request.path_params.get("memory_id", ""))
    return JSONResponse({"ok": ok} if ok else {"error": "memory not found"}, status_code=200 if ok else 404)


async def api_task_containers_list(request: Request) -> JSONResponse:
    """GET /api/tasks —— Task 容器列表（product-facing）。"""
    session_id = request.query_params.get("session") or None
    project_id = request.query_params.get("project_id") or None
    archived_raw = request.query_params.get("archived")
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
    except ValueError:
        limit = 50
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    # 默认只列活跃任务；archived=1 才看回收站
    archived = False if archived_raw is None else archived_raw.strip().lower() in ("1", "true", "yes")
    containers = runtime.tasks.list_containers(
        session_id=session_id, project_id=project_id, archived=archived, limit=limit
    )
    return JSONResponse(
        {
            "tasks": [_container_payload(c, runtime.tasks) for c in containers],
            "total": len(containers),
        }
    )


async def api_task_containers_create(request: Request) -> JSONResponse:
    """POST /api/tasks —— 建 Task 容器。

    默认分配独立内部会话键（session_id），保证新 Task 上下文独立（不粘连旧对话）；
    显式传 session 时沿用该键（供旧入口/恢复用）。
    """
    import uuid as _uuid

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    session_id = str(body.get("session") or body.get("session_id") or "").strip()
    if not session_id:
        session_id = "sess-" + _uuid.uuid4().hex[:8]
    project_id = body.get("project_id") or None
    title = str(body.get("title") or "").strip() or None
    container = runtime.tasks.get_or_create_container(session_id, project_id=project_id, title=title)
    return JSONResponse({"ok": True, "task": _container_payload(container, runtime.tasks)}, status_code=201)


async def api_task_container_detail(request: Request) -> JSONResponse:
    """GET /api/tasks/{task_id} —— 容器详情（含 messages 与 runs 摘要）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    container = runtime.tasks.get_container(request.path_params.get("task_id", ""))
    if container is None:
        return JSONResponse({"error": "task container not found"}, status_code=404)
    payload = _container_payload(container, runtime.tasks)
    payload["messages"] = runtime.tasks.list_messages(container["id"])
    _attach_messages(runtime, payload["messages"], container["id"])
    runs = runtime.tasks.list_tasks(container_id=container["id"], limit=100)
    payload["runs"] = [_run_payload_short(r) for r in runs]
    payload["sources"] = runtime.tasks.list_project_sources(container["id"])
    return JSONResponse(payload)


async def api_task_messages_list(request: Request) -> JSONResponse:
    """GET /api/tasks/{task_id}/messages —— 该 Task 的对话消息。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    container = runtime.tasks.get_container(request.path_params.get("task_id", ""))
    if container is None:
        return JSONResponse({"error": "task container not found"}, status_code=404)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "500")), 2000))
    except ValueError:
        limit = 500
    return JSONResponse({"messages": runtime.tasks.list_messages(container["id"], limit=limit)})


async def api_task_messages_create(request: Request) -> JSONResponse:
    """POST /api/tasks/{task_id}/messages —— 提交消息并后台启动 Run（幂等，兼容入口）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "需要 JSON 请求体"}, status_code=400)
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    task_id = request.path_params.get("task_id", "")
    container = runtime.tasks.get_container(task_id)
    if container is None:
        return JSONResponse({"error": "task container not found"}, status_code=404)
    if "message" not in body and "content" in body:
        body["message"] = body.get("content")
    payload, code = await _create_run_and_start(container, body)
    return JSONResponse(payload, status_code=code)


async def api_runs_list(request: Request) -> JSONResponse:
    """GET /api/runs?task_id=&session=&state=&limit= —— Run 列表。"""
    container_id = request.query_params.get("task_id") or None
    session_id = request.query_params.get("session") or None
    state = request.query_params.get("state") or None
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
    except ValueError:
        limit = 50
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    runs = runtime.tasks.list_tasks(
        session_id=session_id, state=state, limit=limit, container_id=container_id
    )
    return JSONResponse({"runs": [_run_payload_short(r) for r in runs], "total": len(runs)})


async def api_run_detail(request: Request) -> JSONResponse:
    """GET /api/runs/{run_id} —— Run 详情（老版 _task_payload 语义 + 事件与工具明细）。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    run = runtime.tasks.get_task(request.path_params.get("run_id", ""))
    if run is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    payload = _task_payload(run, runtime.tasks)
    payload["container_id"] = runtime.tasks.get_run_container_id(run.id)
    from runtime.public_activity import public_snapshot
    payload["public_activity"] = public_snapshot(runtime.tasks, run.id)
    if request.query_params.get("debug") != "1":
        payload["events"] = []
        payload["tool_calls"] = []
        payload["model_calls"] = []
        payload["metadata"] = {}
        payload["checkpoint"] = None
        if payload.get("error"):
            payload["error"] = "执行遇到问题，未能完成。"
        return JSONResponse(payload)
    payload["events"] = [
        {"type": e.event_type, "payload": e.payload, "created_at": e.created_at}
        for e in runtime.tasks.list_events(run.id)
    ]
    payload["tool_calls"] = runtime.tasks.list_tool_calls(run.id, limit=200)
    payload["model_calls"] = runtime.tasks.list_model_calls(run.id, limit=100)
    return JSONResponse(payload)


async def api_run_events(request: Request) -> JSONResponse:
    """GET /api/runs/{run_id}/events —— 该 Run 的事件回放。"""
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    run = runtime.tasks.get_task(request.path_params.get("run_id", ""))
    if run is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    if request.query_params.get("debug") != "1":
        from runtime.public_activity import stored_public_events
        return JSONResponse({"events": stored_public_events(runtime.tasks, run.id)})
    events = runtime.tasks.list_events(run.id)
    return JSONResponse(
        {"events": [{"type": e.event_type, "payload": e.payload, "created_at": e.created_at} for e in events]}
    )


async def _run_action(request: Request) -> JSONResponse:
    """POST /api/runs/{run_id}/pause|resume|cancel —— Run 状态动作。

    - cancel：真实中断在飞执行（registry 中 asyncio.Task）+ DB CANCELLED 收口；
    - resume：启动后台执行并返回 run_id（随后用 GET stream 订阅）；
    - pause：保留为状态转换（当前执行器无法真正暂停在飞模型/工具循环，
      产品前端已不再暴露该入口；DB 语义与 resume 配合仍有效）。
    """
    from runtime.errors import AgentError

    path = request.url.path
    action = "pause" if path.endswith("/pause") else "resume" if path.endswith("/resume") else "cancel"
    run_id = request.path_params.get("run_id", "")
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    run = runtime.tasks.get_task(run_id)
    if run is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    terminal = (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)
    try:
        if action == "cancel":
            if run.state in terminal:
                return JSONResponse({"ok": True, "run": _task_payload(run, runtime.tasks, with_details=False)})
            runtime.cancel_run(run_id)
            live = _live_get(run_id)
            if live is not None and live.task is not None and not live.task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(live.task), timeout=12)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            updated = runtime.tasks.get_task(run_id)
            if updated is not None and updated.state not in terminal:
                runtime.finalize_cancelled(run_id, reason="user cancel")
                updated = runtime.tasks.get_task(run_id)
            return JSONResponse({"ok": True, "run": _task_payload(updated or run, runtime.tasks, with_details=False)})
        if action == "resume":
            if not runtime.tasks.is_resumable(run):
                return JSONResponse({"error": f"run 不可恢复（当前 {run.state.value}）"}, status_code=409)
            live = _live_get(run_id)
            if live is not None and live.task is not None and not live.task.done() \
                    and not live.finished:
                return JSONResponse({"ok": True, "already_running": True,
                                     "run": _task_payload(run, runtime.tasks, with_details=False),
                                     "stream": f"/api/runs/{run_id}/stream"})
            started = _start_run_once(run_id)
            if started is None:
                return JSONResponse({"error": "Run 启动失败"}, status_code=400)
            return JSONResponse({"ok": True, "run_id": run_id,
                                 "stream": f"/api/runs/{run_id}/stream",
                                 "run": _task_payload(run, runtime.tasks, with_details=False)},
                                status_code=202)
        # pause
        if run.state == TaskState.PAUSED:
            return JSONResponse({"ok": True, "run": _task_payload(run, runtime.tasks, with_details=False)})
        updated = runtime.tasks.transition(run.id, TaskState.PAUSED, reason="user pause")
        return JSONResponse({"ok": True, "run": _task_payload(updated, runtime.tasks, with_details=False)})
    except AgentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


async def api_task_stream(request: Request) -> StreamingResponse:
    """GET /api/tasks/{task_id}/stream —— 只读订阅容器内某 Run（?run_id=）。

    消息创建已废弃（GET 不写库）：请使用 POST /api/tasks/{task_id}/runs 后订阅返回的 run_id。
    """
    runtime = AgentRuntime.get_default()
    runtime._ensure()
    container = runtime.tasks.get_container(request.path_params.get("task_id", ""))
    if container is None:
        return StreamingResponse(
            iter([_sse("error", {"message": "task container not found"})]), media_type="text/event-stream"
        )
    message = request.query_params.get("message", "").strip()
    run_id = (request.query_params.get("run_id", "") or "").strip()
    if message and not run_id:
        return StreamingResponse(iter([_sse_deprecated_create()]),
                                 media_type="text/event-stream")
    if not run_id:
        return StreamingResponse(
            iter([_sse("error", {"message": "需要 run_id 参数（消息请通过 POST /api/tasks/{task_id}/runs 提交）"})]),
            media_type="text/event-stream",
        )
    if runtime.tasks.get_run_container_id(run_id) != container["id"]:
        return StreamingResponse(
            iter([_sse("error", {"message": f"run {run_id} 不属于该 Task"})]),
            media_type="text/event-stream",
        )
    return StreamingResponse(_subscribe_generator(run_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app = Starlette(
    routes=[
        Route("/", index_page),
        Route("/chat", legacy_chat_page),
        Route("/runtime", runtime_page),
        Route("/api/sessions", api_sessions),
        Route("/api/history", api_history),
        Route("/api/stream", api_stream),
        Route("/api/approval", api_approval, methods=["POST"]),
        Route("/api/approvals", api_approvals_list),
        Route("/api/artifacts", api_artifacts_list),
        Route("/api/artifacts/upload", api_material_upload, methods=["POST"]),
        Route("/api/artifacts/{artifact_id}", api_artifact_meta),
        Route("/api/artifacts/{artifact_id}/download", api_artifact_download),
        # 新模型：Project(产品级) / WorkLocation / Sources / Artifacts（v3）
        Route("/api/projects", api_projects_list),
        Route("/api/projects/create", api_projects_create, methods=["POST"]),
        Route("/api/projects/{project_id}", api_project_detail),
        Route("/api/projects/{project_id}/update", api_project_update, methods=["POST"]),
        Route("/api/projects/{project_id}/delete", api_project_delete, methods=["POST"]),
        Route("/api/projects/{project_id}/reset", api_project_reset, methods=["POST"]),
        Route("/api/projects/{project_id}/messages", api_project_messages_list),
        Route("/api/projects/{project_id}/messages/create", api_project_messages_create, methods=["POST"]),
        Route("/api/projects/{project_id}/stream", api_project_stream),
        Route("/api/projects/{project_id}/sources", api_project_sources_list),
        Route("/api/projects/{project_id}/sources/upload", api_project_sources_upload, methods=["POST"]),
        Route("/api/projects/{project_id}/sources/{source_id}/delete", api_project_sources_delete, methods=["POST"]),
        Route("/api/projects/{project_id}/attachments", api_project_attachments_list),
        Route("/api/projects/{project_id}/attachments/upload", api_project_attachments_upload, methods=["POST"]),
        Route("/api/projects/{project_id}/attachments/refs", api_project_attachment_refs, methods=["POST"]),
        Route("/api/projects/{project_id}/attachments/{attachment_id}/delete", api_project_attachment_delete, methods=["POST"]),
        Route("/api/projects/{project_id}/attachments/{attachment_id}/promote", api_project_attachment_promote, methods=["POST"]),
        Route("/api/projects/{project_id}/artifacts", api_project_artifacts_list),
        Route("/api/projects/{project_id}/memories", api_project_memories_list),
        Route("/api/projects/{project_id}/memories/{memory_id}/delete", api_project_memory_delete, methods=["POST"]),
        Route("/api/worklocations", api_work_locations_list),
        Route("/api/worklocations/create", api_work_locations_create, methods=["POST"]),
        Route("/api/worklocations/pick", api_work_locations_pick, methods=["POST"]),
        # v2 兼容：Task(容器) / Message / Run
        Route("/api/tasks", api_task_containers_list),
        Route("/api/tasks/create", api_task_containers_create, methods=["POST"]),
        Route("/api/tasks/{task_id}", api_task_container_detail),
        Route("/api/tasks/{task_id}/runs", api_task_runs_create, methods=["POST"]),
        Route("/api/tasks/{task_id}/messages", api_task_messages_list),
        Route("/api/tasks/{task_id}/messages/create", api_task_messages_create, methods=["POST"]),
        Route("/api/tasks/{task_id}/stream", api_task_stream),
        Route("/api/runs", api_runs_list),
        Route("/api/projects/{project_id}/runs", api_project_runs_create, methods=["POST"]),
        Route("/api/runs/{run_id}", api_run_detail),
        Route("/api/runs/{run_id}/events", api_run_events),
        Route("/api/runs/{run_id}/pause", _run_action, methods=["POST"]),
        Route("/api/runs/{run_id}/resume", _run_action, methods=["POST"]),
        Route("/api/runs/{run_id}/cancel", _run_action, methods=["POST"]),
        Route("/api/runs/{run_id}/stream", api_run_stream),
        # legacy run-level（旧页面仍可用）
        Route("/api/tasks/{task_id}/events", api_task_events),
        Route("/api/tasks/{task_id}/pause", _task_action, methods=["POST"]),
        Route("/api/tasks/{task_id}/resume", _task_action, methods=["POST"]),
        Route("/api/tasks/{task_id}/cancel", _task_action, methods=["POST"]),
        Route("/api/tools", api_tools),
        Route("/api/runtime/status", api_runtime_status),
        Route("/api/schedules", api_schedules_list),
        Route("/api/memories", api_memories_list),
        Route("/api/memories/delete", api_memory_delete, methods=["POST"]),
        Route("/api/settings/memory", api_memory_setting, methods=["POST"]),
        Route("/api/search", api_search),
        Route("/api/notifications", api_notifications),
        Route("/api/tasks/{task_id}/archive", api_container_archive, methods=["POST"]),
        Route("/api/tasks/{task_id}/pin", api_container_pin, methods=["POST"]),
        Route("/api/tasks/{task_id}/restore", api_container_restore, methods=["POST"]),
        Route("/api/tasks/{task_id}/delete", api_container_delete, methods=["POST"]),
        # llama-ui 前端适配层（OpenAI 协议端点 + 静态资源）
        *llama_bridge.build_llama_ui_routes(),
        Mount("/llama-ui", app=StaticFiles(directory=str(BASE_DIR / "web" / "llama-ui")),
              name="llama-ui-assets"),
        Mount("/rt", app=StaticFiles(directory=str(BASE_DIR / "web" / "runtime")), name="runtime-assets"),
    ]
)


def main() -> None:
    parser = argparse.ArgumentParser(description="全能助手 - 本地网页界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--metrics-port", type=int, default=9095,
                        help="Router 指标 Prometheus 端点端口（0=不启动指标线程，默认 9095）")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    url = f"http://{args.host}:{args.port}"
    print(f"🌐 全能助手网页界面已启动：{url}")
    print("按 Ctrl+C 停止。会话与终端模式共用 sessions.sqlite。")
    try:
        from runtime.task_manager import auto_recover

        recovered = auto_recover()
        if recovered:
            print(f"[RUNTIME] 自动恢复 {len(recovered)} 个崩溃遗留任务（RUNNING→failed）")
    except Exception:
        pass
    # Router 指标线程（Prometheus /metrics on 127.0.0.1:<metrics-port>）
    # 嵌入主进程，避免单独起 scripts/router_metrics_server.py；端口被占或 0 时静默跳过
    if args.metrics_port and args.metrics_port != 0:
        try:
            sys.path.insert(0, str(BASE_DIR))
            from scripts.router_metrics_server import start_metrics_thread
            start_metrics_thread(args.metrics_port)
        except Exception as _e:
            print(f"[router-metrics] 指标线程启动失败（不影响主功能）：{_e}", file=sys.stderr)
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
