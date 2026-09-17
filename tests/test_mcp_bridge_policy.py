# -*- coding: utf-8 -*-
"""MCP 服务器 allowlist + 工具权限映射测试。

离线：不连接真实 MCP；用 FakeServer 注入工具列表，验证：
- 策略解析与 fail-closed 默认（deny 不挂载）；
- FORGE_MCP_ALLOWLIST 收窄；
- allow/approval 映射工具以 <server>_<tool> 挂进 agent.tools；
- approval 工具进入审批门扩展名单 + Write-Ahead 副作用名单；
- allow/approval 工具登记进 FileScope 非文件工具名单（否则严格 Project 会误拒）；
- 工具调用文本正确回传（成功/失败）。
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import mcp_bridge
from runtime import approval as approval_mod
from runtime import filescope as filescope_mod
from runtime import runner as runner_mod


class FakeRemoteTool:
    def __init__(self, name: str):
        self.name = name
        self.description = f"{name} (fake)"
        self.inputSchema = {"type": "object", "properties": {}}


class FakeServer:
    def __init__(self, tool_names: list[str]):
        self.tools = [FakeRemoteTool(n) for n in tool_names]
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "boom":
            return SimpleNamespace(content=[SimpleNamespace(text="内部失败")], isError=True)
        return SimpleNamespace(content=[SimpleNamespace(text=f"ok:{name}")], isError=False)


def _invoke(tool, **kwargs) -> str:
    args_json = json.dumps(kwargs, ensure_ascii=False)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="mcp-test",
                      tool_arguments=args_json)
    result = tool.on_invoke_tool(ctx, args_json)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return str(result)


class ParseAndAllowlistTests(unittest.TestCase):
    def test_policy_defaults_to_deny_and_normalizes(self):
        text = (
            '[{"name": "fs", "command": "python", "tool_policy": {"read_file": "ALLOW", '
            '"write_file": "banana"}}]'
        )
        specs, errors = mcp_bridge.parse_specs(text)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["tool_policy"]["read_file"], "allow")
        self.assertEqual(specs[0]["tool_policy"]["write_file"], "deny")  # 非法值 fail-closed
        self.assertEqual(specs[0]["default_tool_policy"], "deny")
        self.assertTrue(any("非法策略" in e for e in errors))

    def test_allowlist_filters_servers(self):
        specs, _ = mcp_bridge.parse_specs(
            '[{"name": "github", "command": "npx"}, {"name": "notion", "command": "npx"}]'
        )
        kept, reasons = mcp_bridge.filter_allowlist(specs, allowlist="github")
        self.assertEqual([s["name"] for s in kept], ["github"])
        self.assertEqual(len(reasons), 1)
        self.assertIn("notion", reasons[0])

    def test_no_allowlist_keeps_all_configured(self):
        specs, _ = mcp_bridge.parse_specs('[{"name": "a", "command": "x"}]')
        kept, reasons = mcp_bridge.filter_allowlist(specs, allowlist=None)
        self.assertEqual(len(kept), 1)
        self.assertEqual(reasons, [])


class MountAndPolicyTests(unittest.TestCase):
    def test_default_deny_mounts_nothing(self):
        spec = {"name": "fs", "command": "python", "tool_policy": {},
                "default_tool_policy": "deny"}
        server = FakeServer(["read_file", "write_file"])
        with mock.patch.object(mcp_bridge, "_policy_summary",
                               {"allow": 0, "approval": 0, "deny": 0}) as summary, \
             mock.patch.object(approval_mod, "_EXTRA_GATED", set()), \
             mock.patch.object(filescope_mod, "NON_FILE_TOOLS",
                               set(filescope_mod.NON_FILE_TOOLS)), \
             mock.patch.object(runner_mod, "SIDE_EFFECT_TOOLS",
                               set(runner_mod.SIDE_EFFECT_TOOLS)):
            mounted, skipped = asyncio.run(mcp_bridge.attach_server_tools(server, spec))
            self.assertEqual(mounted, [])
            self.assertEqual(len(skipped), 2)
            self.assertEqual(summary["deny"], 2)

    def test_allow_approval_deny_mapping(self):
        spec = {
            "name": "fs", "command": "python",
            "tool_policy": {"read_file": "allow", "write_file": "approval", "admin": "deny"},
            "default_tool_policy": "deny",
        }
        server = FakeServer(["read_file", "write_file", "admin"])
        with mock.patch.object(mcp_bridge, "_policy_summary",
                               {"allow": 0, "approval": 0, "deny": 0}), \
             mock.patch.object(approval_mod, "_EXTRA_GATED", set()), \
             mock.patch.object(filescope_mod, "NON_FILE_TOOLS",
                               set(filescope_mod.NON_FILE_TOOLS)), \
             mock.patch.object(runner_mod, "SIDE_EFFECT_TOOLS",
                               set(runner_mod.SIDE_EFFECT_TOOLS)):
            mounted, skipped = asyncio.run(mcp_bridge.attach_server_tools(server, spec))

            names = [t.name for t in mounted]
            self.assertEqual(names, ["fs_read_file", "fs_write_file"])
            self.assertEqual(len(skipped), 1)
            self.assertIn("fs_admin", skipped[0])
            by_name = {t.name: t for t in mounted}
            self.assertEqual(by_name["fs_read_file"]._mcp_policy, "allow")
            self.assertEqual(by_name["fs_write_file"]._mcp_policy, "approval")
            # 运行时登记：approval → 审批门 + WA；allow/approval → FileScope 非文件名单
            self.assertIn("fs_write_file", approval_mod._EXTRA_GATED)
            self.assertIn("fs_write_file", runner_mod.SIDE_EFFECT_TOOLS)
            self.assertNotIn("fs_read_file", runner_mod.SIDE_EFFECT_TOOLS)
            self.assertIn("fs_read_file", filescope_mod.NON_FILE_TOOLS)
            self.assertIn("fs_write_file", filescope_mod.NON_FILE_TOOLS)
            self.assertNotIn("fs_admin", filescope_mod.NON_FILE_TOOLS)

            # approval 名进入 ApprovalGate 的 gated_names（与 env 名单取并集）
            gate = approval_mod.ApprovalGate(manager=None)
            self.assertTrue(gate.should_gate("fs_write_file"))

            # 调用透传：成功文本 + isError 前缀
            out_ok = _invoke(by_name["fs_read_file"], path="/tmp/a.txt")
            self.assertIn("ok:read_file", out_ok)
            self.assertEqual(server.calls[-1], ("read_file", {"path": "/tmp/a.txt"}))

        # 上下文退出后恢复原始名单，跨测试不污染
        self.assertNotIn("fs_write_file", approval_mod._EXTRA_GATED)
        self.assertNotIn("fs_write_file", filescope_mod.NON_FILE_TOOLS)

    def test_result_error_prefix(self):
        spec = {"name": "x", "command": "python", "tool_policy": {"boom": "allow"},
                "default_tool_policy": "deny"}
        server = FakeServer(["boom"])
        with mock.patch.object(mcp_bridge, "_policy_summary",
                               {"allow": 0, "approval": 0, "deny": 0}), \
             mock.patch.object(approval_mod, "_EXTRA_GATED", set()), \
             mock.patch.object(filescope_mod, "NON_FILE_TOOLS",
                               set(filescope_mod.NON_FILE_TOOLS)), \
             mock.patch.object(runner_mod, "SIDE_EFFECT_TOOLS",
                               set(runner_mod.SIDE_EFFECT_TOOLS)):
            mounted, skipped = asyncio.run(mcp_bridge.attach_server_tools(server, spec))
        out = _invoke(mounted[0], code="1")
        self.assertIn("执行失败", out)
        self.assertIn("内部失败", out)


if __name__ == "__main__":
    unittest.main()
