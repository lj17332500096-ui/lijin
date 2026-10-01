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

错误结构（2026-09-22，API 接口层审计 C3）
----------------------------------------
非 2xx 一律走 `_error()`，产出 OpenAI 标准信封：
    {"error": {"message", "type", "code", "retryable"}, "request_id": "<id>"}
形状不是自定的，而是由前端取错函数 `kIe()` 的**首选**分支决定（`r?.error?.message`）。
**200 响应不要用这个信封**：工具结果走 `plain_text_response`（纯文本）；
200 里若出现 `error`，它必须是**字符串**——前端对它做 `String(a.error)`，
传对象会被渲染成 `[object Object]`。

写操作守卫（2026-09-22，C6）
---------------------------
**会直接产生副作用**的 POST 端点（`/tools`、`/v1/chat/completions`、
`/v1/chat/completions/control`）先过 `_require_json_content_type()`：非
`application/json` 直接 415。这不是格式校验，而是反 CSRF，删掉即重新打开缺口——
跨站 `fetch(url, {method:"POST", body:"..."})` 的 Content-Type 默认是 `text/plain`，
属"简单请求"**不触发预检**、浏览器会直接投递，而 `await request.json()` **不校验**类型；
强制 JSON 会让浏览器先发预检，而本服务不返回 CORS 头 → 实际 POST 被浏览器拦下。
只读端点（`/props`、`/slots`、`/v1/models`、`/v1/streams/lookup`、`/models/*`）与
无 body 的 GET/DELETE **不加**守卫（前端这些请求走 `A0()`，本就不带 Content-Type）。
另：`webapp.main()` 有**绑定断言**——非回环监听需显式 `FORGE_ALLOW_NONLOCAL_UI=1`。
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


# ---------------------------------------------------------------------------
# 统一错误结构（C3）与 POST 守卫（C6）—— 2026-09-22 API 接口层审计
# ---------------------------------------------------------------------------
_JSON_CONTENT_TYPE = "application/json"


def _request_id(request: Request) -> str:
    """请求标识：优先沿用调用方的 X-Request-Id，否则本层生成。

    同时绑定到当前 context（审计 D3）：后续该请求触发的所有结构化日志行
    （runner / task_manager / provider_gateway 的 slog）都会带上同一个 id，
    用 grep `request_id=...` 即可把一次 HTTP 请求的全链路日志串起来。
    """
    rid = (request.headers.get("x-request-id") or "").strip()
    if rid:
        rid = rid[:64]
    else:
        rid = "llmb-" + uuid.uuid4().hex[:12]
    try:
        from runtime.structured_log import _request_id as _bind_rid
        _bind_rid.set(rid)
    except Exception:
        pass
    return rid


def _error(request: Request, status: int, message: str, *,
           code: str | None = None, err_type: str | None = None,
           retryable: bool = False, **extra) -> JSONResponse:
    """统一错误信封（C3）：`{"error": {message, type, code, retryable}, "request_id": ...}`。

    形状选择**不是**自定的：前端取错的函数 `kIe()` 按此优先级读取——
    `r?.error?.message` → `r?.error`（字符串）→ `r?.message` → HTTP 状态码表。
    这里命中的是**第一优先分支**，因此采用 OpenAI 标准信封。
    注意：信封不能反过来用于**200**响应——工具执行结果走 `_tool_payload()`，
    那里 `error` 必须是**字符串**（前端对它做 `String(a.error)`，传对象会渲染成 `[object Object]`）。
    """
    body: dict[str, Any] = {
        "error": {
            "message": str(message),
            "type": err_type or "invalid_request_error",
            "code": code or f"http_{status}",
            "retryable": bool(retryable),
        },
        "request_id": _request_id(request),
    }
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _require_json_content_type(request: Request) -> JSONResponse | None:
    """POST 必须带 `Content-Type: application/json`，否则 415（C6 反 CSRF）。

    对本地前端**零影响**：实测 bundle 内 `Lb(){return{[ds.CONTENT_TYPE]:sv.JSON,...A0()}}`，
    所有带 body 的请求（`JT` / `x_e` / 裸 `fetch(..., {headers:Lb()})`）都经它发出，
    必带该类型。无 body 的 GET / DELETE 走 `A0()`，因此**本守卫只加在 POST 上**。

    为什么这条能挡跨站请求（不是可有可无的格式校验，删掉即重新打开缺口）：
    跨站 `fetch(url, {method:"POST", body:"..."})` 的 Content-Type 默认是 `text/plain`，
    属"简单请求"**不触发预检**、浏览器会直接投递；而 `await request.json()`
    **并不校验** Content-Type、照样解析 body。于是任意网页都能在用户浏览器里
    触发本服务执行工具（响应用户读不到，但副作用已经发生）。
    强制 JSON 类型会让浏览器先发预检，而本服务不返回 CORS 头 → 实际 POST 被浏览器拦下。
    """
    raw = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if raw != _JSON_CONTENT_TYPE:
        return _error(
            request, 415, "Content-Type 必须是 application/json",
            code="unsupported_media_type",
            err_type="invalid_request_error",
        )
    return None


async def _json_body(request: Request, *,
                     code: str = "invalid_json") -> tuple[dict | None, JSONResponse | None]:
    """解析 JSON body，失败时返回统一错误（而不是让异常冒成 500）。"""
    try:
        body = await request.json()
    except Exception as exc:
        return None, _error(request, 400, f"请求体不是合法 JSON：{exc}", code=code)
    if not isinstance(body, dict):
        return None, _error(request, 400, "请求体必须是 JSON 对象", code=code)
    return body, None


def _model_name() -> str:
    return os.getenv("AGENT_MODEL") or "agnes-2.5-flash"


def _sessions_db() -> str:
    from runtime.session_storage import SESSIONS_DB
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


def _request_principal(request: Request, payload: dict | None = None) -> str:
    """Return verified remote identity, or a local session label in loopback mode."""
    authenticated = getattr(getattr(request, "state", None), "forge_principal", None)
    if authenticated:
        return "remote-" + _sanitize_user(str(authenticated))
    return "local-" + _user_id_from_request(request, payload)


def _session_id_for(user: str, model: str) -> str:
    """session_id = 用户标识 + model 双键，彻底隔离多用户 / 多模型历史。"""
    return "llama-" + _sanitize_user(user) + "-" + _sanitize_user(model)


# ---------------------------------------------------------------------------
# 静态资源入口
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
    """模型由 Agent Provider 管理；此兼容端点不支持加载模型。"""
    return JSONResponse({
        "success": False, "status": "unsupported", "operation": "load",
        "message": "模型生命周期由 Agent Provider 管理；该接口未执行加载。",
    }, status_code=501)


def api_models_unload(request: Request) -> Response:
    """模型由 Agent Provider 管理；此兼容端点不支持卸载模型。"""
    return JSONResponse({
        "success": False, "status": "unsupported", "operation": "unload",
        "message": "模型生命周期由 Agent Provider 管理；该接口未执行卸载。",
    }, status_code=501)


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
    owner_id = _request_principal(request, body)
    out: list[dict[str, Any]] = []
    with _STREAM_LOCK:
        _stream_prune_locked()
        for raw in ids:
            st = _stream_find_locked(str(raw), owner_id)
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
    owner_id = _request_principal(request)
    if request.method == "DELETE":
        st = _stream_find(key, owner_id)
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

    st = _stream_find(key, owner_id)
    if st is None:
        # 统一错误信封（C3）；conv_id 作为附加上下文保留（前端也会读它做清理判断）。
        return _error(request, 404, "no such stream", code="stream_not_found", conv_id=key)

    raw_from = request.query_params.get("from") or "0"
    try:
        offset = int(float(raw_from))
    except (TypeError, ValueError):
        offset = 0
    if offset < st.base_offset:
        return _error(
            request, 410, "该流的早期字节已超出缓存预算，无法从请求偏移继续。",
            code="stream_offset_expired", conv_id=key,
            earliest_offset=st.base_offset,
        )

    async def gen():
        async for chunk in st.read_from(offset):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def api_tools_execute(request) -> Response:
    # C6：本端点是全桥唯一「直接执行工具、直接产生副作用」的入口，先过 Content-Type 守卫。
    guard = _require_json_content_type(request)
    if guard is not None:
        return guard

    from runtime.runner import AgentRuntime
    rt = AgentRuntime.get_default()
    rt._ensure()
    body, bad = await _json_body(request, code="invalid_tool_request")
    if bad is not None:
        return bad
    tool = body.get("tool") or body.get("name")
    params = body.get("params") or body.get("arguments") or {}
    # 工具名必须是字符串：非字符串会在 gate.should_gate 的集合查找里抛 TypeError，
    # 变成 500 而不是可读的 400。
    if isinstance(tool, str):
        tool = tool.strip()
    if not tool or not isinstance(tool, str):
        return _error(request, 400, "缺少 tool 字段（需为非空字符串）", code="missing_tool")

    # Direct calls are only a read-only compatibility path. Mutations must enter
    # through run_turn so RunContext, approval, state, and write-ahead evidence agree.
    gate = _approval_gate()
    try:
        binding = rt.registry.get(tool)
    except Exception:
        return _error(request, 404, f"未知工具：{tool}", code="tool_not_found")
    if binding.spec.side_effect or (gate is not None and gate.should_gate(tool)):
        reason = f"工具 {tool} 不能通过直调接口执行；请通过完整 Agent 对话发起该操作。"
        return _error(request, 409, reason, code="tool_requires_agent_run")

    try:
        principal = _request_principal(request, body)
        invocation = await rt.execute_readonly_api_tool(
            tool, params, session_id=f"api-tool-{principal}", user_id=principal,
        )
    except Exception as e:
        # 200 + plain_text_response：成功与失败都走文本结果，保持前端既有契约。
        return JSONResponse({"plain_text_response": f"工具执行失败：{type(e).__name__}: {e}"}, status_code=200)
    return JSONResponse({
        "plain_text_response": invocation.get("result") or "",
        "run_id": invocation.get("run_id"),
        "execution_status": invocation.get("execution_status"),
    })


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
    from runtime.session_storage import SESSIONS_DB
    from runtime.runner import AgentRuntime

    rt = AgentRuntime.get_default()
    rt._ensure()

    # ① session 按用户 + model 双键隔离
    model = payload.get("model") or _model_name()
    session_id = _session_id_for(user, model)
    session = SQLiteSession(session_id, db_path=str(SESSIONS_DB))

    run_id = uuid.uuid4().hex[:16]
    queue: asyncio.Queue = asyncio.Queue()
    _remember_run_owner(run_id, user)

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
            model_calls = rt.tasks.list_model_calls(res.task.id) if res.task and rt.tasks else []
            successful_calls = [call for call in model_calls if call.get("status") in ("ok", "success", "succeeded")]
            usage = None
            if model_calls:
                usage = {
                    "prompt_tokens": sum(int(call.get("input_tokens") or 0) for call in model_calls),
                    "completion_tokens": sum(int(call.get("output_tokens") or 0) for call in model_calls),
                }
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            actual_model = next((call.get("model") for call in reversed(successful_calls)
                                 if call.get("model")), None) or model
            queue.put_nowait(("__end__", {
                "content": content, "kind": kind, "state": state, "ok": res.ok,
                "error": res.error, "model": actual_model, "usage": usage,
            }))
        except Exception as e:
            queue.put_nowait(("__error__", {"error": f"{type(e).__name__}: {e}"}))
        finally:
            # close() 是同步方法；已关过/连接被 SDK 内部 invalidate 时静默降级。
            try:
                session.close()
            except Exception:
                pass

    task = rt.spawn_run_task(run_id, _bg())
    task.add_done_callback(lambda _task, rid=run_id: _mark_run_owner_finished(rid))
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
_STREAM_MAX_BYTES = 4 * 1024 * 1024
_STREAM_TOTAL_MAX_BYTES = 32 * 1024 * 1024
_STREAMS: dict[str, "_ConvStream"] = {}
_STREAM_ALIAS: dict[str, str] = {}  # 裸 conv 前缀 → 实际 key（容忍 ::model 差异）
_STREAM_LOCK = threading.Lock()
_PUMP_TASKS: set[asyncio.Task] = set()
_RUN_OWNER_RETENTION_SECONDS = 300.0
_RUN_OWNER_MAX_ENTRIES = 4096
_RUN_OWNERS: dict[str, tuple[str, float | None]] = {}
_RUN_OWNER_LOCK = threading.Lock()


def _prune_run_owners_locked(now: float) -> None:
    expired = [run_id for run_id, (_owner, expiry) in _RUN_OWNERS.items()
               if expiry is not None and expiry <= now]
    for run_id in expired:
        _RUN_OWNERS.pop(run_id, None)
    if len(_RUN_OWNERS) > _RUN_OWNER_MAX_ENTRIES:
        finished = sorted(((expiry, run_id) for run_id, (_owner, expiry)
                           in _RUN_OWNERS.items() if expiry is not None))
        for _expiry, run_id in finished[:len(_RUN_OWNERS) - _RUN_OWNER_MAX_ENTRIES]:
            _RUN_OWNERS.pop(run_id, None)


def _remember_run_owner(run_id: str, owner_id: str) -> None:
    with _RUN_OWNER_LOCK:
        _prune_run_owners_locked(time.time())
        _RUN_OWNERS[run_id] = (owner_id, None)


def _mark_run_owner_finished(run_id: str) -> None:
    with _RUN_OWNER_LOCK:
        current = _RUN_OWNERS.get(run_id)
        if current is not None:
            _RUN_OWNERS[run_id] = (current[0], time.time() + _RUN_OWNER_RETENTION_SECONDS)
        _prune_run_owners_locked(time.time())


def _run_owned_by(run_id: str, owner_id: str) -> bool:
    with _RUN_OWNER_LOCK:
        _prune_run_owners_locked(time.time())
        current = _RUN_OWNERS.get(run_id)
        return bool(current and current[0] == owner_id)


class _ConvStream:
    """单个会话的 SSE 字节缓冲，支持从任意字节偏移续读。"""

    __slots__ = ("key", "run_id", "owner_id", "started_at", "buf", "base_offset", "done", "closed_at",
                 "_version", "_tick")

    def __init__(self, key: str, run_id: str, owner_id: str = "") -> None:
        self.key = key
        self.run_id = run_id
        self.owner_id = owner_id
        self.started_at = time.time()
        self.buf = bytearray()
        self.base_offset = 0
        self.done = False
        self.closed_at: float | None = None
        self._version = 0
        self._tick = asyncio.Event()

    # publish/finish 必须能**同步调用**：它们由 run 的事件回调直接触发，
    # 且不能依赖 HTTP 连接是否还活着。
    def publish(self, frame: str) -> None:
        if not frame:
            return
        payload = frame.encode("utf-8")
        with _STREAM_LOCK:
            self.buf.extend(payload)
            overflow = max(0, len(self.buf) - _STREAM_MAX_BYTES)
            if overflow:
                del self.buf[:overflow]
                self.base_offset += overflow
            _stream_enforce_byte_budget_locked()
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
        pos = max(self.base_offset, min(int(offset or 0), self.base_offset + len(self.buf)))
        while True:
            if pos < self.base_offset:
                payload = json.dumps({
                    "error": {"code": "stream_truncated",
                              "message": "流缓存已超过字节预算，早期输出无法恢复。"},
                    "earliest_offset": self.base_offset,
                }, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n".encode("utf-8")
                return
            end = self.base_offset + len(self.buf)
            if pos < end:
                start = max(0, pos - self.base_offset)
                chunk = bytes(self.buf[start:])
                pos = end
                yield chunk
                continue
            if self.done:
                return
            version = self._version
            # 清事件前再确认一次，避免 clear/wait 之间丢通知导致永久挂起。
            if pos < self.base_offset + len(self.buf) or self.done or self._version != version:
                continue
            self._tick.clear()
            if pos < self.base_offset + len(self.buf) or self.done or self._version != version:
                continue
            await self._tick.wait()


def _stream_enforce_byte_budget_locked() -> None:
    """Trim oldest cached bytes, never silently evict a live stream object."""
    total = sum(len(st.buf) for st in _STREAMS.values())
    if total <= _STREAM_TOTAL_MAX_BYTES:
        return
    for st in sorted(_STREAMS.values(), key=lambda item: item.started_at):
        if total <= _STREAM_TOTAL_MAX_BYTES:
            break
        remove = min(len(st.buf), total - _STREAM_TOTAL_MAX_BYTES)
        if remove:
            del st.buf[:remove]
            st.base_offset += remove
            total -= remove


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
        # Never evict an active stream merely to satisfy the entry target; byte
        # budgets bound payload memory and active stream IDs remain resumable.
    for base, key in list(_STREAM_ALIAS.items()):
        if key not in _STREAMS:
            _STREAM_ALIAS.pop(base, None)


def _stream_open(key: str, run_id: str, owner_id: str = "") -> _ConvStream:
    base_key = key or "anon"
    stable_key = f"{base_key}::run:{run_id}"
    st = _ConvStream(stable_key, run_id, owner_id)
    with _STREAM_LOCK:
        _stream_prune_locked()
        _STREAMS[st.key] = st
        base = _base_conv_key(base_key)
        if base:
            _STREAM_ALIAS[_stream_alias_key(owner_id, base)] = st.key
        if base_key:
            _STREAM_ALIAS[_stream_alias_key(owner_id, base_key)] = st.key
    return st


def _stream_alias_key(owner_id: str, conversation_id: str) -> str:
    return f"{owner_id}\0{conversation_id}" if owner_id else conversation_id


def _stream_find_locked(key: str, owner_id: str | None = None) -> _ConvStream | None:
    """按会话标识找流（调用方须持锁）。容忍 `conv` 与 `conv::model` 两种写法。"""
    if not key:
        return None
    st = _STREAMS.get(key)
    if st is not None and (owner_id is None or st.owner_id == owner_id):
        return st
    alias_name = _base_conv_key(key)
    alias = _STREAM_ALIAS.get(_stream_alias_key(owner_id, alias_name)) if owner_id is not None else None
    if alias is None and owner_id is None:
        alias = _STREAM_ALIAS.get(alias_name)
    if alias:
        st = _STREAMS.get(alias)
        if st is not None and (owner_id is None or st.owner_id == owner_id):
            return st
    for candidate_key, candidate in _STREAMS.items():
        if (candidate_key.startswith(key + "::")
                and (owner_id is None or candidate.owner_id == owner_id)):
            return candidate
    return None


def _stream_find(key: str, owner_id: str | None = None) -> _ConvStream | None:
    with _STREAM_LOCK:
        return _stream_find_locked(key, owner_id)


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
    # C6：本端点会跑完整 agent turn（内部含工具执行），与 /tools 同级，同样过守卫。
    guard = _require_json_content_type(request)
    if guard is not None:
        return guard
    payload, bad = await _json_body(request, code="invalid_chat_request")
    if bad is not None:
        return bad
    message = _last_user_message(payload)
    stream = bool(payload.get("stream"))
    model = _model_name()
    requested_model = str(payload.get("model") or "").strip()
    if requested_model and requested_model != model:
        return _error(
            request, 400,
            f"请求模型 {requested_model} 未配置为当前模型；当前 API 模型别名为 {model}。",
            code="model_not_configured",
        )

    # ① session 按用户隔离：从请求头 / body 提取 user_id
    user = _request_principal(request, payload)

    run_id, session_id, queue, bg_task = await _run_chat_turn(message, payload, request, user)

    if not stream:
        # 非流式：等终态事件。RunActivityProjector 会先发 run.started 等活动事件，
        # 因此不能把队列首项当作结果；仅 __end__/__error__ 才是本轮终态。
        await bg_task
        name = ""
        ev = {}
        while True:
            name, ev = await queue.get()
            if name in ("__end__", "__error__"):
                break
        if name == "__error__":
            # 统一错误信封（C3）：run 内部异常 → 500 + retryable。
            return _error(request, 500, ev["error"], code="run_failed",
                          err_type="server_error", retryable=True)
        content = ev.get("content") or ""
        kind = ev.get("kind", "answer")
        effective_model = ev.get("model") or model
        # ④ 审批门：若本轮需要审批，在 content 里带上标记让前端处理
        body = {
            "id": run_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": effective_model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                },
            ],
        }
        if isinstance(ev.get("usage"), dict):
            body["usage"] = ev["usage"]
        if kind in ("needs_approval", "approval_required"):
            body["choices"][0]["finish_reason"] = "tool_calls"
            body["choices"][0]["message"]["approval_required"] = True
        return JSONResponse(body)

    # ② 真·逐 token 流式 SSE：先写入会话缓冲，HTTP 连接只做订阅者。
    # 这样即便前端按 visibilitychange 逻辑 cancel 掉本次 POST，生成仍继续、
    # 缓冲继续增长，后续 GET /v1/stream 可从断点完整续读（不丢正文）。
    conv_key = _conv_key_from_request(request, payload)
    conv_stream = _stream_open(conv_key, run_id, user)

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
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Forge-Stream-ID": conv_stream.key})


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
    guard = _require_json_content_type(request)
    if guard is not None:
        return guard
    body, bad = await _json_body(request, code="invalid_control_request")
    if bad is not None:
        return bad
    run_id = body.get("run_id") or body.get("id") or ""
    if not run_id:
        # 统一错误信封（C3）。保留 ok=False 供既有调用方按旧契约判断完成度。
        return _error(request, 400, "缺少 run_id", code="missing_run_id", ok=False)
    owner_id = _request_principal(request, body)
    if not _run_owned_by(str(run_id), owner_id):
        return _error(request, 404, "找不到此运行或无权操作", code="run_not_found", ok=False)
    from runtime.runner import AgentRuntime
    rt = AgentRuntime.get_default()
    rt._ensure()
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
