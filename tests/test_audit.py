import sys
import tempfile
import types
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.audit import AuditCollector
from runtime.task_manager import TaskManager


def fake_usage(input_tokens: int, output_tokens: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


def fake_response(model: str, inp: int, out: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(model=model, usage=fake_usage(inp, out))


def raw_item(kind: str, **fields) -> types.SimpleNamespace:
    data = {"type": kind, **fields}
    return types.SimpleNamespace(raw_item=data)


def fake_result() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        raw_responses=[fake_response("agnes-2.5-flash", 100, 30), fake_response("agnes-2.5-flash", 220, 60)],
        new_items=[
            raw_item("function_call", name="calculate", arguments='{"expression": "1+1"}'),
            raw_item("function_call_output", output="2 = 2"),
            raw_item("function_call", name="web_search", arguments='{"query": "x"}'),
            # 无配对输出的调用（模拟异常/被拒）
        ],
    )


class AuditCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="audit_"))
        self.manager = TaskManager(self._tmp / "agent.db")
        self.task = self.manager.create_task("s1", "审计任务")

    def test_ingest_records_model_and_tool_calls(self) -> None:
        collector = AuditCollector(self.manager, self.task.id)
        totals = collector.ingest(fake_result())
        self.assertEqual(totals["model_calls"], 2)
        self.assertEqual(totals["input_tokens"], 320)
        self.assertEqual(totals["output_tokens"], 90)
        self.assertEqual(totals["tool_calls"], 2)

        models = self.manager.list_model_calls(self.task.id)
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0]["model"], "agnes-2.5-flash")
        self.assertEqual(models[0]["input_tokens"], 100)
        self.assertEqual(models[1]["output_tokens"], 60)

        tools = self.manager.list_tool_calls(self.task.id)
        self.assertEqual(len(tools), 2)
        paired = {t["tool_name"]: t for t in tools}
        self.assertEqual(paired["calculate"]["status"], "succeeded")
        self.assertEqual(paired["calculate"]["result_excerpt"], "2 = 2")
        self.assertEqual(paired["calculate"]["arguments"]["expression"], "1+1")
        self.assertEqual(paired["web_search"]["status"], "failed_or_denied")

        restored = self.manager.get_task(self.task.id)
        self.assertEqual(restored.usage.tool_calls, 2)
        self.assertEqual(restored.usage.input_tokens, 320)

    def test_insert_list_roundtrip(self) -> None:
        self.manager.insert_model_call(task_id=self.task.id, model="m1", input_tokens=10, output_tokens=5)
        self.manager.insert_tool_call(
            task_id=self.task.id,
            tool_name="run_python",
            arguments={"code": "print(1)"},
            status="denied",
            result_excerpt="审批拒绝",
        )
        tools = self.manager.list_tool_calls(self.task.id)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["status"], "denied")
        self.assertEqual(tools[0]["arguments"]["code"], "print(1)")


class ExecuteAuditWiringTests(unittest.TestCase):
    def test_runtime_passes_audit_collector(self) -> None:
        import asyncio

        import main as main_module
        from runtime.runner import AgentRuntime

        captured = {}

        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None,
            agent=None, audit=None, stream_events_cb=None,
        ):
            captured["audit"] = audit
            return "ok"

        orig = main_module.execute_turn
        main_module.execute_turn = fake_execute_turn
        try:
            runtime = AgentRuntime(db_path=str(self._tmp_path()))
            result = asyncio.run(runtime.run_turn("短任务", session_id="unit"))
            self.assertTrue(result.ok)
            self.assertIsNotNone(captured.get("audit"))
        finally:
            main_module.execute_turn = orig

    @staticmethod
    def _tmp_path() -> str:
        import tempfile

        return tempfile.mkdtemp(prefix="audit_wire_") + "/agent.db"


if __name__ == "__main__":
    unittest.main()

