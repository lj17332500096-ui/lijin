import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import scheduler
from tools import schedule_add, schedule_list, schedule_remove


def call_tool(tool, **kwargs) -> str:
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="test_call",
            tool_arguments=input_json,
        )
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


class ScheduleParseTests(unittest.TestCase):
    def test_daily_time(self) -> None:
        spec = scheduler.parse_schedule("08:30")
        self.assertEqual(spec["hour"], {8})
        self.assertEqual(spec["minute"], {30})
        self.assertIsNone(spec["weekday"])

    def test_next_run_daily_rolls_to_tomorrow(self) -> None:
        spec = scheduler.parse_schedule("08:30")
        nxt = scheduler.next_run_after(spec, datetime(2026, 9, 4, 7, 0))
        self.assertEqual(nxt, datetime(2026, 9, 4, 8, 30))
        nxt2 = scheduler.next_run_after(spec, datetime(2026, 9, 4, 9, 0))
        self.assertEqual(nxt2, datetime(2026, 9, 5, 8, 30))

    def test_chinese_weekday(self) -> None:
        spec = scheduler.parse_schedule("周一 09:00")
        self.assertEqual(spec["weekday"], {0})
        # 2026-09-06 是周日，下一次周一应为 2026-09-07 09:00
        nxt = scheduler.next_run_after(spec, datetime(2026, 9, 6, 10, 0))
        self.assertEqual(nxt, datetime(2026, 9, 7, 9, 0))

    def test_english_weekday(self) -> None:
        spec = scheduler.parse_schedule("Mon 09:00")
        self.assertEqual(spec["weekday"], {0})

    def test_cron_weekday_range_skips_weekend(self) -> None:
        spec = scheduler.parse_schedule("30 8 * * 1-5")
        # 2026-09-05 是周六，下一次工作日为周一 2026-09-07 08:30
        nxt = scheduler.next_run_after(spec, datetime(2026, 9, 5, 7, 0))
        self.assertEqual(nxt, datetime(2026, 9, 7, 8, 30))

    def test_cron_step(self) -> None:
        spec = scheduler.parse_schedule("*/15 * * * *")
        self.assertEqual(spec["minute"], {0, 15, 30, 45})

    def test_invalid_schedule_raises(self) -> None:
        with self.assertRaises(ValueError):
            scheduler.parse_schedule("25:99")
        with self.assertRaises(ValueError):
            scheduler.parse_schedule("随便写点什么")


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_sched_test_"))
        self.path = self.tmp / "tasks.json"
        self.orig_path = scheduler.TASKS_PATH
        scheduler.TASKS_PATH = self.path

    def tearDown(self) -> None:
        scheduler.TASKS_PATH = self.orig_path

    def test_add_and_list(self) -> None:
        task = scheduler.add_task("晨间天气", "08:30", "查询今天的天气并保存笔记")
        self.assertTrue(task["id"].startswith("task_"))
        self.assertTrue(self.path.exists())
        tasks = scheduler.load_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertIn("下次", scheduler.format_task_line(tasks[0]))

    def test_due_prepare_mark_cycle(self) -> None:
        task = scheduler.add_task("每分任务", "* * * * *", "随便执行一下")
        # 把 next_run 拨到过去，模拟到点
        tasks = scheduler.load_tasks()
        tasks[0]["next_run"] = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
        scheduler.save_tasks(tasks)
        due = scheduler.due_tasks()
        self.assertEqual(len(due), 1)

        scheduler.prepare_next_run(task["id"])
        self.assertEqual(len(scheduler.due_tasks()), 0)

        scheduler.mark_run_result(task["id"], "ok", "执行成功摘要")
        updated = scheduler.load_tasks()[0]
        self.assertEqual(updated["last_status"], "ok")
        self.assertIsNotNone(updated["last_run"])

    def test_remove_and_disable(self) -> None:
        task = scheduler.add_task("临时", "09:00", "测试")
        self.assertTrue(scheduler.remove_task(task["id"]))
        self.assertFalse(scheduler.remove_task(task["id"]))
        self.assertEqual(scheduler.load_tasks(), [])

        task2 = scheduler.add_task("可停用", "10:00", "测试")
        scheduler.set_task_enabled(task2["id"], False)
        self.assertFalse(scheduler.load_tasks()[0]["enabled"])
        self.assertEqual(scheduler.due_tasks(), [])


class ScheduleToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_sched_tool_"))
        self.orig_path = scheduler.TASKS_PATH
        scheduler.TASKS_PATH = self.tmp / "tasks.json"

    def tearDown(self) -> None:
        scheduler.TASKS_PATH = self.orig_path

    def test_tools_add_list_remove(self) -> None:
        out = call_tool(schedule_add, name="喝水提醒", schedule="每天 14:00", prompt="提醒我喝水")
        self.assertIn("已创建定时任务", out)
        listing = call_tool(schedule_list)
        self.assertIn("喝水提醒", listing)
        task = scheduler.load_tasks()[0]
        removed = call_tool(schedule_remove, task_id=task["id"])
        self.assertIn("已删除", removed)
        self.assertEqual(scheduler.load_tasks(), [])


if __name__ == "__main__":
    unittest.main()
