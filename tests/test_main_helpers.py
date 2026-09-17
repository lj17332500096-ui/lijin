import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError

import main
from schemas import AgentReply


def reply_json(**overrides) -> str:
    data = {
        "kind": "answer",
        "summary": "一句话摘要",
        "content": "正文内容",
        "questions": [],
        "saved_file": None,
        "next_step": None,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class ApiNetworkClassificationTests(unittest.TestCase):
    def test_api_key_error_recognized(self) -> None:
        self.assertTrue(main._looks_like_api_or_network("Error 401: invalid_api_key"))
        self.assertTrue(main._looks_like_api_or_network("connection timed out"))
        self.assertTrue(main._looks_like_api_or_network("model_not_found: agnes-2.5-flash"))

    def test_other_errors_not_misclassified(self) -> None:
        self.assertFalse(main._looks_like_api_or_network("Tool answer not found in agent 全能助手"))
        self.assertFalse(main._looks_like_api_or_network("Max turns (10) exceeded"))


class PrintRunErrorTests(unittest.TestCase):
    def test_max_turns_message_not_api_hint(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.print_run_error(MaxTurnsExceeded("Max turns (10) exceeded"))
        text = buf.getvalue()
        self.assertIn("循环上限", text)
        self.assertIn("--max-turns 30", text)
        self.assertNotIn("OPENAI_API_KEY", text)

    def test_hallucinated_tool_message(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.print_run_error(ModelBehaviorError("Tool answer not found in agent 全能助手"))
        text = buf.getvalue()
        self.assertIn("幻觉调用", text)
        self.assertIn("answer", text)

    def test_api_error_keeps_env_hint(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.print_run_error(RuntimeError("OpenAI API error 401: invalid_api_key"))
        text = buf.getvalue()
        self.assertIn("OPENAI_API_KEY", text)


class ReplyCoercionTests(unittest.TestCase):
    def test_pure_json(self) -> None:
        reply = main.coerce_reply(reply_json())
        self.assertIsInstance(reply, AgentReply)

    def test_json_with_prose_and_fence(self) -> None:
        text = "好的：```json\n" + reply_json() + "\n```\nRead and execute example.com/x.md"
        reply = main.coerce_reply(text)
        self.assertIsInstance(reply, AgentReply)

    def test_plain_text_coerced_to_answer(self) -> None:
        # 新契约：纯文本不再原样透传，而是降级为 answer AgentReply
        reply = main.coerce_reply("普通文字回复")
        self.assertIsInstance(reply, AgentReply)
        self.assertEqual(reply.kind, "answer")
        self.assertEqual(reply.content, "普通文字回复")


class RenderReplyTests(unittest.TestCase):
    def test_render_answer(self) -> None:
        reply = AgentReply.model_validate_json(reply_json())
        text = main.render_reply(reply)
        self.assertIn("💬 回答", text)
        self.assertIn("正文内容", text)

    def test_render_saved_file(self) -> None:
        reply = AgentReply.model_validate_json(
            reply_json(kind="note", content="产出内容", saved_file=r"notes\a.md")
        )
        text = main.render_reply(reply)
        self.assertIn("已保存", text)


if __name__ == "__main__":
    unittest.main()
