import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.errors import AgentError
from runtime.runner import AgentRuntime
from runtime.state_machine import ALLOWED_TRANSITIONS, assert_transition, can_transition
from runtime.task import TaskState
from runtime.task_manager import TaskManager


class StateMachineTests(unittest.TestCase):
    def test_allowed_transitions(self) -> None:
        self.assertTrue(can_transition(TaskState.SUBMITTED, TaskState.RUNNING))
        self.assertTrue(can_transition(TaskState.RUNNING, TaskState.COMPLETED))
        self.assertTrue(can_transition(TaskState.RUNNING, TaskState.FAILED))
        self.assertTrue(can_transition(TaskState.RUNNING, TaskState.PAUSED))
        self.assertTrue(can_transition(TaskState.PAUSED, TaskState.RUNNING))
        self.assertTrue(can_transition(TaskState.WAITING_USER, TaskState.RUNNING))
        self.assertTrue(can_transition(TaskState.WAITING_APPROVAL, TaskState.RUNNING))

    def test_terminal_is_frozen(self) -> None:
        for state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            self.assertEqual(ALLOWED_TRANSITIONS[state], set())
            with self.assertRaises(AgentError):
                assert_transition(state, TaskState.RUNNING)

    def test_backwards_transition_denied(self) -> None:
        with self.assertRaises(AgentError):
            assert_transition(TaskState.RUNNING, TaskState.SUBMITTED)
        with self.assertRaises(AgentError):
            assert_transition(TaskState.COMPLETED, TaskState.FAILED)


class TaskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="agent_db_"))
        self.manager = TaskManager(self._tmp / "agent.db")

    def test_create_and_transition_lifecycle(self) -> None:
        task = self.manager.create_task("s1", "调研一下 X")
        self.assertEqual(task.state, TaskState.SUBMITTED)
        events = self.manager.list_events(task.id)
        self.assertEqual([e.event_type for e in events], ["task.created"])

        running = self.manager.transition(task.id, TaskState.RUNNING)
        self.assertEqual(running.state, TaskState.RUNNING)
        self.assertIsNotNone(running.started_at)

        done = self.manager.mark_success(task.id, "完成摘要")
        self.assertEqual(done.state, TaskState.COMPLETED)
        self.assertIsNotNone(done.completed_at)
        types = [e.event_type for e in self.manager.list_events(task.id)]
        self.assertIn("task.running", types)
        self.assertIn("task.completed", types)
        self.assertIn("task.result", types)

    def test_failure_records_error_and_state(self) -> None:
        task = self.manager.create_task("s1", "做某事")
        self.manager.transition(task.id, TaskState.RUNNING)
        failed = self.manager.mark_failure(task.id, "模型超时")
        self.assertEqual(failed.state, TaskState.FAILED)
        self.assertIn("模型超时", failed.error_message or "")

    def test_invalid_transition_rejected_without_event(self) -> None:
        task = self.manager.create_task("s1", "x")
        self.manager.transition(task.id, TaskState.RUNNING)
        self.manager.mark_success(task.id)
        before = len(self.manager.list_events(task.id))
        with self.assertRaises(AgentError):
            self.manager.transition(task.id, TaskState.RUNNING)
        self.assertEqual(len(self.manager.list_events(task.id)), before)

    def test_list_filters_and_persistence(self) -> None:
        a = self.manager.create_task("s1", "任务A")
        b = self.manager.create_task("s2", "任务B")
        self.manager.transition(b.id, TaskState.RUNNING)
        s1_tasks = self.manager.list_tasks(session_id="s1")
        self.assertEqual([t.id for t in s1_tasks], [a.id])
        running = self.manager.list_tasks(state=TaskState.RUNNING)
        self.assertEqual([t.id for t in running], [b.id])

        reopened = TaskManager(self._tmp / "agent.db")
        restored = reopened.get_task(a.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.goal, "任务A")
        self.assertEqual(restored.state, TaskState.SUBMITTED)

    def test_update_usage(self) -> None:
        task = self.manager.create_task("s1", "x")
        self.manager.update_usage(task.id, turns=3, tool_calls=5, failures=1, input_tokens=100)
        restored = self.manager.get_task(task.id)
        self.assertEqual(restored.usage.turns, 3)
        self.assertEqual(restored.usage.tool_calls, 5)
        self.assertEqual(restored.usage.input_tokens, 100)

    def test_checkpoint_write_read(self) -> None:
        task = self.manager.create_task("s1", "做调研", metadata={"mode": "async"})
        self.manager.write_checkpoint(task.id, 1, {"goal": "做调研", "final_summary": "完成"})
        latest = self.manager.read_latest_checkpoint(task.id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["snapshot"]["final_summary"], "完成")
        self.assertEqual(self.manager.count_checkpoints(task.id), 1)
        types = [e.event_type for e in self.manager.list_events(task.id)]
        self.assertIn("task.checkpoint", types)

    def test_metadata_hydrated(self) -> None:
        task = self.manager.create_task("s1", "x", metadata={"channel": "web", "max_turns": 9})
        restored = self.manager.get_task(task.id)
        self.assertEqual(restored.metadata.get("channel"), "web")
        self.assertEqual(restored.metadata.get("max_turns"), 9)

    def test_recover_stale_running(self) -> None:
        import datetime as _dt
        from runtime.task import utcnow_iso

        task = self.manager.create_task("s1", "长任务")
        self.manager.transition(task.id, TaskState.RUNNING)
        # 把 updated_at 拨老 10 分钟，模拟进程崩溃后遗留的 RUNNING
        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)).isoformat(
            timespec="seconds"
        )
        with self.manager._connect() as conn:
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (old, task.id))
        recovered = self.manager.recover_stale_tasks(max_age_seconds=120)
        self.assertIn(task.id, recovered)
        restored = self.manager.get_task(task.id)
        self.assertEqual(restored.state, TaskState.FAILED)
        types = [e.event_type for e in self.manager.list_events(task.id)]
        self.assertIn("task.recovered", types)
        # 再次恢复没有可处理的
        self.assertEqual(self.manager.recover_stale_tasks(max_age_seconds=120), [])

    # ---- P1-1: 预算感知的 stale 恢复 ----
    def test_recover_stale_budget_relative_high_budget(self) -> None:
        import datetime as _dt
        import json as _json

        task = self.manager.create_task("s1", "长任务")
        self.manager.transition(task.id, TaskState.RUNNING)
        # 模拟 45 分钟前崩溃遗留（updated_at 拨老 2700s）
        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=2700)).isoformat(
            timespec="seconds"
        )
        with self.manager._connect() as conn:
            conn.execute(
                "UPDATE runs SET updated_at = ?, budget_json = ? WHERE id = ?",
                (old, _json.dumps({"max_wall_seconds": 1800}), task.id),
            )
        # 固定 900s 阈值下该 Run 必被回收（stale）
        recovered = self.manager.recover_stale_tasks(max_age_seconds=900)
        self.assertIn(task.id, recovered)

        # 再造一个同样的 2700s 陈旧 + 大预算 Run，走预算相对模式
        task2 = self.manager.create_task("s2", "长任务")
        self.manager.transition(task2.id, TaskState.RUNNING)
        with self.manager._connect() as conn:
            conn.execute(
                "UPDATE runs SET updated_at = ?, budget_json = ? WHERE id = ?",
                (old, _json.dumps({"max_wall_seconds": 1800}), task2.id),
            )
        # 预算相对模式：min(4500, 1800*2)=3600 → 2700s 未超阈值，被豁免
        self.assertNotIn(
            task2.id,
            self.manager.recover_stale_tasks(budget_relative=True, max_age_seconds=4500),
        )
        # 而把 Run 再拨老到 4000s（>3600s）则命中
        older = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=4000)).isoformat(
            timespec="seconds"
        )
        with self.manager._connect() as conn:
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (older, task2.id))
        self.assertIn(
            task2.id,
            self.manager.recover_stale_tasks(budget_relative=True, max_age_seconds=4500),
        )

    def test_recover_stale_budget_relative_no_budget_falls_back(self) -> None:
        import datetime as _dt

        task = self.manager.create_task("s1", "无预算长任务")
        self.manager.transition(task.id, TaskState.RUNNING)
        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)).isoformat(
            timespec="seconds"
        )
        with self.manager._connect() as conn:
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (old, task.id))
        # 无 budget_json、无 goal 文本 → 回落 max_age_seconds
        self.assertIn(
            task.id,
            self.manager.recover_stale_tasks(budget_relative=True, max_age_seconds=300),
        )

    # ---- P1-3: 陈旧审批 TTL 自动拒绝 ----
    def test_expire_stale_approvals_denies_and_keeps_fresh(self) -> None:
        import datetime as _dt

        task = self.manager.create_task("s1", "审批测试")
        self.manager.transition(task.id, TaskState.RUNNING)
        # 两条 pending：一条 4000s 陈旧，一条刚创建（1s 前）
        old_id = self.manager.create_approval(task.id, "exec", "exec", {"cmd": "rm"})
        fresh_id = self.manager.create_approval(task.id, "exec", "exec", {"cmd": "ls"})
        with self.manager._connect() as conn:
            conn.execute(
                "UPDATE approvals SET created_at = ? WHERE id = ?",
                (
                    (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=4000)).isoformat(
                        timespec="seconds"
                    ),
                    old_id,
                ),
            )
        expired = self.manager.expire_stale_approvals(task.id, max_age_seconds=3600, auto_deny=True)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["id"], old_id)
        self.assertEqual(expired[0]["status"], "denied")
        self.assertEqual(expired[0]["actor"], "ttl")
        # 陈旧条被拒，新鲜条保留
        row = self.manager.list_approvals(task_id=task.id, state="pending")[0]
        self.assertEqual(row["id"], fresh_id)

    def test_expire_stale_approvals_no_auto_deny_keeps_pending(self) -> None:
        import datetime as _dt

        task = self.manager.create_task("s1", "审批测试")
        self.manager.transition(task.id, TaskState.RUNNING)
        aid = self.manager.create_approval(task.id, "exec", "exec", {"cmd": "rm"})
        with self.manager._connect() as conn:
            conn.execute(
                "UPDATE approvals SET created_at = ? WHERE id = ?",
                (
                    (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=4000)).isoformat(
                        timespec="seconds"
                    ),
                    aid,
                ),
            )
        expired = self.manager.expire_stale_approvals(task.id, max_age_seconds=3600, auto_deny=False)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["status"], "pending")
        # 仍处于 pending
        self.assertEqual(self.manager.list_approvals(task_id=task.id, state="pending")[0]["id"], aid)


class RuntimeRunTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="agent_run_"))
        import main as main_module

        self._main = main_module
        self._orig = main_module.execute_turn
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))

    def tearDown(self) -> None:
        self._main.execute_turn = self._orig

    def _install_fake(self, result: object = "fake-ok", error: Exception | None = None):
        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None, agent=None, audit=None,
            stream_events_cb=None,
        ):
            if error is not None:
                raise error
            return result

        self._main.execute_turn = fake_execute_turn

    def test_success_creates_completed_task(self) -> None:
        self._install_fake(result='{"kind":"answer","summary":"好","content":"OK"}')
        result = asyncio.run(self.runtime.run_turn("帮我算 1+1", session_id="unit"))
        self.assertTrue(result.ok)
        # 新契约：final_output 为 canonical JSON（字段顺序/默认值由 parser 决定）
        import json as _json

        canonical = _json.loads(result.final_output)
        self.assertEqual(canonical["kind"], "answer")
        self.assertEqual(canonical["content"], "OK")
        task = result.task
        self.assertEqual(task.state, TaskState.COMPLETED)
        self.assertEqual(task.goal, "帮我算 1+1")
        types = [e.event_type for e in self.runtime.tasks.list_events(task.id)]
        self.assertIn("task.created", types)
        self.assertIn("task.completed", types)
        self.assertEqual(self.runtime.tasks.count_checkpoints(task.id), 1)
        cp = self.runtime.tasks.read_latest_checkpoint(task.id)
        self.assertIn("OK", cp["snapshot"]["final_summary"])

    def test_failure_marks_failed_and_returns(self) -> None:
        self._install_fake(error=RuntimeError("网关挂了"))
        result = asyncio.run(self.runtime.run_turn("读一下文件", session_id="unit"))
        self.assertFalse(result.ok)
        self.assertIn("网关挂了", result.error or "")
        self.assertEqual(result.task.state, TaskState.FAILED)
        cp = self.runtime.tasks.read_latest_checkpoint(result.task.id)
        self.assertIn("网关挂了", cp["snapshot"]["error"])

    def test_resume_paused_task_and_terminal_denied(self) -> None:
        # 失败是终态：重试 = 新任务
        self._install_fake(error=RuntimeError("第一次失败"))
        first = asyncio.run(self.runtime.run_turn("做 A", session_id="unit"))
        self.assertEqual(first.task.state, TaskState.FAILED)

        self._install_fake(result="重试成功")
        fresh = asyncio.run(self.runtime.run_turn("做 A", session_id="unit"))
        self.assertTrue(fresh.ok)
        self.assertNotEqual(fresh.task.id, first.task.id)

        # 暂停的任务可以原地恢复
        paused = self.runtime.tasks.create_task("unit", "做 B")
        self.runtime.tasks.transition(paused.id, TaskState.RUNNING)
        self.runtime.tasks.transition(paused.id, TaskState.PAUSED)
        self._install_fake(result="恢复成功")
        resumed = asyncio.run(self.runtime.run_turn("做 B", session_id="unit", task_id=paused.id))
        self.assertTrue(resumed.ok)
        self.assertEqual(resumed.task.state, TaskState.COMPLETED)

        # 终态不可再恢复
        with self.assertRaises(AgentError):
            asyncio.run(self.runtime.run_turn("做 B", session_id="unit", task_id=resumed.task.id))


if __name__ == "__main__":
    unittest.main()

