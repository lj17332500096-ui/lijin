# -*- coding: utf-8 -*-
"""Task Readiness & Missing Information Check 回归（离线确定性）。

覆盖：AgentReply.readiness 结构化透传；NEEDS_USER → questions 正常收尾并记录
task.readiness 事件；READY/普通问答不受影响（不强制澄清）。
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import main as main_module
from runtime.approval import ApprovalGate
from runtime.reply_parser import parse as parse_reply
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager
from schemas import AgentReply


def _reply(readiness=None, kind="answer", content="ok", questions=None):
    return {
        "kind": kind, "summary": "s", "content": content,
        "questions": questions or [], "saved_file": None,
        "next_step": None, "ui": [], "readiness": readiness,
    }


class ReadinessParserTests(unittest.TestCase):
    def test_schema_accepts_readiness(self) -> None:
        r = AgentReply(**_reply(
            readiness={"status": "NEEDS_USER", "missing_count": 1,
                       "reason": "出发地缺失"},
            kind="questions", content="从哪里出发？",
            questions=["从哪里出发？"]))
        self.assertEqual(r.readiness["status"], "NEEDS_USER")

    def test_parser_keeps_safe_readiness_fields(self) -> None:
        parsed = parse_reply(_reply(
            readiness={"status": "NEEDS_USER", "missing_count": 3,
                       "reason": "出发地、联系人、金额缺失"},
            kind="questions", content="", questions=["从哪里出发？", "发给谁？"]))
        rd = parsed.canonical.get("readiness") or {}
        self.assertEqual(rd["status"], "NEEDS_USER")
        self.assertEqual(rd["missing_count"], 3)
        self.assertIn("reason", rd)

    def test_parser_rejects_unknown_status(self) -> None:
        parsed = parse_reply(_reply(
            readiness={"status": "MAYBE", "missing_count": 1},
            kind="answer"))
        self.assertIsNone(parsed.canonical.get("readiness"))


class _Harness:
    def __init__(self, testcase):
        self.tmp = Path(tempfile.mkdtemp(prefix="readiness_"))
        self.manager = TaskManager(self.tmp / "agent.db")
        self.runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        self.runtime._initialized = True
        self.runtime._tools_patched = True
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self.runtime.artifact_dirs = (self.tmp / "notes",)
        (self.tmp / "notes").mkdir(exist_ok=True)
        self._orig = main_module.execute_turn
        self._saved_router = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "off"
        self.fn = None

    async def _execute(self, mode, message, **kwargs):
        return await self.fn(message)

    def __enter__(self):
        main_module.execute_turn = self._execute
        return self

    def __exit__(self, *exc):
        main_module.execute_turn = self._orig
        if self._saved_router is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._saved_router

    def run(self, msg):
        return asyncio.run(self.runtime.run_turn(msg, session_id="unit", mode="async"))


class ReadinessRunnerTests(unittest.TestCase):
    def test_needs_user_questions_pauses_waiting_user_and_records_event(self) -> None:
        # Phase 5：缺必需信息 → Run 终态为 WAITING_USER（不再伪装 COMPLETED）。
        async def fake(message):
            return json.dumps(_reply(
                readiness={"status": "NEEDS_USER", "missing_count": 1,
                           "reason": "出发地必须由用户确认"},
                kind="questions", content="",
                questions=["从哪里出发？"]), ensure_ascii=False)

        with _Harness(self) as h:
            h.fn = fake
            result = h.run("帮我预订明天去上海的机票")
            self.assertTrue(result.ok)
            self.assertEqual(result.task.state, TaskState.WAITING_USER)
            types = [e.event_type for e in h.manager.list_events(result.task.id)]
            self.assertIn("task.readiness", types)
            ev = next(e for e in h.manager.list_events(result.task.id)
                      if e.event_type == "task.readiness")
            self.assertEqual(ev.payload.get("status"), "NEEDS_USER")
            self.assertEqual(ev.payload.get("missing_count"), 1)

    def test_plain_qa_has_no_readiness_and_completes(self) -> None:
        async def fake(message):
            return json.dumps(_reply(kind="answer", content="Python 是一种编程语言。"),
                              ensure_ascii=False)

        with _Harness(self) as h:
            h.fn = fake
            result = h.run("Python 是什么？")
            self.assertTrue(result.ok)
            self.assertEqual(result.task.state, TaskState.COMPLETED)
            types = [e.event_type for e in h.manager.list_events(result.task.id)]
            self.assertNotIn("task.readiness", types)

    def test_questions_without_readiness_field_still_records_needs_user(self) -> None:
        async def fake(message):
            return json.dumps(_reply(
                kind="questions", content="",
                questions=["从哪里出发？", "几个人？"]), ensure_ascii=False)

        with _Harness(self) as h:
            h.fn = fake
            result = h.run("帮我订明天去上海的机票")
            self.assertTrue(result.ok)
            ev = next(e for e in h.manager.list_events(result.task.id)
                      if e.event_type == "task.readiness")
            self.assertEqual(ev.payload.get("status"), "NEEDS_USER")
            self.assertEqual(ev.payload.get("missing_count"), 2)


if __name__ == "__main__":
    unittest.main()
