"""Phase 23：Mutation Commit Truth 回归套件。

验证：
- mutation attempt ≠ mutation commit
- 失败 mutation 不得推进 revision / 不得创建 verification_due
- 成功 mutation 后才推进 evidence_epoch / mutation_seen
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["FORGE_MODEL_PREF"] = "gateway"


class MutationProgressSignalTests(unittest.TestCase):
    """直接测试 DiscoveryTracker.observe() 的 mutation 语义。"""

    def _make_tracker(self):
        from runtime.readiness_gate import DiscoveryTracker
        return DiscoveryTracker()

    def test_mutate_fail_does_not_set_seen(self):
        t = self._make_tracker()
        t.observe("edit_project_file", {"path": "x"},
                  "错误：没有找到要替换的内容", status="executed")
        self.assertFalse(t.mutation_seen,
                         "失败 edit 结果不得设 mutation_seen")

    def test_mutate_success_sets_seen(self):
        t = self._make_tracker()
        t.observe("edit_project_file", {"path": "x"},
                  "已在 x 里替换 1 处匹配", status="executed")
        self.assertTrue(t.mutation_seen,
                        "成功 edit 结果应设 mutation_seen")

    def test_mutate_error_status_skips_seen(self):
        t = self._make_tracker()
        t.observe("edit_project_file", {"path": "x"},
                  "timeout", status="error", error_class="mutation_failed")
        self.assertFalse(t.mutation_seen)

    def test_read_tool_does_not_set_seen(self):
        t = self._make_tracker()
        t.observe("read_workspace_file", {"path": "x"}, "content", status="executed")
        self.assertFalse(t.mutation_seen)

    def test_verify_tool_independent(self):
        t = self._make_tracker()
        t.observe("run_tests", {}, "15 passed\n退出码: 0", status="executed")
        self.assertFalse(t.mutation_seen)
        self.assertTrue(t.verification_passed)


class MutationSuccessPredicateTests(unittest.TestCase):
    """测试 _mutation_result_ok 的 per-tool 判断逻辑。"""

    def test_edit_failure_markers(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(_mutation_result_ok("edit_project_file",
                      "错误：在 calc.py 里没有找到要替换的内容"), "FAILED")
        self.assertEqual(_mutation_result_ok("edit_project_file",
                      "not found matching text"), "FAILED")

    def test_edit_success_markers(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(_mutation_result_ok("edit_project_file",
                      "已在 micro_fixture/calc.py 里替换 1 处匹配"), "COMMITTED")
        self.assertEqual(_mutation_result_ok("edit_project_file",
                      "已修改 1 处"), "COMMITTED")

    def test_write_success(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(_mutation_result_ok("write_code_file",
                      "已写入 micro_fixture/hello.py（42 字符）"), "COMMITTED")

    def test_write_failure(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(_mutation_result_ok("write_code_file",
                      "错误：路径不存在"), "FAILED")

    def test_unknown_result_conservative(self):
        from runtime.runner import _mutation_result_ok
        # Phase 24：无明确标记 → UNKNOWN（不再默认 COMMITTED）
        self.assertEqual(_mutation_result_ok("save_note", "ok"), "UNKNOWN")

    def test_permission_denied(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(_mutation_result_ok("write_code_file",
                      "permission denied"), "FAILED")

    def test_exception_text(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(_mutation_result_ok("edit_project_file",
                      "exception: file lock held"), "FAILED")


class IntegrationMutationFlowTests(unittest.TestCase):
    """端到端：直接调用 RunContext 方法验证 mutation 语义。"""

    def test_success_then_fail_does_not_bump_epoch(self):
        from runtime.runctx import RunContext
        rc = RunContext(run_id="flow1", request_text="修复 calc.py 并运行测试验证。")
        # 成功 mutation
        rc.note_progress("write_code_file", {"project": "micro_fixture",
                                              "filename": "t.py"},
                         "已写入 micro_fixture/t.py", status="executed")
        rc.note_execution_identity("write_code_file",
                                   {"project": "micro_fixture", "filename": "t.py"})
        self.assertTrue(rc._t().mutation_seen)
        self.assertEqual(rc._t().evidence_epoch, 1)
        self.assertTrue(rc.verification_due())
        # 失败 mutation（模拟）
        rc.note_progress("edit_project_file", {"path": "calc.py"},
                         "错误：没有找到要替换的内容",
                         status="executed", error_class="mutation_failed")
        # note_execution_identity 不应被调用 → epoch 保持不变
        self.assertEqual(rc._t().evidence_epoch, 1,
                         "失败 mutation 不得 bump epoch")
        self.assertTrue(rc.verification_due(),
                        "epoch=1 仍要求验证")

    def test_verify_before_mutate_no_due(self):
        from runtime.runctx import RunContext
        rc = RunContext(run_id="flow2",
                        request_text="运行测试验证 calc.py 的 add 函数。")
        rc.note_progress("run_tests", {"project": "micro_fixture"},
                         "15 passed\n退出码: 0", status="executed")
        self.assertFalse(rc._t().mutation_seen)
        self.assertFalse(rc.verification_due(),
                         "无 mutation 时 verification_due 应为 False")

    def test_read_only_no_mutation_seen(self):
        from runtime.runctx import RunContext
        rc = RunContext(run_id="flow3", request_text="只读 calc.py。")
        rc.note_progress("read_workspace_file", {"path": "calc.py"},
                         "def add(a,b): return a-b", status="executed")
        self.assertFalse(rc._t().mutation_seen)
        self.assertEqual(rc._t().evidence_epoch, 0)


import json


if __name__ == "__main__":
    unittest.main()
