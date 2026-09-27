"""Provider/SDK execution adapter used by AgentRuntime and CLI frontends."""
from __future__ import annotations

import os

from agents import OutputGuardrailTripwireTriggered, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.memory import SQLiteSession, SessionSettings
from integrations.mcp_bridge import ensure_connected as ensure_mcp
from runtime.errors import FinalResponseFailed

_PROCESS_PRINTS = True

def _run_config(
    history_limit: int | None = None,
    session: SQLiteSession | None = None,
    provider: object | None = None,
) -> RunConfig | None:
    """按网关配置构造 RunConfig（模型服务在 agent.py 里按 .env 决定）。

    provider：本 Run 显式选择的模型 Provider（本地模型/远程网关）；None=全局默认。
    """
    kwargs: dict = {
        # 模型偶尔会幻觉调用不存在的工具（例如把输出字段名 answer 当工具名）。
        # 默认行为是直接抛错中断；这里让 SDK 把“工具不存在”的提示回传给模型，
        # 让模型自行纠正，而不是整轮失败。
        "tool_not_found_behavior": "return_error_to_model",
    }
    if provider is None:
        from agent import MODEL_PROVIDER as default_provider

        chosen = default_provider
    else:
        chosen = provider
    if chosen is not None:
        kwargs["model_provider"] = chosen
    if history_limit is not None:
        kwargs["session_settings"] = SessionSettings(limit=max(1, history_limit))
    if session is not None:
        kwargs["workflow_name"] = "全能助手"
        kwargs["group_id"] = getattr(session, "session_id", "default")
    return RunConfig(**kwargs)

def _guardrail_reason(exc: Exception) -> str:
    """从 guardrail 异常里取出给用户看的拦截原因。"""
    def _output_info(holder: object) -> object:
        output = getattr(holder, "output", None)
        if output is not None:
            return getattr(output, "output_info", None)
        return getattr(holder, "output_info", None)

    run_data = getattr(exc, "run_data", None)
    if run_data is not None:
        for attr in ("input_guardrail_results", "output_guardrail_results"):
            for result in getattr(run_data, attr, None) or []:
                info = _output_info(result)
                if isinstance(info, dict) and info.get("reason"):
                    return info["reason"]
    info = getattr(exc, "guardrail_result", None)
    output_info = _output_info(info)
    if isinstance(output_info, dict) and output_info.get("reason"):
        return output_info["reason"]
    if output_info:
        return str(output_info)
    return str(exc)[:200]

def _attr(obj: object, name: str, default: object = None) -> object:
    """同时兼容对象（Response 类型）和 dict 形态的 raw_item。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)

def print_run_items(result: object) -> None:
    """把一轮 run 的内部记录（agent 循环产物）打印出来，用于理解运行原理。"""
    print("\n── 本轮 agent 内部循环记录 ──")
    items = getattr(result, "new_items", []) or []
    if not items:
        print("  （没有可展示的内部条目）")
        return
    for item in items:
        raw = getattr(item, "raw_item", None)
        if isinstance(raw, dict):
            raw_type = raw.get("type", "dict")
        else:
            raw_type = getattr(raw, "type", None) or type(raw).__name__
        if raw_type in ("function_call", "tool_call"):
            name = _attr(raw, "name", "?")
            args = _attr(raw, "arguments", None) or _attr(raw, "input", None)
            print(f"  [工具调用] {name}  参数: {_short(args)}")
        elif raw_type in ("function_call_output", "tool_call_output", "function_output", "tool_output"):
            output = _attr(raw, "output", None) or _attr(raw, "output_text", None)
            print(f"  [工具结果] {_short(output)}")
        elif raw_type == "reasoning":
            summary = _attr(raw, "summary", None)
            if isinstance(summary, list):
                summary = summary[0] if summary else None
            if summary is not None:
                summary = _attr(summary, "text", None) or summary
            if summary is not None:
                print(f"  [模型推理] {_short(summary)}")
            else:
                print("  [模型推理]（有推理过程）")
        elif raw_type == "message":
            role = _attr(raw, "role", "?")
            content = _attr(raw, "content", "") or ""
            if isinstance(content, list):
                texts = [
                    _attr(part, "text", None)
                    for part in content
                    if _attr(part, "text", None)
                ]
                content = " ".join(t for t in texts if t)
            print(f"  [消息] {role}: {_short(content, 300)}")
        else:
            print(f"  [{raw_type}] {_short(raw)}")
    print("── 记录结束 ──")

async def _run_attempt(
    mode: str,
    message: str,
    session: SQLiteSession | None,
    run_config: RunConfig | None,
    max_turns: int,
    debug: bool = False,
    agent: object | None = None,
    stream_events_cb: object | None = None,
) -> object:
    """执行一次 Runner（stream 会消费事件流），返回 RunResult。

    stream_events_cb: 可选同步/异步回调 (event_name: str, payload: dict)，
    用于把“过程事件（工具调用/文字增量）”实时导出（web 端转 SSE 用），不影响原有逻辑。
    """
    if agent is None:
        from agent import assistant_agent
        active_agent = assistant_agent
    else:
        active_agent = agent
    if mode == "sync":
        return Runner.run_sync(
            active_agent,
            message,
            session=session,
            run_config=run_config,
            max_turns=max_turns,
        )
    if mode == "stream":
        result = Runner.run_streamed(
            active_agent,
            message,
            session=session,
            run_config=run_config,
            max_turns=max_turns,
        )
        # 流式：边跑边看事件（工具调用即时显示；--debug 显示全部事件类型）
        last_raw_type = None

        def _emit(name: str, payload: dict) -> None:
            if stream_events_cb is None:
                return
            try:
                stream_events_cb(name, payload)
            except Exception:
                pass  # 导出回调失败不影响执行

        async for event in result.stream_events():
            if event.type == "run_item_stream_event":
                if event.name == "tool_called":
                    raw = getattr(event.item, "raw_item", None)
                    if isinstance(raw, dict):
                        tool_name = raw.get("name") or "?"
                        args = raw.get("arguments") or raw.get("input") or ""
                    else:
                        tool_name = getattr(raw, "name", "?")
                        args = getattr(raw, "arguments", "") or ""
                    _process_print(f"\n[调用工具: {tool_name}]")
                    # Execution wrapper owns public tool state; SDK observations are internal.
                elif event.name == "tool_call_created":
                    raw = getattr(event.item, "raw_item", None)
                    tool_name = getattr(raw, "name", "?")
                    # A planned call is not evidence that execution started.
                elif debug:
                    _process_print(f"[流事件] {event.name}")
            elif event.type == "raw_response_event":
                raw_data = getattr(event.data, "data", None) or event.data
                raw_type = getattr(raw_data, "type", "") or getattr(event.data, "type", "")
                # response.output_text.delta 的转发分两种契约，不能无条件转发：
                #  - _public_final（tools-disabled 的最终表达轮）：发 "final.content.delta"
                #    （由 FinalContentStream → activity.delta → assistant_delta 通道消费）。
                #  - 其余 agent（_public_final=False，中间推理/带工具的中间文本）：
                #    **默认不公开**（这些文本对外部消费者是私有的，见
                #    test_sdk_reasoning_tool_args_and_content_isolation）。
                #    只有显式标记 _public_stream=True 的本地 TUI 场景才转发
                #    "assistant_delta" 供实时渲染 —— 由调用方（TUI）显式开启，
                #    不由本函数替所有消费者做决定。
                if raw_type == "response.output_text.delta":
                    delta = getattr(raw_data, "delta", None)
                    if isinstance(delta, str) and delta:
                        if getattr(active_agent, "_public_final", False):
                            _emit("final.content.delta", {"text": delta})
                        elif getattr(active_agent, "_public_stream", False):
                            _emit("assistant_delta", {"metadata": {"delta": delta}})
                if debug:
                    if raw_type != last_raw_type:
                        _process_print(f"[流事件] LLM 原始事件: {raw_type}")
                        last_raw_type = raw_type
            elif event.type == "agent_updated_stream_event" and debug:
                _process_print(f"[流事件] 切换 Agent: {event.new_agent.name}")
        return result
    return await Runner.run(
        active_agent,
        message,
        session=session,
        run_config=run_config,
        max_turns=max_turns,
    )

def _format_retry_max() -> int:
    """输出格式闸允许的重跑次数（L3 重试预算）。默认 1。

    这是「一轮用户请求内」重试叠乘的最后一层：每次重跑都会完整走一遍 agent loop，
    因此会重新消耗 provider 层的尝试预算（`FORGE_PROVIDER_MAX_TOTAL_ATTEMPTS`）。
    旧实现写死 `while attempt < 3` + `if attempt == 1: raise`——第 3 次永远到不了，
    读代码的人会以为有 3 次机会，实际只有 1 次（名册漂移）。现在次数只声明一处。
    """
    try:
        return max(0, int((os.getenv("FORGE_FORMAT_RETRY_MAX") or "").strip() or 1))
    except ValueError:
        return 1

async def execute_turn(
    mode: str,
    message: str,
    session: SQLiteSession | None = None,
    debug: bool = False,
    max_turns: int = 20,
    history_limit: int | None = None,
    agent: object | None = None,
    audit: object | None = None,
    stream_events_cb: object | None = None,
    provider: object | None = None,
) -> object:
    """按指定执行方式跑一轮对话，返回 final_output。

    输出格式闸拦截时自动携带原因重试（次数 = FORGE_FORMAT_RETRY_MAX，默认 1 次；
    长文档/长回复偶发被截断时能自愈）。这是重试叠乘的最外层，见 provider_gateway 模块
    docstring 的「重试与超时的归属」。设为 0 则首轮不通过即刻收口。
    agent 可传模型路由克隆的实例；不传则用当前全局 Agent。
    audit 可选：AuditCollector，成功时自动落每次模型调用/工具调用明细。
    stream_events_cb 可选：实时过程回调（工具/增量文本），供 web SSE 转发。
    provider 可选：本 Run 显式选择的模型 Provider（本地模型/远程网关）；None=全局默认。
    """
    await ensure_mcp()
    run_config = _run_config(history_limit, session, provider=provider)

    # 审计 D3：本 turn 绑定一个 request_id（非 HTTP 入口自行生成），
    # 后续该轮触发的所有 slog 行自动带上它。
    try:
        from runtime.structured_log import new_request_id
        new_request_id("turn")
    except Exception:
        pass

    current = message
    overflow_compacted = False
    max_reruns = _format_retry_max()
    attempt = 0
    while attempt <= max_reruns:
        try:
            result = await _run_attempt(
                mode, current, session, run_config, max_turns, debug=debug, agent=agent,
                stream_events_cb=stream_events_cb,
            )
            if debug:
                print_run_items(result)
            if audit is not None:
                try:
                    audit.ingest(result)
                except Exception:
                    pass  # 审计失败不影响主流程
            return result.final_output
        except OutputGuardrailTripwireTriggered as exc:
            if attempt >= max_reruns:
                # 执行（工具/文件/验证）与「最终回复」分离：第二次仍失败不再以 SDK 内部
                # 异常上抛，改抛 FinalResponseFailed（携带面向用户的友好原因），
                # 由 Runner 根据真实执行证据决定收口（完成+降级说明 / 真失败）。
                raise FinalResponseFailed(_guardrail_reason(exc)) from exc
            reason = _guardrail_reason(exc)
            # 流式场景：清空上一轮的失败增量，避免“回答重复三次”的视觉叠加
            if stream_events_cb is not None:
                try:
                    stream_events_cb("stream_reset", {"reason": reason[:120]})
                    stream_events_cb("tool", {"name": "自动重试", "args": "上轮输出未通过格式校验，重试一次…"})
                except Exception:
                    pass
            try:
                print(f"[格式校验] 未通过（{reason[:120]}），正在自动重试一次…", flush=True)
            except UnicodeEncodeError:
                print("[格式校验] 未通过，正在自动重试一次…", flush=True)
            current = (
                f"{message}\n"
                "（系统提示：上一轮输出未通过格式校验。请只输出单个合法的 AgentReply JSON 对象，"
                "不要代码块、不要多余文字；内容过长时提炼要点即可，不要整篇转述。）"
            )
            attempt += 1
            continue
        except Exception as exc:
            # P1：context overflow → 强制压缩一次（不消耗输出闸/修复额度）后重试
            if not overflow_compacted:
                from runtime.provider_errors import is_context_overflow as _is_overflow

                if _is_overflow(exc) and session is not None:
                    overflow_compacted = True
                    try:
                        from runtime.context import force_compact as _force_compact

                        _status, _summary = await _force_compact(session)
                        print("[上下文] 超出模型窗口，已强制压缩历史后重试一次…", flush=True)
                        continue
                    except Exception:
                        pass
            raise
    raise OutputGuardrailTripwireTriggered("unreachable")

def set_process_prints(enabled: bool) -> None:
    """允许调用方接管「过程输出」（工具调用行 / 流事件行）。

    这些打印属于表现层，调用方（CLI 消息平台，将来的其它前端）应当自己决定怎么渲染。
    默认保持旧行为（开启）；CLI 平台会在非 debug 模式下关闭，避免与自己的渲染层重复
    输出、也避免破坏调用方的行状态。
    """
    global _PROCESS_PRINTS
    _PROCESS_PRINTS = bool(enabled)

def process_prints_enabled() -> bool:
    return _PROCESS_PRINTS

def _process_print(*parts: object) -> None:
    if not _PROCESS_PRINTS:
        return
    print(*parts, flush=True)
