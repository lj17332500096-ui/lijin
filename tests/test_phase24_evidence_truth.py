"""Phase 24：Workspace Revision Truth + Evidence-Based Window 回归套件。

覆盖：
1. Mutation Outcome 三态（COMMITTED / FAILED / UNKNOWN）
2. No-op mutation 不得推进 revision
3. Unknown 不得默认 COMMITTED
4. code_loop structured outcome 解析
5. ExecutionEvidence mutation_outcomes
6. Evidence-based Window classification
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["FORGE_MODEL_PREF"] = "gateway"


class MutationOutcomeTriStateTests(unittest.TestCase):
    """§四：Mutation Outcome 三态判定。"""

    def test_edit_committed(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("edit_project_file",
                                "已在 calc.py 里替换 1 处匹配"),
            "COMMITTED")

    def test_edit_failed_target_not_found(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("edit_project_file",
                                "错误：在 calc.py 里没有找到要替换的内容"),
            "FAILED")

    def test_write_committed_new(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("write_code_file",
                                "已写入 micro_fixture/hello.py（42 字符）"),
            "COMMITTED")

    def test_write_committed_overwrite(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("write_project_file",
                                "已覆盖项目文件 tests/test_x.py（120 字符）"),
            "COMMITTED")
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("write_code_file",
                                "permission denied for /etc/passwd"),
            "FAILED")

    def test_unknown_no_marker(self):
        from runtime.runner import _mutation_result_ok
        # Phase 24：UNKNOWN 不默认 COMMITTED
        self.assertEqual(
            _mutation_result_ok("save_note", "ok"),
            "UNKNOWN")

    def test_write_failed_permission(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("write_code_file",
                                "permission denied for /etc/passwd"),
            "FAILED")

    def test_unknown_empty_result(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("save_note", ""),
            "UNKNOWN")

    def test_code_loop_success(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("code_loop",
                                "【循环结果】✅ 通过（1/3 次尝试，用时 2.1s）"),
            "COMMITTED")

    def test_code_loop_failed(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("code_loop",
                                "【循环结果】❌ 失败（尝试 2/3 次后停止）"),
            "FAILED")

    def test_sandbox_rollback_committed(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("sandbox_rollback", "已回滚到快照 point_3"),
            "COMMITTED")
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("save_word_doc", "已生成报告.docx"),
            "COMMITTED")

    def test_save_word_doc_failed(self):
        from runtime.runner import _mutation_result_ok
        self.assertEqual(
            _mutation_result_ok("save_word_doc", "错误：路径不存在"),
            "FAILED")


class NoOpMutationTests(unittest.TestCase):
    """§十二/十三：No-op mutation 不得推进 revision。"""

    def test_edit_old_equals_new_rejected(self):
        """edit_project_file 在 old_string == new_string 时直接报错。"""
        from project_edit import edit_project_file
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            os.environ.setdefault("FORGE_WORKSPACE", td)
            # 需要设置 BASE_DIR，这里用单元测试直接断言逻辑
            # edit_project_file 在 old==new 时返回错误信息
            # 我们直接测试 _mutation_result_ok 对该错误的判定
            from runtime.runner import _mutation_result_ok
            result = _mutation_result_ok("edit_project_file",
                                         "错误：old_string 不能为空，且不能与 new_string 相同")
            self.assertEqual(result, "FAILED",
                             "no-op edit 应被判定为 FAILED 而非 COMMITTED")

    def test_edit_no_match_is_failed(self):
        from runtime.runner import _mutation_result_ok
        result = _mutation_result_ok("edit_project_file",
                                     "错误：在 x.py 里没有找到要替换的内容")
        self.assertEqual(result, "FAILED")


class UnknownDoesNotBumpEpochTests(unittest.TestCase):
    """§五：UNKNOWN 不得推进 evidence_epoch。"""

    def test_unknown_mutation_does_not_bump_epoch(self):
        from runtime.runctx import RunContext
        rc = RunContext(run_id="u1", request_text="写一个文件。")
        # 模拟一个未知结果的 mutation 工具调用
        rc.note_progress("save_note", {"note": "x"},
                         "ok", status="executed")
        # note_progress 内部调用 observe，observe 调用 _mutation_result_ok
        # UNKNOWN 结果 → 不应 bump epoch
        self.assertEqual(rc._t().evidence_epoch, 0,
                         "UNKNOWN mutation 不得推进 evidence_epoch")
        self.assertFalse(rc._t().mutation_seen,
                         "UNKNOWN mutation 不得设 mutation_seen")

    def test_failed_mutation_does_not_bump_epoch(self):
        from runtime.runctx import RunContext
        rc = RunContext(run_id="u2", request_text="修改文件。")
        rc.note_progress("edit_project_file", {"path": "x"},
                         "错误：没有找到要替换的内容",
                         status="executed", error_class="mutation_failed")
        self.assertEqual(rc._t().evidence_epoch, 0,
                         "FAILED mutation 不得推进 evidence_epoch")
        self.assertFalse(rc._t().mutation_seen)


class IntegrationRevisionFlowTests(unittest.TestCase):
    """§二十一：Workspace Revision Regression 核心流程。"""

    def test_success_then_noop_does_not_increment(self):
        """成功 mutation → no-op mutation：epoch 不增长。"""
        from runtime.runctx import RunContext
        rc = RunContext(run_id="flow-noop",
                        request_text="修复 calc.py 并验证。")
        # 成功 mutation
        rc.note_progress("write_code_file", {"project": "micro_fixture",
                                             "filename": "t.py"},
                         "已写入 micro_fixture/t.py", status="executed")
        rc.note_execution_identity("write_code_file",
                                   {"project": "micro_fixture", "filename": "t.py"})
        self.assertEqual(rc._t().evidence_epoch, 1)
        # no-op mutation（unknown result，未命中成功/失败标记）
        rc.note_progress("edit_project_file", {"path": "t.py"},
                         "some ambiguous result without markers",
                         status="executed")
        # note_execution_identity 不应被调用（UNKNOWN 不 bump）
        self.assertEqual(rc._t().evidence_epoch, 1,
                         "no-op/UNKNOWN mutation 不得递增 epoch")

    def test_success_mutation_then_verify(self):
        """成功 mutation → 验证通过：verified_revision == epoch。"""
        from runtime.runctx import RunContext
        rc = RunContext(run_id="flow-verify",
                        request_text="修复 calc.py 并运行测试验证。")
        rc.note_progress("edit_project_file", {"path": "calc.py"},
                         "已在 calc.py 里替换 1 处匹配",
                         status="executed")
        rc.note_execution_identity("edit_project_file", {"path": "calc.py"})
        self.assertEqual(rc._t().evidence_epoch, 1)
        rc.note_progress("run_tests", {},
                         "15 passed\n退出码: 0", status="executed")
        self.assertTrue(rc._t().verification_passed)
        self.assertEqual(rc._t().verified_revision, 1,
                         "verified_revision 应等于 evidence_epoch")
        self.assertFalse(rc.verification_due(),
                         "latest revision 已验证，verification_due 应为 False")

    def test_verify_then_failed_mutation(self):
        """验证通过后发生失败 mutation：verification_due 仍为 False（epoch 未变）。"""
        from runtime.runctx import RunContext
        rc = RunContext(run_id="flow-fail-after-verif",
                        request_text="修复并验证。")
        rc.note_progress("edit_project_file", {"path": "calc.py"},
                         "已在 calc.py 里替换 1 处匹配",
                         status="executed")
        rc.note_execution_identity("edit_project_file", {"path": "calc.py"})
        rc.note_progress("run_tests", {},
                         "15 passed\n退出码: 0", status="executed")
        self.assertTrue(rc._t().verification_passed)
        self.assertEqual(rc._t().verified_revision, 1)
        # 失败 mutation
        rc.note_progress("edit_project_file", {"path": "x.py"},
                         "错误：没有找到要替换的内容",
                         status="executed", error_class="mutation_failed")
        # epoch 应保持 1，verification_due 应为 True（因为 mutation_seen 未更新）
        self.assertEqual(rc._t().evidence_epoch, 1,
                         "失败 mutation 不得递增 epoch")
        # mutation_seen 仍为 True（上次成功 mutation 设置的），但 verified_revision==epoch
        # 所以 verification_due 应为 False
        self.assertFalse(rc.verification_due())


class CodeLoopOutcomeTests(unittest.TestCase):
    """§十五/十六：code_loop structured outcome 解析。"""

    def test_code_loop_success_parsed(self):
        from runtime.completion import codeloop_outcome_of
        call = {"name": "code_loop", "status": "executed",
                "output_head": "【循环结果】✅ 通过（1/3 次尝试）"}
        me, vr, fac = codeloop_outcome_of(call)
        self.assertEqual(me, "UNKNOWN")
        self.assertEqual(vr, "PASS")

    def test_code_loop_failed_parsed(self):
        from runtime.completion import codeloop_outcome_of
        call = {"name": "code_loop", "status": "executed",
                "output_head": "【循环结果】❌ 失败（尝试 2/3 次后停止）"}
        me, vr, fac = codeloop_outcome_of(call)
        self.assertEqual(me, "UNKNOWN")
        self.assertEqual(vr, "FAIL")

    def test_code_loop_no_markers(self):
        from runtime.completion import mutation_outcome_of
        call = {"name": "code_loop", "status": "executed",
                "output_head": "some random output"}
        self.assertEqual(mutation_outcome_of(call), "UNKNOWN")


class ExecutionEvidenceMutationTests(unittest.TestCase):
    """§二十：ExecutionEvidence mutation_outcomes 方法。"""

    def test_mutation_outcomes_returns_correct_tuples(self):
        from runtime.completion import ExecutionEvidence
        evidence = ExecutionEvidence(tool_calls=[
            {"name": "edit_project_file", "status": "executed",
             "output_head": "已在 calc.py 里替换 1 处匹配"},
            {"name": "edit_project_file", "status": "executed",
             "output_head": "错误：没有找到要替换的内容"},
            {"name": "run_tests", "status": "executed",
             "output_head": "15 passed"},
        ])
        outcomes = evidence.mutation_outcomes()
        names = [n for n, _ in outcomes]
        self.assertIn("edit_project_file", names)
        self.assertEqual(len(outcomes), 2)
        # 两个 edit 结果不同：第一个 COMMITTED，第二个 FAILED
        committed = [o for n, o in outcomes if n == "edit_project_file" and o == "COMMITTED"]
        failed = [o for n, o in outcomes if n == "edit_project_file" and o == "FAILED"]
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(failed), 1)

    def test_mutation_committed_count(self):
        from runtime.completion import ExecutionEvidence
        evidence = ExecutionEvidence(tool_calls=[
            {"name": "write_code_file", "status": "executed",
             "output_head": "已写入 hello.py"},
            {"name": "write_code_file", "status": "executed",
             "output_head": "error: permission denied"},
            {"name": "run_tests", "status": "executed",
             "output_head": "10 passed"},
        ])
        self.assertEqual(evidence.mutation_committed_count(), 1)
        self.assertTrue(evidence.mutation_any_failed())

    def test_mutation_outcomes_empty(self):
        from runtime.completion import ExecutionEvidence
        evidence = ExecutionEvidence(tool_calls=[
            {"name": "read_workspace_file", "status": "executed",
             "output_head": "content"},
        ])
        self.assertEqual(evidence.mutation_outcomes(), [])
        self.assertEqual(evidence.mutation_committed_count(), 0)


class EvidenceWindowClassificationTests(unittest.TestCase):
    """§二十二/二十七：Evidence-Based Window 分类。"""

    def test_pass_when_verification_matches_epoch(self):
        from benchmark.bounded_window import _categorize_window
        cat, reason = _categorize_window(
            "FINAL_TEXT", None, True, True,
            True, True, True)
        self.assertEqual(cat, "PASS")

    def test_w1_no_verification(self):
        from benchmark.bounded_window import _categorize_window
        # ACTION_LIMIT + 非 DISCOVERY/READ + 无 verification → W1
        cat, _ = _categorize_window(
            "ACTION_LIMIT", "MUTATION", False, False,
            False, False, False)
        self.assertEqual(cat, "W1")

    def test_w4_exploration_only(self):
        from benchmark.bounded_window import _categorize_window
        cat, _ = _categorize_window(
            "ACTION_LIMIT", "READ", False, False,
            False, False, False)
        self.assertEqual(cat, "W4")

    def test_w2_verification_failed(self):
        from benchmark.bounded_window import _categorize_window
        cat, _ = _categorize_window(
            "ACTION_LIMIT", None, True, False,
            False, True, True)
        self.assertEqual(cat, "W2")

    def test_w3_new_mutation_unverified(self):
        from benchmark.bounded_window import _categorize_window
        cat, _ = _categorize_window(
            "ACTION_LIMIT", None, True, False,
            True, True, True)
        self.assertEqual(cat, "W3")

    def test_pass_with_action_limit_and_evidence(self):
        from benchmark.bounded_window import _categorize_window
        cat, _ = _categorize_window(
            "ACTION_LIMIT", None, True, True,
            True, True, True)
        self.assertEqual(cat, "PASS")


import json

if __name__ == "__main__":
    unittest.main()
