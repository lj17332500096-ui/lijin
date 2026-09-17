"""Phase 4 —— Convergence Semantics（progress-based）确定性单元测试。

覆盖 DiscoveryTracker 的 Run 级 progress 状态与收敛级别，以及 RunContext 委托。
不依赖模型/网络。
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TOOL_ROUTER"] = "on"

from runtime.readiness_gate import (  # noqa: E402
    CONVERGENCE_CAUTION,
    CONVERGENCE_CONVERGENCE,
    CONVERGENCE_NORMAL,
    CONVERGENCE_TERMINALIZE,
    DiscoveryTracker,
)
from runtime.runctx import RunContext  # noqa: E402


class ProgressDefinitionTests(unittest.TestCase):
    def test_new_action_is_progress(self):
        dt = DiscoveryTracker()
        sig = dt.observe("read_workspace_file", {"path": "a.py"}, "content-a")
        self.assertTrue(sig.novel)
        self.assertEqual(dt.meaningful_progress_count, 1)

    def test_same_action_same_result_is_not_progress(self):
        dt = DiscoveryTracker()
        dt.observe("read_workspace_file", {"path": "a.py"}, "content-a")
        sig = dt.observe("read_workspace_file", {"path": "a.py"}, "content-a")
        self.assertFalse(sig.novel)
        self.assertEqual(dt.consecutive_no_progress, 1)

    def test_new_result_resets_no_progress(self):
        dt = DiscoveryTracker()
        dt.observe("read_workspace_file", {"path": "a.py"}, "v1")
        dt.observe("read_workspace_file", {"path": "a.py"}, "v1")
        self.assertEqual(dt.consecutive_no_progress, 1)
        sig = dt.observe("read_workspace_file", {"path": "a.py"}, "v2")
        self.assertTrue(sig.novel)
        self.assertEqual(dt.consecutive_no_progress, 0)
        self.assertEqual(dt.meaningful_progress_count, 2)

    def test_different_query_same_result_is_not_progress(self):
        dt = DiscoveryTracker()
        dt.observe("read_workspace_file", {"path": "a.py"}, "same")
        sig = dt.observe("read_workspace_file", {"path": "b.py"}, "same")
        # 不同签名 = 新动作 = progress（资源 identity 不同）
        self.assertTrue(sig.novel)

    def test_same_semantic_search_repeated_converges(self):
        dt = DiscoveryTracker()
        res = "1. 来源\n   链接: https://a.com"
        dt.observe("web_search", {"query": "北京现在多少度"}, res)
        dt.observe("web_search", {"query": "北京当前气温"}, res)
        # 同语义 + 同结果 → 无进展累积
        self.assertGreaterEqual(dt.consecutive_no_progress, 1)
        self.assertIn(dt.convergence_level(),
                      (CONVERGENCE_CAUTION, CONVERGENCE_CONVERGENCE))

    def test_mutation_first_time_is_progress(self):
        dt = DiscoveryTracker()
        sig = dt.observe("write_project_file", {"path": "x.py", "content": "new"},
                         "written")
        self.assertTrue(sig.novel)

    def test_repeated_identical_mutation_is_not_progress(self):
        dt = DiscoveryTracker()
        dt.observe("write_code_file", {"path": "x.py", "content": "same"}, "ok")
        sig = dt.observe("write_code_file", {"path": "x.py", "content": "same"}, "ok")
        self.assertFalse(sig.novel)

    def test_successful_verification_new_result_is_progress(self):
        dt = DiscoveryTracker()
        dt.observe("run_python", {"filename": "test_a.py"}, "exit 1 failed")
        sig = dt.observe("run_python", {"filename": "test_a.py"}, "exit 0 passed")
        self.assertTrue(sig.novel)

    def test_failed_verification_with_new_diagnostic_is_progress(self):
        dt = DiscoveryTracker()
        dt.observe("run_python", {"filename": "t.py"}, "Traceback A")
        sig = dt.observe("run_python", {"filename": "t.py"}, "Traceback B: new hint")
        self.assertTrue(sig.novel)

    def test_same_blocked_reason_repeated_not_progress(self):
        dt = DiscoveryTracker()
        dt.observe("run_python", {"code": "x"}, status="blocked")
        dt.observe("run_python", {"code": "x"}, status="blocked")
        self.assertGreaterEqual(dt.blocked_repeat_count, 2)
        self.assertGreaterEqual(dt.consecutive_no_progress, 2)

    def test_same_tool_error_repeated_not_progress(self):
        dt = DiscoveryTracker()
        dt.observe("run_python", {"code": "x"}, status="error", error_class="Timeout")
        dt.observe("run_python", {"code": "x"}, status="error", error_class="Timeout")
        self.assertGreaterEqual(dt.repeated_error_count, 1)


class ConvergenceLevelTests(unittest.TestCase):
    def test_normal_then_caution_then_convergence_then_terminalize(self):
        dt = DiscoveryTracker()
        dt.observe("read_workspace_file", {"path": "a.py"}, "same")
        self.assertEqual(dt.convergence_level(), CONVERGENCE_NORMAL)
        dt.observe("read_workspace_file", {"path": "a.py"}, "same")
        self.assertEqual(dt.convergence_level(), CONVERGENCE_CAUTION)
        dt.observe("read_workspace_file", {"path": "a.py"}, "same")
        self.assertEqual(dt.convergence_level(), CONVERGENCE_CONVERGENCE)
        dt.observe("read_workspace_file", {"path": "a.py"}, "same")
        self.assertEqual(dt.convergence_level(), CONVERGENCE_TERMINALIZE)

    def test_force_stop_is_terminalize(self):
        dt = DiscoveryTracker()
        dt.force_stop = True
        self.assertEqual(dt.convergence_level(), CONVERGENCE_TERMINALIZE)

    def test_progress_prevents_terminalize(self):
        dt = DiscoveryTracker()
        for i in range(5):
            dt.observe("read_workspace_file", {"path": f"{i}.py"}, f"content-{i}")
        self.assertEqual(dt.convergence_level(), CONVERGENCE_NORMAL)
        self.assertEqual(dt.meaningful_progress_count, 5)

    def test_is_duplicate_action(self):
        dt = DiscoveryTracker()
        dt.observe("read_workspace_file", {"path": "a.py"}, "same")
        self.assertFalse(dt.is_duplicate_action("read_workspace_file", {"path": "a.py"}))
        dt.observe("read_workspace_file", {"path": "a.py"}, "same")
        self.assertTrue(dt.is_duplicate_action("read_workspace_file", {"path": "a.py"}))


class RunContextProgressTests(unittest.TestCase):
    def test_runcontext_delegates(self):
        rc = RunContext(run_id="r1")
        self.assertEqual(rc.convergence_level(), CONVERGENCE_NORMAL)
        rc.note_progress("read_workspace_file", {"path": "a.py"}, "x")
        summary = rc.progress_summary()
        self.assertEqual(summary["meaningful_progress_count"], 1)
        self.assertEqual(summary["convergence_level"], CONVERGENCE_NORMAL)

    def test_runcontext_converges_on_repeats(self):
        rc = RunContext(run_id="r1")
        for _ in range(4):
            rc.note_progress("read_workspace_file", {"path": "a.py"}, "same")
        self.assertEqual(rc.convergence_level(), CONVERGENCE_TERMINALIZE)


if __name__ == "__main__":
    unittest.main()
