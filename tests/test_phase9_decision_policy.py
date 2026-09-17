"""Phase 9 —— Exploration Saturation + Evidence-Driven Decision Hint（确定性）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.evidence_analysis import primary_stage  # noqa: E402
from runtime.readiness_gate import DiscoveryTracker  # noqa: E402
from runtime.runctx import RunContext  # noqa: E402


class SaturationTests(unittest.TestCase):
    def test_repeated_reads_saturate(self):
        dt = DiscoveryTracker()
        for _ in range(3):
            dt.observe("read_workspace_file", {"path": "a.py"}, "same content")
        self.assertTrue(dt.saturated())
        self.assertIsNotNone(dt.decision_hint())

    def test_new_evidence_prevents_saturation(self):
        dt = DiscoveryTracker()
        for i in range(4):
            dt.observe("read_workspace_file", {"path": f"f{i}.py"}, f"content-{i}")
        self.assertFalse(dt.saturated())
        self.assertIsNone(dt.decision_hint())

    def test_mutation_then_discovery_saturates(self):
        dt = DiscoveryTracker()
        dt.observe("edit_project_file", {"path": "a.py", "new": "x"}, "已修改 a.py")
        for i in range(3):
            dt.observe("list_workspace_files", {"directory": f"d{i}"}, f"listing-{i}")
        self.assertGreaterEqual(dt.post_mutation_discovery, 3)
        self.assertTrue(dt.saturated())

    def test_verification_passed_hint_finalizes(self):
        dt = DiscoveryTracker()
        dt.observe("run_python", {"filename": "t.py"}, "15 passed\n退出码: 0")
        dt.observe("list_workspace_files", {"directory": "a"}, "x")
        dt.observe("list_workspace_files", {"directory": "b"}, "y")
        self.assertTrue(dt.verification_passed)
        hint = dt.decision_hint()
        self.assertIsNotNone(hint)
        self.assertIn("验证已通过", hint)

    def test_runcontext_delegates_hint(self):
        rc = RunContext(run_id="r1")
        self.assertIsNone(rc.decision_hint())
        for _ in range(3):
            rc.note_progress("read_workspace_file", {"path": "a.py"}, "same")
        self.assertIsNotNone(rc.decision_hint())
        self.assertIn("low_novelty_streak", rc.saturation_summary())


class StageMetricTests(unittest.TestCase):
    def test_exclusive_primary_stage(self):
        self.assertEqual(primary_stage("run_python"), "VERIFICATION")
        self.assertEqual(primary_stage("edit_project_file"), "MUTATION")
        self.assertEqual(primary_stage("read_workspace_file"), "READ")
        self.assertEqual(primary_stage("list_workspace_files"), "DISCOVERY")
        self.assertEqual(primary_stage("calculate"), "OTHER")


if __name__ == "__main__":
    unittest.main()
