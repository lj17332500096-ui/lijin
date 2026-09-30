"""Runtime task-plan intent routing regressions."""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.task_plan import infer_task_plan


class LocalSearchRoutingTests(unittest.TestCase):
    TOOLS = ["search_documents", "web_search", "read_workspace_file"]
    ANYSEARCH_TOOLS = TOOLS + ["anysearch_search", "anysearch_batch_search"]

    def test_project_folder_person_search_routes_to_local_documents(self) -> None:
        plan = infer_task_plan("在项目文件夹中搜索韩海庆", self.TOOLS)

        self.assertEqual(plan["required_tools"], ["search_documents"])
        self.assertEqual([phase["phase"] for phase in plan["phases"]], ["local_search"])

    def test_web_search_intent_routes_to_anysearch(self) -> None:
        plan = infer_task_plan("联网搜索韩海庆", self.ANYSEARCH_TOOLS)

        self.assertEqual(plan["required_tools"], ["anysearch_search"])
        self.assertEqual([phase["phase"] for phase in plan["phases"]], ["external_research"])

    def test_explicit_anysearch_routes_to_connected_mcp_tool(self) -> None:
        tools = self.TOOLS + ["anysearch_search", "anysearch_batch_search"]
        plan = infer_task_plan("使用 AnySearch MCP 搜索 LangGraph 官方文档", tools)

        self.assertEqual(plan["required_tools"], ["anysearch_search"])
        self.assertEqual(plan["phases"][0]["phase"], "external_research")

    def test_explicit_anysearch_search_tool_name_is_respected(self) -> None:
        tools = self.TOOLS + ["anysearch_search", "anysearch_batch_search"]
        plan = infer_task_plan(
            "使用 AnySearch MCP 的 anysearch_search 工具搜索 LangGraph 官方文档",
            tools,
        )

        self.assertEqual(plan["required_tools"], ["anysearch_search"])

    def test_explicit_anysearch_falls_back_when_mcp_is_unavailable(self) -> None:
        plan = infer_task_plan("使用 AnySearch MCP 搜索 LangGraph 官方文档", self.TOOLS)

        self.assertEqual(plan["required_tools"], [])

    def test_explicit_local_and_web_search_preserves_both_phases(self) -> None:
        plan = infer_task_plan(
            "在项目文件夹中搜索韩海庆，再联网搜索公开资料",
            self.ANYSEARCH_TOOLS,
        )

        self.assertEqual(plan["required_tools"], ["search_documents", "anysearch_search"])
        self.assertEqual(
            [phase["phase"] for phase in plan["phases"]],
            ["local_search", "external_research"],
        )


if __name__ == "__main__":
    unittest.main()
