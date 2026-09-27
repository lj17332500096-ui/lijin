"""P0-B-1 工具消息裁剪回归测试。

覆盖《Practical Guide to Context Engineering》三层体系里的「层 1 · 工具消息裁剪」
（FORGE 此前缺失的中间档：compact 要 60 轮才触发的重型 LLM 摘要、window_history
是超硬限才整轮丢；层 1 专治「工具碎片堆叠占上下文」，零模型调用）。

验收点（对应 FORGE-ARCHITECTURE-AUDIT §2.2 P0-B-1）：
1. identify_tool_rounds：function_call ↔ function_call_output 成对识别，缺结果不崩；
2. trim_tool_messages：保留最近 keep_rounds 轮工具调用，较早的裁成一条 system 占位；
3. 成对约束：裁剪后绝不留孤儿 function_call_output；
4. 非工具消息（system/user/assistant）与摘要不受影响；
5. 纯函数零模型调用、可独立回滚（FORGE_TOOL_TRIM_GUARD=off 完全不裁）；
6. prepare_session_context 中间档触发 EV_TOOL_TRIMMED，短会话/未达阈值不裁。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from runtime import compact
from agents.memory import SQLiteSession

from runtime import context
from runtime.context import (
    EV_TOOL_TRIMMED,
    EV_METRICS,
    identify_tool_rounds,
    prepare_session_context,
    trim_tool_messages,
)


def _tool_turn(n: int, *, pad: int = 200) -> list[dict]:
    """构造一个「含 2 次工具调用」的用户轮（复用 test_context_guard.make_tool_turn 模式）。"""
    return [
        {"role": "user", "content": "第%d轮：运行代码并搜索" % n},
        {"role": "assistant", "content": json.dumps(
            {"kind": "answer", "summary": "s%d" % n, "content": "第%d轮回答" % n,
             "questions": [], "saved_file": None, "next_step": None}, ensure_ascii=False)},
        {"type": "function_call", "name": "run_python", "arguments": '{"code": "print(%d)" % n}',
         "call_id": "call_a_%d" % n},
        {"type": "function_call_output", "call_id": "call_a_%d" % n,
         "output": "stdout [ok] " + ("x" * pad)},
        {"type": "function_call", "name": "web_search", "arguments": '{"query": "q%d" % n}',
         "call_id": "call_b_%d" % n},
        {"type": "function_call_output", "call_id": "call_b_%d" % n,
         "output": "结果片段 " + ("y" * pad)},
    ]


class IdentifyRoundsTests(unittest.TestCase):
    def test_pairs_call_with_output_by_call_id(self) -> None:
        items = _tool_turn(1)
        rounds = identify_tool_rounds(items)
        self.assertEqual(len(rounds), 2)
        # 每个 (call_idx, output_idx)：call_idx 是 function_call，output_idx 是同 call_id 的 output
        for ci, oi in rounds:
            self.assertEqual(items[ci].get("type"), "function_call")
            self.assertGreaterEqual(oi, 0)
            self.assertEqual(items[oi].get("type"), "function_call_output")
            self.assertEqual(items[ci].get("call_id"), items[oi].get("call_id"))

    def test_call_without_output_gets_minus_one(self) -> None:
        items = [
            {"role": "user", "content": "hi"},
            {"type": "function_call", "name": "t", "arguments": "{}", "call_id": "c1"},
            # 故意不给 call_id=c1 的 output
        ]
        rounds = identify_tool_rounds(items)
        self.assertEqual(rounds, [(1, -1)])


class TrimPureFunctionTests(unittest.TestCase):
    def test_keeps_recent_rounds_trims_old(self) -> None:
        items = []
        for n in range(1, 6):
            items.extend(_tool_turn(n))  # 5 轮 × 2 = 10 个 function_call
        out = trim_tool_messages(items, keep_rounds=4)  # 保留最近 4 个工具调用
        calls = [i for i in out if i.get("type") == "function_call"]
        # 10 个 call 保留最近 4 个 → 裁掉最早 6 个
        self.assertEqual(len(calls), 4)
        # 占位摘要恰好 1 条
        placeholders = [i for i in out if i.get("role") == "system" and "工具消息裁剪" in i.get("content", "")]
        self.assertEqual(len(placeholders), 1)

    def test_no_orphan_output_after_trim(self) -> None:
        """成对约束：裁剪后每个 function_call_output 必须能找到同 call_id 的 function_call。"""
        items = []
        for n in range(1, 8):
            items.extend(_tool_turn(n))
        out = trim_tool_messages(items, keep_rounds=2)
        kept_call_ids = {i.get("call_id") for i in out if i.get("type") == "function_call"}
        for i in out:
            if i.get("type") == "function_call_output":
                self.assertIn(i.get("call_id"), kept_call_ids,
                              "孤儿 function_call_output 未被成对删除: %r" % (i.get("call_id"),))

    def test_short_history_returns_unchanged(self) -> None:
        items = _tool_turn(1)  # 只有 2 轮工具调用
        out = trim_tool_messages(items, keep_rounds=2)
        self.assertEqual(out, items)

    def test_non_tool_messages_always_kept(self) -> None:
        items = []
        for n in range(1, 6):
            items.extend(_tool_turn(n))
        out = trim_tool_messages(items, keep_rounds=0)  # 最激进：全裁
        # user / assistant 全保留
        users = [i for i in out if i.get("role") == "user"]
        assistants = [i for i in out if i.get("role") == "assistant"]
        self.assertEqual(len(users), 5)
        self.assertEqual(len(assistants), 5)
        # 工具调用被裁成 1 条占位，0 条真实 call/output
        self.assertEqual(len([i for i in out if i.get("type") == "function_call"]), 0)
        self.assertEqual(len([i for i in out if i.get("type") == "function_call_output"]), 0)
        self.assertEqual(len([i for i in out if i.get("role") == "system" and "工具消息裁剪" in i.get("content", "")]), 1)


class PrepareToolTrimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tool_trim_")
        self.db = os.path.join(self.tmp, "sessions.sqlite")
        self.orig_summarize = compact.summarize_transcript
        compact.summarize_transcript = lambda t: "摘要"
        compact.SUMMARY_DIR = Path(self.tmp) / "summaries"
        self._saved: dict[str, str | None] = {}
        for k in ("FORGE_TOOL_TRIM_GUARD", "FORGE_TOOL_TRIM_KEEP_ROUNDS", "FORGE_TOOL_TRIM_MIN_CHARS",
                  "FORGE_CONTEXT_GUARD", "FORGE_HISTORY_HARD_CHARS", "FORGE_HISTORY_HARD_MESSAGES",
                  "AUTO_SUMMARY_TRIGGER_TURNS", "AUTO_SUMMARY_TRIGGER_CHARS"):
            self._saved[k] = os.environ.get(k)
            if k in ("FORGE_TOOL_TRIM_GUARD", "FORGE_CONTEXT_GUARD"):
                os.environ[k] = "on"
            elif k in ("FORGE_TOOL_TRIM_KEEP_ROUNDS",):
                os.environ[k] = "2"
            elif k in ("FORGE_TOOL_TRIM_MIN_CHARS",):
                os.environ[k] = "100"  # 低阈值，确保裁剪触发
            elif k in ("FORGE_HISTORY_HARD_CHARS", "FORGE_HISTORY_HARD_MESSAGES"):
                os.environ[k] = "999999999"  # 抬到不触发 Hard Window，隔离中间档
            elif k in ("AUTO_SUMMARY_TRIGGER_TURNS",):
                os.environ[k] = "9999"  # 抬到不触发 compact，隔离中间档
            elif k in ("AUTO_SUMMARY_TRIGGER_CHARS",):
                os.environ[k] = "999999999"

    def tearDown(self) -> None:
        compact.summarize_transcript = self.orig_summarize
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _session(self, sid: str = "trim") -> SQLiteSession:
        return SQLiteSession(sid, db_path=self.db)

    def go(self, coro):
        return asyncio.run(coro)

    def test_prepare_emits_tool_trimmed_event(self) -> None:
        session = self._session("p1")
        for n in range(1, 8):
            self.go(session.add_items(_tool_turn(n)))
        prep = self.go(prepare_session_context(session))
        session.close()
        self.assertIn(prep.action, ("tool_trimmed", "compacted", "windowed"))
        events = [e["action"] for e in prep.events]
        self.assertIn(EV_TOOL_TRIMMED, events)

    def test_off_switch_does_not_trim(self) -> None:
        os.environ["FORGE_TOOL_TRIM_GUARD"] = "off"
        session = self._session("p2")
        for n in range(1, 8):
            self.go(session.add_items(_tool_turn(n)))
        prep = self.go(prepare_session_context(session))
        session.close()
        self.assertNotIn(EV_TOOL_TRIMMED, [e["action"] for e in prep.events])

    def test_short_history_not_trimmed(self) -> None:
        session = self._session("p3")
        self.go(session.add_items(_tool_turn(1)))  # 只有 1 轮
        prep = self.go(prepare_session_context(session))
        session.close()
        self.assertEqual(prep.action, "none")
        self.assertNotIn(EV_TOOL_TRIMMED, [e["action"] for e in prep.events])


if __name__ == "__main__":
    unittest.main(verbosity=2)
