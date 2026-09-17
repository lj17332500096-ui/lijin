import asyncio
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agent import assistant_agent
from runtime import (
    TOOL_CATALOG,
    AgentRuntime,
    ToolError,
    ToolRegistry,
    binding_from_function_tool,
    discover_from_agent,
    spec_for,
)
from runtime.broker import ToolBroker


class RegistryTests(unittest.TestCase):
    def test_discovery_covers_agent_tools(self) -> None:
        reg = discover_from_agent(assistant_agent)
        self.assertEqual(len(reg), len(assistant_agent.tools))
        self.assertGreaterEqual(len(reg), 32)

    def test_bindings_have_spec_and_invoke(self) -> None:
        reg = discover_from_agent(assistant_agent)
        for binding in reg.all():
            self.assertTrue(binding.spec.name)
            self.assertIsInstance(binding.spec.input_schema, dict)
            self.assertIsNotNone(binding.invoke)

    def test_catalog_metadata_applied(self) -> None:
        reg = discover_from_agent(assistant_agent)
        calc = reg.get("calculate").spec
        self.assertEqual(calc.category, "math")
        self.assertEqual(calc.risk, "low")
        self.assertFalse(calc.side_effect)
        self.assertTrue(calc.idempotent)

        edit = reg.get("edit_project_file").spec
        self.assertTrue(edit.side_effect)
        self.assertFalse(edit.destructive)  # 有备份，不算破坏性
        self.assertFalse(edit.idempotent)

        run = reg.get("run_python").spec
        self.assertEqual(run.risk, "high")
        self.assertTrue(run.side_effect)

    def test_duplicate_register_rejected(self) -> None:
        reg = ToolRegistry()
        binding = binding_from_function_tool(next(iter(assistant_agent.tools)))
        reg.register(binding)
        with self.assertRaises(ToolError):
            reg.register(binding)

    def test_unknown_tool_gets_conservative_defaults(self) -> None:
        spec = spec_for("future_mcp_tool")
        self.assertEqual(spec.risk, "medium")
        self.assertTrue(spec.side_effect)
        self.assertFalse(spec.idempotent)


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = discover_from_agent(assistant_agent)
        self.broker = ToolBroker(self.reg)

    def test_execute_math_matches_direct_call(self) -> None:
        result = asyncio.run(self.broker.execute("calculate", {"expression": "2 + 3"}))
        self.assertIn("= 5", result)

    def test_execute_unknown_tool_raises(self) -> None:
        with self.assertRaises(ToolError) as cm:
            asyncio.run(self.broker.execute("not_a_tool", {}))
        self.assertIn("tool not found", str(cm.exception))

    def test_pre_hook_invoked(self) -> None:
        calls = []

        async def hook(name: str, args: dict) -> None:
            calls.append((name, args.get("expression")))

        broker = ToolBroker(self.reg, pre_hook=hook)
        result = asyncio.run(broker.execute("calculate", {"expression": "1+1"}))
        self.assertEqual(calls, [("calculate", "1+1")])
        self.assertIn("= 2", result)


class RuntimeFacadeTests(unittest.TestCase):
    def test_summary_reports_inventory(self) -> None:
        rt = AgentRuntime()
        text = rt.summary()
        self.assertIn("[RUNTIME]", text)
        self.assertIn("工具", text)
        self.assertGreaterEqual(len(rt.list_tools()), 32)

    def test_catalog_has_no_empty_key(self) -> None:
        self.assertNotIn("", TOOL_CATALOG)


if __name__ == "__main__":
    unittest.main()
