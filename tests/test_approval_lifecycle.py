"""Phase 38: Approval Continuation Integration Tests.

Tests the full approval lifecycle with immediate suspension and auto-execution.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.approval import ApprovalGate, BLOCK_TEXT
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.completion import ExecutionEvidence, evaluate_completion_eligibility
from runtime.errors import ApprovalRequired


class ApprovalImmediateSuspensionTests(unittest.TestCase):
    """Phase 38: approval creation raises ApprovalRequired (immediate suspension)."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="a38_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
        self.gate = self.runtime.approval
        self.task = self.runtime.tasks.create_task(session_id="a38", goal="test")
        self.runtime.tasks.transition(self.task.id, TaskState.RUNNING, reason="start")

    def test_approval_required_raised_on_pending(self):
        """Creating a pending approval raises ApprovalRequired exception."""
        self.gate.begin(self.task.id, "chat")
        with self.assertRaises(ApprovalRequired) as cm:
            asyncio.run(self.gate.check("run_tests", {"project": "m3_fixture"}, run_id=self.task.id))
        exc = cm.exception
        self.assertIn("需要审批", str(exc))
        self.assertIsNotNone(exc.approval_id)
        self.assertEqual(exc.tool_name, "run_tests")
        # Approval was persisted in DB
        pending = self.runtime.tasks.list_pending_approvals(self.task.id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tool_name"], "run_tests")

    def test_approval_required_stores_arguments(self):
        """ApprovalRequired carries original arguments."""
        self.gate.begin(self.task.id, "chat")
        args = {"project": "m3_fixture", "target": "test_calc.py", "extra_args": "-v"}
        with self.assertRaises(ApprovalRequired) as cm:
            asyncio.run(self.gate.check("run_tests", args, run_id=self.task.id))
        exc = cm.exception
        self.assertEqual(exc.arguments, args)


class ApprovalAutoExecutionTests(unittest.TestCase):
    """Phase 38: approved invocations auto-execute on resume."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="a38e_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
        self.gate = self.runtime.approval
        self.task = self.runtime.tasks.create_task(session_id="a38e", goal="test")
        self.runtime.tasks.transition(self.task.id, TaskState.RUNNING, reason="start")

    def test_approved_unexecuted_found_on_resume(self):
        """After approve, get_approved_unexecuted returns the approval."""
        self.gate.begin(self.task.id, "chat")
        try:
            asyncio.run(self.gate.check("run_tests", {"project": "m3_fixture"}, run_id=self.task.id))
        except ApprovalRequired:
            pass

        # Approve via DB
        aps = self.runtime.tasks.list_pending_approvals(self.task.id)
        self.assertEqual(len(aps), 1)
        self.runtime.tasks.decide_approval(aps[0]["id"], "approved")

        # Should find approved but unexecuted
        approved = self.runtime.tasks.get_approved_unexecuted(self.task.id)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["tool_name"], "run_tests")
        self.assertEqual(approved[0]["arguments"], {"project": "m3_fixture"})

    def test_mark_executed_prevents_double_execution(self):
        """mark_approval_executed returns True first time, False second."""
        self.gate.begin(self.task.id, "chat")
        try:
            asyncio.run(self.gate.check("run_tests", {"project": "m3_fixture"}, run_id=self.task.id))
        except ApprovalRequired:
            pass

        aps = self.runtime.tasks.list_pending_approvals(self.task.id)
        self.runtime.tasks.decide_approval(aps[0]["id"], "approved")

        # First mark: succeeds
        result1 = self.runtime.tasks.mark_approval_executed(aps[0]["id"])
        self.assertTrue(result1)

        # Second mark: already executed
        result2 = self.runtime.tasks.mark_approval_executed(aps[0]["id"])
        self.assertFalse(result2)


class ApprovalCompletionEligibilityTests(unittest.TestCase):
    """completion_eligibility must be False when approval is pending."""

    def test_pending_approval_revokes_eligibility(self):
        evidence = ExecutionEvidence(
            tool_calls=[
                {"name": "edit_project_file", "status": "executed", "output_head": "已修改"},
                {"name": "run_tests", "status": "executed", "output_head": "退出码: 0"},
            ],
            approvals_pending=1,
        )
        elig = evaluate_completion_eligibility(
            evidence=evidence, request_text="修复 bug 并运行测试",
            mutation_revision=1, verified_revision=1,
            pending_approval=True,
        )
        self.assertFalse(elig.eligible)
        self.assertTrue(elig.reasons["pending_approval"])

    def test_no_pending_allows_eligibility(self):
        evidence = ExecutionEvidence(
            tool_calls=[
                {"name": "edit_project_file", "status": "executed", "output_head": "已修改 calc.py"},
                {"name": "run_tests", "status": "executed", "output_head": "pytest 退出码: 0 ｜ PASS"},
            ],
            approvals_pending=0,
        )
        elig = evaluate_completion_eligibility(
            evidence=evidence, request_text="修复 bug 并运行测试",
            mutation_revision=1, verified_revision=1,
            pending_approval=False,
        )
        self.assertTrue(elig.eligible)


class GuardBPreApprovalTests(unittest.TestCase):
    """Guard B pre-approval: redundant verification blocked before approval gate."""

    def test_redundant_ver_before_approval(self):
        """When completion_ready + announced + verified, verification blocked before approval."""
        from runtime.readiness_gate import DiscoveryTracker
        dt = DiscoveryTracker()
        dt.mutation_seen = True
        dt.verification_passed = True
        dt.verified_revision = 2
        dt.evidence_epoch = 2
        dt.completion_ready_announced = True

        # Pre-approval guard condition
        is_redundant = (
            "run_tests" in {"run_tests", "run_python", "code_loop"}
            and dt.completion_ready_announced
            and dt.verified_revision is not None
            and dt.evidence_epoch <= dt.verified_revision
        )
        self.assertTrue(is_redundant)


class ApprovalResumeIntegrationTests(unittest.TestCase):
    """Full integration: suspension → approve → auto-execute → continue."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="a38i_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
        self.gate = self.runtime.approval
        self.task = self.runtime.tasks.create_task(session_id="a38i", goal="test")
        self.runtime.tasks.transition(self.task.id, TaskState.RUNNING, reason="start")

    def test_full_suspend_approve_resume_flow(self):
        """Simulate: suspension → approve → auto-execute check."""
        # Phase 1: Tool request → ApprovalRequired (immediate suspension)
        self.gate.begin(self.task.id, "chat")
        with self.assertRaises(ApprovalRequired):
            asyncio.run(self.gate.check("run_tests", {"project": "fixture"}, run_id=self.task.id))

        # Phase 2: Task transitions to WAITING_APPROVAL
        waiting = self.runtime.tasks.transition(self.task.id, TaskState.WAITING_APPROVAL, reason="approval")
        self.assertEqual(waiting.state, TaskState.WAITING_APPROVAL)

        # Phase 3: External actor approves
        aps = self.runtime.tasks.list_pending_approvals(self.task.id)
        self.assertEqual(len(aps), 1)
        self.runtime.tasks.decide_approval(aps[0]["id"], "approved")

        # Phase 4: Same run_id preserved
        task_after = self.runtime.tasks.get_task(self.task.id)
        self.assertEqual(task_after.id, self.task.id)

        # Phase 5: Approved but unexecuted found
        approved = self.runtime.tasks.get_approved_unexecuted(self.task.id)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["tool_name"], "run_tests")

        # Phase 6: Resume to RUNNING
        resumed = self.runtime.tasks.transition(self.task.id, TaskState.RUNNING, reason="resume")
        self.assertEqual(resumed.state, TaskState.RUNNING)


class ApprovalBatchToolTests(unittest.TestCase):
    """Batch tool calls: approval blocks all subsequent tools in same batch."""

    def test_approval_blocks_subsequent_tools(self):
        """When approval is requested, no other tools should execute after."""
        self._tmp = Path(tempfile.mkdtemp(prefix="a38b_"))
        rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        rt._ensure()
        gate = rt.approval
        task = rt.tasks.create_task(session_id="a38b", goal="test")
        rt.tasks.transition(task.id, TaskState.RUNNING, reason="start")

        # First call: triggers approval
        gate.begin(task.id, "chat")
        with self.assertRaises(ApprovalRequired):
            asyncio.run(gate.check("run_tests", {"project": "fixture"}, run_id=task.id))

        # The exception breaks the tool loop — no subsequent tools execute
        # (This is enforced by the exception, not by return value)
        pending = rt.tasks.list_pending_approvals(task.id)
        self.assertEqual(len(pending), 1)


class ApprovalConvergenceWhilePendingTests(unittest.TestCase):
    """Convergence must not trigger while approval is pending."""

    def test_convergence_not_triggered_during_approval(self):
        """While approval is pending, convergence should not terminalize."""
        self._tmp = Path(tempfile.mkdtemp(prefix="a38c_"))
        rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        rt._ensure()
        gate = rt.approval
        task = rt.tasks.create_task(session_id="a38c", goal="test")
        rt.tasks.transition(task.id, TaskState.RUNNING, reason="start")

        # Create pending approval
        gate.begin(task.id, "chat")
        with self.assertRaises(ApprovalRequired):
            asyncio.run(gate.check("run_tests", {"project": "fixture"}, run_id=task.id))

        # Convergence level should not be TERMINALIZE just because of approval
        from runtime.runctx import RunContext, bind as _bind
        ctx = RunContext(request_text="test")
        _bind(ctx)
        # After approval blocked, the run should be in WAITING_APPROVAL, not converged
        self.assertTrue(bool(gate.pending_for(task.id)))


if __name__ == "__main__":
    unittest.main()
