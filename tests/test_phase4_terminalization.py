"""Phase 4 —— Terminalization 终态治理单元测试。

覆盖：异常 → 结构化终态原因、终态/暂停态判定、状态一致性检查、
stale run 恢复、以及状态机终态不可再转。
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.errors import BudgetExceeded, FinalResponseFailed  # noqa: E402
from runtime.state_machine import (  # noqa: E402
    TERMINAL_STATES,
    assert_transition,
    can_transition,
)
from runtime.task import TaskState  # noqa: E402
from runtime.terminalization import (  # noqa: E402
    KIND_BOUNDED_FAILURE,
    KIND_CANCELLED,
    KIND_FINAL_RESPONSE_FAILED,
    KIND_PROVIDER_ERROR,
    KIND_TIMEOUT,
    classify_exception,
    consistency_errors,
    is_terminal,
)


class ProviderFailure:
    provider_kind = "auth"
    status_code = 401


class ClassifyExceptionTests(unittest.TestCase):
    def test_provider_error(self):
        tr = classify_exception(ProviderFailure())
        self.assertEqual(tr.kind, KIND_PROVIDER_ERROR)
        self.assertEqual(tr.state, TaskState.FAILED)

    def test_provider_error_by_module(self):
        class OpenAIError(Exception):
            pass

        OpenAIError.__module__ = "openai._exceptions"
        tr = classify_exception(OpenAIError("boom"))
        self.assertEqual(tr.kind, KIND_PROVIDER_ERROR)

    def test_wall_timeout(self):
        tr = classify_exception(BudgetExceeded("任务 x 超过墙钟预算 1800s"))
        self.assertEqual(tr.kind, KIND_TIMEOUT)
        self.assertEqual(tr.state, TaskState.FAILED)

    def test_tool_timeout(self):
        tr = classify_exception(TimeoutError("tool timed out"))
        self.assertEqual(tr.kind, KIND_TIMEOUT)

    def test_final_response_failed(self):
        tr = classify_exception(FinalResponseFailed("bad json"))
        self.assertEqual(tr.kind, KIND_FINAL_RESPONSE_FAILED)

    def test_cancel(self):
        tr = classify_exception(asyncio.CancelledError())
        self.assertEqual(tr.kind, KIND_CANCELLED)
        self.assertEqual(tr.state, TaskState.CANCELLED)

    def test_unknown_is_bounded_failure(self):
        tr = classify_exception(RuntimeError("weird"))
        self.assertEqual(tr.kind, KIND_BOUNDED_FAILURE)


class TerminalStateTests(unittest.TestCase):
    def test_terminal_detection(self):
        self.assertTrue(is_terminal(TaskState.COMPLETED))
        self.assertTrue(is_terminal(TaskState.FAILED))
        self.assertTrue(is_terminal(TaskState.CANCELLED))
        self.assertFalse(is_terminal(TaskState.RUNNING))
        self.assertFalse(is_terminal(TaskState.WAITING_USER))

    def test_terminal_cannot_transition(self):
        for state in TERMINAL_STATES:
            for target in TaskState:
                self.assertFalse(can_transition(state, target),
                                 f"{state} -> {target} 不应允许")

    def test_waiting_states_resumable(self):
        self.assertTrue(can_transition(TaskState.WAITING_USER, TaskState.RUNNING))
        self.assertTrue(can_transition(TaskState.WAITING_APPROVAL, TaskState.RUNNING))


class ConsistencyTests(unittest.TestCase):
    def test_completed_with_reject_verdict_is_inconsistent(self):
        errs = consistency_errors(TaskState.COMPLETED,
                                  completion_verdict="claim_unsupported")
        self.assertTrue(errs)

    def test_completed_with_pass_is_consistent(self):
        errs = consistency_errors(TaskState.COMPLETED, completion_verdict="pass")
        self.assertEqual(errs, [])

    def test_failed_with_false_claim_is_inconsistent(self):
        errs = consistency_errors(TaskState.FAILED, assistant_text="已经修改完成",
                                  has_evidence=False)
        self.assertTrue(errs)

    def test_failed_with_evidence_claim_is_ok(self):
        errs = consistency_errors(TaskState.FAILED, assistant_text="已经修改完成",
                                  has_evidence=True)
        self.assertEqual(errs, [])


class StaleRunRecoveryTests(unittest.TestCase):
    def test_recover_stale_running_run(self):
        import tempfile
        import uuid

        from runtime.task import TaskState as TS
        from runtime.task_manager import TaskManager

        with tempfile.TemporaryDirectory() as td:
            tm = TaskManager(str(Path(td) / "t.db"))
            task = tm.create_task(session_id="s", goal="g")
            tm.transition(task.id, TS.RUNNING, reason="start")
            # max_age_seconds=-1 → 截止点在未来，立即视为 stale
            recovered = tm.recover_stale_tasks(max_age_seconds=-1)
            self.assertIn(task.id, recovered)
            after = tm.get_task(task.id)
            self.assertEqual(after.state, TS.FAILED)
            events = [e.event_type for e in tm.list_events(task.id)]
            self.assertIn("task.recovered", events)


if __name__ == "__main__":
    unittest.main()
