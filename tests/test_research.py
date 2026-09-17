import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import research


class QueryParseTests(unittest.TestCase):
    def test_fenced_json(self) -> None:
        text = '好的：```json\n{"queries": ["A是什么", "B 现状", "B vs A", "case study"]}\n```'
        self.assertEqual(len(research._parse_queries(text)), 4)

    def test_plain_line_list(self) -> None:
        text = "1. 关键词一\n2. 关键词二\n3. 关键词三"
        out = research._parse_queries(text)
        self.assertGreaterEqual(len(out), 3)

    def test_dedupes(self) -> None:
        text = '{"queries": ["重复词", "重复词", "另一个"]}'
        self.assertEqual(len(research._parse_queries(text)), 2)


class SearchPageParseTests(unittest.TestCase):
    def test_parses_numbered_blocks(self) -> None:
        page = (
            "1. 标题甲\n"
            "   链接: https://a.example.com/1\n"
            "   摘要: 这是关于甲的内容摘要。\n"
            "\n2. 标题乙\n"
            "   链接: https://b.example.com/2\n"
            "   摘要: 乙的相关介绍。"
        )
        out = research._parse_search_page(page)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["url"], "https://a.example.com/1")
        self.assertIn("甲", out[0]["title"])
        self.assertIn("内容摘要", out[0]["snippet"])

    def test_no_results_empty(self) -> None:
        self.assertEqual(research._parse_search_page("三个搜索源都失败了。"), [])


class FormatReturnTests(unittest.TestCase):
    def test_includes_path_and_excerpt(self) -> None:
        text = research._format_return("主题", "F:/notes/report.md", "报告正文……", 4, 9, 30.0)
        self.assertIn("4 个检索词", text)
        self.assertIn("9 个来源", text)
        self.assertIn("F:/notes/report.md", text)
        self.assertIn("报告正文", text)

    def test_long_report_truncated_notice(self) -> None:
        text = research._format_return("主题", "p", "字" * 3000, 4, 9, 1.0)
        self.assertIn("完整报告见保存的文件", text)
        self.assertLess(len(text), 2600)


class DeepResearchPipelineTests(unittest.TestCase):
    """假模型 + 假搜索全流程：验证两轮检索 → 报告落盘。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="research_test_"))
        self._orig_llm = research._llm_text
        self._orig_search = research._run_searches
        self._orig_notes = None
        import tools

        self._orig_notes = tools.NOTES_DIR
        tools.NOTES_DIR = self._tmp / "notes"
        self._calls = []

    def tearDown(self) -> None:
        research._llm_text = self._orig_llm
        research._run_searches = self._orig_search
        import tools

        tools.NOTES_DIR = self._orig_notes

    def _fake_llm(self, system: str, user: str, max_tokens: int = 1200) -> str:
        self._calls.append(system)
        n = len(self._calls)
        if n == 1:
            return '{"scope": "了解 A 与 B", "queries": ["A 介绍", "B 介绍", "A vs B", "A 缺点"]}'
        if n == 2:
            return '{"queries": ["A 最新进展"]}'
        return "# 调研报告\n\n一句话结论：A 和 B 各有优劣。\n\n## 正文\n\nA 是……（来源 [1]）。\n\n## 来源\n\n[1] 标题：A 官网 https://a.example.com"

    def _fake_search(self, queries: list[str]) -> list[dict]:
        hits = []
        for q in queries:
            hits.append(
                {
                    "query": q,
                    "title": f"关于{q}的文章",
                    "url": f"https://example.com/{len(hits)}",
                    "snippet": f"这是与 {q} 相关的摘要内容。",
                }
            )
        return hits

    def test_full_pipeline_saves_report(self) -> None:
        research._llm_text = self._fake_llm
        research._run_searches = self._fake_search
        out = research.deep_research_impl("A 和 B 对比调研", max_queries=12)
        self.assertEqual(len(self._calls), 3, "应发生 规划→追问→综合 三次模型调用")
        self.assertIn("已保存", out)
        self.assertIn("调研报告", out)
        saved = list((self._tmp / "notes").glob("*.md"))
        self.assertEqual(len(saved), 1)
        content = saved[0].read_text(encoding="utf-8")
        self.assertIn("A 和 B", content)

    def test_failed_plan_returns_error(self) -> None:
        def broken(*args, **kwargs):
            return "抱歉，本次无法输出内容。"

        research._llm_text = broken
        research._run_searches = self._fake_search
        out = research.deep_research_impl("某主题")
        self.assertIn("失败", out)


if __name__ == "__main__":
    unittest.main()
