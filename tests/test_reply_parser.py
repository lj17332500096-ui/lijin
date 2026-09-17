"""ReplyParser 容错管线测试：格式问题绝不导致失败（15+ 场景）。"""

import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from schemas import AgentReply
from runtime.reply_parser import ParseResult, coerce_reply, normalize, parse, structured_output_supported


def reply_json(**overrides) -> str:
    data = {
        "kind": "answer", "summary": "一句话摘要", "content": "正文内容",
        "questions": [], "saved_file": None, "next_step": None,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class ReplyParserTests(unittest.TestCase):
    def test_normal_json(self) -> None:
        r = parse(reply_json())
        self.assertTrue(r.ok)
        self.assertEqual(r.stage, "direct")
        self.assertEqual(r.canonical["kind"], "answer")
        self.assertEqual(r.canonical["content"], "正文内容")
        self.assertEqual(r.warnings, [])

    def test_missing_optional_fields(self) -> None:
        r = parse('{"kind": "answer", "content": "只有核心字段"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["questions"], [])
        self.assertIsNone(r.canonical["saved_file"])
        self.assertEqual(r.canonical["ui"], [])

    def test_field_order_changed(self) -> None:
        r = parse('{"content": "反序", "questions": [], "next_step": null, "kind": "answer"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["content"], "反序")

    def test_markdown_fence(self) -> None:
        r = parse("```json\n" + reply_json() + "\n```")
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["content"], "正文内容")

    def test_leading_prose(self) -> None:
        r = parse("好的，这就回答：\n" + reply_json())
        self.assertTrue(r.ok)

    def test_trailing_prose(self) -> None:
        r = parse(reply_json() + "\n\n以上是我的回答，请查收。")
        self.assertTrue(r.ok)

    def test_type_alias_for_kind(self) -> None:
        r = parse('{"type": "plan", "message": "第一步：读代码。", "summary": ""}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["kind"], "plan")
        self.assertEqual(r.canonical["content"], "第一步：读代码。")
        self.assertIn("kind", r.canonical)

    def test_message_alias_for_content(self) -> None:
        r = parse('{"kind": "answer", "message": "别名内容"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["content"], "别名内容")

    def test_questions_missing_defaults(self) -> None:
        r = parse('{"kind": "answer", "content": "没有追问"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["questions"], [])

    def test_ui_missing_defaults(self) -> None:
        r = parse('{"kind": "answer", "content": "x", "questions": [], "saved_file": null}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["ui"], [])

    def test_saved_file_missing_defaults(self) -> None:
        r = parse('{"kind": "note", "content": "产出"}')
        self.assertTrue(r.ok)
        self.assertIsNone(r.canonical["saved_file"])

    def test_invalid_json_repaired_locally(self) -> None:
        # 尾逗号 + 智能引号 → 本地修复
        raw = '{"kind": "answer", "content": "已修复", "questions": [], }'
        r = parse(raw)
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["content"], "已修复")
        self.assertIn("repair", r.stage)

    def test_repair_fails_but_readable_text_falls_back(self) -> None:
        r = parse("今天的天气很不错，适合出门散步。")
        self.assertTrue(r.ok)
        self.assertEqual(r.stage, "fallback")
        self.assertEqual(r.canonical["kind"], "answer")
        self.assertIn("天气", r.canonical["content"])
        self.assertTrue(any("reply_validation_warning" in w or "降级" in w for w in r.warnings))

    def test_unknown_kind_falls_back_to_answer(self) -> None:
        r = parse('{"kind": "chatgpt_style", "content": "内容"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["kind"], "answer")

    def test_provider_output_differences(self) -> None:
        # 1) dict 形态（部分 provider 返回对象）
        self.assertTrue(parse({"kind": "answer", "content": "dict形态"}).ok)
        # 2) AgentReply 实例
        inst = AgentReply(kind="answer", summary="s", content="inst形态")
        self.assertTrue(parse(inst).ok)
        # 3) 带 summary 无 content
        r = parse('{"kind": "done", "summary": "收尾了"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["content"], "收尾了")
        # 4) questions 形态
        r = parse('{"kind": "questions", "question": ["出发地是哪里？"]}')
        self.assertTrue(r.ok)
        self.assertEqual(r.canonical["questions"], ["出发地是哪里？"])

    def test_empty_output_not_ok(self) -> None:
        r = parse("   ")
        self.assertFalse(r.ok)

    def test_secret_not_dropped_but_canonical_ok(self) -> None:
        # parser 不管安全（guardrail 负责），但结果必须可解析
        r = parse(reply_json(content="密钥 sk-" + "a" * 24))
        self.assertTrue(r.ok)

    def test_coerce_never_raises(self) -> None:
        for sample in ["纯文本而已", "```json\n" + reply_json() + "\n```", "", None, 123]:
            reply = coerce_reply(sample)
            self.assertIsInstance(reply, AgentReply)

    def test_structured_output_capability_honest(self) -> None:
        supported, why = structured_output_supported()
        self.assertIsInstance(supported, bool)
        self.assertTrue(why)


class NormalizerTests(unittest.TestCase):
    def test_questions_single_string(self) -> None:
        out, warnings = normalize({"kind": "questions", "question": "只有一个？"})
        self.assertEqual(out["questions"], ["只有一个？"])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
