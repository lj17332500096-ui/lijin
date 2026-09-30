"""Focused tests for LangGraph-owned Runtime retry transitions."""
from __future__ import annotations

import unittest

from runtime.conversation_workflow import (
    MAX_EXECUTION_ATTEMPTS,
    build_conversation_workflow,
    initial_state,
)


class ConversationWorkflowRetryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, execute, review, retry_exhausted=None):
        calls = {"analyze": 0, "plan": 0, "execute": 0, "review": 0, "exhausted": 0}

        async def analyze(state):
            calls["analyze"] += 1
            return {"intent": "test"}

        async def plan(state):
            calls["plan"] += 1
            return {"phase_count": calls["plan"]}

        async def run_execute(state):
            calls["execute"] += 1
            return await execute(state, calls)

        async def run_review(state):
            calls["review"] += 1
            return await review(state, calls)

        async def exhausted(state):
            calls["exhausted"] += 1
            if retry_exhausted is not None:
                return await retry_exhausted(state, calls)
            return {"terminal": {"kind": "bounded_failure"}}

        graph = build_conversation_workflow(
            analyze=analyze,
            plan=plan,
            execute=run_execute,
            review=run_review,
            retry_exhausted=exhausted,
        )
        state = await graph.ainvoke(initial_state("test request"))
        return state, calls

    async def test_completion_repair_is_a_single_graph_retry(self):
        async def execute(state, calls):
            if calls["execute"] == 1:
                return {"retry": {"kind": "completion_repair", "prompt": "repair"}}
            return {"answer": "complete"}

        async def review(state, calls):
            return {"verdict": "complete"}

        state, calls = await self._run(execute=execute, review=review)
        self.assertEqual(calls["execute"], 2)
        self.assertEqual(calls["plan"], 2)
        self.assertEqual(state["completion_repairs"], 1)
        self.assertEqual(state["retry_instruction"], "repair")
        self.assertEqual(calls["review"], 1)

    async def test_repeated_obligation_signature_exhausts_in_graph(self):
        async def execute(state, calls):
            return {"retry": {"kind": "obligation_feedback", "signature": "same",
                               "prompt": "verify"}}

        async def review(state, calls):
            self.fail("review must not run before execution passes")

        state, calls = await self._run(execute=execute, review=review)
        self.assertEqual(calls["execute"], 2)
        self.assertEqual(calls["exhausted"], 1)
        self.assertEqual(state["obligation_feedback_signatures"], ["same"])
        self.assertEqual(state["terminal"]["kind"], "bounded_failure")

    async def test_review_supplement_runs_once_through_plan_and_execute(self):
        async def execute(state, calls):
            return {"answer": f"answer-{calls['execute']}"}

        async def review(state, calls):
            if calls["review"] == 1:
                return {"verdict": "supplement", "supplement_prompt": "fill gap",
                        "gaps": ["missing detail"]}
            return {"verdict": "complete"}

        state, calls = await self._run(execute=execute, review=review)
        self.assertEqual(calls["execute"], 2)
        self.assertEqual(calls["plan"], 2)
        self.assertEqual(state["supplement_count"], 1)
        self.assertEqual(state["analysis"]["supplement_prompt"], "fill gap")
        self.assertEqual(state["review"]["verdict"], "complete")
        self.assertIsNone(state.get("terminal"))

    async def test_execution_attempt_limit_is_enforced_by_graph(self):
        async def execute(state, calls):
            return {"retry": {"kind": "obligation_feedback",
                               "signature": f"sig-{calls['execute']}",
                               "prompt": "continue"}}

        async def review(state, calls):
            self.fail("review must not run before execution passes")

        state, calls = await self._run(execute=execute, review=review)
        self.assertEqual(calls["execute"], MAX_EXECUTION_ATTEMPTS)
        self.assertEqual(calls["exhausted"], 1)
        self.assertEqual(state["execution_attempts"], MAX_EXECUTION_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
