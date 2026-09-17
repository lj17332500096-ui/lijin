"""Phase 26：code_loop Dual Outcome（mutation_effect + verification_result）回归套件。

覆盖：
- unchanged + pass → mutation=UNCHANGED, verify=PASS
- changed + pass → mutation=CHANGED, verify=PASS
- changed + fail → mutation=CHANGED, verify=FAIL
- unchanged + fail → mutation=UNCHANGED, verify=FAIL
- missing marker → mutation=UNKNOWN, verify from keyword
- ExecutionEvidence.codeloop_outcomes()
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["FORGE_MODEL_PREF"] = "gateway"


class CodeLoopDualOutcomeTests(unittest.TestCase):
    """§六/七：code_loop 拆分为 mutation_effect + verification_result。"""

    def _call(self, output_head: str) -> dict:
        return {"name": "code_loop", "status": "executed", "output_head": output_head}

    def test_unchanged_pass(self):
        """首次运行即通过，无内部写操作 → UNCHANGED + PASS。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】✅ 通过（1/3 次尝试，用时 1.2s）\n"
            "[Phase24-outcome] workspace_changed=false verification_passed=true"))
        self.assertEqual(me, "UNCHANGED")
        self.assertEqual(vr, "PASS")
        self.assertIsNone(fac, "无写入时 final_after_change 应为 None")

    def test_changed_pass(self):
        """修复后通过，有内部写操作 → CHANGED + PASS + final_after=True。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】✅ 通过（2/3 次尝试，用时 3.5s）\n"
            "[Phase24-outcome] workspace_changed=true verification_passed=true"))
        self.assertEqual(me, "CHANGED")
        self.assertEqual(vr, "PASS")
        self.assertTrue(fac, "changed+pass 应标记 final_verification_after_last_change=True")

    def test_changed_fail(self):
        """修复后仍未通过 → CHANGED + FAIL。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】❌ 失败（尝试 2/3 次后停止）（模型认为无法修复）\n"
            "[Phase24-outcome] workspace_changed=true verification_passed=false"))
        self.assertEqual(me, "CHANGED")
        self.assertEqual(vr, "FAIL")
        self.assertFalse(fac)

    def test_unchanged_fail(self):
        """首次运行即失败且无修复 → UNCHANGED + FAIL。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】❌ 失败（尝试 1/3 次后停止）\n"
            "[Phase24-outcome] workspace_changed=false verification_passed=false"))
        self.assertEqual(me, "UNCHANGED")
        self.assertEqual(vr, "FAIL")
        self.assertIsNone(fac)

    def test_missing_marker_fallback_pass(self):
        """无 Phase24-outcome 标签，回退到关键词 → mutation=UNKNOWN, verify=PASS。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】✅ 通过（1/3 次尝试，用时 1.2s）\n"
            "做了什么：代码 hello.py，经运行验证后通过。"))
        self.assertEqual(me, "UNKNOWN")
        self.assertEqual(vr, "PASS")
        self.assertIsNone(fac)

    def test_missing_marker_fallback_fail(self):
        """无 Phase24-outcome 标签，回退到关键词 → mutation=UNKNOWN, verify=FAIL。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】❌ 失败（达到 3 次上限）\n"
            "做了什么：连续 3 次「运行→自动修复→重跑」仍未通过。"))
        self.assertEqual(me, "UNKNOWN")
        self.assertEqual(vr, "FAIL")
        self.assertIsNone(fac)

    def test_multiple_writes_final_pass(self):
        """多次内部写操作，最终通过 → CHANGED + PASS + final_after=True。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】✅ 通过（3/3 次尝试，用时 8.1s）\n"
            "[Phase24-outcome] workspace_changed=true verification_passed=true"))
        self.assertEqual(me, "CHANGED")
        self.assertEqual(vr, "PASS")
        self.assertTrue(fac)

    def test_multiple_writes_final_fail(self):
        """多次内部写操作，最终未通过 → CHANGED + FAIL。"""
        from runtime.completion import codeloop_outcome_of
        me, vr, fac = codeloop_outcome_of(self._call(
            "【循环结果】❌ 失败（达到 3 次上限）\n"
            "[Phase24-outcome] workspace_changed=true verification_passed=false"))
        self.assertEqual(me, "CHANGED")
        self.assertEqual(vr, "FAIL")
        self.assertFalse(fac)

    def test_simple_mutation_tool_ignores_code_loop_marker(self):
        """简单 mutation tool 不受 code_loop 标签影响。"""
        from runtime.completion import mutation_outcome_of
        # edit_project_file 不应被 code_loop 逻辑影响
        result = mutation_outcome_of({"name": "edit_project_file", "status": "executed",
                                       "output_head": "已在 calc.py 里替换 1 处匹配"})
        self.assertEqual(result, "COMMITTED")
        # code_loop 工具名时应返回 UNKNOWN
        result2 = mutation_outcome_of(self._call("ok"))
        self.assertEqual(result2, "UNKNOWN")


class CodeLoopEvidenceIntegrationTests(unittest.TestCase):
    """Integration：codeloop_outcomes 在 ExecutionEvidence 中的表现。"""

    def test_codeloop_outcomes_with_changed_pass(self):
        from runtime.completion import ExecutionEvidence
        evidence = ExecutionEvidence(tool_calls=[
            {"name": "code_loop", "status": "executed",
             "output_head": "[Phase24-outcome] workspace_changed=true verification_passed=true"},
            {"name": "run_tests", "status": "executed",
             "output_head": "15 passed\n退出码: 0"},
        ])
        outcomes = evidence.codeloop_outcomes()
        self.assertEqual(len(outcomes), 1)
        name, (me, vr, fac) = outcomes[0]
        self.assertEqual(name, "code_loop")
        self.assertEqual(me, "CHANGED")
        self.assertEqual(vr, "PASS")
        self.assertTrue(fac)
        self.assertEqual(evidence.codeloop_changed_count(), 1)
        self.assertEqual(evidence.codeloop_pass_count(), 1)

    def test_codeloop_outcomes_unchanged_pass(self):
        from runtime.completion import ExecutionEvidence
        evidence = ExecutionEvidence(tool_calls=[
            {"name": "code_loop", "status": "executed",
             "output_head": "[Phase24-outcome] workspace_changed=false verification_passed=true"},
        ])
        outcomes = evidence.codeloop_outcomes()
        self.assertEqual(len(outcomes), 1)
        name, (me, vr, fac) = outcomes[0]
        self.assertEqual(me, "UNCHANGED")
        self.assertEqual(vr, "PASS")
        self.assertIsNone(fac)
        self.assertEqual(evidence.codeloop_changed_count(), 0)
        self.assertEqual(evidence.codeloop_pass_count(), 1)

    def test_codeloop_outcomes_changed_fail(self):
        from runtime.completion import ExecutionEvidence
        evidence = ExecutionEvidence(tool_calls=[
            {"name": "code_loop", "status": "executed",
             "output_head": "[Phase24-outcome] workspace_changed=true verification_passed=false"},
        ])
        outcomes = evidence.codeloop_outcomes()
        name, (me, vr, fac) = outcomes[0]
        self.assertEqual(me, "CHANGED")
        self.assertEqual(vr, "FAIL")
        self.assertFalse(fac)
        self.assertEqual(evidence.codeloop_changed_count(), 1)
        self.assertEqual(evidence.codeloop_pass_count(), 0)

    def test_codeloop_no_calls(self):
        from runtime.completion import ExecutionEvidence
        evidence = ExecutionEvidence(tool_calls=[
            {"name": "edit_project_file", "status": "executed",
             "output_head": "已在 calc.py 里替换 1 处匹配"},
            {"name": "run_tests", "status": "executed",
             "output_head": "10 passed\n退出码: 0"},
        ])
        self.assertEqual(evidence.codeloop_outcomes(), [])
        self.assertEqual(evidence.codeloop_changed_count(), 0)
        self.assertEqual(evidence.codeloop_pass_count(), 0)


if __name__ == "__main__":
    unittest.main()
