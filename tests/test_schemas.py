import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from pydantic import ValidationError

from schemas import AgentReply


def valid_reply() -> dict:
    return {
        "kind": "answer",
        "summary": "一句话摘要",
        "content": "正文内容",
        "questions": [],
        "saved_file": None,
        "next_step": None,
    }


class AgentReplySchemaTests(unittest.TestCase):
    def test_minimal_valid_json(self) -> None:
        reply = AgentReply.model_validate_json(json.dumps(valid_reply(), ensure_ascii=False))
        self.assertEqual(reply.kind, "answer")
        self.assertEqual(reply.questions, [])
        self.assertIsNone(reply.saved_file)
        self.assertIsNone(reply.next_step)

    def test_defaults_filled(self) -> None:
        data = valid_reply()
        data.pop("questions")
        data.pop("saved_file")
        data.pop("next_step")
        reply = AgentReply.model_validate_json(json.dumps(data, ensure_ascii=False))
        self.assertEqual(reply.questions, [])
        self.assertIsNone(reply.saved_file)
        self.assertIsNone(reply.next_step)

    def test_rejects_unknown_kind(self) -> None:
        data = valid_reply()
        data["kind"] = "hack"
        with self.assertRaises(ValidationError):
            AgentReply.model_validate_json(json.dumps(data, ensure_ascii=False))

    def test_rejects_missing_summary(self) -> None:
        data = valid_reply()
        del data["summary"]
        with self.assertRaises(ValidationError):
            AgentReply.model_validate_json(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
