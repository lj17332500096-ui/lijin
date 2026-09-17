# -*- coding: utf-8 -*-
"""Capability Introspection 回归：能力回答必须来自真实 Runtime 状态。

覆盖 TEST-CAP-01..10 的离线确定性部分：origin 分类、enabled/connected/available
语义、展示名与 tool_id 分离、未知不冒充没有、历史不污染默认能力回答。
"""

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import capability_introspection as cap


def _tool(name, desc="tool", mcp_server=None, plugin=False, enabled=True):
    t = SimpleNamespace(
        name=name,
        description=desc,
        params_json_schema={},
        strict_json_schema=True,
        _enabled=enabled,
    )
    if mcp_server:
        t._mcp_source = "mcp"
        t._mcp_server = mcp_server
        t._mcp_policy = "allow"
    if plugin:
        t._tool_origin = "plugin"
    return t


class _FakeAgent:
    def __init__(self, tools):
        self.tools = tools
        self.instructions = ""


class CapabilityOriginAndStateTests(unittest.TestCase):
    def test_testcap01_mcp_query_only_returns_mcp(self) -> None:
        agent = _FakeAgent([
            _tool("web_search"),
            _tool("gorden_ppt_build", plugin=True),
            _tool("youtube_get-transcript", mcp_server="youtube"),
        ])
        with mock.patch.object(cap, "_server_connected", return_value=True):
            snap = cap.capability_snapshot(agent, origin="mcp", available_only=True)
            ids = [t["tool_id"] for t in snap["tools"]]
            self.assertEqual(ids, ["youtube_get-transcript"])
            block = cap.capability_context_block("有哪些 MCP？", agent)
            # 注入块默认不出现内置/技能工具 ID，也不把内置说成 MCP
            self.assertNotIn("gorden_ppt_build", block)
            self.assertNotIn("[web_search]", block)

    def test_testcap02_registered_but_disconnected_not_available(self) -> None:
        agent = _FakeAgent([
            _tool("youtube_get-transcript", mcp_server="youtube"),
            _tool("fetch_fetch", mcp_server="fetch"),
        ])
        with mock.patch.object(cap, "_server_connected", return_value=False):
            avail = cap.capability_snapshot(agent, available_only=True)
            self.assertEqual(avail["tools"], [])
            full = cap.capability_snapshot(agent, available_only=False)
            for t in full["tools"]:
                self.assertFalse(t["connected"])
                self.assertFalse(t["available"])

    def test_testcap03_disabled_tool_not_available(self) -> None:
        agent = _FakeAgent([
            _tool("web_search", enabled=True),
            _tool("gorden_ppt_build", plugin=True, enabled=False),
        ])
        snap = cap.capability_snapshot(agent, available_only=True)
        ids = [t["tool_id"] for t in snap["tools"]]
        self.assertIn("web_search", ids)
        self.assertNotIn("gorden_ppt_build", ids)
        full = cap.capability_snapshot(agent, available_only=False)
        by_id = {t["tool_id"]: t for t in full["tools"]}
        self.assertFalse(by_id["gorden_ppt_build"]["enabled"])
        self.assertFalse(by_id["gorden_ppt_build"]["available"])

    def test_testcap10_status_combos_consistent(self) -> None:
        agent = _FakeAgent([
            _tool("builtin_a"),
            _tool("mcp_ok", mcp_server="ok"),
            _tool("mcp_down", mcp_server="down"),
            _tool("skill_disabled", plugin=True, enabled=False),
        ])

        def _conn(sid):
            return sid == "ok"

        with mock.patch.object(cap, "_server_connected", side_effect=_conn):
            snap = cap.capability_snapshot(agent, available_only=False)
            by_id = {t["tool_id"]: t for t in snap["tools"]}
            self.assertEqual(by_id["builtin_a"]["origin"], "builtin")
            self.assertEqual(by_id["builtin_a"]["enabled"], True)
            self.assertEqual(by_id["builtin_a"]["available"], True)
            self.assertEqual(by_id["skill_disabled"]["origin"], "plugin")
            self.assertEqual(by_id["skill_disabled"]["enabled"], False)
            self.assertEqual(by_id["skill_disabled"]["available"], False)
            self.assertEqual(by_id["mcp_ok"]["connected"], True)
            self.assertEqual(by_id["mcp_ok"]["available"], True)
            self.assertEqual(by_id["mcp_down"]["connected"], False)
            self.assertEqual(by_id["mcp_down"]["available"], False)


class CapabilityDisplayAndFailureTests(unittest.TestCase):
    def test_testcap05_tool_id_preserved_exact(self) -> None:
        agent = _FakeAgent([_tool("gorden_ppt_build", plugin=True)])
        with mock.patch.object(cap, "_server_connected", return_value=True):
            block = cap.capability_context_block(
                "把你当前所有工具的技术名称列出来。", agent)
        self.assertIn("[gorden_ppt_build]", block)
        self.assertNotIn("gordenpptbuild", block)

    def test_testcap06_default_friendly_name_no_id(self) -> None:
        agent = _FakeAgent([_tool("web_search"), _tool("schedule_add")])
        block = cap.capability_context_block("你能做什么？", agent)
        self.assertIn("联网搜索", block)
        self.assertIn("新建定时任务", block)
        self.assertNotIn("[web_search]", block)
        self.assertNotIn("[schedule_add]", block)

    def test_testcap07_technical_list_shows_both(self) -> None:
        agent = _FakeAgent([_tool("gorden_ppt_build", plugin=True)])
        block = cap.capability_context_block("把工具 ID 也列出来。", agent)
        self.assertIn("PPT 制作 [gorden_ppt_build]", block)

    def test_testcap08_history_not_injected_into_mcp_answer(self) -> None:
        agent = _FakeAgent([
            _tool("youtube_get-transcript", mcp_server="youtube"),
            _tool("gorden_ppt_build", plugin=True),
        ])
        with mock.patch.object(cap, "_server_connected", return_value=True):
            block = cap.capability_context_block("你有哪些 MCP？", agent)
        self.assertNotIn("之前生成", block)
        self.assertNotIn("历史真实成功记录", block)
        self.assertNotIn("刚才成功", block)

    def test_testcap09_connection_state_live_toggle(self) -> None:
        agent = _FakeAgent([_tool("youtube_get-transcript", mcp_server="youtube")])
        with mock.patch.object(cap, "_server_connected", return_value=True):
            self.assertTrue(
                cap.capability_snapshot(agent, available_only=False)["tools"][0]["connected"])
        with mock.patch.object(cap, "_server_connected", return_value=False):
            self.assertFalse(
                cap.capability_snapshot(agent, available_only=False)["tools"][0]["connected"])

    def test_testcap04_unknown_state_not_reported_as_none(self) -> None:
        with mock.patch.object(cap, "capability_snapshot",
                               side_effect=RuntimeError("boom")):
            block = cap.capability_context_block("有哪些 MCP？")
        self.assertIn("无法", block)
        self.assertIn("不能确认", block)
        # 不能把“无法查询”说成“没有 MCP”
        self.assertNotIn("当前没有已连接的 MCP", block)


class RegistryOriginTests(unittest.TestCase):
    def test_discover_uses_real_markers_not_type_name(self) -> None:
        from runtime.registry import discover_from_agent

        agent = _FakeAgent([
            _tool("web_search"),
            _tool("gorden_ppt_build", plugin=True),
            _tool("fetch_fetch", mcp_server="fetch"),
        ])
        registry = discover_from_agent(agent)
        sources = {b.spec.name: b.spec.source for b in registry.all()}
        self.assertEqual(sources["web_search"], "native")
        self.assertEqual(sources["gorden_ppt_build"], "skill")
        self.assertEqual(sources["fetch_fetch"], "mcp")


if __name__ == "__main__":
    unittest.main()
