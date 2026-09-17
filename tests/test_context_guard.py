# -*- coding: utf-8 -*-
"""P0 第二阶段 Context Guard / Compact 接线测试（全部离线，真实 SQLiteSession）。

覆盖（任务书 §34-44、54）：
- 短对话：10 条内零模型调用、不触发 compact、历史正确、连续性正常
- Soft Limit：Web/Project 同一条 run_turn 链真正触发 compact（而不是只有 CLI）
- Hard Limit：摘要模型失败 + 超硬阈值 → 自动硬窗口，本轮继续、不得 failed
- 1500+ 级历史：分块摘要（多次 summarize 调用）+ 收敛到 bounded
- Tool Pair 完整性：窗口/切割不产生孤立 tool_call / tool_result
- 附件：compact 后下一条消息附件仍注入（attach 块仍出现在指令里）
- Memory Scope / Project Context：摘要输入不含 Project Context 行 / 全局记忆内容
- Summary Growth：多 compact 周期后 summary 有界、会话中只保留一条摘要
- Token 曲线：60 轮 est_tokens 锯齿有界（无线性增长）
- Restart：compact 后重开 Session 不恢复旧历史
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

import compact
import main as main_module
from agents.memory import SQLiteSession
from runtime.approval import ApprovalGate
from runtime.context import (
    EV_COMPACTION_COMPLETED,
    EV_COMPACTION_FAILED,
    EV_WINDOWED,
    prepare_session_context,
    quick_stats,
    window_history,
)
from runtime.runner import AgentRuntime, RunResult
from runtime.task import TaskState
from runtime.task_manager import TaskManager

_TEST_ENV = {
    "AUTO_SUMMARY_MIN_TURNS": "3",
    "AUTO_SUMMARY_TRIGGER_TURNS": "12",
    "AUTO_SUMMARY_TRIGGER_CHARS": "50000",
    "AUTO_SUMMARY_KEEP_TURNS": "4",
    "AUTO_SUMMARY_TRANSCRIPT_CAP": "20000",
    "FORGE_HISTORY_HARD_CHARS": "80000",
    "FORGE_HISTORY_HARD_MESSAGES": "300",
    "FORGE_CONTEXT_GUARD": "on",
}

_FAKE_SUMMARY = "测试摘要：用户想修斐波那契 bug；已写入 hello.py 正确实现；验证通过 [0,1,1,2,3,5]。"


def _turn_text(n: int, pad: int) -> str:
    return "长" * pad


def make_turn(n: int, *, pad: int = 60) -> list[dict]:
    content = "第%d轮：处理项目 alpha 的需求（%s）" % (n, _turn_text(n, pad))
    body = "第%d轮的回答正文（%s）" % (n, _turn_text(n, pad))
    assistant = {"role": "assistant", "content": json.dumps(
        {"kind": "answer", "summary": "s%d" % n, "content": body,
         "questions": [], "saved_file": None, "next_step": None}, ensure_ascii=False)}
    return [{"role": "user", "content": content}, assistant]


def make_tool_turn(n: int) -> list[dict]:
    return [
        {"role": "user", "content": "第%d轮：运行代码看结果" % n},
        {"role": "assistant", "content": json.dumps(
            {"kind": "answer", "summary": "s%d" % n, "content": "第%d轮回答" % n,
             "questions": [], "saved_file": None, "next_step": None}, ensure_ascii=False)},
        {"type": "function_call", "name": "run_python", "arguments": '{"code": "print(1)"}',
         "call_id": "call_%d" % n},
        {"type": "function_call_output", "call_id": "call_%d" % n,
         "output": "stdout [ok] " + ("x" * 40)},
    ]


class ContextEnvMixin:
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="context_guard_")
        self.db = os.path.join(self.tmp, "sessions.sqlite")
        self.orig_summarize = compact.summarize_transcript
        self.orig_summary_dir = compact.SUMMARY_DIR
        compact.SUMMARY_DIR = Path(self.tmp) / "summaries"
        self._saved = {}
        for k, v in _TEST_ENV.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v

    def tearDown(self) -> None:
        compact.summarize_transcript = self.orig_summarize
        compact.SUMMARY_DIR = self.orig_summary_dir
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _session(self, sid: str = "ctx_t") -> SQLiteSession:
        return SQLiteSession(sid, db_path=self.db)

    def go(self, coro):
        return asyncio.run(coro)


class QuickStatsTests(ContextEnvMixin, unittest.TestCase):
    def test_short_session_stats_cheap(self) -> None:
        session = self._session("q1")
        self.go(session.add_items(make_turn(1, pad=40)))
        stats = self.go(quick_stats(session))
        session.close()
        self.assertIsNotNone(stats)
        self.assertTrue(stats.fast)
        self.assertEqual(stats.messages, 2)

    def test_short_conversation_no_trigger_no_model_call(self) -> None:
        """§34：短消息不触发 compact，摘要模型零调用。"""
        calls: list[str] = []
        compact.summarize_transcript = lambda t: calls.append(t) or _FAKE_SUMMARY
        session = self._session("q2")
        self.go(session.add_items([item for i in range(1, 11) for item in make_turn(i)]))
        prep = self.go(prepare_session_context(session))
        session.close()
        self.assertEqual(prep.action, "none")
        self.assertEqual(calls, [])


class SoftTriggerTests(ContextEnvMixin, unittest.TestCase):
    def test_prepare_triggers_compact_with_summary_and_recent(self) -> None:
        """§35：达到阈值触发 compact；摘要保存、recent 保留、当前无消息丢失。"""
        compact.summarize_transcript = lambda t: _FAKE_SUMMARY
        session = self._session("s1")
        self.go(session.add_items([item for i in range(1, 21) for item in make_turn(i)]))
        prep = self.go(prepare_session_context(session))
        after = self.go(session.get_items())
        session.close()
        self.assertEqual(prep.action, "compacted")
        self.assertGreater(prep.summary_chars, 0)
        events = [e["action"] for e in prep.events]
        self.assertIn(EV_COMPACTION_COMPLETED, events)
        self.assertNotIn(EV_WINDOWED, events)
        self.assertEqual(after[0]["role"], "system")
        self.assertIn("自动摘要", after[0]["content"])
        users = [i for i in after if i.get("role") == "user"]
        self.assertEqual(len(users), 4)

    def test_summary_transcript_excludes_project_context_and_memory(self) -> None:
        """§15/16/40-41：摘要只吃对话；Project Context 与记忆内容不进摘要。"""
        captured: list[str] = []
        compact.summarize_transcript = lambda t: captured.append(t) or _FAKE_SUMMARY
        session = self._session("s2")
        self.go(session.add_items([item for i in range(1, 21) for item in make_turn(i)]))
        self.go(prepare_session_context(session))
        session.close()
        self.assertTrue(captured)
        transcript = "\n".join(captured)
        for marker in ("【当前项目", "项目说明", "来源文件", "工作位置", "记忆范围",
                       "TEST_GLOBAL_LEAK_MARKER", "TEST_PROJECTA_LEAK"):
            self.assertNotIn(marker, transcript)

    def test_multi_cycle_summary_bounded_and_single(self) -> None:
        """§43/42：多次 compact 后 summary 有界，会话内只有一条头部摘要、无 ctx 副本。"""
        sizes: list[int] = []
        compact.summarize_transcript = lambda t: _FAKE_SUMMARY
        session = self._session("s3")
        items = []
        for cycle in range(5):
            items = []
            for i in range(1, 17):
                items += make_turn(cycle * 20 + i, pad=30)
            self.go(session.add_items(items))
            prep = self.go(prepare_session_context(session))
            if prep.action == "compacted":
                sizes.append(prep.summary_chars)
        after = self.go(session.get_items())
        session.close()
        self.assertGreaterEqual(len(sizes), 4)
        self.assertLessEqual(max(sizes), 3000)
        heads = [i for i in after
                 if i.get("role") == "system" and "自动摘要" in i.get("content", "")]
        self.assertEqual(len(heads), 1)


class HierarchicalMigrationTests(ContextEnvMixin, unittest.TestCase):
    def test_2400_items_history_chunked_and_bounded(self) -> None:
        """§37/29：超大历史分块摘要（多次 summarize），收敛 bounded、不 OOM。"""
        calls: list[str] = []
        compact.summarize_transcript = lambda t: calls.append(t) or _FAKE_SUMMARY
        session = self._session("h1")
        for start in range(1, 601, 100):
            batch = []
            for i in range(start, min(start + 100, 601)):
                batch += make_tool_turn(i)
            self.go(session.add_items(batch))
        before = self.go(session.get_items())
        self.assertGreaterEqual(len(before), 2400)
        prep = self.go(prepare_session_context(session))
        after = self.go(session.get_items())
        session.close()
        self.assertEqual(prep.action, "compacted")
        self.assertGreater(len(calls), 2)  # 分块 + 合并
        self.assertLess(len(after), 100)
        total_chars = sum(compact._item_rough_chars(i) for i in after)
        self.assertLess(total_chars, 20000)
        users = [i for i in after if i.get("role") == "user"]
        self.assertEqual(len(users), 4)


class HardWindowTests(ContextEnvMixin, unittest.TestCase):
    def test_summary_failure_then_hard_window(self) -> None:
        """§36：摘要失败 + 超硬阈值 → 硬窗口；不依赖模型、可继续。"""
        def broken(t: str) -> str:
            raise RuntimeError("summary model down")

        compact.summarize_transcript = broken
        session = self._session("w1")
        for start in range(1, 241, 60):
            batch = []
            for i in range(start, min(start + 60, 241)):
                batch += make_tool_turn(i)
            self.go(session.add_items(batch))
        before = self.go(session.get_items())
        prep = self.go(prepare_session_context(session))
        after = self.go(session.get_items())
        session.close()
        events = [e["action"] for e in prep.events]
        self.assertIn(EV_COMPACTION_FAILED, events)
        self.assertIn(EV_WINDOWED, events)
        self.assertEqual(prep.action, "windowed")
        self.assertLess(len(after), len(before))
        users = [i for i in after if i.get("role") == "user"]
        self.assertGreaterEqual(len(users), 1)
        self.assertLessEqual(len(after), 300)
        self.assertEqual(prep.summary_chars, 0)


class WindowPairIntegrityTests(unittest.TestCase):
    def test_tool_call_result_pairs_kept(self) -> None:
        """§38：窗口后 tool_call ↔ tool_result 成对完整。"""
        items = [item for i in range(1, 21) for item in make_tool_turn(i)]
        out = window_history(items, keep_turns=4, max_chars=100000, max_messages=500)
        calls = [i for i in out if i.get("type") == "function_call"]
        results = [i for i in out if i.get("type") == "function_call_output"]
        self.assertEqual(len(calls), len(results))
        self.assertEqual({c["call_id"] for c in calls},
                         {r["call_id"] for r in results})
        self.assertEqual(len(calls), 4)

    def test_window_budget_respected_and_boundary_clean(self) -> None:
        items = [item for i in range(1, 41) for item in make_tool_turn(i)]
        out = window_history(items, keep_turns=4, max_chars=9999999, max_messages=40)
        users = [i for i in out if i.get("role") == "user"]
        self.assertGreaterEqual(len(users), 1)
        self.assertLessEqual(len(out), 40)
        first = out[0]
        self.assertNotEqual(first.get("type"), "function_call")
        self.assertNotEqual(first.get("type"), "function_call_output")


class RunTurnIntegrationTests(ContextEnvMixin, unittest.TestCase):
    """§35/45/39：run_turn 主链（fake 模型 + 真实 SQLiteSession + 真实状态机）。"""

    def setUp(self) -> None:
        super().setUp()
        self.manager = TaskManager(os.path.join(self.tmp, "agent.db"))
        self.runtime = AgentRuntime(db_path=os.path.join(self.tmp, "agent.db"))
        self.runtime._initialized = True
        self.runtime._tools_patched = True
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self._orig_execute = main_module.execute_turn
        self._saved_router = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "off"
        self.last_agent = None

    def tearDown(self) -> None:
        main_module.execute_turn = self._orig_execute
        if self._saved_router is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._saved_router
        super().tearDown()

    def _install_fake(self, reply_text: str):
        async def fake_execute(mode, message, session=None, debug=False, max_turns=20,
                               history_limit=None, agent=None, audit=None, stream_events_cb=None):
            self.last_agent = agent
            return json.dumps({"kind": "answer", "summary": "ok", "content": reply_text,
                               "questions": [], "saved_file": None, "next_step": None},
                              ensure_ascii=False)
        main_module.execute_turn = fake_execute

    def _run(self, message: str, session, **kw) -> RunResult:
        return self.go(self.runtime.run_turn(message, session=session, session_id="unit",
                                              mode="sync", **kw))

    def test_short_chat_no_compact_still_completes(self) -> None:
        compact.summarize_transcript = lambda t: (_ for _ in ()).throw(
            AssertionError("短对话不应触发摘要模型"))
        self._install_fake("第1轮回答")
        session = self._session("unit")
        r1 = self._run("你好", session)
        r2 = self._run("继续", session)
        session.close()
        self.assertTrue(r1.ok and r2.ok)
        self.assertEqual(r1.task.state, TaskState.COMPLETED)
        prep = self.runtime._context_prep
        self.assertIsNotNone(prep)
        self.assertEqual(prep.action, "none")

    def test_long_history_compacts_inside_run_turn_and_completes(self) -> None:
        """§35：compact 在 run_turn 链（execute 前）真实发生，Completion 不受影响。"""
        calls: list[str] = []
        compact.summarize_transcript = lambda t: calls.append(t) or _FAKE_SUMMARY
        session = self._session("unit")
        self.go(session.add_items([item for i in range(1, 31) for item in make_turn(i)]))
        self._install_fake("长对话之后继续正常回答。")
        r = self._run("在长历史之后发的新消息", session)
        after = self.go(session.get_items())
        session.close()
        self.assertTrue(r.ok)
        self.assertEqual(r.task.state, TaskState.COMPLETED)
        self.assertEqual(self.runtime._context_prep.action, "compacted")
        self.assertTrue(calls)
        users = [i for i in after if i.get("role") == "user"]
        self.assertEqual(len(users), 4)

    def test_attachment_still_injected_after_compact(self) -> None:
        """§39：compact 后下一条消息的附件仍然注入指令。"""
        compact.summarize_transcript = lambda t: _FAKE_SUMMARY
        session = self._session("unit")
        self.go(session.add_items([item for i in range(1, 31) for item in make_turn(i)]))
        self._install_fake("分析完成。")
        pid = self.manager.get_or_create_container("unit")["id"]
        # 预创建 run + 绑定附件（对应 web 的 messages/create 路径）
        goal = "分析这个文件。"
        run = self.manager.create_task(session_id="unit", goal=goal,
                                       metadata={"channel": "web"}, thread_id=pid)
        msg = self.manager.add_message(pid, "user", goal, run_id=run.id)
        att = self.manager.add_message_attachment(
            task_id=pid, display_name="test.xlsx", stored_path="F:\\tmp\\test.xlsx",
            mime_type="application/vnd.ms-excel", size_bytes=2048, sha256="0" * 64,
        )
        self.manager.bind_message_attachments([att["id"]], msg["id"], run.id)
        r = self._run(goal, session, task_id=run.id)
        session.close()
        self.assertTrue(r.ok)
        instructions = str(getattr(self.last_agent, "instructions", "") or "")
        self.assertIn("本次消息附件", instructions)
        self.assertIn("test.xlsx", instructions)

    def test_restart_does_not_reinflate(self) -> None:
        """§46：compact 持久化到 Session；重开 Session 不恢复旧历史。"""
        compact.summarize_transcript = lambda t: _FAKE_SUMMARY
        session = self._session("r1")
        self.go(session.add_items([item for i in range(1, 25) for item in make_turn(i)]))
        self.go(prepare_session_context(session))
        session.close()
        reopened = self._session("r1")  # 模拟重启后重开同一 Session
        after = self.go(reopened.get_items())
        reopened.close()
        users = [i for i in after if i.get("role") == "user"]
        self.assertLessEqual(len(users), 4)
        self.assertEqual(after[0]["role"], "system")


class TokenCurveTests(ContextEnvMixin, unittest.TestCase):
    def test_60_turn_curve_sawtooth_bounded(self) -> None:
        """§44：60 轮 est_tokens 曲线为锯齿有界，不持续线性增长。"""
        compact.summarize_transcript = lambda t: _FAKE_SUMMARY
        curve: list[int] = []
        compactions = 0
        session = self._session("curve")
        for step in range(1, 61):
            self.go(session.add_items(make_turn(step, pad=40)))
            prep = self.go(prepare_session_context(session))
            items = self.go(session.get_items())
            curve.append(sum(compact._item_rough_chars(i) for i in items))
            if prep.action == "compacted":
                compactions += 1
        session.close()
        self.assertGreaterEqual(compactions, 3)
        self.assertLess(max(curve[45:]), 25000)  # 后期也保持有界
        self.assertLess(max(curve), 60000)


class AsyncCompactTests(ContextEnvMixin, unittest.TestCase):
    """Phase 2：摘要异步化（不阻塞其他 Project）+ per-session 锁（不双压缩）。"""

    def test_same_session_concurrent_prepare_compacts_once(self) -> None:
        import time as _time

        calls: list[str] = []

        def slow_summarize(transcript: str) -> str:
            calls.append(transcript)
            _time.sleep(0.15)
            return _FAKE_SUMMARY

        compact.summarize_transcript = slow_summarize
        session = self._session("dup")
        self.go(session.add_items([item for i in range(1, 21) for item in make_turn(i)]))

        async def both() -> tuple:
            return await asyncio.gather(
                prepare_session_context(session), prepare_session_context(session))

        r1, r2 = self.go(both())
        after = self.go(session.get_items())
        session.close()
        self.assertEqual(len(calls), 1)  # 同会话不会并行 compact 两次
        actions = sorted({r1.action, r2.action})
        self.assertIn("compacted", actions)
        users = [i for i in after if i.get("role") == "user"]
        self.assertEqual(len(users), 4)
        heads = [i for i in after if i.get("role") == "system" and "自动摘要" in i.get("content", "")]
        self.assertEqual(len(heads), 1)

    def test_slow_compact_does_not_block_other_session(self) -> None:
        import time as _time

        def slow_summarize(transcript: str) -> str:
            _time.sleep(0.5)
            return _FAKE_SUMMARY

        compact.summarize_transcript = slow_summarize
        big = self._session("big")
        small = self._session("small")
        self.go(big.add_items([item for i in range(1, 21) for item in make_turn(i)]))
        self.go(small.add_items(make_turn(1, pad=20)))

        async def scenario() -> dict:
            order: list[str] = []
            started = _time.monotonic()
            a_task = asyncio.create_task(prepare_session_context(big))

            async def quick() -> dict:
                await asyncio.sleep(0.05)  # A 摘要线程正在跑（0.5s）
                order.append("quick_done")
                prep = await prepare_session_context(small)
                order.append("quick_returned")
                return {"elapsed": _time.monotonic() - started, "action": prep.action}

            q = await quick()
            await a_task
            big.close()
            small.close()
            return {"order": order, "quick_elapsed": round(q["elapsed"], 2)}

        res = self.go(scenario())
        self.assertLess(res["quick_elapsed"], 0.4)  # B 未等 A 的 0.5s 摘要
        self.assertEqual(res["order"][0], "quick_done")


if __name__ == "__main__":
    unittest.main()

