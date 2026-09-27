import unittest
from unittest.mock import patch

from runtime import trace_export


class TraceExportTests(unittest.TestCase):
    def test_tool_calls_use_execution_status_and_unknown_for_missing(self) -> None:
        events = [
            {"event_type": "tool.invocation", "payload": {
                "tool_name": "ok_tool", "execution_status": "executed",
            }},
            {"event_type": "tool.invocation", "payload": {
                "tool_name": "failed_tool", "execution_status": "error",
            }},
            {"event_type": "tool.invocation", "payload": {
                "tool_name": "blocked_tool", "execution_status": "blocked",
            }},
            {"event_type": "tool.invocation", "payload": {
                "tool_name": "legacy_tool",
            }},
        ]
        with (
            patch.object(trace_export, "_load_container", return_value="container"),
            patch.object(trace_export, "_load_events", return_value=events),
            patch.object(trace_export, "_load_spans", return_value=[]),
            patch.object(trace_export, "_load_provider_attempts", return_value=[]),
            patch.object(trace_export, "_load_messages", return_value=[]),
        ):
            result = trace_export.export("run-1", db_path=":memory:", jsonl_path=None)

        self.assertEqual(
            [call["status"] for call in result["tool_calls"]],
            ["executed", "error", "blocked", "unknown"],
        )


if __name__ == "__main__":
    unittest.main()
