import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import compact
from agents.memory import SQLiteSession


def make_items(turns: int = 20, content: str | None = None) -> list[dict]:
    items: list[dict] = []
    for i in range(turns):
        items.append({"role": "user", "content": content or f"第{i + 1}轮：聊聊旅行App的拍照整理功能"})
        items.append(
            {
                "role": "assistant",
                "content": (
                    '{"kind":"answer","summary":"第%d轮回答","content":"第%d轮的回答正文",'
                    '"questions":[],"saved_file":null,"next_step":null}'
                )
                % (i + 1, i + 1),
            }
        )
    return items


def user_turn_count(items: list) -> int:
    return sum(1 for item in items if item.get("role") == "user")


_TEST_ENV = {
    "AUTO_SUMMARY_MIN_TURNS": "3",
    "AUTO_SUMMARY_TRIGGER_TURNS": "14",
    "AUTO_SUMMARY_TRIGGER_CHARS": "5000",
    "AUTO_SUMMARY_KEEP_TURNS": "6",
    "AUTO_SUMMARY_TRANSCRIPT_CAP": "40000",
}


class CompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="agent_compact_test_")
        self.db = os.path.join(self.tmp, "session.sqlite")
        self.orig_summarize = compact.summarize_transcript
        self.orig_summary_dir = compact.SUMMARY_DIR
        compact.SUMMARY_DIR = Path(self.tmp) / "summaries"
        self._saved_env = {}
        for key, value in _TEST_ENV.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value

    def tearDown(self) -> None:
        compact.summarize_transcript = self.orig_summarize
        compact.SUMMARY_DIR = self.orig_summary_dir
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _session(self) -> SQLiteSession:
        return SQLiteSession("compact_t", db_path=self.db)

    def test_long_session_gets_compacted(self) -> None:
        compact.summarize_transcript = lambda transcript: (
            "测试摘要：用户想做一个旅行照片整理 App，功能清单已确定，下一步写产品方案。"
        )

        async def run() -> tuple[str | None, list, list]:
            session = self._session()
            await session.add_items(make_items(20))
            before = await session.get_items()
            summary = await compact.maybe_compact(session)
            after = await session.get_items()
            session.close()
            return summary, before, after

        summary, before, after = asyncio.run(run())
        self.assertIsNotNone(summary)
        self.assertEqual(len(before), 40)
        self.assertLess(len(after), len(before))
        self.assertEqual(after[0]["role"], "system")
        self.assertIn("自动摘要", after[0]["content"])
        self.assertEqual(user_turn_count(after), compact.KEEP_USER_TURNS)

    def test_short_session_not_compacted(self) -> None:
        compact.summarize_transcript = lambda transcript: "不应被调用"

        async def run() -> str | None:
            session = self._session()
            await session.add_items(make_items(4))
            summary = await compact.maybe_compact(session)
            session.close()
            return summary

        self.assertIsNone(asyncio.run(run()))

    def test_summary_failure_rolls_back(self) -> None:
        def broken(transcript: str) -> str:
            raise RuntimeError("模型挂了")

        compact.summarize_transcript = broken

        async def run() -> tuple[str | None, int]:
            session = self._session()
            await session.add_items(make_items(20))
            summary = await compact.maybe_compact(session)
            after = await session.get_items()
            session.close()
            return summary, len(after)

        summary, count = asyncio.run(run())
        self.assertIsNone(summary)
        self.assertEqual(count, 40)

    def test_previous_summary_kept_in_transcript(self) -> None:
        compact.summarize_transcript = lambda transcript: transcript

        async def run() -> str | None:
            session = self._session()
            items = [{"role": "system", "content": compact._SUMMARY_MARK + " · 2026-01-01]\n旧摘要"}]
            items += make_items(20)
            await session.add_items(items)
            summary = await compact.maybe_compact(session)
            session.close()
            return summary

        summary = asyncio.run(run())
        self.assertIsNotNone(summary)
        self.assertIn("先前摘要", summary)
        self.assertIn("旧摘要", summary)

    def test_tool_outputs_count_toward_budget(self) -> None:
        """工具输出内容也应计入预算（防止实际上下文被大工具结果撑爆）。"""
        compact.summarize_transcript = lambda transcript: "摘要"
        os.environ["AUTO_SUMMARY_TRIGGER_TURNS"] = "999"  # 轮数不触发
        os.environ["AUTO_SUMMARY_TRIGGER_CHARS"] = "3000"  # 只靠字符触发

        async def run() -> str | None:
            session = self._session()
            items: list[dict] = []
            for i in range(10):
                items.append({"role": "user", "content": f"第{i}轮问题"})
                items.append(
                    {"role": "assistant", "content": '{"kind":"answer","summary":"s","content":"正文","questions":[]}'}
                )
                items.append({"type": "function_call_output", "role": "tool", "output": "x" * 2000})
            await session.add_items(items)
            summary = await compact.maybe_compact(session)
            session.close()
            return summary

        summary = asyncio.run(run())
        self.assertIsNotNone(summary)  # 2 万字符的工具输出触发了预算

    def test_env_thresholds_refresh(self) -> None:
        os.environ["AUTO_SUMMARY_TRIGGER_TURNS"] = "77"
        compact._refresh_thresholds()
        self.assertEqual(compact.TRIGGER_USER_TURNS, 77)
        os.environ["AUTO_SUMMARY_TRIGGER_TURNS"] = _TEST_ENV["AUTO_SUMMARY_TRIGGER_TURNS"]
        compact._refresh_thresholds()
        self.assertEqual(compact.TRIGGER_USER_TURNS, 14)


if __name__ == "__main__":
    unittest.main()
