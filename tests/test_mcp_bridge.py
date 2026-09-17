import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import mcp_bridge


class ConfigParseTests(unittest.TestCase):
    def test_empty_config_ok(self) -> None:
        self.assertEqual(mcp_bridge.parse_specs(""), ([], []))
        self.assertEqual(mcp_bridge.parse_specs("   "), ([], []))

    def test_valid_json(self) -> None:
        text = (
            '[{"name": "github", "command": "npx", "args": ["-y", "@github/mcp-server"], '
            '"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}}]'
        )
        specs, errors = mcp_bridge.parse_specs(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["name"], "github")
        self.assertEqual(specs[0]["command"], "npx")
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", specs[0]["env"])

    def test_invalid_json_reports_error(self) -> None:
        specs, errors = mcp_bridge.parse_specs("{not json")
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_missing_fields_and_duplicate_names(self) -> None:
        text = '[{"name": "a", "command": "x"}, {"name": "a", "command": "y"}, {"args": ["z"]}]'
        specs, errors = mcp_bridge.parse_specs(text)
        self.assertEqual(len(specs), 1)
        self.assertGreaterEqual(len(errors), 1)


class EnsureTests(unittest.TestCase):
    def test_no_config_is_noop(self) -> None:
        old = mcp_bridge._connected
        mcp_bridge._connected = False
        import os

        prev = os.environ.get("MCP_SERVERS")
        os.environ["MCP_SERVERS"] = ""
        import asyncio

        ok = asyncio.run(mcp_bridge.ensure_connected())
        self.assertTrue(ok)
        self.assertEqual(mcp_bridge._connected_names, [])
        if prev is None:
            os.environ.pop("MCP_SERVERS", None)
        else:
            os.environ["MCP_SERVERS"] = prev
        mcp_bridge._connected = old


if __name__ == "__main__":
    unittest.main()
