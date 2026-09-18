"""llama-ui 前端 → 本项目 AgentRuntime 的 OpenAI 协议适配层。

把 llama.cpp 内置 WebUI（SvelteKit SPA，只认 OpenAI 协议）接到本项目的
AgentRuntime（Starlette + agents SDK，输出 AgentReply JSON）上。

设计：嵌入 webapp.py，单端口、无跨进程反代。浏览器驱动的 agent loop 中：
- /v1/chat/completions：把 OpenAI messages 转成 user message，调 run_turn；
  SSE 流式（asyncio.Queue 桥接，逐 token 转发 assistant.delta）与非流式两种模式。
- /tools：GET 列工具（OpenAI function 格式），POST 执行（经 ToolBroker；
  写权限工具若命中 ApprovalGate → 返回「需审批」状态而非直接执行）。
- /props、/v1/models：能力标记与模型列表。
- /v1/chat/completions/control：真实取消在飞 run（cancel_run + finalize_cancelled）。

run_turn 内部已跑完整工具循环，浏览器无需回灌 tool_calls；只发 user 消息即可。

会话隔离：session_id 按「用户标识 + model」构造（Authorization Bearer /
X-Forge-User / body.user_id），避免多用户共用同一 SQLiteSession 历史串台。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _model_name() -> str:
    return os.getenv("AGENT_MODEL") or "agnes-2.5-flash"


def _sessions_db() -> str:
    from main import SESSIONS_DB
    return str(SESSIONS_DB)


def _new_session():
    from agents.memory import SQLiteSession
    return SQLiteSession("llama-ui-" + uuid.uuid4().hex[:12], db_path=_sessions_db())


def _sanitize_user(user: str) -> str:
    """把用户标识收敛为文件系统/DB 安全的安全段（防注入 / 长度溢出）。"""
    user = (user or "").strip()
    user = user.replace("/", "_").replace("\\", "_").replace(":", "_")
    user = user.replace(" ", "_").replace("\n", "").replace("\r", "")
    # 只保留字母数字与有限符号，防路径穿越与超长
    import re
    user = re.sub(r"[^A-Za-z0-9._-]", "", user)
    return user[:64] or "anon"


def _user_id_from_request(request: Request, payload: dict | None = None) -> str:
    """从请求头 / body 提取用户标识，作为 session 隔离键。

    优先级：body.user_id > X-Forge-User > Authorization Bearer（去前缀）。
    无标识时回退到 client ip + 随机后缀（同 IP 同浏览器仍共享一条历史，
    但不会跨用户串台）。
    """
    if payload:
        uid = str(payload.get("user_id") or "").strip()
        if uid:
            return _sanitize_user(uid)
    forge_user = (request.headers.get("x-forge-user") or "").strip()
    if forge_user:
        return _sanitize_user(forge_user)
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            # Bearer token 直接用哈希片段做 session 键（稳定，不泄漏原值）
            import hashlib
            return "tok-" + hashlib.sha256(token.encode()).hexdigest()[:16]
    # 回退：client ip + 启动时随机盐（同浏览器会话稳定，跨用户隔离）
    if not hasattr(_user_id_from_request, "_ip_salt"):
        _user_id_from_request._ip_salt = uuid.uuid4().hex[:8]
    ip = request.client.host if request.client else "unknown"
    return f"ip-{_user_id_from_request._ip_salt}-{_sanitize_user(ip)}"


def _session_id_for(user: str, model: str) -> str:
    """session_id = 用户标识 + model 双键，彻底隔离多用户 / 多模型历史。"""
    return "llama-" + _sanitize_user(user) + "-" + _sanitize_user(model)


# ---------------------------------------------------------------------------
# 静态资源（替代 web/runtime.html 的旧入口）
# ---------------------------------------------------------------------------
def mount_llama_ui(app) -> None:
    """把 llama-ui 静态目录挂到 /（SPA fallback 由 index.html 承担）。"""
    static = StaticFiles(directory=os.path.join(BASE_DIR, "web", "llama-ui"))
    app.router.routes.append(static.mount("/", app=static))


# ---------------------------------------------------------------------------
# /v1/models —— OpenAI 模型列表
# ---------------------------------------------------------------------------
def api_models(request: Request) -> Response:
    model = _model_name()
    body = {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "owned_by": "forge", "created": int(time.time())},
        ],
    }
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# /props —— 能力标记
# ---------------------------------------------------------------------------
def api_props(request: Request) -> Response:
    return JSONResponse({
        "defaults": {"temperature": 0.2, "top_p": 0.9},
        "capabilities": {"tools": True, "stream": True},
        "model": _model_name(),
    })


# ---------------------------------------------------------------------------
# /tools —— GET 列工具 / POST 执行工具
# ---------------------------------------------------------------------------
def api_tools_list(request: Request) -> Response:
    from runtime.runner import AgentRuntime
    rt = AgentRuntime.get_default()
    rt._ensure()
    gate = rt.approval
    out = []
    for b in rt.registry.all():
        spec = b.spec
        schema = spec.input_schema or {"type": "object", "properties": {}}
        needs_approval = bool(gate and gate.should_gate(spec.name))
        out.append({
            "tool": spec.name,
            "display_name": spec.name.replace("_", " ").title(),
            "type": "server",
            "permissions": {"write": bool(spec.side_effect), "approval": needs_approval},
            "uses_cwd": spec.name in ("run_python", "write_code_file"),
            "definition": {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description or "",
                    "parameters": schema,
                },
            },
        })
    return JSONResponse(out)


def _approval_gate():
    """取 AgentRuntime 的 ApprovalGate（若可用）。"""
    try:
        from runtime.runner import AgentRuntime
        rt = AgentRuntime.get_default()
        rt._ensure()
        return rt.approval
    except Exception:
        return None


async def api_tools_execute(request) -> Response:
    from runtime.runner import AgentRuntime
    rt = AgentRuntime.get_default()
    rt._ensure()
    body = await request.json()
    tool = body.get("tool") or body.get("name")
    params = body.get("params") or body.get("arguments") or {}
    if not tool:
        return JSONResponse({"error": "缺少 tool 字段"}, status_code=400)

    # ④ 写工具审批门：命中 ApprovalGate 的工具不直接执行，返回「需审批」状态。
    gate = _approval_gate()
    if gate is not None and gate.should_gate(tool):
        # 前端确认语义：body.confirmation_token 与本轮登记的 pending 匹配才放行
        # （与 run_turn 内部审批流一致；此处仅阻断「无人值守静默写盘」）
        return JSONResponse({
            "plain_text_response": "",
            "approval_required": True,
            "approval": {
                "tool": tool,
                "reason": "该操作风险较高，需用户确认后才执行",
                "status": "pending",
            },
        }, status_code=200)

    try:
        result = await rt.broker.execute(tool, params)
    except Exception as e:
        return JSONResponse({"plain_text_response": f"工具执行失败：{type(e).__name__}: {e}"}, status_code=200)
    return JSONResponse({"plain_text_response": result or ""})


# ---------------------------------------------------------------------------
# /v1/chat/completions —— OpenAI chat completions（SSE / 非流式）
# ---------------------------------------------------------------------------
def _last_user_message(payload: dict) -> str:
    """从 OpenAI messages 取最后一条 user 文本（兼容 content 为数组）。"""
    msgs = payload.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") in ("user", "system"):
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(seg.get("text", "") for seg in c if isinstance(seg, dict))
            return (c or "").strip()
    return ""


async def _run_chat_turn(message: str, payload: dict, request: Request | None, user: str):
    """跑一轮 run_turn，返回 (run_id, session_id, queue, task)。

    - session 按「用户 + model」隔离（①）
    - 逐 token 流式：监听 assistant_delta 通道转发 SSE（②）
    - 登记 run_id 供 /control 真实取消（③）
    """
    from agents.memory import SQLiteSession
    from main import SESSIONS_DB
    from runtime.runner import AgentRuntime

    rt = AgentRuntime.get_default()
    rt._ensure()

    # ① session 按用户 + model 双键隔离
    model = payload.get("model") or _model_name()
    session_id = _session_id_for(user, model)
    session = SQLiteSession(session_id, db_path=str(SESSIONS_DB))

    run_id = uuid.uuid4().hex[:16]
    queue: asyncio.Queue = asyncio.Queue()

    def _cb(channel, event):
        """RunActivityProjector._send(channel, event) 回调。

        - channel == 'assistant_delta'：逐 token 文本增量（②真流式）
        - 其它 channel：工具/上下文事件（以注释透传）
        """
        queue.put_nowait((channel, event or {}))

    async def _bg():
        try:
            res = await rt.run_turn(
                message,
                session=session,
                session_id=session_id,
                mode="stream",
                max_turns=20,
                metadata={"channel": "llama-ui", "user": user},
                stream_events_cb=_cb,
            )
            content, kind = _assistant_parts(res.final_output)
            state = res.task.state.value if res.task else "completed"
            queue.put_nowait(("__end__", {"content": content, "kind": kind, "state": state, "ok": res.ok, "error": res.error}))
        except Exception as e:
            queue.put_nowait(("__error__", {"error": f"{type(e).__name__}: {e}"}))

    task = rt.spawn_run_task(run_id, _bg())
    return run_id, session_id, queue, task


def _assistant_parts(final_output):
    from runtime.runner import _assistant_parts as _ap
    try:
        return _ap(final_output)
    except Exception:
        return "", "answer"


def _openai_chunk(model: str, run_id: str, delta: dict, finish: str | None = None,
                  choices_index: int = 0) -> str:
    chunk = {
        "id": run_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": choices_index,
                "delta": delta,
                "finish_reason": finish,
            },
        ],
    }
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"


def _openai_done(run_id: str) -> str:
    return "data: " + json.dumps(
        {"id": run_id, "object": "chat.completion", "created": int(time.time()),
         "model": _model_name(), "choices": []},
        ensure_ascii=False,
    ) + "\n\n"


async def api_chat_completions(request) -> Response:
    payload = await request.json()
    message = _last_user_message(payload)
    stream = bool(payload.get("stream"))
    model = payload.get("model") or _model_name()

    # ① session 按用户隔离：从请求头 / body 提取 user_id
    user = _user_id_from_request(request, payload)

    run_id, session_id, queue, bg_task = await _run_chat_turn(message, payload, request, user)

    if not stream:
        # 非流式：等终态
        await bg_task
        end = queue.get_nowait()
        # 队列里可能残留事件，排空
        while not queue.empty():
            queue.get_nowait()
        name, ev = end
        if name == "__error__":
            return JSONResponse({"error": ev["error"]}, status_code=500)
        content = ev.get("content") or ""
        kind = ev.get("kind", "answer")
        # ④ 审批门：若本轮需要审批，在 content 里带上标记让前端处理
        body = {
            "id": run_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": len(content), "total_tokens": len(content)},
        }
        if kind in ("needs_approval", "approval_required"):
            body["choices"][0]["finish_reason"] = "tool_calls"
            body["choices"][0]["message"]["approval_required"] = True
        return JSONResponse(body)

    # ② 真·逐 token 流式 SSE：转发 assistant_delta 通道的每个 delta
    async def gen():
        yield _openai_chunk(model, run_id, {"role": "assistant", "content": ""})
        try:
            while True:
                channel, ev = await queue.get()
                if channel == "__end__":
                    content = ev.get("content") or ""
                    if content:
                        yield _openai_chunk(model, run_id, {"content": content})
                    yield _openai_chunk(model, run_id, {}, finish="stop")
                    yield _openai_done(run_id)
                    break
                elif channel == "__error__":
                    yield _openai_chunk(model, run_id, {"content": "运行出错：" + (ev.get("error") or "")})
                    yield _openai_chunk(model, run_id, {}, finish="error")
                    yield _openai_done(run_id)
                    break
                elif channel == "assistant_delta":
                    # ② 逐 token：event.metadata.delta 是文本增量
                    delta = (ev.get("metadata") or {}).get("delta", "")
                    if delta:
                        yield _openai_chunk(model, run_id, {"content": delta})
                elif channel == "tool" and ev.get("name"):
                    # 工具调用透传（辅助观察）
                    label = ev.get("name") or ""
                    args = ev.get("args") or ""
                    yield _openai_chunk(model, run_id, {"content": f"\n[{label} {args}]\n"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield _openai_chunk(model, run_id, {"content": f"\n[error] {e}\n"})
            yield _openai_chunk(model, run_id, {}, finish="error")
            yield _openai_done(run_id)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# /v1/chat/completions/control —— 真实取消在飞 run
# ---------------------------------------------------------------------------
async def api_chat_control(request: Request) -> Response:
    """③ 真实取消：取 body.run_id（前端回传本条 SSE 的 run_id），调 cancel_run。

    cancel_run 会标记 _cancel_requested 并 cancel 对应 asyncio.Task；
    spawn_run_task 的 _managed 捕获 CancelledError 时自动 finalize_cancelled
    （收口 CANCELLED + interrupted 副作用），确保"取消后不再进入后续
    Model/Tool iteration"。
    """
    from runtime.runner import AgentRuntime
    rt = AgentRuntime.get_default()
    rt._ensure()
    body = await request.json()
    run_id = body.get("run_id") or body.get("id") or ""
    if not run_id:
        return JSONResponse({"ok": False, "error": "缺少 run_id"}, status_code=400)
    cancelled = rt.cancel_run(run_id)
    return JSONResponse({
        "ok": True,
        "cancelled": cancelled,
        # cancelled=False 表示任务已不在 registry（已结束 / 尚未启动）
        "note": "cancelled=False 时该 run 已自然结束或尚未登记，无需再取消"
                if not cancelled else "已标记取消，finalize_cancelled 由 spawn_run_task 自动收口",
    })


# ---------------------------------------------------------------------------
# 路由注册（供 webapp.py 调用）
# ---------------------------------------------------------------------------
def build_llama_ui_routes():
    """返回 Starlette Route 列表，供 webapp.py 合进 routes。"""
    from starlette.routing import Route
    return [
        Route("/v1/models", api_models, methods=["GET"]),
        Route("/props", api_props, methods=["GET"]),
        Route("/tools", api_tools_list, methods=["GET"]),
        Route("/tools", api_tools_execute, methods=["POST"]),
        Route("/v1/chat/completions", api_chat_completions, methods=["POST"]),
        Route("/v1/chat/completions/control", api_chat_control, methods=["POST"]),
    ]
