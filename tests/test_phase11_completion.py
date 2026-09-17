"""Phase 11 —— Completion Eligibility + Capability + Instrumentation（确定性）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.completion import extract_obligations  # noqa: E402
from runtime.readiness_gate import canonical_target_of  # noqa: E402
from runtime.runctx import RunContext  # noqa: E402
from runtime.spec import capability_of  # noqa: E402


class CapabilityTests(unittest.TestCase):
    def test_capability_classification(self):
        self.assertEqual(capability_of("read_workspace_file"), "READ")
        self.assertEqual(capability_of("list_workspace_files"), "DISCOVERY")
        self.assertEqual(capability_of("edit_project_file"), "MUTATION")
        self.assertEqual(capability_of("run_tests"), "VERIFICATION")
        self.assertEqual(capability_of("run_python"), "CODE_EXECUTION")
        self.assertEqual(capability_of("code_loop"), "MUTATION_VERIFICATION_LOOP")
        self.assertEqual(capability_of("web_search"), "EXTERNAL_FACT")
        self.assertEqual(capability_of("remember"), "MEMORY")

    def test_canonical_target_none_when_unresolvable(self):
        self.assertIsNone(canonical_target_of("read_workspace_file", {}))
        self.assertEqual(canonical_target_of("read_workspace_file", {"path": "A.PY"}), "a.py")
        self.assertEqual(canonical_target_of("search_documents", {"query": "Token Refresh"}),
                         "query:token refresh")


class ObligationTests(unittest.TestCase):
    def test_extract_obligations(self):
        self.assertEqual(extract_obligations("修复这个 bug 并跑测试"),
                         {"mutation": "required", "verification": "required"})
        self.assertEqual(extract_obligations("介绍一下你自己"),
                         {"mutation": "not_required", "verification": "not_required"})


class CompletionEligibilityTests(unittest.TestCase):
    def _rc(self, text="修复这个 bug 并跑测试"):
        return RunContext(run_id="r1", request_text=text)

    def test_eligible_after_mutation_and_latest_verification(self):
        rc = self._rc()
        rc.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
        rc.note_execution_identity("edit_project_file", {"path": "a.py"})  # epoch 0→1
        rc.note_progress("run_python", {"filename": "t.py"}, "退出码: 0")  # verified=1
        elig = rc.completion_eligibility()
        self.assertTrue(elig["eligible"], elig["reasons"])
        self.assertTrue(elig["reasons"]["latest_revision_verified"])

    def test_new_mutation_invalidates_old_pass(self):
        rc = self._rc()
        rc.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
        rc.note_execution_identity("edit_project_file", {"path": "a.py"})  # epoch 1
        rc.note_progress("run_python", {"filename": "t.py"}, "退出码: 0")  # verified=1
        self.assertTrue(rc.completion_eligibility()["eligible"])
        rc.note_progress("edit_project_file", {"path": "b.py"}, "已修改 b.py")
        rc.note_execution_identity("edit_project_file", {"path": "b.py"})  # epoch 2
        elig = rc.completion_eligibility()
        self.assertFalse(elig["eligible"])
        self.assertFalse(elig["reasons"]["latest_revision_verified"])

    def test_pending_user_not_eligible(self):
        rc = self._rc()
        rc.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
        rc.note_execution_identity("edit_project_file", {"path": "a.py"})
        rc.note_progress("run_python", {"filename": "t.py"}, "退出码: 0")
        rc.enter_needs_user_input(["还需要什么？"])
        elig = rc.completion_eligibility()
        self.assertFalse(elig["eligible"])
        self.assertTrue(elig["reasons"]["pending_user"])

    def test_no_obligation_requires_both(self):
        rc = self._rc(text="你好")
        rc.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
        rc.note_execution_identity("edit_project_file", {"path": "a.py"})
        # 无义务但只有 mutation、没有 verification → 保守不 eligible
        self.assertFalse(rc.completion_eligibility()["eligible"])
        rc.note_progress("run_python", {"filename": "t.py"}, "退出码: 0")
        self.assertTrue(rc.completion_eligibility()["eligible"])

    def test_explainability_reasons_present(self):
        rc = self._rc()
        elig = rc.completion_eligibility()
        for key in ("mutation_requirement", "verification_requirement",
                    "latest_revision_verified", "pending_user", "execution_evidence"):
            self.assertIn(key, elig["reasons"])


if __name__ == "__main__":
    unittest.main()
