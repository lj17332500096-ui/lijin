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
)
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.memory import SQLiteSession
from dotenv import load_dotenv

from agent import (build_assistant_agent, assistant_agent, gateway_assistant_agent,
                   set_assistant_agent_override)
from runtime.compact import maybe_compact
from integrations.mcp_bridge import ensure_connected as ensure_mcp
from runtime.observability import install_local_tracing
from runtime.runner import AgentRuntime
from runtime.task_manager import TaskManager
from runtime.errors import FinalResponseFailed
from runtime_paths import LOG_DIR
from runtime.session_storage import SESSIONS_DB
import scheduler
from schemas import AgentReply


# SDK/Provider execution lives in runtime.execution. These imports keep CLI/API
# callers source-compatible without making Runtime depend on this module.
from runtime.execution import (
    _attr, _format_retry_max, _guardrail_reason, _process_print, _run_attempt,
    _run_config, execute_turn, print_run_items, process_prints_enabled,
    set_process_prints,
)

BASE_DIR = Path(__file__).resolve().parent
# P0-1：进程入口的**唯一显式** .env 加载点（另一个是 agent.py）。必须早于 Runtime 构造。
# 任何 runtime/* 模块都不得在 import 期 load_dotenv，理由见 agent.py 同处注释。
load_dotenv(BASE_DIR / ".env")

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
        with conn:
            # SQLite foreign-key enforcement is connection-local and is not
            # enabled by every SDK build. Delete both sides explicitly.
            messages = conn.execute(
                "DELETE FROM agent_messages WHERE session_id = ?", (session_name,)
            ).rowcount
            sessions = conn.execute(
                "DELETE FROM agent_sessions WHERE session_id = ?", (session_name,)
            ).rowcount
        deleted = sessions or messages
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






async def _auto_compact(session: SQLiteSession, auto_summary: bool) -> None:
    """每轮结束后执行一次长会话自动摘要（默认开启，--no-auto-summary 关闭）。

    修复 B：SQLiteSession 可能已被 SDK 内部 invalidate（_closed=True），
    此时 close() 后调用 maybe_compact 会抛 RuntimeError("SQLiteSession is closed")。
    对 closed 状态静默降级（记 debug 日志后跳过），不阻塞主流程。
    """
    if not auto_summary:
        return
    # SQLiteSession 无公开 is_closed 属性；用 _check_not_closed 探测（它会抛 RuntimeError）。
    try:
        session._check_not_closed()
    except RuntimeError:
        import logging
        logging.getLogger(__name__).debug("session already closed, skip auto_compact")
        return
    try:
        summary = await maybe_compact(session)
        if summary:
            print("\n🧹 长会话自动摘要：较早的对话已压缩保留（summaries/ 目录可查），最近几轮原文完整保留。")
    except RuntimeError as e:
        if "closed" in str(e).lower():
            import logging
            logging.getLogger(__name__).debug("session closed during compact, skipping")
        else:
            raise


def display_output(final_output: object) -> None:
    """显示最终输出：优先渲染成结构化文本，兜底原文。"""
    reply = coerce_reply(final_output)
    if isinstance(reply, AgentReply):
        print(render_reply(reply))
    else:
        print(reply)
        print("（提示：本轮输出不是标准 AgentReply JSON，已原文显示）")


def current_assistant_agent() -> object:
    """返回当前生效的主 Agent（main() 里 --no-guardrails 会替换全局的那一份）。

    动态调用 gateway_assistant_agent() 使 TUI 切模型后无需重启。
    如果 --no-guardrails 已替换过全局实例（guardrails 被关闭），仍用全局版本。
    """
    from agent import current_assistant_agent as _current
    return _current()














def _print_artifacts(artifacts: list[dict]) -> None:
    for art in artifacts or []:
        print(f"🗂 产物已登记：{art.get('name', '?')}（{art.get('id', '?')}，{art.get('kind', '?')}）")


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
        from cli.app import run_cli

        raise SystemExit(run_cli(
            session_name=session_name,
            mode=fallback_mode,
            debug=debug,
            max_turns=max_turns,
            history_limit=history_limit,
            auto_summary=auto_summary,
        ))
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
                async def _voice_turn():
                    from integrations.mcp_bridge import close_servers

                    try:
                        result = await AgentRuntime.get_default().run_turn(
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
                        if result.waiting_approval:
                            result = await _resolve_approvals_interactive(
                                AgentRuntime.get_default(), result, session=session,
                                mode="async", max_turns=max_turns,
                                history_limit=history_limit,
                            )
                        return result
                    finally:
                        await close_servers()

                voice_result = asyncio.run(_voice_turn())
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
        outcome = str(getattr(result, "outcome", "completed") or "completed")
        if outcome != "completed":
            detail = result.error or outcome
            return "error", str(detail)[:300]
        reply = coerce_reply(result.final_output)
        if isinstance(reply, AgentReply):
            summary = reply.summary or reply.content[:200]
        else:
            summary = str(result.final_output)[:300]
        return "ok", summary
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {str(exc)[:200]}"


def _log_task_result(task: dict, status: str, summary: str) -> None:
    log_dir = LOG_DIR
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
    """常驻进程：每分钟检查一次定时任务，到点自动执行（带幂等台账与 config/tasks.json 镜像）。"""
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


async def _run_with_mcp_cleanup(awaitable):
    """保持 MCP stdio resources 与 daemon/单次任务处于同一 asyncio 生命周期。"""
    try:
        return await awaitable
    finally:
        from integrations.mcp_bridge import close_servers

        await close_servers()


def main() -> None:
    ensure_utf8_console()
    # 审计 D3：结构化日志（opt-in，FORGE_STRUCTURED_LOG=1）。默认不挂 handler，
    # 调用点 slog.* 静默；开启后 stdout 出 JSON 行（run_id / request_id 自动注入）。
    try:
        from runtime.structured_log import install_structured_logging
        install_structured_logging()
    except Exception:
        pass
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
    parser.add_argument("--daemon", action="store_true", help="常驻运行：按 config/tasks.json 定时自动执行任务")
    parser.add_argument("--voice", action="store_true", help="语音对话：麦克风输入 + 朗读回复（Windows 本机能力）")
    parser.add_argument(
        "--no-speak",
        action="store_true",
        help="语音输入时只显示文字、不朗读回复（与 --voice 一起用）",
    )
    parser.add_argument("--trace", action="store_true", help="开启本地追踪，把每一步 span 写入 var/traces/traces.jsonl")
    parser.add_argument("--tui", action="store_true", help="使用 Textual TUI 界面（需安装 textual，默认仍走 CLI 消息平台）")
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
    set_assistant_agent_override(assistant_agent if not (input_on and output_on) else None)
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

    # B8：--trace 与 FORGE_TRACE 收敛为同一开关 —— Runtime（_ensure_tracing）也读它，
    # 否则「CLI 开了追踪」与「Runtime 认为自己没开」会各说一套；Web/定时入口也能用
    # 同一个环境变量打开追踪，而不必各自加一个 CLI 参数。
    if args.trace:
        os.environ["FORGE_TRACE"] = "1"
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
        raise SystemExit(asyncio.run(_run_with_mcp_cleanup(
            run_scheduled_task_once(args.run_task, args.max_turns)
        )))
    if args.daemon:
        check_api_key()
        asyncio.run(_run_with_mcp_cleanup(daemon_loop(args.max_turns)))
        return

    # 默认入口：CLI 消息平台（进程内直连 Runtime，带命令体系/过程展示/诊断码）
    if args.tui:
        check_api_key()
        from cli.tui.app import run_tui

        raise SystemExit(
            run_tui(
                session_name=args.session,
                mode=args.mode,
                debug=args.debug,
                max_turns=args.max_turns,
                history_limit=args.history,
                auto_summary=not args.no_auto_summary,
            )
        )
    check_api_key()
    from cli.app import run_cli

    raise SystemExit(
        run_cli(
            session_name=args.session,
            mode=args.mode,
            debug=args.debug,
            max_turns=args.max_turns,
            history_limit=args.history,
            auto_summary=not args.no_auto_summary,
        )
    )


if __name__ == "__main__":
    main()
