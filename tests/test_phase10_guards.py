"""Phase 10 —— Exact Redundant Evidence Guard + Completion-Ready Guard（确定性）。"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402
from runtime.errors import CompletionReadyTerminated  # noqa: E402
from runtime.readiness_gate import DiscoveryTracker  # noqa: E402
from runtime.runner import AgentRuntime  # noqa: E402
from runtime.task import TaskState  # noqa: E402
from tests.test_readiness_closure import _GateHarness, _make_tool  # noqa: E402


class ExactIdentityTests(unittest.TestCase):
    def test_read_identity_epoch(self):
        dt = DiscoveryTracker()
        a = dt.exact_identity("read_workspace_file", {"path": "A.py"})
        b = dt.exact_identity("read_workspace_file", {"path": "a.py"})
        self.assertEqual(a, b)  # 大小写归一
        dt.bump_epoch()
        c = dt.exact_identity("read_workspace_file", {"path": "a.py"})
        self.assertNotEqual(a, c)  # mutation 后 epoch 变化 → 不视为相同

    def test_mutation_has_no_identity(self):
        dt = DiscoveryTracker()
        self.assertIsNone(dt.exact_identity("edit_project_file", {"path": "a.py"}))

    def test_redundant_check_and_mark(self):
        dt = DiscoveryTracker()
        red, _ = dt.redundant_check("read_workspace_file", {"path": "a.py"})
        self.assertFalse(red)
        dt.mark_exact_seen("read_workspace_file", {"path": "a.py"})
        red, msg = dt.redundant_check("read_workspace_file", {"path": "a.py"})
        self.assertTrue(red)
        self.assertIn("已有证据", msg)
        # mutation → epoch+1 → 不再是 redundant
        dt.bump_epoch()
        red2, _ = dt.redundant_check("read_workspace_file", {"path": "a.py"})
        self.assertFalse(red2)

    def test_search_and_verification_identity(self):
        dt = DiscoveryTracker()
        dt.mark_exact_seen("search_documents", {"query": "token refresh"})
        self.assertTrue(dt.redundant_check("search_documents", {"query": "token refresh"})[0])
        dt.mark_exact_seen("run_python", {"filename": "t.py"})
        self.assertTrue(dt.redundant_check("run_python", {"filename": "t.py"})[0])
        dt.bump_epoch()
        self.assertFalse(dt.redundant_check("run_python", {"filename": "t.py"})[0])

    def test_completion_ready_requires_mutation_and_verification(self):
        dt = DiscoveryTracker()
        dt.observe("run_python", {"filename": "t.py"}, "退出码: 0")
        self.assertTrue(dt.verification_passed)
        self.assertFalse(dt.completion_ready())  # 未 mutation
        dt.observe("edit_project_file", {"path": "a.py"}, "已修改 a.py")
        self.assertTrue(dt.completion_ready())


class GuardWrapperTests(unittest.TestCase):
    def setUp(self):
        self._rg = os.environ.get("FORGE_REDUNDANT_GUARD")
        self._cr = os.environ.get("FORGE_COMPLETION_READY")
        self._ap = os.environ.get("APPROVAL")
        os.environ["FORGE_REDUNDANT_GUARD"] = "on"
        os.environ["FORGE_COMPLETION_READY"] = "on"
        # 关闭 Approval Gate：让测试聚焦 Completion Ready Guard 本身
        os.environ["APPROVAL"] = "off"

    def tearDown(self):
        for k, v in (("FORGE_REDUNDANT_GUARD", self._rg),
                      ("FORGE_COMPLETION_READY", self._cr),
                      ("APPROVAL", self._ap)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_guard_a_suppresses_exact_repeat_read(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_workspace_file", executed)])
        try:
            out1 = h.call("read_workspace_file", {"path": "a.py"}, status=None)
            self.assertIn("ok-", out1)
            out2 = h.call("read_workspace_file", {"path": "a.py"}, status=None)
            self.assertIn("已有证据", out2)
            self.assertEqual(len(executed), 1)  # 第二次未执行底层
            self.assertGreaterEqual(h.ctx.guard_summary()["redundant_guard_hits"], 1)
        finally:
            h.close()

    def test_guard_a_not_triggered_after_mutation(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_workspace_file", executed),
                          _make_tool("edit_project_file", executed,
                                     result="已修改 a.py")])
        try:
            h.call("read_workspace_file", {"path": "a.py"}, status=None)
            h.call("edit_project_file", {"path": "a.py", "old": "a", "new": "b"}, status=None)
            out = h.call("read_workspace_file", {"path": "a.py"}, status=None)
            self.assertIn("ok-", out)  # mutation 后重读允许
            self.assertEqual(len([e for e in executed if e == "read_workspace_file"]), 2)
        finally:
            h.close()

    def test_guard_b_announces_then_terminates(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_workspace_file", executed)])
        try:
            # 制造 completion_ready：mutation + verification pass
            h.ctx.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
            h.ctx.note_progress("run_python", {"filename": "t.py"}, "退出码: 0")
            self.assertTrue(h.ctx.completion_ready())
            out1 = h.call("read_workspace_file", {"path": "b.py"}, status=None)
            self.assertIn("完成条件已满足", out1)
            # 一次例外放行：允许最后一次读操作
            out2 = h.call("read_workspace_file", {"path": "c.py"}, status=None)
            self.assertEqual(out2, "ok-read_workspace_file")
            self.assertEqual(executed, ["read_workspace_file"])
            # 再次读 → CompletionReadyTerminated
            with self.assertRaises(CompletionReadyTerminated):
                h.call("read_workspace_file", {"path": "d.py"}, status=None)
            self.assertEqual(len(executed), 1)  # 第二次读未执行（被阻断）
        finally:
            h.close()

    def test_guard_b_blocks_ver_after_obligations_satisfied(self):
        """Phase 34/35 Run4 regression：obligations satisfied 后持续 read 被拦截（read 类工具已足够防无限循环）。

        注：VERIFICATION 工具（run_tests）不在 Guard B 拦截范围内——多轮修复场景
        需要模型在verification失败后继续编辑源码。Run 4的无限循环由 Approval 重复文本
        机制（REPEAT_TEXT）和 run_turn max_turns 共同兜底。
        """
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_code_file", executed)])
        try:
            # 制造 completion_ready：mutation + verification pass
            h.ctx.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
            h.ctx.note_progress("run_tests", {"project": "fixture"}, "退出码: 0 ｜ PASS")
            self.assertTrue(h.ctx.completion_ready())
            # 首次 read → 提示收口 + 一次例外放行
            out1 = h.call("read_code_file", {"path": "series.py"}, status=None)
            self.assertIn("完成条件已满足", out1)
            # 一次例外放行：最后一次读操作允许
            out2 = h.call("read_code_file", {"path": "calc.py"}, status=None)
            self.assertEqual(out2, "ok-read_code_file")
            # 再次 read → CompletionReadyTerminated（强制收口）
            with self.assertRaises(CompletionReadyTerminated):
                h.call("read_code_file", {"path": "test.py"}, status=None)
            self.assertEqual(len(executed), 1)  # 最后一次读执行，之后阻断
        finally:
            h.close()

    def test_guard_b_allows_new_ver_after_new_mutation(self):
        """验证新 mutation 后的重新验证不被误拦（多轮修复场景）。"""
        executed: list[str] = []
        h = _GateHarness([_make_tool("run_tests", executed),
                          _make_tool("edit_project_file", executed),
                          _make_tool("read_code_file", executed)])
        try:
            # 第一轮：mutation + verification pass
            h.ctx.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
            h.ctx.note_execution_identity("edit_project_file", {"path": "a.py"})
            h.ctx.note_progress("run_tests", {"project": "fixture"}, "退出码: 0 ｜ PASS")
            self.assertTrue(h.ctx.completion_ready())
            # 宣布 completion_ready
            out1 = h.call("read_code_file", {"path": "b.py"}, status=None)
            self.assertIn("完成条件已满足", out1)
            # 新 mutation → epoch bump → verified_revision < evidence_epoch
            h.ctx.note_progress("edit_project_file", {"path": "c.py"}, "已修改 c.py")
            h.ctx.note_execution_identity("edit_project_file", {"path": "c.py"})
            # 重新验证应被允许（epoch 已变化）
            out2 = h.call("run_tests", {"project": "fixture"}, status=None)
            self.assertEqual(out2, "ok-run_tests")
            self.assertEqual(executed, ["run_tests"])
        finally:
            h.close()

    def test_guard_b_allows_ver_before_obligations_satisfied(self):
        """确保：obligations 未满足时，run_tests 不会被误拦。"""
        executed: list[str] = []
        h = _GateHarness([_make_tool("run_tests", executed),
                          _make_tool("edit_project_file", executed)])
        try:
            # 只做 mutation，尚未 verification → completion_ready=False
            h.ctx.note_progress("edit_project_file", {"path": "calc.py"}, "已修改 calc.py")
            self.assertFalse(h.ctx.completion_ready())  # verification 未完成
            # run_tests 应正常执行（未被拦截）
            out = h.call("run_tests", {"project": "fixture"}, status=None)
            self.assertEqual(out, "ok-run_tests")
            self.assertEqual(executed, ["run_tests"])
        finally:
            h.close()


class CompletionReadyRunTurnTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="p10_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self._orig = main_module.execute_turn

    def tearDown(self):
        main_module.execute_turn = self._orig

    def test_completion_ready_terminated_finalizes_completed(self):
        async def fake(mode, message, **kw):
            raise CompletionReadyTerminated("completion_ready")

        main_module.execute_turn = fake
        result = asyncio.run(self.runtime.run_turn("修好这个 bug", session_id="unit", mode="async"))
        self.assertTrue(result.ok)
        self.assertEqual(result.task.state, TaskState.COMPLETED)
        terminal = next(e for e in self.runtime.tasks.list_events(result.task.id)
                        if e.event_type == "run.terminal")
        self.assertEqual(terminal.payload.get("kind"), "completed")


if __name__ == "__main__":
    unittest.main()
