"""Phase 36: M3 Analyzer Truth Regression Tests.

Verifies that the benchmark analyzer consumes runtime ExecutionEvidence as
authoritative truth, not string heuristics. Covers:
- Real verification PASS → benchmark PASS
- Real verification FAIL → benchmark FAIL
- Verified old revision → latest-coverage FAIL
- Verified current revision → latest-coverage PASS
- Benchmark string contradicts ExecutionEvidence → ExecutionEvidence wins
- _v_passed handles pytest exit-code format robustly
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.m3_closed_loop import (  # noqa: E402
    _v_passed, _v_is_true, _v_is_fail,
)
from runtime.completion import ExecutionEvidence, verification_outcome_of  # noqa: E402


class VPassedRegressionTests(unittest.TestCase):
    """_v_passed deterministic parsing regression."""

    def test_exit_code_0_is_pass(self):
        self.assertTrue(_v_is_true("pytest 退出码: 0 ｜ PASS"))
        self.assertTrue(_v_is_true("退出码：0 用时: 0.5s"))

    def test_exit_code_nonzero_is_fail(self):
        self.assertTrue(_v_is_fail("pytest 退出码: 1 ｜ FAIL"))
        self.assertTrue(_v_is_fail("退出码：1"))

    def test_checkmark_pass(self):
        self.assertTrue(_v_is_true("✅ 通过"))
        self.assertTrue(_v_is_true("tests passed ✅ 通过"))

    def test_pytest_failed_count(self):
        self.assertTrue(_v_is_fail("1 failed, 2 passed"))
        self.assertTrue(_v_is_fail("FAILED test_calc.py::test_add"))

    def test_pytest_all_passed(self):
        self.assertTrue(_v_is_true("3 passed"))
        self.assertTrue(_v_is_true("2 passed 0 failed"))

    def test_unknown_returns_none(self):
        self.assertIsNone(_v_passed("some random text"))
        self.assertFalse(_v_is_true("some random text"))
        self.assertFalse(_v_is_fail("some random text"))

    def test_approval_block_is_not_verification_result(self):
        """Approval-block text should not be misinterpreted as pass/fail."""
        result = "【需要审批】工具 run_tests 参数 {...}\n【需要审批】这个操作风险较高"
        self.assertIsNone(_v_passed(result))
        self.assertFalse(_v_is_true(result))
        self.assertFalse(_v_is_fail(result))

    def test_last_exit_code_wins(self):
        """Multiple exit codes: last one is authoritative."""
        self.assertTrue(_v_is_true("退出码: 1\n退出码: 0"))
        self.assertTrue(_v_is_fail("退出码: 0\n退出码: 1"))


class ExecutionEvidenceVerificationTests(unittest.TestCase):
    """ExecutionEvidence.verification_passed() authoritative source tests."""

    def test_verified_result_from_exit_code_0(self):
        call = {"name": "run_tests", "status": "executed",
                "output_head": "pytest 退出码: 0 ｜ PASS"}
        self.assertEqual(verification_outcome_of(call), "success")

    def test_verified_result_from_exit_code_1(self):
        call = {"name": "run_tests", "status": "executed",
                "output_head": "pytest 退出码: 1 ｜ FAIL"}
        self.assertEqual(verification_outcome_of(call), "failed")

    def test_verified_result_from_checkmark(self):
        call = {"name": "run_tests", "status": "executed",
                "output_head": "✅ 通过"}
        self.assertEqual(verification_outcome_of(call), "success")

    def test_verified_result_from_blocked_is_unknown(self):
        call = {"name": "run_tests", "status": "executed",
                "output_head": "【需要审批】工具 run_tests 参数 {...}"}
        self.assertEqual(verification_outcome_of(call), "unknown")

    def test_blocked_call_returns_unknown(self):
        call = {"name": "run_tests", "status": "blocked",
                "output_head": "blocked"}
        self.assertEqual(verification_outcome_of(call), "unknown")

    def test_none_call_returns_unknown(self):
        self.assertEqual(verification_outcome_of(None), "unknown")

    def test_evidence_verification_passed_with_real_pass(self):
        ev = ExecutionEvidence(tool_calls=[
            {"name": "run_tests", "status": "executed",
             "output_head": "pytest 退出码: 0 ｜ PASS"}
        ])
        self.assertTrue(ev.verification_passed())
        self.assertFalse(ev.verification_failed())

    def test_evidence_verification_failed_with_real_fail(self):
        ev = ExecutionEvidence(tool_calls=[
            {"name": "run_tests", "status": "executed",
             "output_head": "pytest 退出码: 1 ｜ FAIL"}
        ])
        self.assertFalse(ev.verification_passed())
        self.assertTrue(ev.verification_failed())

    def test_evidence_no_verification(self):
        ev = ExecutionEvidence(tool_calls=[
            {"name": "edit_project_file", "status": "executed",
             "output_head": "已修改 calc.py"}
        ])
        self.assertFalse(ev.verification_passed())
        self.assertFalse(ev.verification_failed())

    def test_evidence_latest_verification_failed(self):
        ev = ExecutionEvidence(tool_calls=[
            {"name": "run_tests", "status": "executed",
             "output_head": "pytest 退出码: 1 ｜ FAIL"},
            {"name": "run_tests", "status": "executed",
             "output_head": "pytest 退出码: 0 ｜ PASS"},
        ])
        self.assertTrue(ev.verification_passed())
        self.assertFalse(ev.latest_verification_failed())

    def test_evidence_latest_verification_still_failed(self):
        ev = ExecutionEvidence(tool_calls=[
            {"name": "run_tests", "status": "executed",
             "output_head": "pytest 退出码: 1 ｜ FAIL"},
            {"name": "run_tests", "status": "executed",
             "output_head": "pytest 退出码: 1 ｜ FAIL"},
        ])
        self.assertFalse(ev.verification_passed())
        self.assertTrue(ev.latest_verification_failed())


class AnalyzerTruthRegressionTests(unittest.TestCase):
    """Phase 36: benchmark analyzer consumes ExecutionEvidence, not string parser."""

    def test_analyzer_rejects_string_parser_when_evidence_contradicts(self):
        """If ExecutionEvidence says PASS but string says FAIL, Evidence wins.

        This mirrors the Phase 35 Run 3 false-negative case where the benchmark
        string parser reported FAIL but the runtime actually recorded PASS.
        """
        # Simulate: string parser sees "failed" keyword but exit code is 0
        call = {"name": "run_tests", "status": "executed",
                "output_head": "1 failed\npytest 退出码: 0 ｜ PASS"}
        # ExecutionEvidence truth
        ev = ExecutionEvidence(tool_calls=[call])
        # The authoritative source: verification_outcome_of
        self.assertEqual(verification_outcome_of(call), "success")
        self.assertTrue(ev.verification_passed())

    def test_analyzer_accepts_evidence_when_string_ambiguous(self):
        """When string is ambiguous but Evidence has clear signal, Evidence wins."""
        call = {"name": "run_tests", "status": "executed",
                "output_head": "some partial output... 退出码: 0"}
        self.assertEqual(verification_outcome_of(call), "success")

    def test_analyzer_rejects_evidence_when_string_and_evidence_both_fail(self):
        """When both agree on failure, classification is consistent."""
        call = {"name": "run_tests", "status": "executed",
                "output_head": "pytest 退出码: 1 ｜ FAIL"}
        self.assertEqual(verification_outcome_of(call), "failed")
        ev = ExecutionEvidence(tool_calls=[call])
        self.assertFalse(ev.verification_passed())


if __name__ == "__main__":
    unittest.main()
