"""Phase A 修复的验收测试（2026-09-22《FORGE API Layer Audit Report》§17）。

覆盖（每条对应审计报告里的一个 A 项）：
- A1  工具参数校验失败（SDK 吞异常返回文案）→ 账本记 TOOL_ERROR，不计入「执行证据」
- A2  控制事件白名单由 TaskState 派生：所有 ``run.<state>`` 都能过闸口，status 推导确定
- A4  ``run_turn`` 的 finally 兜底：BaseException 穿透后 Run 落到 failed，不卡 running
- A5  审批名单由 ``spec.TOOL_CATALOG`` 的 side_effect 派生：副作用工具 ⊆ 审批 ∪ 显式豁免
- A6  已注册工具 ⊆ 工具目录（消除目录漂移；think/search_sources 曾被保守判副作用）
- A8  ``_record_tool`` 写 task_events 的 ``normalized_args`` 与 tool_calls.arguments 同一套脱敏

全部离线：不联网、不调真实模型。
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("FORGE_TEST_MODE", "1")
import runtime.execution as runtime_execution
from tests.workflow_model_stub import workflow_model_stub

from runtime import public_activity as pa
from runtime.approval import GATED_DEFAULT, SIDE_EFFECT_EXEMPT
from runtime.spec import TOOL_CATALOG, spec_for
from runtime.task import TaskState


# ---------------------------------------------------------------------------
# A2：控制事件白名单 / status 推导
# ---------------------------------------------------------------------------


def _collect_projector_events(script) -> list[tuple[str, dict]]:
    """跑一段 projector 脚本，收集所有真实下发（sink）的 (channel, event)。"""
    sent: list[tuple[str, dict]] = []
    proj = pa.RunActivityProjector("task_x", None, sink=lambda ch, ev: sent.append((ch, ev)))
    script(proj)
    return sent


class PublicWireTests(unittest.TestCase):
    def test_every_task_state_kind_passes_the_gate(self) -> None:
        """枚举里有几个状态，就有几个 run.<state> 事件名必须能过闸口。"""
        for state in TaskState:
            kind = f"run.{state.value}"
            wire = pa.public_wire("task_x", kind, {"run_id": "task_x"})
            self.assertIsNotNone(wire, "%s 被闸口静默丢弃" % kind)
            self.assertEqual(wire[0], "control")
            self.assertEqual(wire[1]["type"], kind)

    def test_transport_and_legacy_names_still_pass(self) -> None:
        for kind in (
            "run.started",            # 不是 TaskState 成员，但 runner 在 Run 启动时 emit
            "run.waiting_for_user",   # 历史别名
            "runtime.done", "runtime.error", "approval.required",
            "assistant.reply", "source.not_ready",
        ):
            self.assertIsNotNone(pa.public_wire("task_x", kind, {}), kind)

    def test_unknown_kind_is_dropped(self) -> None:
        self.assertIsNone(pa.public_wire("task_x", "totally.unknown", {}))
        self.assertIsNone(pa.public_wire("task_x", "task.failed", {}))

    def test_control_status_derivation(self) -> None:
        cases = {
            "run.completed": "completed",
            "run.failed": "failed",
            "run.cancelled": "cancelled",
            "run.waiting_user": "waiting_for_user",
            "run.waiting_for_user": "waiting_for_user",
            "run.waiting_approval": "waiting_for_user",
            "run.paused": "waiting_for_user",
            "run.started": "running",
            "run.submitted": "running",
            "runtime.done": "completed",
            "runtime.error": "failed",
            "approval.required": "waiting_for_user",
        }
        for kind, expected in cases.items():
            self.assertEqual(pa.control_status_of(kind), expected, kind)

    def test_all_projector_emits_survive_the_gate(self) -> None:
        """生产端 emit 的每个 kind 都必须能过 public_wire（端到端一致性）。"""

        def script(proj: pa.RunActivityProjector) -> None:
            proj.tool_started("read_workspace_file", {"path": "a.py"}, "c1")
            proj.tool_finished("c1", "文件内容")
            proj.tool_started("run_tests", {"project": "p"}, "c2")
            proj.tool_finished("c2", "ERROR: 1 failed", error=True)
            proj.summary("已检查 1 个文件")
            proj.generating()
            proj.delta("你好")
            proj.wait()

        sent = _collect_projector_events(script)
        self.assertTrue(sent, "projector 未下发任何事件")
        for ch, ev in sent:
            self.assertIsNotNone(
                pa.public_wire("task_x", ch, ev),
                "emit(%s) 过不了 public_wire" % ev.get("type"),
            )

    def test_finish_waiting_user_is_canonical_and_not_failed(self) -> None:
        def script(proj: pa.RunActivityProjector) -> None:
            proj.tool_started("read_workspace_file", {"path": "a.py"}, "c1")
            proj.tool_finished("c1", "文件内容")
            proj.finish("waiting_user")

        sent = _collect_projector_events(script)
        types = [ev.get("type") for _, ev in sent]
        self.assertIn("run.waiting_user", types)
        # 追问态绝不是 failed（修复前 finish() 把非 completed/cancelled 一律算 failed）
        for _, ev in sent:
            if ev.get("type") == "run.waiting_user":
                self.assertEqual(ev.get("status"), "waiting_for_user")
            self.assertIsNotNone(pa.public_wire("task_x", ev.get("channel"), ev))

    def test_finish_terminal_states_status(self) -> None:
        for state, expected in (("completed", "completed"),
                                ("failed", "failed"),
                                ("cancelled", "cancelled")):
            sent = _collect_projector_events(
                lambda proj, _s=state: (proj.tool_started("read_workspace_file", {"path": "a.py"}, "c1"),
                                        proj.tool_finished("c1", "ok"),
                                        proj.finish(_s))
            )
            tail = [ev for _, ev in sent if ev.get("type") == f"run.{state}" and ev.get("channel") == "control"]
            self.assertTrue(tail, "%s 未下发 control 终态帧" % state)
            self.assertEqual(tail[-1]["status"], expected)


# ---------------------------------------------------------------------------
# A5：审批名单由 side_effect 派生
# ---------------------------------------------------------------------------


class ApprovalDerivationTests(unittest.TestCase):
    def test_every_side_effect_tool_is_gated_or_explicitly_exempt(self) -> None:
        ungated = sorted(
            name for name, entry in TOOL_CATALOG.items()
            if entry[2] and name not in GATED_DEFAULT and name not in SIDE_EFFECT_EXEMPT
        )
        self.assertEqual(
            ungated, [],
            "有副作用却既不审批也不豁免（可无审批静默写盘）：%s" % ungated,
        )

    def test_exemptions_exist_in_catalog(self) -> None:
        unknown = sorted(n for n in SIDE_EFFECT_EXEMPT if n not in TOOL_CATALOG)
        self.assertEqual(unknown, [], "豁免表里出现目录外的工具名：%s" % unknown)

    def test_exemption_is_a_strict_subset(self) -> None:
        side_effect_tools = {n for n, e in TOOL_CATALOG.items() if e[2]}
        self.assertTrue(
            SIDE_EFFECT_EXEMPT < side_effect_tools,
            "豁免表不能等于全部副作用工具（那就等于没管）",
        )

    def test_network_and_code_execution_tools_are_gated(self) -> None:
        for name in ("deep_research", "fetch_github_repo", "run_python", "code_loop"):
            self.assertIn(name, GATED_DEFAULT, "%s 是联网/落盘副作用，必须默认审批" % name)
        self.assertIn(
            "write_code_file", SIDE_EFFECT_EXEMPT,
            "受路径围栏的代码沙箱写入由用户意图门控制；执行仍需审批",
        )

    def test_read_only_tools_are_not_gated(self) -> None:
        for name in ("calculate", "read_workspace_file", "list_notes", "think", "search_sources"):
            self.assertNotIn(name, GATED_DEFAULT, "%s 只读，不应被审批门打断" % name)


# ---------------------------------------------------------------------------
# A6：工具目录一致性
# ---------------------------------------------------------------------------


class ToolCatalogConsistencyTests(unittest.TestCase):
    def test_registered_tools_have_catalog_entries(self) -> None:
        from agent import assistant_agent

        registered = {
            getattr(t, "name", "")
            for t in (getattr(assistant_agent, "tools", []) or [])
        }
        missing = sorted(registered - set(TOOL_CATALOG))
        self.assertEqual(
            missing, [],
            "已注册工具缺目录条目（会被保守判 side_effect=True 并被 Readiness 门拦掉）：%s" % missing,
        )

    def test_think_and_search_sources_are_zero_side_effect(self) -> None:
        for name in ("think", "search_sources"):
            self.assertFalse(TOOL_CATALOG[name][2], "%s 应为零副作用" % name)

    def test_think_is_allowed_in_discoverable_state(self) -> None:
        from runtime.readiness_gate import (
            STATUS_DISCOVERABLE,
            check_status_tool,
            classify_tool,
        )

        effect = classify_tool("think", spec_for("think"))
        allowed, reason = check_status_tool(STATUS_DISCOVERABLE, effect)
        self.assertTrue(allowed, "think 在 DISCOVERABLE 下应放行，实际被拦：%s" % reason)


# ---------------------------------------------------------------------------
# A1：SDK 吞掉的工具失败 → TOOL_ERROR（不计入执行证据）
# ---------------------------------------------------------------------------


class SdkToolFailureTests(unittest.TestCase):
    def test_detects_sdk_default_failure_texts(self) -> None:
        from runtime.runner import _sdk_tool_failure_text

        self.assertTrue(_sdk_tool_failure_text(
            "An error occurred while running the tool. Please try again. "
            "Error: Invalid JSON input for tool read_workspace_file"
        ))
        self.assertTrue(_sdk_tool_failure_text(
            "An error occurred while parsing tool arguments. Please try again with valid JSON. Error: ..."
        ))

    def test_normal_output_is_not_flagged(self) -> None:
        from runtime.runner import _sdk_tool_failure_text

        self.assertEqual(_sdk_tool_failure_text("文件内容：正常输出"), "")
        self.assertEqual(_sdk_tool_failure_text(""), "")
        self.assertEqual(_sdk_tool_failure_text(None), "")
        self.assertEqual(_sdk_tool_failure_text({"ok": True}), "")

    def test_error_status_is_not_execution_evidence(self) -> None:
        from runtime.completion import TOOL_ERROR, TOOL_EXECUTED, ExecutionEvidence

        evidence = ExecutionEvidence(tool_calls=[
            {"name": "read_workspace_file", "status": TOOL_ERROR, "output_head": "err"},
        ])
        self.assertEqual(evidence.executed_count(), 0)
        self.assertFalse(evidence.executed("read_workspace_file"))
        evidence_ok = ExecutionEvidence(tool_calls=[
            {"name": "read_workspace_file", "status": TOOL_EXECUTED, "output_head": "ok"},
        ])
        self.assertEqual(evidence_ok.executed_count(), 1)


class ToolValidationLedgerTests(unittest.TestCase):
    """A1 真实链路：模型漏传必填参数 → 账本不得记 TOOL_EXECUTED。

    复现路径：SDK 的 failure_error_function 把 ModelBehaviorError 转成普通字符串，
    项目 wrapper 拿到的是字符串而不是异常。若不识别，它会被记为「执行成功」。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_a_"))
        import main as main_module

        self._main = main_module
        self._orig = runtime_execution.execute_turn
        from runtime.runner import AgentRuntime

        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))

    def tearDown(self) -> None:
        runtime_execution.execute_turn = self._orig

    def _install_tool_probe(self, tool_name: str, args_json: str) -> dict:
        captured: dict = {}
        rt = self.runtime

        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None,
            agent=None, audit=None, stream_events_cb=None, **kwargs,
        ):
            from runtime.runctx import current as _cur

            rid = getattr(_cur(), "run_id", None)
            if any(call.get("name") == tool_name
                   for call in rt._ledgers.get(rid, [])):
                captured["run_id"] = rid
                captured["ledger"] = list(rt._ledgers.get(rid, []))
                return '{"kind":"answer","summary":"x","content":"done"}'
            from agent import assistant_agent
            from agents.tool import ToolContext

            tool = next(
                (t for t in (getattr(assistant_agent, "tools", []) or [])
                 if getattr(t, "name", "") == tool_name),
                None,
            )
            self.assertIsNotNone(tool, "%s 未注册" % tool_name)
            ctx = ToolContext(
                context=None, tool_name=tool_name,
                tool_call_id="call_phase_a", tool_arguments=args_json,
            )
            out = tool.on_invoke_tool(ctx, args_json)
            if asyncio.iscoroutine(out):
                out = await out
            captured["out"] = out
            # 账本在 run_turn 收尾时会被 pop 掉，必须在 Run 内取快照
            captured["run_id"] = rid
            captured["ledger"] = list(rt._ledgers.get(rid, []))
            return '{"kind":"answer","summary":"x","content":"done"}'

        runtime_execution.execute_turn = workflow_model_stub(fake_execute_turn)
        return captured

    def test_missing_required_argument_is_recorded_as_error(self) -> None:
        captured = self._install_tool_probe("read_workspace_file", '{"max_chars": 100}')
        result = asyncio.run(self.runtime.run_turn("读一下文件", session_id="phase_a"))

        out = captured.get("out")
        self.assertIsInstance(out, str, "SDK 应把校验失败吞成字符串（本测试的前提）")
        self.assertIn("An error occurred while running the tool", out)

        entries = [c for c in captured.get("ledger", []) if c.get("name") == "read_workspace_file"]
        self.assertTrue(entries, "该工具调用未被记账")
        self.assertEqual(
            [c.get("status") for c in entries], ["error"],
            "参数校验失败必须记 TOOL_ERROR，不能记 executed",
        )
        self.assertIsNotNone(result.task)

    def test_well_formed_call_is_still_recorded_as_executed(self) -> None:
        captured = self._install_tool_probe("read_workspace_file", '{"path": "agent.py", "max_chars": 200}')
        asyncio.run(self.runtime.run_turn("读一下文件", session_id="phase_a"))
        self.assertIsInstance(captured.get("out"), str)
        entries = [c for c in captured.get("ledger", []) if c.get("name") == "read_workspace_file"]
        self.assertTrue(entries, "该工具调用未被记账")
        self.assertEqual([c.get("status") for c in entries], ["executed"])


# ---------------------------------------------------------------------------
# A8：审计出口脱敏（账本 + task_events 两个出口必须同一套规则）
# ---------------------------------------------------------------------------


class ToolArgumentRedactionTests(unittest.TestCase):
    """A8：``normalized_args`` 是 ``tool_calls.arguments`` 之外的第二条敏感数据出口。

    回归背景：此前只有 tool_calls 侧做了 redact，``tool.invocation`` 事件的
    ``normalized_args`` 是原样 json.dumps —— 同一份密钥在两个出口泄露程度不同。
    本测试在**真实 Run 内**触发 ``_record_tool``（不真正调用工具，避免副作用），
    再读回事件断言。
    """

    SECRET = "sk-livephasea1234567890abcdefghij"

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_a_redact_"))
        import main as main_module

        self._main = main_module
        self._orig = runtime_execution.execute_turn
        from runtime.runner import AgentRuntime

        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))

    def tearDown(self) -> None:
        runtime_execution.execute_turn = self._orig

    def test_normalized_args_in_task_events_are_redacted(self) -> None:
        rt = self.runtime
        captured: dict = {}

        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None,
            agent=None, audit=None, stream_events_cb=None, **kwargs,
        ):
            from runtime.runctx import current as _cur

            rid = getattr(_cur(), "run_id", None)
            captured["run_id"] = rid
            rt._record_tool(
                "run_python",
                {"code": "print(1)", "api_key": self.SECRET},
                "executed",
                "ok",
                invocation_id="inv_redact_1",
            )
            return '{"kind":"answer","summary":"x","content":"done"}'

        runtime_execution.execute_turn = workflow_model_stub(fake_execute_turn)
        asyncio.run(rt.run_turn("跑段代码", session_id="phase_a_redact"))

        rid = captured.get("run_id")
        self.assertTrue(rid, "未捕获到 run_id")
        events = [e for e in rt.tasks.list_events(rid) if e.event_type == "tool.invocation"]
        self.assertTrue(events, "缺少 tool.invocation 事件")

        blob = json.dumps(events[-1].payload, ensure_ascii=False)
        self.assertNotIn(self.SECRET, blob, "normalized_args 泄露了密钥明文")
        self.assertIn("***", blob, "密钥应被替换为掩码而不是整字段丢弃")

    def test_ledger_args_are_also_redacted(self) -> None:
        """账本 ``args`` 与事件 ``normalized_args`` 是同一次脱敏的两个消费者。"""
        rt = self.runtime
        captured: dict = {}

        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None,
            agent=None, audit=None, stream_events_cb=None, **kwargs,
        ):
            from runtime.runctx import current as _cur

            rid = getattr(_cur(), "run_id", None)
            rt._record_tool("run_python", {"api_key": self.SECRET}, "executed", "ok")
            captured["ledger"] = list(rt._ledgers.get(rid, []))
            return '{"kind":"answer","summary":"x","content":"done"}'

        runtime_execution.execute_turn = workflow_model_stub(fake_execute_turn)
        asyncio.run(rt.run_turn("跑段代码", session_id="phase_a_redact2"))

        entries = captured.get("ledger") or []
        self.assertTrue(entries, "账本为空")
        blob = json.dumps(entries, ensure_ascii=False)
        self.assertNotIn(self.SECRET, blob, "内存账本 args 泄露了密钥明文")


# ---------------------------------------------------------------------------
# A4：异常穿透兜底
# ---------------------------------------------------------------------------


class AbnormalExitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_a_exit_"))
        import main as main_module

        self._main = main_module
        self._orig = runtime_execution.execute_turn
        from runtime.runner import AgentRuntime

        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))

    def tearDown(self) -> None:
        runtime_execution.execute_turn = self._orig

    def test_base_exception_does_not_leave_run_running(self) -> None:
        """SystemExit 穿透时：Run 落 FAILED，且**不得**把进程退出信号带出 run_turn。

        两层断言缺一不可：
        1. 状态层：Run 不能卡在 running（asyncio 的 Task.__step 会跳过 finally）；
        2. 语义层：SystemExit 不能从 run_turn 冒泡（`_close_run` 的既有教训——
           清理路径抛出的 SystemExit 曾让 uvicorn 进程“跑一段时间后突然退出”）。
        """
        seen: dict = {}

        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None,
            agent=None, audit=None, stream_events_cb=None, **kwargs,
        ):
            from runtime.runctx import current as _cur

            seen["run_id"] = getattr(_cur(), "run_id", None)
            raise SystemExit(3)

        runtime_execution.execute_turn = workflow_model_stub(fake_execute_turn)
        async def _run():
            asyncio.get_running_loop().set_exception_handler(lambda _loop, _ctx: None)
            return await self.runtime.run_turn("做 A", session_id="phase_a_exit")

        # 不得抛出 SystemExit —— 进程退出信号必须在 Run 内被收口
        try:
            result = asyncio.run(_run())
        except SystemExit as exc:  # pragma: no cover - 回归时才会走到
            self.fail(f"SystemExit 逃出了 run_turn（exit code={exc.code}）")

        run_id = seen.get("run_id")
        self.assertTrue(run_id, "未捕获到 run_id")
        task = self.runtime.tasks.get_task(run_id)
        self.assertIsNotNone(task)
        self.assertEqual(
            task.state, TaskState.FAILED,
            "BaseException 穿透后 Run 必须落到终态，不能卡在 %s" % task.state.value,
        )
        self.assertFalse(result.ok, "被中断的 Run 不得报告 ok=True")
        self.assertIn("进程级退出信号", result.error or "",
                      "失败原因应说明是进程级退出信号，而不是笼统的未知错误")

        events = self.runtime.tasks.list_events(run_id)
        terminal = [e for e in events if e.event_type == "run.terminal"]
        self.assertTrue(terminal, "缺少 run.terminal 事件")
        self.assertEqual(terminal[-1].payload.get("state"), "failed")
        self.assertEqual(terminal[-1].payload.get("kind"), "bounded_failure")

    def test_keyboard_interrupt_lands_state_then_reaches_caller(self) -> None:
        """Ctrl-C 双约束（run_turn 这一半）：先写完终态，再把 KeyboardInterrupt 交还上层。

        两条都必须成立，缺一条就是 bug：
        - 只重抛不落终态 → 一次 Ctrl-C 留下永远停在 running 的僵尸 Run；
        - 只落终态不重抛 → Ctrl-C 被吞，用户按了没反应，只能杀进程。

        注意断言方式：必须在 ``_run()`` 里接住。若让它逃出 ``asyncio.run``，
        pytest 会把它当成真实的 Ctrl-C 而中止整个测试会话。
        """
        seen: dict = {}

        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None,
            agent=None, audit=None, stream_events_cb=None, **kwargs,
        ):
            from runtime.runctx import current as _cur

            seen["run_id"] = getattr(_cur(), "run_id", None)
            raise KeyboardInterrupt

        runtime_execution.execute_turn = workflow_model_stub(fake_execute_turn)
        async def _run():
            asyncio.get_running_loop().set_exception_handler(lambda _loop, _ctx: None)
            try:
                await self.runtime.run_turn("做 A", session_id="phase_a_kb")
            except KeyboardInterrupt:
                seen["delivered"] = True

        asyncio.run(_run())

        self.assertTrue(seen.get("delivered"), "KeyboardInterrupt 必须送达调用方（Ctrl-C 不能失效）")
        run_id = seen.get("run_id")
        self.assertTrue(run_id, "未捕获到 run_id")
        task = self.runtime.tasks.get_task(run_id)
        self.assertIsNotNone(task)
        self.assertEqual(
            task.state, TaskState.FAILED,
            "Ctrl-C 之后 Run 必须落到终态，不能卡在 %s" % task.state.value,
        )


if __name__ == "__main__":
    unittest.main()
