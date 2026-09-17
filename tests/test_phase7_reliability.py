"""Phase 7 —— 把可确定的行为下沉为系统保证（deterministic behavior tests）。

覆盖：
- 缺关键参数（部署环境 / 破坏性删除 / 发送收件人）→ 强制 needs_user_input；
- 只读意图 → 不暴露 mutation/执行工具（Router 层，见 test_tool_router）；
- 实时事实 → 暴露实时检索工具；
- 明确无需工具 → 0 工具。
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402
from runtime.readiness_gate import (  # noqa: E402
    missing_required_fields,
    required_questions,
)
from runtime.runner import AgentRuntime  # noqa: E402
from runtime.task import TaskState  # noqa: E402


class MissingRequiredFieldsTests(unittest.TestCase):
    def _missing(self, msg, tool="run_python"):
        return missing_required_fields(msg, tool, {})

    def test_deploy_without_target_asks(self):
        missing, _ = self._missing("把这个项目部署一下。")
        self.assertTrue(missing)
        qs = required_questions("把这个项目部署一下。")
        self.assertTrue(any("部署到" in q for q in qs), qs)

    def test_deploy_with_target_ok(self):
        missing, _ = self._missing("把这个项目部署到 docker。")
        self.assertFalse(missing)

    def test_bulk_delete_asks(self):
        missing, _ = self._missing("删除项目里所有数据库迁移文件。")
        self.assertTrue(missing)
        qs = required_questions("删除项目里所有数据库迁移文件。")
        self.assertTrue(any("删除" in q for q in qs), qs)

    def test_send_to_person_asks(self):
        missing, _ = self._missing("把这个报告发给李总。")
        self.assertTrue(missing)
        qs = required_questions("把这个报告发给李总。")
        self.assertTrue(any("收件人" in q for q in qs), qs)

    def test_flight_missing_origin_still_asks(self):
        self.assertTrue(missing_required_fields("帮我查明天去上海的航班。", "web_search", {})[0])

    def test_reminder_missing_time_still_asks(self):
        self.assertTrue(missing_required_fields("提醒我去交资料。", "schedule_add", {})[0])


class DeterministicNeedsUserInputTests(unittest.TestCase):
    """缺关键参数时，即使模型继续调工具，也必须在有限步内收口为 WAITING_USER。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="p7_rel_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self._orig = main_module.execute_turn

    def tearDown(self):
        main_module.execute_turn = self._orig

    def _run(self, message, fake):
        main_module.execute_turn = fake
        return asyncio.run(self.runtime.run_turn(message, session_id="unit", mode="async"))

    def test_deploy_prompt_pauses_waiting_user(self):
        # 模型即使返回一个普通 answer，也因缺关键参数被收口为 questions → waiting_user
        async def fake(mode, message, **kw):
            return json.dumps({"kind": "answer", "summary": "s", "content": "正在部署",
                               "questions": [], "saved_file": None, "next_step": None},
                              ensure_ascii=False)

        result = self._run("把这个项目部署一下。", fake)
        self.assertTrue(result.ok)
        self.assertEqual(result.task.state, TaskState.WAITING_USER)
        events = [e.event_type for e in self.runtime.tasks.list_events(result.task.id)]
        self.assertIn("run.terminal", events)
        terminal = next(e for e in self.runtime.tasks.list_events(result.task.id)
                        if e.event_type == "run.terminal")
        self.assertEqual(terminal.payload.get("kind"), "needs_user_input")


if __name__ == "__main__":
    unittest.main()
