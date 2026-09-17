import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from pydantic import ValidationError

import guardrails
from schemas import AgentReply


def reply_json(**overrides) -> str:
    data = {
        "kind": "answer",
        "summary": "一句话摘要",
        "content": "正文内容",
        "questions": [],
        "saved_file": None,
        "next_step": None,
        "ui": [],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def metric(title="指标A", **kw) -> dict:
    item = {"type": "metric", "title": title, "value": "42", "hint": "单位"}
    item.update(kw)
    return item


def bar(n_series=1, n_labels=3, labels=None) -> dict:
    labels = labels or [f"L{i}" for i in range(n_labels)]
    return {
        "type": "bar",
        "title": "图",
        "labels": labels,
        "series": [{"name": f"S{i}", "values": [float(i + 1)] * len(labels)} for i in range(n_series)],
    }


class UiSchemaTests(unittest.TestCase):
    def test_reply_with_ui_valid(self) -> None:
        reply = AgentReply.model_validate_json(
            reply_json(ui=[metric(), bar()])
        )
        self.assertEqual(len(reply.ui), 2)
        self.assertEqual(reply.ui[0].type, "metric")
        self.assertEqual(reply.ui[0].value, "42")
        self.assertEqual(reply.ui[1].type, "bar")
        self.assertEqual(len(reply.ui[1].labels), 3)

    def test_legacy_reply_without_ui_defaults_empty(self) -> None:
        data = json.loads(reply_json())
        del data["ui"]
        reply = AgentReply.model_validate_json(json.dumps(data, ensure_ascii=False))
        self.assertEqual(reply.ui, [])

    def test_unknown_ui_type_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AgentReply.model_validate_json(
                reply_json(ui=[{"type": "iframe", "src": "https://evil"}])
            )

    def test_block_without_type_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AgentReply.model_validate_json(reply_json(ui=[{"title": "缺 type"}]))

    def test_form_needs_field_name(self) -> None:
        with self.assertRaises(ValidationError):
            AgentReply.model_validate_json(
                reply_json(ui=[{"type": "form", "title": "f", "fields": [{"label": "无名字段"}]}])
            )


class UiGuardrailTests(unittest.TestCase):
    """新契约：ui 畸形不再让任务失败——parser 丢弃 ui 并记录 warning（安全闸只拦密钥）。"""

    def test_valid_ui_passes(self) -> None:
        self.assertIsNone(guardrails.check_output(reply_json(ui=[metric(), bar(), bar(n_series=2)])))

    def test_malformed_ui_forgiven_with_warning(self) -> None:
        from runtime.reply_parser import parse

        cases = [
            [metric(title=f"m{i}") for i in range(guardrails.UI_MAX_BLOCKS + 1)],
            [{"type": "bar", "title": "t", "labels": ["a", "b"], "series": [{"name": "s", "values": [1]}]}],
            [{"type": "table", "title": "t", "columns": ["a", "b"], "rows": [["only-one"]]}],
            [{"type": "form", "title": "f", "fields": [{"name": "x", "label": "X"}, {"name": "x", "label": "X2"}]}],
            [{"type": "form", "title": "f", "fields": [{"name": f"f{i}"} for i in range(guardrails.UI_MAX_FIELDS + 1)]}],
            [{"type": "table", "title": "t", "columns": ["c"], "rows": [["x"]] * (guardrails.UI_MAX_DATA_ROWS + 1)}],
            [{"type": "table", "title": "t", "columns": "不是数组"}],
        ]
        for blocks in cases:
            self.assertIsNone(guardrails.check_output(reply_json(ui=blocks)))
            r = parse(reply_json(ui=blocks))
            self.assertTrue(r.ok)
            self.assertEqual(r.canonical["ui"], [])
            self.assertTrue(any("ui" in w for w in r.warnings))

    def test_empty_chart_without_data_passes(self) -> None:
        self.assertIsNone(guardrails.check_output(reply_json(ui=[{"type": "pie", "title": ""}])))


if __name__ == "__main__":
    unittest.main()
