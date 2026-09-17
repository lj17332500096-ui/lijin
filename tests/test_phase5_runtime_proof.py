"""Phase 5 —— Runtime Proof：证明 TERMINALIZE 真正结束 Agent Loop，且不绕过安全门。

关键断言（不是“工具返回了 TERMINALIZE 字符串”）：
- TERMINALIZE 第一次给一次收口机会，之后模型再发起工具 → 抛 ConvergenceTerminated，
  真正 break SDK 工具循环（不再依赖 max_turns / wall timeout）；
- run_turn 捕获 ConvergenceTerminated → bounded failure（有限、确定终态）；
- Gate Order：Missing required / Readiness / Approval 优先于 Convergence。
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402
from runtime.errors import ConvergenceTerminated, NeedsUserInputTerminated  # noqa: E402
from runtime.runner import AgentRuntime  # noqa: E402
from runtime.task import TaskState  # noqa: E402
from tests.test_readiness_closure import _GateHarness, _make_tool  # noqa: E402


class TerminalizeBreaksLoopTests(unittest.TestCase):
    def _seed_terminalize(self, h):
        # 同一动作 + 同结果连续无进展 → TERMINALIZE
        for _ in range(4):
            h.ctx.note_progress("read_workspace_file", {"path": "seed.py"}, "same")

    def test_terminalize_announces_once_then_raises(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_workspace_file", executed)])
        try:
            self._seed_terminalize(h)
            self.assertEqual(h.ctx.convergence_level(), "TERMINALIZE")
            # 第一次：给一次收口机会（返回 TERMINALIZE 文本）
            out = h.call("read_workspace_file", {"path": "a.py"}, status=None)
            self.assertIn("TERMINALIZE", out)
            executed_after_announce = len(executed)
            # 模型不配合、继续调工具 → 真正终止（抛异常），且工具不再执行
            with self.assertRaises(ConvergenceTerminated):
                h.call("read_workspace_file", {"path": "a.py"}, status=None)
            self.assertEqual(len(executed), executed_after_announce)
            self.assertTrue(h.ctx.convergence_forced)
        finally:
            h.close()

    def test_terminalize_stops_real_execution(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_workspace_file", executed)])
        try:
            for _ in range(6):
                try:
                    h.call("read_workspace_file", {"path": "a.py"}, status=None)
                except ConvergenceTerminated:
                    pass
            # 真实执行有界：远小于 max_turns=20
            self.assertLessEqual(len(executed), 3)
        finally:
            h.close()


class NeedsUserInputBreaksLoopTests(unittest.TestCase):
    def test_second_blocked_call_raises(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("get_current_datetime", executed)])
        try:
            h.ctx.needs_user_input = True
            h.ctx.pending_questions = ["请告诉我从哪里出发？"]
            out = h.call("get_current_datetime", {}, status=None)
            self.assertIn("BLOCKED_NEEDS_USER_INPUT", out)
            self.assertEqual(executed, [])
            with self.assertRaises(NeedsUserInputTerminated):
                h.call("get_current_datetime", {}, status=None)
        finally:
            h.close()

    def test_needs_user_terminated_becomes_waiting_user(self):
        async def fake(mode, message, **kwargs):
            raise NeedsUserInputTerminated(["请告诉我从哪里出发？"])

        tmp = Path(tempfile.mkdtemp(prefix="p5_nui_"))
        runtime = AgentRuntime(db_path=str(tmp / "agent.db"))
        orig = main_module.execute_turn
        main_module.execute_turn = fake
        try:
            result = asyncio.run(runtime.run_turn("帮我查明天去上海的航班", session_id="unit",
                                                  mode="async"))
        finally:
            main_module.execute_turn = orig
        self.assertTrue(result.ok)
        self.assertEqual(result.task.state, TaskState.WAITING_USER)
        terminal = next(e for e in runtime.tasks.list_events(result.task.id)
                        if e.event_type == "run.terminal")
        self.assertEqual(terminal.payload.get("kind"), "needs_user_input")


class AtomicBudgetTests(unittest.TestCase):
    """Phase 5：预算检查必须原子预留，并发批不得突破上限。"""

    def test_budget_reserves_atomically(self):
        from runtime.runctx import RunContext
        rc = RunContext(run_id="r1")
        allowed = 0
        for _ in range(rc.max_total_tool_executions + 5):
            ok, _ = rc.can_execute_tool("read_workspace_file")
            if ok:
                allowed += 1
        self.assertEqual(allowed, rc.max_total_tool_executions)

    def test_web_search_budget_reserves(self):
        from runtime.runctx import RunContext
        rc = RunContext(run_id="r1")
        allowed = 0
        for _ in range(rc.max_web_search_executions + 3):
            ok, _ = rc.can_execute_tool("web_search")
            if ok:
                allowed += 1
        self.assertEqual(allowed, rc.max_web_search_executions)


class GateOrderTests(unittest.TestCase):
    def _seed_terminalize(self, h):
        for _ in range(4):
            h.ctx.note_progress("read_workspace_file", {"path": "seed.py"}, "same")

    def test_missing_required_beats_convergence(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("web_search", executed)])
        try:
            self._seed_terminalize(h)
            h.ctx.request_text = "帮我查明天去上海的航班。"
            out = h.call("web_search", {"query": "上海 航班"}, status=None)
            self.assertIn("缺少必要信息", out)
            self.assertNotIn("TERMINALIZE", out)
            self.assertEqual(executed, [])
            self.assertTrue(h.ctx.needs_user_input)
        finally:
            h.close()

    def test_readiness_needs_user_beats_convergence(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("edit_project_file", executed)])
        try:
            self._seed_terminalize(h)
            out = h.call("edit_project_file",
                         {"path": "src/x.py", "old": "a", "new": "b"},
                         status="NEEDS_USER")
            self.assertIn("NEEDS_USER", out)
            self.assertNotIn("TERMINALIZE", out)
            self.assertEqual(executed, [])
        finally:
            h.close()

    def test_approval_beats_convergence(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("run_python", executed)])
        try:
            self._seed_terminalize(h)
            out = h.call("run_python", {"code": "print(1)"}, status=None)
            # 审批/策略门先于 convergence：不得因为收敛而放行或改成普通失败。
            # 可能是 pending approval（等待审批）或策略直接拒绝——两者都由审批/策略层决定，
            # 关键是不能落到 convergence 分支，且绝不真实执行。
            self.assertNotIn("TERMINALIZE", out)
            self.assertEqual(executed, [])  # 未真实执行
            self.assertTrue(("审批" in out) or ("拒绝" in out),
                            f"应由审批/策略层决策，实际：{out[:120]}")
        finally:
            h.close()


class RunTurnConvergenceTerminalizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="p5_term_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self._orig = main_module.execute_turn

    def tearDown(self):
        main_module.execute_turn = self._orig

    def _install(self, fn):
        main_module.execute_turn = fn

    def test_convergence_terminated_becomes_bounded_failure(self):
        async def fake(mode, message, **kwargs):
            raise ConvergenceTerminated("TERMINALIZE")

        self._install(fake)
        result = asyncio.run(self.runtime.run_turn("做点事", session_id="unit", mode="async"))
        self.assertFalse(result.ok)
        self.assertEqual(result.task.state, TaskState.FAILED)
        events = [e.event_type for e in self.runtime.tasks.list_events(result.task.id)]
        self.assertIn("run.terminal", events)
        terminal = next(e for e in self.runtime.tasks.list_events(result.task.id)
                        if e.event_type == "run.terminal")
        self.assertEqual(terminal.payload.get("kind"), "no_progress")
        self.assertEqual(terminal.payload.get("state"), "failed")

    def test_waiting_user_state_semantics(self):
        async def fake(mode, message, **kwargs):
            return json.dumps({
                "kind": "questions", "summary": "需要信息", "content": "从哪里出发？",
                "questions": ["从哪里出发？"], "saved_file": None, "next_step": None,
            }, ensure_ascii=False)

        self._install(fake)
        result = asyncio.run(self.runtime.run_turn("帮我订机票", session_id="unit", mode="async"))
        self.assertTrue(result.ok)
        self.assertEqual(result.task.state, TaskState.WAITING_USER)
        terminal = next(e for e in self.runtime.tasks.list_events(result.task.id)
                        if e.event_type == "run.terminal")
        self.assertEqual(terminal.payload.get("kind"), "needs_user_input")


if __name__ == "__main__":
    unittest.main()
