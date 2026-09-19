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
import threading
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


def api_slots(request: Request) -> Response:
    """GET /slots —— llama.cpp WebUI 的模型 slot 列表。

    本适配层不走 llama.cpp 本地推理，固定返回一个空 slot 列表，避免前端
    fetch('./slots') 时被 SPA fallback 成 HTML。
    """
    return JSONResponse([])


def api_models_load(request: Request) -> Response:
    """POST /models/load —— 模型加载端点（适配层无本地模型，返回成功占位）。"""
    return JSONResponse({"success": True, "message": "managed model load stub"})


def api_models_unload(request: Request) -> Response:
    """POST /models/unload —— 模型卸载端点（适配层无本地模型，返回成功占位）。"""
    return JSONResponse({"success": True, "message": "managed model unload stub"})


def api_models_sse(request: Request) -> Response:
    """GET /models/sse —— 模型事件 SSE 端点（适配层无本地模型，仅保持协议可用）。"""
    async def gen():
        # llama.cpp WebUI 期望 SSE 流；无本地推理时先保持连接不推送事件。
        # 这里返回一个空生成器，客户端读满后即可断开。
        yield b""
    return StreamingResponse(gen(), media_type="text/event-stream")


async def api_stream_lookup(request: Request) -> Response:
    """POST /v1/streams/lookup —— 列出仍在生成中的会话流。

    前端 `selectActiveStream` 会过滤 `!is_done` 并取 `started_at` 最新的一条，
    据此决定「打开会话时是否需要挂到服务端在飞的流上」（页面刷新后续上）。
    返回空数组即表示没有可挂载的流。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("conversation_ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        ids = []
    out: list[dict[str, Any]] = []
    with _STREAM_LOCK:
        _stream_prune_locked()
        for raw in ids:
            st = _stream_find_locked(str(raw))
            if st is None or st.done:
                continue
            out.append({
                # 前端用 `conversation_id` 再做一次 Y6 解析，必须回传完整 key
                "conversation_id": st.key,
                "is_done": False,
                "started_at": st.started_at,
            })
    return JSONResponse(out)


async def api_stream_resume(request: Request) -> Response:
    """GET / DELETE /v1/stream —— 会话流续读 / 取消（llama.cpp 可恢复流语义）。

    GET  ?conv_id=<会话标识>&from=<已读字节偏移>
        从偏移处回放存量字节，并继续跟随新字节，直到本轮生成结束。
        偏移语义与前端一致：`from` = 前端已完整处理的字节数，因此直接
        `buf[from:]` 回放既不会重复也不会漏帧。

    DELETE ?conv_id=<会话标识>
        真实取消该会话在飞的 run（前端“停止生成”走这条）。

    未知会话返回 404：前端 `probeResumeStatus` 见到非 200 会清理本地残留
    stream 状态；若这里也回 200，前端会误以为「服务端仍有流」而反复挂载。
    """
    key = request.query_params.get("conv_id") or ""
    if request.method == "DELETE":
        st = _stream_find(key)
        if st is None:
            return JSONResponse({"ok": True, "cancelled": False,
                                 "note": "no such stream"})
        try:
            from runtime.runner import AgentRuntime
            rt = AgentRuntime.get_default()
            rt._ensure()
            cancelled = bool(rt.cancel_run(st.run_id))
        except Exception as e:
            return JSONResponse({"ok": False, "cancelled": False, "error": str(e)})
        return JSONResponse({"ok": True, "cancelled": cancelled,
                             "run_id": st.run_id})

    st = _stream_find(key)
    if st is None:
        return JSONResponse({"error": "no such stream", "conv_id": key}, status_code=404)

    raw_from = request.query_params.get("from") or "0"
    try:
        offset = int(float(raw_from))
    except (TypeError, ValueError):
        offset = 0

    async def gen():
        async for chunk in st.read_from(offset):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


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
        # 修复 B：session 生命周期与 Run 对齐。
        # SQLiteSession.close() 是**同步**方法（非 async），且无公开 is_closed 属性
        # （SDK 内部 _check_not_closed 抛 RuntimeError("SQLiteSession is closed")）。
        # 在 _bg() 的 finally 统一 close，确保 run_turn 末尾的 save_result_to_session
        # 之后才关连接；关闭失败静默降级（不阻塞 UI）。
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
        finally:
            # close() 是同步方法；已关过/连接被 SDK 内部 invalidate 时静默降级。
            try:
                session.close()
            except Exception:
                pass

    task = rt.spawn_run_task(run_id, _bg())
    return run_id, session_id, queue, task


def _assistant_parts(final_output):
    from runtime.runner import _assistant_parts as _ap
    try:
        return _ap(final_output)
    except Exception:
        return "", "answer"


# ---------------------------------------------------------------------------
# 可恢复流会话注册表（llama.cpp `/v1/stream` 语义）
# ---------------------------------------------------------------------------
#
# 为什么必须有它（真实故障复盘）：
# llama-ui 前端在「页面重新变为可见」且距上次收到字节超过 3s 时，会主动
# `reader.cancel()` 掉当前 SSE（bundle 内 `const P = () => ... g.cancel()`），
# 然后请求 `GET /v1/stream?conv_id=<会话>&from=<已读字节数>` 续读。
# 用户只要在模型思考期间切一下窗口再点回来，就必然触发这条路径。
#
# 若服务端用空占位/假帧应付，后果是：
#   1) 前端报 “Stream connection lost and could not be resumed”；
#   2) 这一轮已经生成的正文**全部丢失**（实测页面文本为空）；
#   3) 前端还会反复轮询 /v1/streams/lookup。
#
# 因此这里把「生成」与「HTTP 连接」彻底解耦：帧先写进会话缓冲，HTTP 侧只是
# 订阅者。POST 被 cancel、页面刷新、稍后再连，都能按字节偏移续读。
_STREAM_RETENTION_SECONDS = 300.0   # 已结束的流保留时长（供迟到续读回放）
_STREAM_MAX_ENTRIES = 64            # 最多保留的会话数（防内存无界增长）
_STREAMS: dict[str, "_ConvStream"] = {}
_STREAM_ALIAS: dict[str, str] = {}  # 裸 conv 前缀 → 实际 key（容忍 ::model 差异）
_STREAM_LOCK = threading.Lock()
_PUMP_TASKS: set[asyncio.Task] = set()


class _ConvStream:
    """单个会话的 SSE 字节缓冲，支持从任意字节偏移续读。"""

    __slots__ = ("key", "run_id", "started_at", "buf", "done", "closed_at",
                 "_version", "_tick")

    def __init__(self, key: str, run_id: str) -> None:
        self.key = key
        self.run_id = run_id
        self.started_at = time.time()
        self.buf = bytearray()
        self.done = False
        self.closed_at: float | None = None
        self._version = 0
        self._tick = asyncio.Event()

    # publish/finish 必须能**同步调用**：它们由 run 的事件回调直接触发，
    # 且不能依赖 HTTP 连接是否还活着。
    def publish(self, frame: str) -> None:
        if not frame:
            return
        self.buf += frame.encode("utf-8")
        self._version += 1
        self._tick.set()

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        self.closed_at = time.time()
        self._version += 1
        self._tick.set()

    async def read_from(self, offset: int):
        """async generator：先回放 offset 之后的存量字节，再跟随新字节直到流结束。"""
        pos = max(0, min(int(offset or 0), len(self.buf)))
        while True:
            if pos < len(self.buf):
                chunk = bytes(self.buf[pos:])
                pos = len(self.buf)
                yield chunk
                continue
            if self.done:
                return
            version = self._version
            # 清事件前再确认一次，避免 clear/wait 之间丢通知导致永久挂起。
            if pos < len(self.buf) or self.done or self._version != version:
                continue
            self._tick.clear()
            if pos < len(self.buf) or self.done or self._version != version:
                continue
            await self._tick.wait()


def _base_conv_key(key: str) -> str:
    """`conv::model` → `conv`；`conv` → `conv`。"""
    return key.split("::", 1)[0] if key else ""


def _conv_key_from_request(request: Request, payload: dict | None) -> str:
    """会话标识：优先 `X-Conversation-Id` 头（前端对同一会话恒定），
    fallback 到 body 里的 conversation_id / conv_id。

    该值也是前端 `GET /v1/stream?conv_id=` 回传的值，两端必须同源才能真正续上。
    """
    raw = ""
    try:
        raw = request.headers.get("x-conversation-id") or ""
    except Exception:
        raw = ""
    if not raw:
        for field in ("conversation_id", "conv_id", "chat_id"):
            value = (payload or {}).get(field)
            if value:
                raw = str(value)
                break
    return str(raw).strip()


def _stream_prune_locked() -> None:
    """清理过期 / 超量条目（调用方须持锁）。"""
    now = time.time()
    for key, st in list(_STREAMS.items()):
        if st.done and st.closed_at and now - st.closed_at > _STREAM_RETENTION_SECONDS:
            _STREAMS.pop(key, None)
    if len(_STREAMS) > _STREAM_MAX_ENTRIES:
        overflow = len(_STREAMS) - _STREAM_MAX_ENTRIES
        finished = sorted((s for s in _STREAMS.values() if s.done),
                          key=lambda s: s.closed_at or 0.0)
        for st in finished[:overflow]:
            _STREAMS.pop(st.key, None)
        overflow = len(_STREAMS) - _STREAM_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(_STREAMS.values(), key=lambda s: s.started_at)[:overflow]
            for st in oldest:
                _STREAMS.pop(st.key, None)
    for base, key in list(_STREAM_ALIAS.items()):
        if key not in _STREAMS:
            _STREAM_ALIAS.pop(base, None)


def _stream_open(key: str, run_id: str) -> _ConvStream:
    st = _ConvStream(key or f"anon::{run_id}", run_id)
    with _STREAM_LOCK:
        _stream_prune_locked()
        _STREAMS[st.key] = st
        base = _base_conv_key(st.key)
        if base:
            _STREAM_ALIAS[base] = st.key
    return st


def _stream_find_locked(key: str) -> _ConvStream | None:
    """按会话标识找流（调用方须持锁）。容忍 `conv` 与 `conv::model` 两种写法。"""
    if not key:
        return None
    st = _STREAMS.get(key)
    if st is not None:
        return st
    alias = _STREAM_ALIAS.get(_base_conv_key(key))
    if alias:
        st = _STREAMS.get(alias)
        if st is not None:
            return st
    for candidate_key, candidate in _STREAMS.items():
        if candidate_key.startswith(key + "::"):
            return candidate
    return None


def _stream_find(key: str) -> _ConvStream | None:
    with _STREAM_LOCK:
        return _stream_find_locked(key)


def _spawn_pump(coro) -> asyncio.Task:
    """把转帧任务挂到全局集合上，避免被 GC 提前回收。

    关键：pump 不挂在 HTTP 请求上——前端 cancel 掉 POST 后它仍继续写缓冲，
    否则续读只能拿到半截内容。
    """
    task = asyncio.create_task(coro)
    _PUMP_TASKS.add(task)
    task.add_done_callback(_PUMP_TASKS.discard)
    return task


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


# OpenAI 协议的流结束标记。llama-ui 前端只有在收到 `data: [DONE]` 时才把流
# 判定为「正常完成」（bundle 内 `if (payload === "[DONE]") { done = true }`）。
# 缺少它，前端会认为流被中断，进而尝试 stream resume，最终报
# “Stream resume produced no new bytes, giving up”。
_SSE_STREAM_DONE = "data: [DONE]\n\n"


def _openai_sse_done() -> str:
    """OpenAI SSE 流的标准结束帧。"""
    return _SSE_STREAM_DONE


def _final_content_frames(model: str, run_id: str, streamed: str,
                          final_content: str) -> list[str]:
    """计算终帧正文，避免「逐 token 增量 + 终帧整段重发」把正文显示两遍。

    - 增量已完整覆盖终稿 → 不重发
    - 增量只覆盖前缀 → 只补尾部
    - 本轮完全没有增量 → 整段补发（保证内容不丢）
    """
    if not final_content:
        return []
    s = streamed.strip()
    f = final_content.strip()
    if not s:
        return [_openai_chunk(model, run_id, {"content": final_content})]
    if f in s:
        return []
    if s in f and len(f) > len(s):
        idx = final_content.find(s)
        tail = final_content[idx + len(s):] if idx >= 0 else final_content
        return [_openai_chunk(model, run_id, {"content": tail})] if tail.strip() else []
    return [_openai_chunk(model, run_id, {"content": final_content})]


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

    # ② 真·逐 token 流式 SSE：先写入会话缓冲，HTTP 连接只做订阅者。
    # 这样即便前端按 visibilitychange 逻辑 cancel 掉本次 POST，生成仍继续、
    # 缓冲继续增长，后续 GET /v1/stream 可从断点完整续读（不丢正文）。
    conv_key = _conv_key_from_request(request, payload)
    conv_stream = _stream_open(conv_key, run_id)

    async def _pump() -> None:
        """消费 run 事件 → 转 OpenAI SSE 帧 → 写会话缓冲（与连接无关）。"""
        conv_stream.publish(_openai_chunk(model, run_id, {"role": "assistant", "content": ""}))
        streamed_parts: list[str] = []
        try:
            while True:
                channel, ev = await queue.get()
                if channel == "__end__":
                    content = ev.get("content") or ""
                    for frame in _final_content_frames(model, run_id,
                                                       "".join(streamed_parts), content):
                        conv_stream.publish(frame)
                    conv_stream.publish(_openai_chunk(model, run_id, {}, finish="stop"))
                    conv_stream.publish(_openai_done(run_id))
                    conv_stream.publish(_SSE_STREAM_DONE)
                    return
                if channel == "__error__":
                    conv_stream.publish(_openai_chunk(
                        model, run_id, {"content": "运行出错：" + (ev.get("error") or "")}))
                    conv_stream.publish(_openai_chunk(model, run_id, {}, finish="error"))
                    conv_stream.publish(_openai_done(run_id))
                    conv_stream.publish(_SSE_STREAM_DONE)
                    return
                if channel == "assistant_delta":
                    # ② 逐 token：event.metadata.delta 是文本增量
                    delta = (ev.get("metadata") or {}).get("delta", "")
                    if delta:
                        streamed_parts.append(delta)
                        conv_stream.publish(_openai_chunk(model, run_id, {"content": delta}))
                elif channel == "tool" and ev.get("name"):
                    # 工具调用透传（辅助观察）
                    label = ev.get("name") or ""
                    args = ev.get("args") or ""
                    conv_stream.publish(_openai_chunk(model, run_id, {"content": f"\n[{label} {args}]\n"}))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 兜底：任何异常也必须给出结束帧，否则续读方会一直等下去。
            conv_stream.publish(_openai_chunk(model, run_id, {"content": f"\n[error] {e}\n"}))
            conv_stream.publish(_openai_chunk(model, run_id, {}, finish="error"))
            conv_stream.publish(_openai_done(run_id))
            conv_stream.publish(_SSE_STREAM_DONE)
        finally:
            conv_stream.finish()

    _spawn_pump(_pump())

    async def gen():
        async for chunk in conv_stream.read_from(0):
            yield chunk

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
    """返回 Starlette Route 列表，供 webapp.py 合进 routes。

    同时提供根路径版本与 /llama-ui 前缀版本：
    - llama-ui 构建产物内的大量 fetch 是相对路径（./props、./v1/models 等），
      页面挂在 /llama-ui/ 时会被解析成 /llama-ui/props，需要带前缀路由承接。
    - 根路径版本保留，兼容旧调用方（直接访问 /v1/models、/props 等）。
    """
    from starlette.routing import Route
    prefix_paths = [
        ("/v1/models", api_models, ["GET"]),
        ("/props", api_props, ["GET"]),
        ("/tools", api_tools_list, ["GET"]),
        ("/tools", api_tools_execute, ["POST"]),
        ("/v1/chat/completions", api_chat_completions, ["POST"]),
        ("/v1/chat/completions/control", api_chat_control, ["POST"]),
        ("/slots", api_slots, ["GET"]),
        ("/models/load", api_models_load, ["POST"]),
        ("/models/unload", api_models_unload, ["POST"]),
        ("/models/sse", api_models_sse, ["GET"]),
        ("/v1/streams/lookup", api_stream_lookup, ["POST"]),
        # GET 续读 / DELETE 取消（前端“停止生成”走 DELETE）
        ("/v1/stream", api_stream_resume, ["GET", "DELETE"]),
    ]
    routes: list[Route] = []
    for path, handler, methods in prefix_paths:
        routes.append(Route(path, handler, methods=methods))
        if not path.startswith("/llama-ui"):
            prefixed = "/llama-ui" + path
            routes.append(Route(prefixed, handler, methods=methods))
    return routes
