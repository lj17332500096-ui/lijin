import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import runtime.execution as runtime_execution

from agent import assistant_agent
from runtime import budget as budget_mod
from runtime import router as router_mod
from runtime.errors import BudgetExceeded, ProcessExitInterrupted
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

    def test_default_wall_budget_still_installs_the_guard(self) -> None:
        """A9 判据：``max_wall_seconds == 3600``（RunBudget 默认值）**不得**被跳过。

        为什么必须做结构性断言：旧实现写的是 ``if limit <= 0 or limit >= 3600:``，
        而 RunBudget 的默认 max_wall_seconds 恰好就是 3600 —— 等于默认配置下这个
        唯一的卡死兜底从未安装。行为测试永远区分不出来（两个分支对 1s 协程都返回
        "done"），只能直接断言 ``asyncio.wait_for`` 确实被调用过。
        """
        task = make_task(budget=RunBudget(max_wall_seconds=3600))
        calls: list[float] = []
        real_wait_for = asyncio.wait_for

        async def _recording_wait_for(aw, timeout):
            calls.append(timeout)
            return await real_wait_for(aw, timeout)

        async def fast():
            return "done"

        async def run():
            orig = budget_mod.asyncio.wait_for
            budget_mod.asyncio.wait_for = _recording_wait_for
            try:
                ok = await budget_mod.run_with_wall_limit(
                    fast(), RunBudget(max_wall_seconds=3600), task
                )
            finally:
                budget_mod.asyncio.wait_for = orig
            self.assertEqual(ok, "done")

        asyncio.run(run())
        self.assertEqual(calls, [3600.0],
                         "默认 3600s 预算必须仍然装上墙钟护栏，不能静默短路")

    def test_system_exit_becomes_ordinary_error(self) -> None:
        """A4 配套：SystemExit 必须在同一 Task 内被转成普通异常。

        若不转换，asyncio 的 ``Task.__step`` 会把它 raise 出 ``run_forever``，
        外层 await 永不恢复、协程 finally 整段跳过（Run 卡 running）。
        """
        from runtime.errors import ProcessExitInterrupted

        task = make_task(budget=RunBudget(max_wall_seconds=1))

        async def suicide():
            raise SystemExit(7)

        async def run():
            with self.assertRaises(ProcessExitInterrupted) as cm:
                await budget_mod.run_with_wall_limit(suicide(), task.budget, task)
            self.assertEqual(cm.exception.exit_code, 7)
            self.assertEqual(cm.exception.original_type, "SystemExit")

        asyncio.run(run())

    def test_keyboard_interrupt_is_converted_but_marked_for_reraise(self) -> None:
        """Ctrl-C 双约束（预算层这一半）：必须被转换，但标记为「收口后要重抛」。

        为什么不能在这里直接原样上抛：asyncio 的 ``Task.__step`` 对 KeyboardInterrupt
        有特例，原样上抛会让外层 await 永不恢复、Run 的 finally 整段跳过 ——
        一次 Ctrl-C 就留下一个永远停在 running 的僵尸 Run。
        为什么不能直接吞掉：Ctrl-C 是用户意图，必须穿透到 main.py / cli 的顶层处理器。
        所以预算层只做「转成普通异常以便清理」，重抛由 ``run_turn`` 在终态写完后再做。
        """
        task = make_task(budget=RunBudget(max_wall_seconds=1))
        seen: list[tuple[str, bool]] = []

        async def interrupt():
            raise KeyboardInterrupt

        async def run():
            try:
                await budget_mod.run_with_wall_limit(interrupt(), task.budget, task)
            except ProcessExitInterrupted as exc:
                seen.append((type(exc.original).__name__, exc.must_reraise))

        asyncio.run(run())
        self.assertEqual(seen, [("KeyboardInterrupt", True)],
                         "KeyboardInterrupt 应被转换且标记为需重抛")

    def test_system_exit_is_not_marked_for_reraise(self) -> None:
        """SystemExit 相反：收口成 failed 即可，不得重抛。

        依据 runner._close_run 的既有教训：清理路径抛出的 SystemExit 曾让 uvicorn
        「跑一段时间后突然退出」。工具/沙箱里的 sys.exit() 不是本进程的退出意图。
        """
        task = make_task(budget=RunBudget(max_wall_seconds=1))
        seen: list[bool] = []

        async def suicide():
            raise SystemExit(0)

        async def run():
            try:
                await budget_mod.run_with_wall_limit(suicide(), task.budget, task)
            except ProcessExitInterrupted as exc:
                seen.append(exc.must_reraise)

        asyncio.run(run())
        self.assertEqual(seen, [False], "SystemExit 不应被标记为重抛")


class RuntimeBudgetIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="budget_run_"))
        import main as main_module

        self._main = main_module
        self._orig = runtime_execution.execute_turn
        from runtime.runner import AgentRuntime

        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))

    def tearDown(self) -> None:
        runtime_execution.execute_turn = self._orig

    def test_wall_budget_fails_task(self) -> None:
        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None, agent=None, audit=None,
            stream_events_cb=None,
        ):
            await asyncio.sleep(0.5)
            return "ok-ignored"

        runtime_execution.execute_turn = fake_execute_turn
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

        runtime_execution.execute_turn = fake_execute_turn
        result = asyncio.run(self.runtime.run_turn("快任务", session_id="unit"))
        self.assertTrue(result.ok)
        # 新契约：纯文本输出被 canonical 化为 answer AgentReply JSON
        import json as _json

        canonical = _json.loads(result.final_output)
        self.assertEqual(canonical["kind"], "answer")
        self.assertEqual(canonical["content"], "fast-ok")


if __name__ == "__main__":
    unittest.main()

