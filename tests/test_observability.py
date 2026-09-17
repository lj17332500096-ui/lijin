import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from observability import (
    LocalJsonlProcessor,
    _to_nano,
    add_exporter,
    span_attributes,
)


def make_span(kind: str, **extra) -> types.SimpleNamespace:
    data = {"type": kind, **extra}
    span_data = types.SimpleNamespace(type=kind, export=lambda: data)
    return types.SimpleNamespace(
        span_id="span0001",
        trace_id="trace0001",
        parent_id="span0000",
        started_at=datetime(2026, 9, 5, 1, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 5, 1, 0, 1, tzinfo=timezone.utc),
        span_data=span_data,
        error=None,
    )


class SpanMappingTests(unittest.TestCase):
    def test_generation_attributes_follow_genai_semconv(self) -> None:
        span = make_span(
            "generation",
            model="agnes-2.5-flash",
            usage={"input_tokens": 12, "output_tokens": 34},
        )
        attrs = span_attributes(span)
        self.assertEqual(attrs["gen_ai.operation.name"], "chat")
        self.assertEqual(attrs["gen_ai.request.model"], "agnes-2.5-flash")
        self.assertEqual(attrs["gen_ai.usage.input_tokens"], 12)
        self.assertEqual(attrs["gen_ai.usage.output_tokens"], 34)

    def test_tool_attributes(self) -> None:
        span = make_span("function", name="web_search", input='{"q":"x"}', output="结果")
        attrs = span_attributes(span)
        self.assertEqual(attrs["gen_ai.tool.name"], "web_search")
        self.assertIn("结果", attrs["local.io.preview.output"])

    def test_agent_turn_attributes(self) -> None:
        span = make_span("turn", data={"turn": 3, "agent_name": "assistant"})
        attrs = span_attributes(span)
        self.assertEqual(attrs["agent.turn"], 3)

    def test_to_nano_conversions(self) -> None:
        dt = datetime(2026, 9, 5, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_to_nano(dt), 1788566400000000000)
        self.assertIsNone(_to_nano("不是时间"))
        self.assertEqual(_to_nano(1788566400000000000), 1788566400000000000)


class JsonlProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="otel_"))
        self.path = self._tmp / "traces.jsonl"
        self.processor = LocalJsonlProcessor(self.path)

    def _read(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_span_records_have_semconv_shape(self) -> None:
        trace = types.SimpleNamespace(trace_id="t1", workflow_name="全能助手", group_id="g1")
        self.processor.on_trace_start(trace)
        self.processor.on_span_end(make_span("generation", model="m", usage={"input_tokens": 5}))
        self.processor.on_trace_end(trace)

        records = self._read()
        self.assertEqual(len(records), 3)
        span_rec = next(r for r in records if r.get("span_id") == "span0001")
        self.assertEqual(span_rec["otel_schema"].startswith("1.0-local"), True)
        self.assertIn("resource", span_rec)
        self.assertIn("start_time_unix_nano", span_rec)
        self.assertIn("end_time_unix_nano", span_rec)
        self.assertEqual(span_rec["status"], {"code": 1})
        self.assertEqual(span_rec["attributes"]["gen_ai.request.model"], "m")

    def test_exporter_hook_receives_records(self) -> None:
        received = []
        add_exporter(lambda record: received.append(record.get("name")))
        trace = types.SimpleNamespace(trace_id="t", workflow_name=None, group_id=None)
        self.processor.on_trace_start(trace)
        self.assertIn("trace.start", received)


if __name__ == "__main__":
    unittest.main()
