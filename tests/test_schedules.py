import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.task_manager import TaskManager, auto_recover


def fake_task(task_id: str = "task_a", next_run: str = "2026-09-05T08:00:00") -> dict:
    return {
        "id": task_id,
        "name": "晨间任务",
        "schedule": "08:00",
        "prompt": "查天气",
        "enabled": True,
        "session_id": "s1",
        "next_run": next_run,
        "last_run": None,
    }


class ScheduleMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="sched_db_"))
        self.manager = TaskManager(self._tmp / "agent.db")

    def test_mirror_upserts_by_id(self) -> None:
        self.assertEqual(self.manager.mirror_tasks_json([fake_task()]), 1)
        rows = self.manager.list_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "task_a")
        self.assertTrue(rows[0]["enabled"])
        # 再次镜像同 id（幂等 upsert 不新增）
        self.manager.mirror_tasks_json([fake_task(next_run="2026-09-06T08:00:00")])
        rows = self.manager.list_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["next_run"], "2026-09-06T08:00:00")

    def test_idempotent_run_ledger(self) -> None:
        first = self.manager.begin_schedule_run("task_a", "2026-09-05T08:00:00")
        self.assertTrue(first)
        duplicate = self.manager.begin_schedule_run("task_a", "2026-09-05T08:00:00")
        self.assertFalse(duplicate)  # 幂等键拦截
        self.manager.finalize_schedule_run(
            "task_a", "2026-09-05T08:00:00", "ok", task_id="task_r1", detail="完成"
        )
        runs = self.manager.list_schedule_runs(schedule_id="task_a")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "ok")
        self.assertEqual(runs[0]["task_id"], "task_r1")
        # 不同触发时刻允许再次执行
        self.assertTrue(self.manager.begin_schedule_run("task_a", "2026-09-06T08:00:00"))

    def test_auto_recover_function(self) -> None:
        import datetime as _dt

        from runtime.task import TaskState

        task = self.manager.create_task("s1", "崩溃任务")
        self.manager.transition(task.id, TaskState.RUNNING)
        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=3600)).isoformat(timespec="seconds")
        with self.manager._connect() as conn:
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (old, task.id))
        recovered = auto_recover(db_path=self._tmp / "agent.db")
        self.assertIn(task.id, recovered)
        restored = self.manager.get_task(task.id)
        self.assertEqual(restored.state, TaskState.FAILED)


if __name__ == "__main__":
    unittest.main()
