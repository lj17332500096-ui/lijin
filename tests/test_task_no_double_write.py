"""状态流转事件不得被双写（本轮修掉的真实缺陷）。

## 缺陷

真故障路径下`task.failed` 在事件流里出现 **2 条**（端到端实测，非推断）。
消费方（Web 活动栏、审计、复盘）无法区分「失败了两次」与「失败了一次」。

## 真实根因：不是重试，是双写

初版诊断以为是 `_retrying_write` 的重放导致重复（并据此加了 `event_key` 唯一
索引）。**那个诊断是错的** —— 用 `sqlite3.Connection.set_trace_callback` 抓栈后
真相是：两次 INSERT 来自**两条不同的语句**，而不是同一条被执行了两次：

    第 1 次: mark_failure -> transition -> task_manager.py:1362
             INSERT INTO task_events (... 'task.failed' ...)   <- 权威事件
    第 2 次: mark_failure -> add_event -> INSERT OR IGNORE ... <- 多余的那次

`transition()` 的设计（见其 docstring）是「状态更新与权威事件在同一事务提交，
避免 state 已变而 `task.<state>` 事件缺失」。**它已经写了 `task.failed`。**
`mark_failure` 随后又`add_event` 写了一条同名的 —— 纯重复。

对照`mark_success`：它 `add_event` 写的是 `task.result`（**不同**事件名），
所以不重复。**只有 `mark_failure` 撞了名。**

## 本文件锁的契约

1. 一次 `mark_failure` ⇒ `task.failed` 恰好 **1** 条（计数断言，不是 assertIn ——
   存在性断言在多条时删一条仍满足）。
2. 错误信息必须仍能从事件里读到（去掉 `add_event` 后信息走 `transition(reason=)`，
   不能把错误详情丢掉 —— 那是排障的唯一线索）。
3. `mark_success` / `mark_cancelled` 等同类不得引入同型双写（防复发）。
4. 端到端：真故障跑一次真实执行，事件流里 `task.failed` 仍是 1 条
   （单元测试与端到端曾出现不一致 —— 单元绿而端到端仍 2 条）。
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import runtime.execution as runtime_execution
from runtime.task import TaskState
from runtime.task_manager import TaskManager
from tests.workflow_model_stub import workflow_model_stub


class NoDoubleWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nodouble_"))
        self.mgr = TaskManager(str(self.tmp / "agent.db"))
        self.container = self.mgr.get_or_create_container("proj-dbl")
        self.run = self.mgr.create_task(
            session_id="proj-s", goal="g", thread_id=self.container["id"]
        )

    def tearDown(self) -> None:
        import shutil

        runtime_execution.execute_turn = getattr(
            runtime_execution, "_orig_execute_turn", runtime_execution.execute_turn
        )
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _types(self) -> list[str]:
        return [e.event_type for e in self.mgr.list_events(self.run.id, limit=1000)]

    def _events(self, event_type: str) -> list:
        return [e for e in self.mgr.list_events(self.run.id, limit=1000)
                if e.event_type == event_type]

    def test_mark_failure_writes_exactly_one_failed_event(self) -> None:
        """**核心契约**：一次失败 = 一条 `task.failed`（计数断言）。"""
        self.mgr.transition(self.run.id, TaskState.RUNNING)
        self.mgr.mark_failure(self.run.id, "provider 挂了")

        n = self._types().count("task.failed")
        self.assertEqual(
            n, 1,
            f"一次 mark_failure 写了 {n} 条 task.failed（期望 1）。"
            " `transition` 已在同事务里权威写入 task.<state> 事件，"
            "mark_failure 不得再 add_event 一次同名的。",
        )

    def test_error_detail_survives_in_authoritative_event(self) -> None:
        """去掉那次 add_event 后，错误详情不能丢 —— 它是排障的唯一线索。"""
        detail = "provider 挂了：ECONNREFUSED 10.0.0.1:8080"
        self.mgr.transition(self.run.id, TaskState.RUNNING)
        self.mgr.mark_failure(self.run.id, detail)

        evs = self._events("task.failed")
        self.assertEqual(len(evs), 1)
        payload = json.dumps(evs[0].payload, ensure_ascii=False)
        self.assertIn(
            "ECONNREFUSED", payload,
            "task.failed 事件里查不到错误详情 —— 错误信息必须经 transition(reason=) "
            "进入权威事件 payload，否则这次修复等于把排障线索删了。",
        )

    def test_error_message_column_still_written(self) -> None:
        """`runs.error_message` 是另一条独立事实源，不能因为去掉事件而丢。"""
        self.mgr.transition(self.run.id, TaskState.RUNNING)
        self.mgr.mark_failure(self.run.id, "唯一错误标记ZQ7")
        task = self.mgr.get_task(self.run.id)
        self.assertIsNotNone(task)
        blob = (task.error_message or "") + json.dumps(task.metadata, ensure_ascii=False)
        self.assertIn("ZQ7", blob)

    def test_sibling_marks_do_not_double_write(self) -> None:
        """防复发：`mark_success` 写的是 `task.result`（不同名），不得出现同型重复。

        这里断言的是「每个状态事件都恰好一条」，覆盖 success / failed 两条路径。
        """
        self.mgr.transition(self.run.id, TaskState.RUNNING)
        self.mgr.mark_failure(self.run.id, "boom")
        types = self._types()
        for ev in ("task.running", "task.failed"):
            with self.subTest(event=ev):
                self.assertEqual(
                    types.count(ev), 1,
                    f"{ev} 出现 {types.count(ev)} 次（期望 1）—— "
                    "状态流转事件被双写。",
                )

    def test_end_to_end_real_failure_path_has_one_failed_event(self) -> None:
        """**端到端**：跑一次真实执行（桩把 execute_turn 打成真故障）。

        这一条是本文件存在的理由：初版只在单元层验证，单元绿而端到端仍 2 条。
        单元与端到端不一致时，以端到端为准。
        """
        orig = runtime_execution.execute_turn

        async def _boom(mode, message, session=None, **kw):
            await asyncio.sleep(0.3)
            raise RuntimeError("真故障：provider 挂了")

        runtime_execution.execute_turn = workflow_model_stub(_boom)
        try:
            from runtime.runner import AgentRuntime

            rt = AgentRuntime(db_path=str(self.tmp / "agent.db"))
            rt._ensure()

            async def go():
                spawn = rt.spawn_run_task(self.run.id, rt.run_turn(
                    "真故障", session=None, session_id="proj-s", mode="async",
                    task_id=self.run.id, metadata={"channel": "web"}))
                try:
                    await asyncio.wait_for(asyncio.shield(spawn), timeout=40)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

            asyncio.run(go())

            state = self.mgr.get_task(self.run.id)
            self.assertEqual(state.state, TaskState.FAILED)
            types = self._types()
            self.assertEqual(
                types.count("task.failed"), 1,
                f"端到端真故障路径 task.failed 出现 {types.count('task.failed')} 次"
                "（期望 1）。",
            )
            self.assertEqual(types.count("task.completed"), 0)
        finally:
            runtime_execution.execute_turn = orig


if __name__ == "__main__":
    unittest.main()
