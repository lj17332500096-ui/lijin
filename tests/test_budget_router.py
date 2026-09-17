import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agent import assistant_agent
from runtime import budget as budget_mod
from runtime import router as router_mod
from runtime.errors import BudgetExceeded
from runtime.task import RunBudget, Task, TaskState, utcnow_iso


def make_task(**overrides) -> Task:
    defaults = {
        "id": "task_x",
        "session_id": "s",
        "goal": "目标",
        "state": TaskState.SUBMITTED,
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
    }
    defaults.update(overrides)
    return Task(**defaults)


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {}
        for name in ("MODEL_DEFAULT", "MODEL_CHEAP", "MODEL_REASONING", "AGENT_MODEL", "MODEL_MAX_OUTPUT_TOKENS"):
            self._saved[name] = os.environ.get(name)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_profile_routing_rules(self) -> None:
        meta_task = make_task(metadata={"model_profile": "reasoning", "channel": "chat"})
        self.assertEqual(router_mod.route_profile(meta_task), "reasoning")
        deep = make_task(metadata={"deep": True, "channel": "chat"})
        self.assertEqual(router_mod.route_profile(deep), "reasoning")
        chat = make_task(metadata={"channel": "chat"})
        self.assertEqual(router_mod.route_profile(chat), "default")
        os.environ["MODEL_CHEAP"] = "agnes-2.0-flash"
        sched = make_task(metadata={"channel": "scheduled"})
        self.assertEqual(router_mod.route_profile(sched), "cheap")

    def test_agent_for_default_returns_same(self) -> None:
        self.assertIs(router_mod.agent_for(assistant_agent, "default"), assistant_agent)

    def test_agent_for_clone_when_model_differs(self) -> None:
        os.environ["MODEL_REASONING"] = "agnes-2.0-flash"
        clone = router_mod.agent_for(assistant_agent, "reasoning")
        self.assertIsNot(clone, assistant_agent)
        model = clone.model
        self.assertEqual(model.name if not isinstance(model, str) else model, "agnes-2.0-flash")
        self.assertEqual(len(clone.tools), len(assistant_agent.tools))
        self.assertTrue(clone.instructions)

    def test_unconfigured_profile_keeps_base(self) -> None:
        os.environ.pop("MODEL_CHEAP", None)
        self.assertIs(router_mod.agent_for(assistant_agent, "cheap"), assistant_agent)


class BudgetTests(unittest.TestCase):
    def test_resolve_takes_stricter_turns(self) -> None:
        task = make_task(budget=RunBudget(max_turns=5))
        budget, turns = budget_mod.resolve_budget(task, None, max_turns=20)
        self.assertEqual(turns, 5)
        budget2, turns2 = budget_mod.resolve_budget(make_task(), RunBudget(max_turns=3), max_turns=20)
        self.assertEqual(turns2, 3)

    def test_wall_limit_timeout_raises(self) -> None:
        task = make_task(budget=RunBudget(max_wall_seconds=3600))

        async def slow():
            await asyncio.sleep(1)
            return "done"

        async def run():
            budget = RunBudget(max_wall_seconds=0.1)
            with self.assertRaises(BudgetExceeded):
                await budget_mod.run_with_wall_limit(slow(), budget, task)
            ok = await budget_mod.run_with_wall_limit(slow(), task.budget, task)
            self.assertEqual(ok, "done")

        asyncio.run(run())


class RuntimeBudgetIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="budget_run_"))
        import main as main_module

        self._main = main_module
        self._orig = main_module.execute_turn
        from runtime.runner import AgentRuntime

        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))

    def tearDown(self) -> None:
        self._main.execute_turn = self._orig

    def test_wall_budget_fails_task(self) -> None:
        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None, agent=None, audit=None,
            stream_events_cb=None,
        ):
            await asyncio.sleep(0.5)
            return "ok-ignored"

        self._main.execute_turn = fake_execute_turn
        result = asyncio.run(
            self.runtime.run_turn(
                "慢任务",
                session_id="unit",
                budget=RunBudget(max_wall_seconds=0.1, max_turns=20),
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.task.state, TaskState.FAILED)
        self.assertIn("墙钟", result.error or "")

    def test_no_budget_preserves_behavior(self) -> None:
        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None, agent=None, audit=None,
            stream_events_cb=None,
        ):
            return "fast-ok"

        self._main.execute_turn = fake_execute_turn
        result = asyncio.run(self.runtime.run_turn("快任务", session_id="unit"))
        self.assertTrue(result.ok)
        # 新契约：纯文本输出被 canonical 化为 answer AgentReply JSON
        import json as _json

        canonical = _json.loads(result.final_output)
        self.assertEqual(canonical["kind"], "answer")
        self.assertEqual(canonical["content"], "fast-ok")


if __name__ == "__main__":
    unittest.main()

