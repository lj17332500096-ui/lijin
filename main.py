"""终端聊天入口：和「全能助手」对话，支持切换三种执行方式并查看内部运行记录。"""

import argparse
import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from agents import (
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
)
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.memory import SQLiteSession, SessionSettings
from dotenv import load_dotenv

from agent import MODEL_PROVIDER, build_assistant_agent, assistant_agent
from compact import maybe_compact
from mcp_bridge import ensure_connected as ensure_mcp
from observability import install_local_tracing, trace_log_path
from runtime.runner import AgentRuntime
from runtime.task_manager import TaskManager
from runtime.errors import FinalResponseFailed
import scheduler
from schemas import AgentReply

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
SESSIONS_DB = BASE_DIR / "sessions.sqlite"

_KIND_LABELS = {
    "answer": "💬 回答",
    "plan": "📋 方案 / 计划",
    "note": "📝 已保存产出",
    "questions": "🤔 需要你确认",
    "done": "✅ 完成",
}


def check_api_key() -> None:
    if os.getenv("OPENAI_API_KEY"):
        return
    print("未检测到 OPENAI_API_KEY。")
    print("请复制 .env.example 为 .env，填入你的 API Key 后再运行：")
    print("  Copy-Item .env.example .env   # 然后编辑 .env")
    sys.exit(1)


def ensure_utf8_console() -> None:
    """让中文在 Windows 终端里正常显示和输入。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


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
    chosen = provider if provider is not None else MODEL_PROVIDER
    if chosen is not None:
        kwargs["model_provider"] = chosen
    if history_limit is not None:
        kwargs["session_settings"] = SessionSettings(limit=max(1, history_limit))
    if session is not None:
        kwargs["workflow_name"] = "全能助手"
        kwargs["group_id"] = getattr(session, "session_id", "default")
    return RunConfig(**kwargs)


def list_sessions(db_path: str | None = None) -> str:
    """列出 sessions.sqlite 里所有会话及其消息数（用于多会话管理）。"""
    path = Path(db_path) if db_path else SESSIONS_DB
    if not path.exists():
        return "还没有任何会话记录（聊过一轮后会自动创建 sessions.sqlite）。"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            """
            SELECT s.session_id, COUNT(m.id) AS msg_count, MAX(m.created_at) AS last_at
            FROM agent_sessions s
            LEFT JOIN agent_messages m ON m.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return f"读取会话列表失败：{exc}"
    if not rows:
        return "还没有任何会话记录。"
    lines = ["已有的会话："]
    for session_id, msg_count, last_at in rows:
        time_text = (last_at or "从未发言").replace("T", " ")[:19]
        lines.append(f"  📚 {session_id} ｜ 消息 {msg_count} 条 ｜ 最近 {time_text}")
    lines.append("用 --session 名字 切换到某个会话继续聊。")
    return "\n".join(lines)


def clear_session(session_name: str, db_path: str | None = None) -> str:
    """清空某个会话的对话记忆（只影响该会话，不删长期记忆 memory.json）。"""
    path = Path(db_path) if db_path else SESSIONS_DB
    if not path.exists():
        return f"没有找到会话文件，无需清理（{session_name} 不存在）。"
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        cur = conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (session_name,))
        conn.commit()
        deleted = cur.rowcount
        conn.close()
    except sqlite3.Error as exc:
        return f"清空会话失败：{exc}"
    if deleted:
        return f"已清空会话“{session_name}”的对话记忆（长期记忆不受影响）。"
    return f"没有找到名为“{session_name}”的会话，无需清理。"


def _extract_json_object(text: str) -> str:
    """去掉可能的 markdown 代码块，取出第一个 {...} JSON 片段。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    return cleaned[start : end + 1] if 0 <= start < end else cleaned


def _clean_display_text(text: str) -> str:
    """把聊天正文里的 Markdown 符号转成纯文本（仅影响展示，不改存储）。

    模型习惯用 Markdown 排版（# 标题、**加粗**、*斜体*、`代码`、--- 分隔线），
    聊天容器按纯文本渲染，这里把这些符号去掉，保留可读性。
    """
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)             # 行首 # 标题
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)       # **加粗**
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)                   # *斜体*
    text = re.sub(r"`([^`\n]+)`", r"\1", text)                     # `行内代码`
    text = re.sub(r"^\s*(---+|===+)\s*$", "", text, flags=re.M)    # 分隔线
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_reply(reply: AgentReply) -> str:
    """把结构化回复渲染成终端里易读的文本。"""
    label = _KIND_LABELS.get(reply.kind, reply.kind)
    lines: list[str] = [f"── {label} ──"]
    if reply.summary:
        lines.append(f"📌 {reply.summary}")
    if reply.questions:
        lines.append("")
        for i, question in enumerate(reply.questions, 1):
            lines.append(f"  {i}. {question}")
    if reply.content.strip():
        lines.append("")
        lines.append(_clean_display_text(reply.content))
    if reply.saved_file:
        lines.append("")
        lines.append(f"💾 已保存: {reply.saved_file}")
    if reply.next_step:
        lines.append("")
        lines.append(f"➡️ 下一步: {reply.next_step}")
    if reply.ui:
        lines.append("")
        kinds = [getattr(b, "type", "?") for b in reply.ui]
        lines.append(f"📊 附 {len(reply.ui)} 张交互卡片（{', '.join(kinds)}；网页端可见）")
    return "\n".join(lines)


def coerce_reply(final_output: object) -> AgentReply | str:
    """把 final_output 转成 AgentReply（经 ReplyParser 容错：缺字段/别名/fence/纯文本均可）。"""
    from runtime.reply_parser import coerce_reply as _coerce

    if isinstance(final_output, AgentReply):
        return final_output
    try:
        return _coerce(final_output)
    except Exception:
        return str(final_output) if final_output is not None else "（本轮没有返回内容）"


def _short(value: object, limit: int = 220) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


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


_API_NETWORK_PATTERNS = (
    r"api[_ -]?key",
    r"auth(entication|orization)?",
    r"\b(401|403|404|429|500|502|503)\b",
    r"rate.?limit",
    r"model[_ -]?not[_ -]?found|no model",
    r"connection|connect error",
    r"timeout|timed? ?out",
    r"network|unreachable|refused|dns|proxy|ssl|socket",
    r"bad gateway|service unavailable|http",
)


def _looks_like_api_or_network(message: str) -> bool:
    """判断错误文本是否像 API Key / 网关 / 网络问题，避免误导性提示。"""
    low = message.lower()
    return any(re.search(pattern, low) for pattern in _API_NETWORK_PATTERNS)


def print_run_error(exc: Exception) -> None:
    """把一轮运行异常转成对用户有帮助的提示，区分轮次上限、API/网络与其他问题。"""
    try:
        from runtime.provider_gateway import provider_public_message as _ppm
        from runtime.provider_errors import mask_request_id

        public = _ppm(exc)
        if public:
            rid = getattr(exc, "provider_request_id", None)
            kind = getattr(exc, "provider_kind", None)
            tail = f"（{kind}）" if kind else ""
            rid_note = f" request={mask_request_id(rid)}" if rid else ""
            print(f"\n[模型服务] {public}{tail}{rid_note}")
            return
    except Exception:
        pass
    if isinstance(exc, MaxTurnsExceeded):
        print("\n[出错了] 这一轮超出了单轮循环上限（LLM 和工具来回太多），不是 API Key 或网络问题。")
        print(f"详情：{exc}")
        print("解决办法：")
        print("  1) 用更大的上限重跑，例如 python main.py --max-turns 30；")
        print("  2) 若仍反复触发，通常是模型在重复调用同一个工具，请把任务拆小，")
        print("     或明确告诉它『拿到结果就作答，不要重复搜索』。")
        return
    if isinstance(exc, ModelBehaviorError) and "not found in agent" in str(exc):
        tool_match = re.search(r"Tool (.+?) not found in agent", str(exc))
        tool_name = tool_match.group(1) if tool_match else "未知工具"
        print(f"\n[出错了] 模型调用了一个不存在的工具“{tool_name}”（幻觉调用）。")
        print("现在已改为把这类错误回传给模型自行纠正，正常不会再中断；")
        print("若仍反复出现，请重新发送或换个说法，可加 --debug 查看本轮内部记录。")
        return
    message = str(exc) or type(exc).__name__
    print(f"\n[出错了] {message}")
    if _looks_like_api_or_network(message):
        print("如果与 API Key / 网络有关，请检查 .env 中的 OPENAI_API_KEY 和 OPENAI_BASE_URL。")
    else:
        print("可加 --debug 查看本轮内部记录，定位是哪一步出了问题。")


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


async def _auto_compact(session: SQLiteSession, auto_summary: bool) -> None:
    """每轮结束后执行一次长会话自动摘要（默认开启，--no-auto-summary 关闭）。"""
    if not auto_summary:
        return
    summary = await maybe_compact(session)
    if summary:
        print("\n🧹 长会话自动摘要：较早的对话已压缩保留（summaries/ 目录可查），最近几轮原文完整保留。")


def display_output(final_output: object) -> None:
    """显示最终输出：优先渲染成结构化文本，兜底原文。"""
    reply = coerce_reply(final_output)
    if isinstance(reply, AgentReply):
        print(render_reply(reply))
    else:
        print(reply)
        print("（提示：本轮输出不是标准 AgentReply JSON，已原文显示）")


def current_assistant_agent() -> object:
    """返回当前生效的主 Agent（main() 里 --no-guardrails 会替换全局的那一份）。"""
    return assistant_agent


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
    active_agent = agent or assistant_agent
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
                    print(f"\n[调用工具: {tool_name}]", flush=True)
                    # Execution wrapper owns public tool state; SDK observations are internal.
                elif event.name == "tool_call_created":
                    raw = getattr(event.item, "raw_item", None)
                    tool_name = getattr(raw, "name", "?")
                    # A planned call is not evidence that execution started.
                elif debug:
                    print(f"[流事件] {event.name}", flush=True)
            elif event.type == "raw_response_event":
                raw_data = getattr(event.data, "data", None) or event.data
                raw_type = getattr(raw_data, "type", "") or getattr(event.data, "type", "")
                # Fail closed: reasoning, tool-argument deltas and intermediate assistant
                # text stay inside the SDK. Only the tools-disabled final call has a sink.
                if raw_type == "response.output_text.delta" and getattr(active_agent, "_public_final", False):
                    delta = getattr(raw_data, "delta", None)
                    if isinstance(delta, str):
                        _emit("final.content.delta", {"text": delta})
                if debug:
                    if raw_type != last_raw_type:
                        print(f"[流事件] LLM 原始事件: {raw_type}", flush=True)
                        last_raw_type = raw_type
            elif event.type == "agent_updated_stream_event" and debug:
                print(f"[流事件] 切换 Agent: {event.new_agent.name}", flush=True)
        return result
    return await Runner.run(
        active_agent,
        message,
        session=session,
        run_config=run_config,
        max_turns=max_turns,
    )


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

    输出格式闸拦截时自动携带原因重试一次（长文档/长回复偶发被截断时能自愈）。
    agent 可传模型路由克隆的实例；不传则用当前全局 Agent。
    audit 可选：AuditCollector，成功时自动落每次模型调用/工具调用明细。
    stream_events_cb 可选：实时过程回调（工具/增量文本），供 web SSE 转发。
    provider 可选：本 Run 显式选择的模型 Provider（本地模型/远程网关）；None=全局默认。
    """
    await ensure_mcp()
    run_config = _run_config(history_limit, session, provider=provider)

    current = message
    overflow_compacted = False
    attempt = 0
    while attempt < 3:
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
            if attempt == 1:
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
    raise OutputGuardrailTripwireTriggered("unreachable")  # pragma: no cover


def _print_banner(mode: str, session_name: str, history_limit: int | None, max_turns: int) -> None:
    mode_hint = {"stream": "流式（实时事件）", "async": "异步（一次返回）", "sync": "同步（阻塞）"}
    history_hint = f"（每轮最多回看 {history_limit} 条）" if history_limit else "（完整历史）"
    print("=" * 52)
    print("  全能助手（通用个人单 Agent）")
    print(f"  会话: {session_name} ｜ 执行方式: {mode} ({mode_hint[mode]})")
    print(f"  单轮循环上限: {max_turns} 次（--max-turns 可调）")
    print(f"  对话记忆: {history_hint}   输入 exit / quit / 退出 结束")
    if trace_log_path():
        print(f"  🛰️ 追踪: {trace_log_path()}（每一步都会记录）")
    print("=" * 52)


def _print_artifacts(artifacts: list[dict]) -> None:
    for art in artifacts or []:
        print(f"🗂 产物已登记：{art.get('name', '?')}（{art.get('id', '?')}，{art.get('kind', '?')}）")


async def chat_async(
    session_name: str,
    mode: str,
    debug: bool,
    max_turns: int,
    history_limit: int | None,
    auto_summary: bool = True,
) -> None:
    """聊天循环核心（stream / async / sync 三种模式共用，每轮登记为一个 Task）。"""
    check_api_key()
    runtime = AgentRuntime.get_default()
    session = SQLiteSession(session_name, db_path=str(SESSIONS_DB))
    _print_banner(mode, session_name, history_limit, max_turns)
    try:
        while True:
            try:
                user_input = input("\n你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见，随时回来找我。")
                return
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", "退出", "q"}:
                print("再见，随时回来找我。")
                return
            print("\n助手 >")
            try:
                result = await runtime.run_turn(
                    user_input,
                    session=session,
                    session_id=session_name,
                    mode=mode,
                    debug=debug,
                    max_turns=max_turns,
                    history_limit=history_limit,
                    metadata={"channel": "chat"},
                    raise_on_error=True,
                    context_guard=auto_summary,
                )
            except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered,
                    FinalResponseFailed) as exc:
                reason = exc.reason if isinstance(exc, FinalResponseFailed) else _guardrail_reason(exc)
                print(f"\n🛡️ 安全校验未通过：{reason}")
                continue
            except Exception as exc:
                print_run_error(exc)
                continue
            if result.waiting_approval:
                result = await _resolve_approvals_interactive(
                    runtime, result, session=session, mode=mode,
                    debug=debug, max_turns=max_turns, history_limit=history_limit,
                )
                if result.waiting_approval:
                    print("任务仍停留在「等待审批」，可以重新发起同样请求继续处理，或用 python -m runtime --task "
                          + result.task.id + " 查看。")
                    continue
            final_output = result.final_output
            display_output(final_output)
            _print_artifacts(getattr(result, "artifacts", []) or [])
            await _auto_compact(session, auto_summary)
    finally:
        session.close()


async def _resolve_approvals_interactive(
    runtime: AgentRuntime,
    result: object,
    *,
    session: SQLiteSession | None = None,
    mode: str = "async",
    debug: bool = False,
    max_turns: int = 20,
    history_limit: int | None = None,
) -> object:
    """逐条向用户确认审批；批准过任何一条就自动续跑同一任务，直到不再等待。"""
    from runtime.runner import RunResult

    current = result
    channel = "chat"
    for _round in range(5):
        if not isinstance(current, RunResult) or not current.waiting_approval:
            return current
        task = current.task
        if task.metadata:
            channel = str(task.metadata.get("channel") or "chat")
        approvals = current.approvals or runtime.tasks.list_pending_approvals(task.id)
        if not approvals:
            return current
        print(f"\n⚠️ 任务 {task.id} 请求 {len(approvals)} 项高风险操作审批：")
        decided_any = False
        for ap in approvals:
            tool = ap.get("tool_name") or ap.get("tool", "?")
            args_text = str(ap.get("arguments"))[:200]
            print(f"  • {tool}（参数：{args_text}）")
            try:
                answer = input("    批准执行？[y=批准 / n=拒绝 / s=跳过] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "s"
            if answer.startswith("s"):
                print("    （跳过，该项保持待审批）")
                continue
            try:
                runtime.tasks.decide_approval(
                    ap["id"], "approved" if answer.startswith("y") or answer == "" else "denied", actor="user"
                )
            except Exception as exc:
                print(f"    （决定失败：{exc}）")
                continue
            decided_any = True
        if not decided_any:
            return current
        try:
            current = await runtime.run_turn(
                task.goal,
                session=session,
                session_id=task.session_id,
                mode=mode,
                debug=debug,
                max_turns=max_turns,
                history_limit=history_limit,
                task_id=task.id,
                metadata={"channel": channel},
                raise_on_error=False,
            )
        except Exception as exc:
            print(f"（续跑出错：{exc}）")
            return current
        if not current.ok and current.error:
            print(f"（续跑失败：{current.error[:200]}）")
    return current


def chat_sync(
    session_name: str,
    debug: bool,
    max_turns: int,
    history_limit: int | None,
    auto_summary: bool = True,
) -> None:
    """同步聊天循环：与异步共用同一核心，只是执行方式为 sync（也登记 Task）。"""
    check_api_key()
    try:
        asyncio.run(ensure_mcp())
    except Exception:
        pass
    asyncio.run(
        chat_async(
            session_name,
            "sync",
            debug,
            max_turns,
            history_limit,
            auto_summary=auto_summary,
        )
    )


def _speech_text_of_output(final_output: object) -> str:
    """把 Agent 回复转成适合朗读的文本。"""
    reply = coerce_reply(final_output)
    if isinstance(reply, AgentReply):
        if reply.content.strip():
            return reply.content
        if reply.questions:
            return "我想确认几个问题：" + "；".join(reply.questions)
        return reply.summary or "已完成。"
    return str(final_output)[:400]


def chat_voice(
    session_name: str,
    fallback_mode: str,
    debug: bool,
    max_turns: int,
    history_limit: int | None,
    auto_summary: bool,
    speak_replies: bool,
) -> None:
    """语音对话：麦克风输入，回复显示并朗读（Windows 本机语音能力）。"""
    import voice

    check_api_key()
    if not voice.check_microphone():
        print("\n⚠️ 未检测到可用的麦克风/音频输入设备，语音输入不可用，已自动切换为键盘输入。")
        print("   想用语音对话：接好麦克风后重新运行 python main.py --voice。")
        asyncio.run(
            chat_async(
                session_name,
                fallback_mode,
                debug,
                max_turns,
                history_limit,
                auto_summary=auto_summary,
            )
        )
        return
    session = SQLiteSession(session_name, db_path=str(SESSIONS_DB))
    print("=" * 52)
    print("  全能助手（语音对话模式）")
    print(f"  会话: {session_name} ｜ 单轮循环上限: {max_turns} 次")
    print("  说“退出 / 再见 / 结束”可结束语音会话")
    print(voice.describe_availability())
    print("=" * 52)
    try:
        while True:
            print("\n🎤 请说话（10 秒内没听到会自动重听）...")
            text = voice.listen_once(timeout_seconds=10)
            text = (text or "").strip()
            if not text:
                print("（没有听清，请再试一次）")
                continue
            if len(text) <= 20 and re.search(r"退出|结束|再见|不聊了", text):
                print("再见，随时回来找我。")
                return
            print(f"\n你（语音）> {text}")
            print("\n助手 >")
            try:
                voice_result = asyncio.run(
                    AgentRuntime.get_default().run_turn(
                        text,
                        session=session,
                        session_id=session_name,
                        mode="async",
                        max_turns=max_turns,
                        history_limit=history_limit,
                        metadata={"channel": "voice"},
                        raise_on_error=True,
                        context_guard=auto_summary,
                    )
                )
                if voice_result.waiting_approval:
                    voice_result = asyncio.run(
                        _resolve_approvals_interactive(
                            AgentRuntime.get_default(),
                            voice_result,
                            session=session,
                            mode="async",
                            max_turns=max_turns,
                            history_limit=history_limit,
                        )
                    )
                    if voice_result.waiting_approval:
                        print("任务仍停留在「等待审批」，请稍后再次发起或查看：python -m runtime --task "
                              + voice_result.task.id)
                        continue
                final_output = voice_result.final_output
            except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered,
                    FinalResponseFailed) as exc:
                reason = exc.reason if isinstance(exc, FinalResponseFailed) else _guardrail_reason(exc)
                print(f"\n🛡️ 安全校验未通过：{reason}")
                continue
            except Exception as exc:
                print_run_error(exc)
                continue
            display_output(final_output)
            _print_artifacts(getattr(voice_result, "artifacts", []) or [])
            if speak_replies and not voice.speak(_speech_text_of_output(final_output)):
                print("（朗读失败：请检查是否有可用的音频输出设备）")
            try:
                asyncio.run(_auto_compact(session, auto_summary))
            except Exception:
                pass
    finally:
        session.close()


def _print_scheduled_tasks() -> None:
    tasks = scheduler.load_tasks()
    if not tasks:
        print("还没有定时任务。可在聊天里说“每天早上 9 点提醒我喝水”来创建。")
        return
    print("定时任务：")
    for task in tasks:
        print()
        print(scheduler.format_task_line(task))


async def _run_task_prompt(task: dict, max_turns: int) -> tuple[str, str]:
    """执行一条定时任务的提示词（每次登记为一个 Task）；返回 (状态, 摘要)。"""
    try:
        await ensure_mcp()
        result = await AgentRuntime.get_default().run_turn(
            task["prompt"],
            session_id=f"sched-{task['id']}",
            mode="async",
            max_turns=max_turns,
            metadata={"channel": "scheduled", "schedule_id": task["id"]},
        )
        if not result.ok:
            return "error", (result.error or "")[:300]
        reply = coerce_reply(result.final_output)
        if isinstance(reply, AgentReply):
            summary = reply.summary or reply.content[:200]
        else:
            summary = str(result.final_output)[:300]
        return "ok", summary
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {str(exc)[:200]}"


def _log_task_result(task: dict, status: str, summary: str) -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    line = (
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {task['id']}｜{task['name']}｜"
        f"{status}｜{summary[:200]}"
    )
    with (log_dir / "tasks.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


async def _execute_task_and_mark(
    task: dict, max_turns: int, ledger: TaskManager | None = None, fire_at: str | None = None
) -> tuple[str, str]:
    print(f"\n▶ [{datetime.now():%H:%M:%S}] 执行定时任务「{task['name']}」")
    status, summary = await _run_task_prompt(task, max_turns)
    scheduler.mark_run_result(task["id"], status, summary)
    _log_task_result(task, status, summary)
    mark = "✅" if status == "ok" else "⚠️"
    print(f"  {mark} {status.upper()}：{summary[:160]}")
    if ledger is not None and fire_at is not None:
        try:
            ledger.finalize_schedule_run(
                task["id"], fire_at, "ok" if status == "ok" else "error", detail=summary[:200]
            )
        except Exception:
            pass
    return status, summary


async def daemon_loop(max_turns: int) -> None:
    """常驻进程：每分钟检查一次定时任务，到点自动执行（带幂等台账与 tasks.json 镜像）。"""
    print("常驻任务进程已启动（每分钟检查一次，Ctrl+C 退出）。")
    ledger = TaskManager()
    try:
        while True:
            now = datetime.now()
            try:
                ledger.mirror_tasks_json(scheduler.load_tasks())
            except Exception:
                pass
            for task in scheduler.due_tasks(now):
                fire_at = str(task.get("next_run") or "") or now.isoformat(timespec="seconds")
                try:
                    started = ledger.begin_schedule_run(task["id"], fire_at)
                except Exception:
                    started = True  # 台账异常不阻断执行（降级为旧行为）
                if not started:
                    print(f"  ⏭ {task['name']}：本次触发（{fire_at}）已在台账中，跳过防重复。")
                    continue
                scheduler.prepare_next_run(task["id"], now)
                await _execute_task_and_mark(task, max_turns, ledger=ledger, fire_at=fire_at)
            # 睡到下一个整分钟，保证准点
            wait_seconds = 60 - datetime.now().second + 0.2
            await asyncio.sleep(min(wait_seconds, 60.0))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n常驻任务进程已退出。")


async def run_scheduled_task_once(task_id: str, max_turns: int) -> int:
    """立即手动执行某条任务（不影响它的下次计划时间）。"""
    task = next((t for t in scheduler.load_tasks() if t["id"] == task_id), None)
    if task is None:
        print(f"找不到任务 id={task_id}，可用 --tasks 查看现有任务。")
        return 1
    await _execute_task_and_mark(task, max_turns)
    return 0


def main() -> None:
    ensure_utf8_console()
    parser = argparse.ArgumentParser(description="全能助手 - 通用个人单 Agent")
    parser.add_argument("--session", default="personal", help="会话名称，用于区分不同主题的对话记忆")
    parser.add_argument(
        "--mode",
        choices=["stream", "async", "sync"],
        default="stream",
        help="执行方式：stream=流式实时（默认）；async=异步一次返回；sync=同步阻塞",
    )
    parser.add_argument("--debug", action="store_true", help="打印每轮 agent 内部循环记录（理解运行原理）")
    parser.add_argument("--max-turns", type=int, default=20, help="单轮最多执行 LLM+工具的循环次数")
    parser.add_argument("--history", type=int, default=None, help="每轮最多回看多少条历史消息（不填=全部）")
    parser.add_argument(
        "--no-input-guardrail",
        action="store_true",
        help="临时停用输入安全校验（越狱/套取系统提示词/索取密钥的拦截），仅本次运行生效",
    )
    parser.add_argument(
        "--no-output-guardrail",
        action="store_true",
        help="临时停用输出安全校验（JSON 结构/假 saved_file/密钥泄露检查），仅本次运行生效",
    )
    parser.add_argument(
        "--no-guardrails",
        action="store_true",
        help="输入+输出安全校验一起停用（测试排查用），仅本次运行生效",
    )
    parser.add_argument(
        "--no-auto-summary",
        action="store_true",
        help="关闭长会话自动摘要（默认开启：会话变长时自动压缩早期对话）",
    )
    parser.add_argument("--list-sessions", action="store_true", help="列出所有会话及消息数后退出")
    parser.add_argument("--clear-session", metavar="NAME", help="清空指定会话的对话记忆后退出")
    parser.add_argument("--tasks", action="store_true", help="列出定时任务后退出")
    parser.add_argument("--run-task", metavar="ID", help="立即手动执行某条定时任务后退出")
    parser.add_argument("--daemon", action="store_true", help="常驻运行：按 tasks.json 定时自动执行任务")
    parser.add_argument("--voice", action="store_true", help="语音对话：麦克风输入 + 朗读回复（Windows 本机能力）")
    parser.add_argument(
        "--no-speak",
        action="store_true",
        help="语音输入时只显示文字、不朗读回复（与 --voice 一起用）",
    )
    parser.add_argument("--trace", action="store_true", help="开启本地追踪，把每一步 span 写入 traces/traces.jsonl")
    args = parser.parse_args()

    if args.list_sessions:
        print(list_sessions())
        return
    if args.clear_session:
        print(clear_session(args.clear_session))
        return
    if args.tasks:
        _print_scheduled_tasks()
        return

    global assistant_agent
    input_on = not (args.no_guardrails or args.no_input_guardrail)
    output_on = not (args.no_guardrails or args.no_output_guardrail)
    assistant_agent = build_assistant_agent(enable_input_guardrail=input_on, enable_output_guardrail=output_on)
    if not (input_on and output_on):
        status = " ｜ ".join(
            [
                f"输入安全闸: {'开启' if input_on else '已停用'}",
                f"输出安全闸: {'开启' if output_on else '已停用'}",
            ]
        )
        print(f"⚠️ 安全校验已临时调整（仅本次运行）：{status}；排查完建议恢复默认开启。")

    # 启动自动恢复：把上次进程崩溃遗留的超时 RUNNING 任务标为 failed（幂等安全）
    try:
        from runtime.task_manager import auto_recover

        recovered = auto_recover()
        if recovered:
            print(f"[RUNTIME] 自动恢复 {len(recovered)} 个崩溃遗留任务（RUNNING→failed）：{', '.join(recovered[:5])}")
    except Exception:
        pass

    trace_path = install_local_tracing(args.trace)
    if trace_path:
        print(f"🛰️ 本地追踪已开启：{trace_path}")

    if args.voice:
        check_api_key()
        chat_voice(
            args.session,
            args.mode,
            args.debug,
            args.max_turns,
            args.history,
            auto_summary=not args.no_auto_summary,
            speak_replies=not args.no_speak,
        )
        return
    if args.run_task:
        check_api_key()
        raise SystemExit(asyncio.run(run_scheduled_task_once(args.run_task, args.max_turns)))
    if args.daemon:
        check_api_key()
        asyncio.run(daemon_loop(args.max_turns))
        return

    if args.mode == "sync":
        chat_sync(
            args.session,
            args.debug,
            args.max_turns,
            args.history,
            auto_summary=not args.no_auto_summary,
        )
    else:
        asyncio.run(
            chat_async(
                args.session,
                args.mode,
                args.debug,
                args.max_turns,
                args.history,
                auto_summary=not args.no_auto_summary,
            )
        )


if __name__ == "__main__":
    main()
