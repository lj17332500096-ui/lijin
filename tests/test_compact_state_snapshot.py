"""P0-B-2 压缩指令增强回归（2026-09-21，上下文工程三层之层 2）。

按《Practical Guide to Context Engineering》把 compact.py 的 _SUMMARY_SYSTEM
从「自由散文摘要」升级为 ClaudeCode 8 节 state_snapshot 结构 + 压缩开篇语。
本组用例钉死：
1. _SUMMARY_SYSTEM 含 8 个 XML 节（task_intent / tech_context / code_changes /
   errors_fixes / current_status / pending_tasks / user_preferences / key_decisions）；
2. _SUMMARY_SYSTEM 含「结构化 8 节」「XML」语义，不用 JSON；
3. _summary_item 产出含开篇语 _SUMMARY_OPENING + _SUMMARY_MARK + 摘要正文；
4. _SUMMARY_OPENING 提示「不要重复执行已完成步骤」（降低压缩后断片/重蹈）；
5. 旧 _SUMMARY_MARK 保持（_is_previous_summary 向后兼容不破坏）；
6. SUMMAY_MAX_CHARS 升到 1600（8 节 XML 需更大空间）。

零模型调用——summarize_transcript 被直接打桩，只测提示词与产物结构。
"""
from __future__ import annotations

import unittest

from runtime import compact

# 参考库 8 节 XML 标签名（与 compact._SUMMARY_SYSTEM 对齐）
EIGHT_SECTIONS = [
    "task_intent", "tech_context", "code_changes", "errors_fixes",
    "current_status", "pending_tasks", "user_preferences", "key_decisions",
]


class SummarySystemEightSectionTests(unittest.TestCase):
    """_SUMMARY_SYSTEM 提示词结构。"""

    def test_contains_all_eight_xml_sections(self) -> None:
        for sec in EIGHT_SECTIONS:
            self.assertIn(f"<{sec}>", compact._SUMMARY_SYSTEM,
                          f"提示词缺 {sec} 节")

    def test_uses_xml_not_json(self) -> None:
        self.assertIn("XML", compact._SUMMARY_SYSTEM)
        # 明确禁止改用 JSON（参考库：Claude 模型对 XML 更友好）
        self.assertIn("不要改用 JSON", compact._SUMMARY_SYSTEM)

    def test_state_snapshot_tag(self) -> None:
        self.assertIn("<state_snapshot>", compact._SUMMARY_SYSTEM)

    def test_8_section_algorithm_mentioned(self) -> None:
        self.assertIn("8 节", compact._SUMMARY_SYSTEM)


class SummaryOpeningTests(unittest.TestCase):
    """压缩开篇语（_SUMMARY_OPENING + _summary_item 接入）。"""

    def test_opening_constants_present(self) -> None:
        self.assertTrue(hasattr(compact, "_SUMMARY_OPENING"))
        self.assertIn("结构化 8 节算法压缩", compact._SUMMARY_OPENING)
        self.assertIn("可无缝继续", compact._SUMMARY_OPENING)

    def test_summary_item_prepends_opening(self) -> None:
        item = compact._summary_item("测试摘要正文")
        content = item["content"]
        self.assertIn(compact._SUMMARY_OPENING, content)
        self.assertIn(compact._SUMMARY_MARK, content)
        self.assertIn("测试摘要正文", content)
        # 开篇语在摘要正文之前
        self.assertLess(
            content.index(compact._SUMMARY_OPENING),
            content.index("测试摘要正文"),
        )

    def test_opening_warns_against_reexecuting(self) -> None:
        # 开篇语要提示「不要重复执行已完成步骤」——降低压缩后断片率的核心
        self.assertIn("不要重复执行", compact._SUMMARY_OPENING)

    def test_previous_summary_detection_unchanged(self) -> None:
        # _SUMMARY_MARK 保持原值，_is_previous_summary 向后兼容
        item = compact._summary_item("x")
        self.assertTrue(compact._is_previous_summary(item))


class SummaryMaxCharsTests(unittest.TestCase):
    def test_max_chars_raised_for_8_section(self) -> None:
        # P0-B-2：8 节 XML 密集摘要需要比旧版 1200 更大的空间
        self.assertGreaterEqual(compact.SUMMARY_MAX_CHARS, 1600)


class MergeStillBoundedTests(unittest.TestCase):
    """_merge_summaries 仍按 SUMMARY_MAX_CHARS 截断（不破坏既有有界性）。"""

    def test_single_summary_truncates_to_max(self) -> None:
        import asyncio
        long = "字" * 4000
        out = asyncio.run(compact._merge_summaries([long]))
        self.assertLessEqual(len(out), compact.SUMMARY_MAX_CHARS)

    def test_two_short_summaries_inline_merge(self) -> None:
        import asyncio
        out = asyncio.run(compact._merge_summaries(["短1", "短2"]))
        self.assertIn("短1", out)
        self.assertIn("短2", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
