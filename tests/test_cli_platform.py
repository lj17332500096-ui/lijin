"""CLI 消息平台回归测试（离线：假 Runtime + 内存 TaskManager，零 LLM / 零网络）。

覆盖点
------
· 命令注册表：注册完整性、别名、参数解析、帮助文案
· 渲染层：活动快照去重、失败活动、流式分片累积、控制事件、未知通道
· 主循环：一轮对话的完整输出（流式正文不重复打印、页脚统计）
· 错误路径：诊断码（E-FINAL-EMPTY / E-BUDGET / E-BUSY / E-TIMEOUT / E-CONFIG / E-UNKNOWN）
· 命令行为：/sessions /new /switch /rename /history /clear /diag /tools
· 样式层：无颜色降级、ANSI 剥离
"""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from cli import theme  # noqa: E402
from cli.app import ChatApp, _same_text  # noqa: E402
from cli.commands import build_registry  # noqa: E402
from cli.diagnostics import diagnose  # noqa: E402
from cli.render import TurnRenderer  # noqa: E402
from cli.store import SessionStore  # noqa: E402


# ── 假 Runtime / 假 TaskManager ─────────────────────────────────
class _FakeTask:
    id = "run_fake_0001"


class _FakeResult:
    def __init__(self, final_output=None, **overrides) -> None:
        self.task = _FakeTask()
        self.final_output = final_output
        self.ok = True
        self.error = None
        self.elapsed_seconds = 0.01
        self.waiting_approval = False
        self.approvals: list = []
        self.artifacts: list = []
        for key, value in overrides.items():
            setattr(self, key, value)


class _FakeRuntime:
    """把 run_turn 的行为脚本化：先推过程事件，再返回结果或抛异常。"""

    def __init__(self, result=None, exc: BaseException | None = None, script=None) -> None:
        self.result = result if result is not None else _FakeResult()
        self.exc = exc
        self.script = script
        self.calls: list[dict] = []

    def _ensure(self) -> None:  # 与 AgentRuntime 同名，store 装配时会调
        return None

    async def run_turn(self, message, **kwargs):
        self.calls.append({"message": message, **kwargs})
        callback = kwargs.get("stream_events_cb")
        if callback and self.script is not None:
            self.script(callback, message)
        if self.exc is not None:
            raise self.exc
        return self.result


def _default_script(callback, message) -> None:
    callback("activity", {
        "type": "tool.started",
        "label": "正在检查相关文件",
        "metadata": {"activities": [
            {"activity_id": "a1", "label": "正在检查相关文件", "status": "running"},
        ], "changed_count": 0},
    })
    callback("activity", {
        "type": "tool.finished",
        "label": "正在检查相关文件",
        "metadata": {"activities": [
            {"activity_id": "a1", "label": "正在检查相关文件", "status": "completed"},
        ], "changed_count": 0},
    })
    callback("assistant_delta", {"type": "assistant.delta", "metadata": {"delta": "你好"}})
    callback("assistant_delta", {"type": "assistant.delta", "metadata": {"delta": "，世界"}})
    callback("control", {"type": "assistant.reply", "metadata": {"content": "你好，世界", "kind": "answer"}})
    callback("control", {"type": "run.completed", "metadata": {}})


class _FakeManager:
    """内存版 TaskManager：只实现 SessionStore 用到的方法。"""

    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self._seq = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"tk_fake{self._seq:04d}"

    def get_or_create_container(self, session_id: str, project_id=None, title=None) -> dict:
        for row in self.containers.values():
            if row["session_id"] == session_id:
                return row
        container_id = self._new_id()
        row = {"id": container_id, "session_id": session_id, "title": title or "",
               "summary": "", "updated_at": "2026-09-19T00:00:00", "pinned": 0}
        self.containers[container_id] = row
        self.messages[container_id] = []
        return row

    def get_container(self, container_id: str):
        return self.containers.get(container_id)

    def list_containers(self, *, session_id=None, project_id=None, archived=None, limit=100):
        return list(self.containers.values())[:limit]

    def container_stat(self, container_id: str) -> dict:
        rows = self.messages.get(container_id, [])
        stat = {"runs": 1 if rows else 0, "messages": len(rows)}
        if rows:
            stat["latest_run"] = {"id": "run_fake_0001", "state": "completed",
                                  "created_at": "2026-09-19T00:00:00", "goal": "测试目标"}
        return stat

    def list_messages(self, container_id: str, limit: int = 500):
        return list(self.messages.get(container_id, []))[:limit]

    def set_container_title(self, container_id: str, title: str) -> bool:
        if container_id not in self.containers:
            return False
        self.containers[container_id]["title"] = title
        return True

    def clear_container_history(self, container_id: str) -> dict:
        removed = len(self.messages.get(container_id, []))
        self.messages[container_id] = []
        return {"messages": removed, "events": 2, "attachments": 0}

    def list_artifacts(self, *, task_id=None, session_id=None, limit=50):
        return [{"id": "art_1", "name": "报告.md", "kind": "note"}] if limit else []

    def list_events(self, task_id: str, limit: int = 200):
        class _Event:
            event_type = "run.started"
            payload = {}
            created_at = "2026-09-19T00:00:00"

        return [_Event()]

    def list_provider_attempts(self, task_id: str, limit: int = 200):
        return [{"kind": "remote", "model": "agnes-2.5-flash", "latency_ms": 1200, "error": None}]

    def list_tool_calls(self, task_id: str, limit: int = 200):
        return [{"tool_name": "read_workspace_file", "status": "ok",
                 "created_at": "2026-09-19T00:00:00"}]


def _make_app(**overrides) -> tuple[ChatApp, io.StringIO, _FakeManager]:
    manager = _FakeManager()
    manager.get_or_create_container("personal", title="默认会话")
    stream = io.StringIO()
    params = {
        "session_name": "personal",
        "auto_summary": False,
        "runtime": _FakeRuntime(script=_default_script),
        "store": SessionStore(manager),
        "stream": stream,
        "sessions_db": str(Path(tempfile.mkdtemp()) / "sessions.sqlite"),
        "input_fn": lambda prompt="": None,
    }
    params.update(overrides)
    app = ChatApp(**params)
    return app, stream, manager


# ── 命令注册表 ──────────────────────────────────────────────────
class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_registry()

    def test_every_command_has_handler(self) -> None:
        for command in self.registry.all():
            self.assertIsNotNone(command.handler, command.name)
            self.assertTrue(command.usage.startswith("/"), command.usage)
            self.assertTrue(command.summary, command.name)

    def test_aliases_resolve_to_same_command(self) -> None:
        self.assertIs(self.registry.get("sw"), self.registry.get("switch"))
        self.assertIs(self.registry.get("q"), self.registry.get("exit"))
        self.assertIs(self.registry.get("?"), self.registry.get("help"))
        self.assertIs(self.registry.get("hist"), self.registry.get("history"))

    def test_parse_splits_name_and_argument(self) -> None:
        command, arg = self.registry.parse("/switch 3")
        self.assertEqual(command.name, "switch")
        self.assertEqual(arg, "3")
        command, arg = self.registry.parse("/HELP")
        self.assertEqual(command.name, "help")
        self.assertEqual(arg, "")
        self.assertIsNone(self.registry.parse("/definitely-not-a-command")[0])

    def test_help_text_lists_every_command(self) -> None:
        text = theme.strip(self.registry.help_text())
        for command in self.registry.all():
            self.assertIn(command.usage, text)
        detail = theme.strip(self.registry.help_text(self.registry.get("clear")))
        self.assertIn("/clear yes", detail)


# ── 渲染层 ─────────────────────────────────────────────────────
class RendererTests(unittest.TestCase):
    def setUp(self) -> None:
        theme.configure(force=False)
        self.stream = io.StringIO()
        self.renderer = TurnRenderer(self.stream)

    def test_activity_snapshot_is_deduplicated(self) -> None:
        payload = {"type": "tool.started", "label": "正在检查相关文件",
                   "metadata": {"activities": [{"activity_id": "a1", "label": "正在检查相关文件",
                                                "status": "running"}]}}
        self.renderer.on_event("activity", payload)
        self.renderer.on_event("activity", payload)
        self.assertEqual(self.stream.getvalue().count("正在检查相关文件"), 1)
        self.assertEqual(self.renderer.activity_lines, ["正在检查相关文件"])

    def test_failed_activity_is_recorded_and_marked(self) -> None:
        self.renderer.on_event("activity", {"type": "tool.started", "label": "正在运行测试",
                                            "metadata": {"activities": [{"activity_id": "v1", "label": "正在运行测试",
                                                                         "status": "running"}]}})
        self.renderer.on_event("activity", {"type": "tool.finished", "label": "正在运行测试",
                                            "metadata": {"activities": [{"activity_id": "v1", "label": "正在运行测试",
                                                                         "status": "failed"}]}})
        self.assertEqual(self.renderer.failed_activities, ["正在运行测试"])
        self.assertIn("✗", self.stream.getvalue())

    def test_deltas_accumulate_and_are_not_html_escaped(self) -> None:
        self.renderer.on_event("assistant_delta", {"type": "assistant.delta", "metadata": {"delta": "a<b"}})
        self.renderer.on_event("assistant_delta", {"type": "assistant.delta", "metadata": {"delta": "&c"}})
        self.assertEqual(self.renderer.streamed_text(), "a<b&c")
        self.assertIn("助手 >", self.stream.getvalue())

    def test_tool_channel_renders_named_line(self) -> None:
        self.renderer.on_event("tool", {"name": "上下文整理", "args": "历史windowed：40 条→12 条"})
        self.assertIn("上下文整理", self.stream.getvalue())
        self.assertEqual(self.renderer.activity_lines, ["上下文整理：历史windowed：40 条→12 条"])

    def test_control_events_record_terminal_state(self) -> None:
        self.renderer.on_event("control", {"type": "run.failed", "metadata": {"message": "boom"}})
        self.assertEqual(self.renderer.terminal_state, "failed")
        self.assertEqual(self.renderer.error_message, "boom")

    def test_unknown_channel_is_stored_and_hidden_by_default(self) -> None:
        self.renderer.on_event("source.not_ready", {"type": "source.not_ready"})
        self.assertEqual(len(self.renderer.other_events), 1)
        self.assertNotIn("source.not_ready", self.stream.getvalue())

    def test_malformed_payload_never_raises(self) -> None:
        for payload in (None, [], "text", 123):
            self.renderer.on_event("activity", payload)
        self.assertEqual(self.stream.getvalue(), "")


# ── 主循环与最终答复 ────────────────────────────────────────────
class SubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        theme.configure(force=False)

    def test_streamed_answer_is_not_printed_twice(self) -> None:
        from schemas import AgentReply

        result = _FakeResult(final_output=AgentReply(kind="answer", summary="", content="你好，世界"))
        app, stream, _ = _make_app(runtime=_FakeRuntime(result=result, script=_default_script))

        asyncio.run(app.submit("打个招呼"))
        output = stream.getvalue()
        self.assertEqual(output.count("你好，世界"), 1, output)
        self.assertIn("── 💬 回答 ──", output)
        self.assertIn("run=run_fake_0001", output)
        self.assertIn("过程活动 1 条", output)

    def test_mismatched_final_answer_is_printed(self) -> None:
        from schemas import AgentReply

        result = _FakeResult(final_output=AgentReply(kind="answer", summary="", content="最终版本的答复"))
        app, stream, _ = _make_app(runtime=_FakeRuntime(result=result, script=_default_script))

        asyncio.run(app.submit("换个说法"))
        output = stream.getvalue()
        self.assertIn("你好，世界", output)       # 流式预览
        self.assertIn("最终版本的答复", output)     # 最终答复（与预览不同，必须补打）

    def test_questions_and_artifacts_are_rendered(self) -> None:
        from schemas import AgentReply

        result = _FakeResult(
            final_output=AgentReply(kind="questions", summary="需要你确认", content="需要确认",
                                    questions=["用哪个库？"]),
            artifacts=[{"id": "art_9", "name": "out.md", "kind": "note"}],
        )
        app, stream, _ = _make_app(runtime=_FakeRuntime(result=result, script=None))
        asyncio.run(app.submit("开始"))
        output = stream.getvalue()
        self.assertIn("用哪个库？", output)
        self.assertIn("产物已登记", output)

    def test_failure_prints_diagnosis_code(self) -> None:
        from runtime.errors import FinalResponseFailed

        app, stream, _ = _make_app(runtime=_FakeRuntime(exc=FinalResponseFailed("正文为空")))
        asyncio.run(app.submit("会失败的一轮"))
        output = stream.getvalue()
        self.assertIn("E-FINAL-EMPTY", output)
        self.assertNotIn("Traceback", output)

    def test_run_turn_receives_cli_channel_and_callback(self) -> None:
        runtime = _FakeRuntime(script=_default_script)
        app, _, _ = _make_app(runtime=runtime)
        asyncio.run(app.submit("你好"))
        call = runtime.calls[0]
        self.assertEqual(call["message"], "你好")
        self.assertEqual(call["metadata"], {"channel": "cli"})
        self.assertEqual(call["session_id"], "personal")
        self.assertTrue(callable(call["stream_events_cb"]))
        self.assertTrue(call["raise_on_error"])


# ── 诊断码 ─────────────────────────────────────────────────────
class DiagnosticsTests(unittest.TestCase):
    def test_runtime_error_mapping(self) -> None:
        from runtime.errors import (
            AgentError,
            BudgetExceeded,
            FinalResponseFailed,
            TaskCancelled,
            ToolNotFound,
            ToolTimeout,
        )

        cases = [
            (FinalResponseFailed("空输出"), "E-FINAL-EMPTY"),
            (BudgetExceeded("超了"), "E-BUDGET"),
            (TaskCancelled("取消"), "E-CANCELLED"),
            (ToolTimeout("超时了"), "E-TOOL-TIMEOUT"),
            (ToolNotFound("没有这个工具"), "E-TOOL-NOTFOUND"),
            (AgentError("容器 tk_x 已有正在处理的 Run（run_1，状态 running）"), "E-BUSY"),
            (ValueError("connection timeout"), "E-TIMEOUT"),
            (ValueError("OPENAI_API_KEY 未配置"), "E-CONFIG"),
            (ValueError("某个说不清的问题"), "E-UNKNOWN"),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                conclusion = diagnose(exc)
                self.assertEqual(conclusion.code, expected)
                self.assertTrue(conclusion.title)
                self.assertTrue(conclusion.hints)
                self.assertTrue(conclusion.head().startswith(f"[{expected}]"))

    def test_diagnosis_serializes_to_dict(self) -> None:
        payload = diagnose(ValueError("boom")).to_dict()
        self.assertEqual(payload["code"], "E-UNKNOWN")
        self.assertIsInstance(payload["hints"], list)


# ── 命令行为 ───────────────────────────────────────────────────
class CommandBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        theme.configure(force=False)
        self.app, self.stream, self.manager = _make_app()

    def _line(self) -> str:
        return self.stream.getvalue()

    def test_sessions_marks_current_and_lists_rows(self) -> None:
        asyncio.run(self.app.dispatch("/sessions"))
        output = self._line()
        self.assertIn("默认会话", output)
        self.assertIn("▶", output)

    def test_new_creates_and_switches_session(self) -> None:
        asyncio.run(self.app.dispatch("/new 重构会话"))
        self.assertTrue(self.app.session_name.startswith("cli-"))
        self.assertIn("已新建并切换到会话", self._line())

    def test_switch_by_index_and_by_id(self) -> None:
        asyncio.run(self.app.dispatch("/new 第二个"))
        second = self.app.session_name
        rows = self.app.store.list()
        self.assertEqual(len(rows), 2)
        # 序号 1 = 先建的 personal；切回去应真的换掉 session_name
        asyncio.run(self.app.dispatch("/switch 1"))
        self.assertEqual(self.app.session_name, "personal")
        # 按容器 id 切回第二个
        asyncio.run(self.app.dispatch(f"/switch {rows[1].container_id}"))
        self.assertEqual(self.app.session_name, second)
        # 按底层会话名切回第一个
        asyncio.run(self.app.dispatch("/switch personal"))
        self.assertEqual(self.app.session_name, "personal")

    def test_switch_unknown_ref_is_reported(self) -> None:
        asyncio.run(self.app.dispatch("/switch 99"))
        self.assertIn("找不到会话", self._line())

    def test_rename_updates_title(self) -> None:
        asyncio.run(self.app.dispatch("/rename 新标题"))
        self.assertIn("已重命名为", self._line())
        self.assertEqual(self.app.store.get(self.app.container_id()).title, "新标题")

    def test_clear_requires_confirmation_then_clears(self) -> None:
        container = self.app.container_id()
        self.manager.messages[container] = [
            {"id": 1, "role": "user", "content": "hi", "created_at": "2026-09-19T00:00:00"},
        ]
        asyncio.run(self.app.dispatch("/clear"))
        self.assertIn("确认请再执行", self._line())
        self.assertTrue(self.manager.messages[container])

        asyncio.run(self.app.dispatch("/clear yes"))
        self.assertIn("已清空", self._line())
        self.assertEqual(self.manager.messages[container], [])

    def test_history_shows_messages(self) -> None:
        container = self.app.container_id()
        self.manager.messages[container] = [
            {"id": 1, "role": "user", "content": "问题一", "created_at": "2026-09-19T00:00:00"},
            {"id": 2, "role": "assistant", "content": "回答一", "created_at": "2026-09-19T00:00:01"},
        ]
        asyncio.run(self.app.dispatch("/history 5"))
        output = self._line()
        self.assertIn("问题一", output)
        self.assertIn("回答一", output)

    def test_last_reprints_latest_assistant_message(self) -> None:
        container = self.app.container_id()
        self.manager.messages[container] = [
            {"id": 1, "role": "assistant", "content": "上一条回答", "created_at": "2026-09-19T00:00:00"},
        ]
        asyncio.run(self.app.dispatch("/last"))
        self.assertIn("上一条回答", self._line())

    def test_diag_renders_sections(self) -> None:
        container = self.app.container_id()
        self.manager.messages[container] = [
            {"id": 1, "role": "user", "content": "x", "created_at": "2026-09-19T00:00:00"},
        ]
        asyncio.run(self.app.dispatch("/diag"))
        output = self._line()
        self.assertIn("诊断", output)
        self.assertIn("Provider 尝试", output)
        self.assertIn("agnes-2.5-flash", output)

    def test_tools_lists_tool_calls(self) -> None:
        container = self.app.container_id()
        self.manager.messages[container] = [
            {"id": 1, "role": "user", "content": "x", "created_at": "2026-09-19T00:00:00"},
        ]
        asyncio.run(self.app.dispatch("/tools"))
        self.assertIn("read_workspace_file", self._line())

    def test_status_and_debug_and_mode(self) -> None:
        asyncio.run(self.app.dispatch("/status"))
        asyncio.run(self.app.dispatch("/debug on"))
        self.assertTrue(self.app.debug)
        asyncio.run(self.app.dispatch("/mode sync"))
        self.assertEqual(self.app.mode, "sync")
        asyncio.run(self.app.dispatch("/mode bogus"))
        self.assertIn("用法", self._line())

    def test_unknown_command_is_reported(self) -> None:
        asyncio.run(self.app.dispatch("/nope"))
        self.assertIn("未知命令", self._line())

    def test_exit_sets_flag(self) -> None:
        asyncio.run(self.app.dispatch("/exit"))
        self.assertTrue(self.app.request_exit)


# ── 主循环 ─────────────────────────────────────────────────────
class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        theme.configure(force=False)

    def _scripted_input(self, lines):
        iterator = iter(lines)

        def _reader(prompt=""):
            try:
                return next(iterator)
            except StopIteration as exc:  # 模拟 Ctrl+D
                raise EOFError from exc

        return _reader

    def test_loop_runs_command_then_exits(self) -> None:
        app, stream, _ = _make_app(input_fn=self._scripted_input(["/status", "/exit"]))
        code = asyncio.run(app.run())
        self.assertEqual(code, 0)
        self.assertIn("当前状态", stream.getvalue())

    def test_loop_sends_message_and_survives_eof(self) -> None:
        app, stream, _ = _make_app(input_fn=self._scripted_input(["你好"]))
        asyncio.run(app.run())
        output = stream.getvalue()
        self.assertIn("你好，世界", output)
        self.assertIn("再见", output)

    def test_multi_line_mode_joins_lines(self) -> None:
        app, stream, _ = _make_app(input_fn=self._scripted_input(["/multi", "第一行", "第二行", ".", "/exit"]))
        asyncio.run(app.run())
        call = app.runtime.calls[0]
        self.assertEqual(call["message"], "第一行\n第二行")

    def test_backslash_continuation_joins_lines(self) -> None:
        app, stream, _ = _make_app(input_fn=self._scripted_input(["上半句 \\", "下半句", "/exit"]))
        asyncio.run(app.run())
        call = app.runtime.calls[0]
        self.assertEqual(call["message"], "上半句\n下半句")

    def test_bare_exit_word_leaves_loop(self) -> None:
        app, stream, _ = _make_app(input_fn=self._scripted_input(["退出"]))
        asyncio.run(app.run())
        self.assertEqual(app.runtime.calls, [])


# ── 样式层 ─────────────────────────────────────────────────────
class ThemeTests(unittest.TestCase):
    def tearDown(self) -> None:
        theme.configure(force=False)

    def test_style_is_plain_when_disabled(self) -> None:
        theme.configure(force=False)
        self.assertEqual(theme.style("hello", "red"), "hello")
        self.assertFalse(theme.enabled())

    def test_style_wraps_and_strips(self) -> None:
        theme.configure(force=True)
        styled = theme.style("hello", "bold", "cyan")
        self.assertIn("\x1b[", styled)
        self.assertEqual(theme.strip(styled), "hello")

    def test_width_is_sane(self) -> None:
        self.assertGreaterEqual(theme.width(), 48)


class HelperTests(unittest.TestCase):
    def test_same_text_ignores_whitespace(self) -> None:
        self.assertTrue(_same_text("你好\n世界", "你好 世界"))
        self.assertFalse(_same_text("你好", "你好，世界"))
        self.assertFalse(_same_text("你好", ""))

    def test_container_id_is_created_on_demand(self) -> None:
        app, _, manager = _make_app()
        container = app.container_id()
        self.assertTrue(container.startswith("tk_"))
        self.assertIn(container, manager.containers)


if __name__ == "__main__":
    unittest.main()
