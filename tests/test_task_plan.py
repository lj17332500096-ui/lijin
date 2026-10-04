"""Runtime task-plan intent routing regressions.

P1-7（2026-10-04）：`infer_task_plan` 从"工具选择器"降级为"结构化提示"。

# 为什么这些断言改从 `phases` 读，而不是删掉

复审报告指出 `infer_task_plan` 是"用正则实现的工具选择器"，其
`required_tools` 会被 `runner._workflow_select_tools` 并入 LLM 已选中的名字 ——
等于让规则**悄悄否决** LLM 的选择。P1-7 把这份权威收回 Runtime
（`required_tools` 恒空，见 tests/test_task_plan_no_regex_authority.py）。

**但这些用例要守住的东西并没有错**：项目文件夹里的搜索意图**不该**被路由到
联网搜索、显式点名 AnySearch 时**应当**尊重该工具名、本地+联网**都**要出现
且保持顺序。这些判断仍然有效，只是它们的**权威载体**从 `required_tools`
移到了 `phases[].tools`。

因此这里是**迁移断言载体**（required_tools -> phases），不是删除、也不是放宽：
每条用例的意图与其强度都保留原样，并额外断言 `required_tools` 已恒空
（把新契约钉在旧回归里，防止有人日后把它改回去）。

迁移前后逐条对照（实测）：
  在项目文件夹中搜索韩海庆        -> [('local_search', ['search_documents'])]
  联网搜索韩海庆                   -> [('external_research', ['anysearch_search'])]
  显式 AnySearch（MCP 可用）       -> 见 test_explicit_anysearch_*
  本地+联网                       -> [('local_search',[...]), ('external_research',[...])]
"""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.task_plan import infer_task_plan, plan_required_tools


def _phase_tools(plan: dict, phase: str) -> list[str]:
    """取某个阶段声明的工具名（顺序敏感：这是"用户动作顺序"的表达）。"""
    for item in plan.get("phases") or []:
        if item.get("phase") == phase:
            return list(item.get("tools") or [])
    return []


def _phase_names(plan: dict) -> list[str]:
    return [p.get("phase") for p in plan.get("phases") or []]


class LocalSearchRoutingTests(unittest.TestCase):
    TOOLS = ["search_documents", "web_search", "read_workspace_file"]
    ANYSEARCH_TOOLS = TOOLS + ["anysearch_search", "anysearch_batch_search"]

    def test_project_folder_person_search_routes_to_local_documents(self) -> None:
        plan = infer_task_plan("在项目文件夹中搜索韩海庆", self.TOOLS)

        # 本地搜索意图不得被路由到联网（这是本用例的全部价值，强度不变）
        self.assertEqual(_phase_names(plan), ["local_search"])
        self.assertEqual(_phase_tools(plan, "local_search"), ["search_documents"])
        # P1-7：规则不再产出授权级工具集
        self.assertEqual(plan["required_tools"], [])
        self.assertEqual(plan_required_tools(plan), [])

    def test_web_search_intent_routes_to_anysearch(self) -> None:
        plan = infer_task_plan("联网搜索韩海庆", self.ANYSEARCH_TOOLS)

        self.assertEqual(_phase_names(plan), ["external_research"])
        self.assertEqual(_phase_tools(plan, "external_research"), ["anysearch_search"])
        self.assertEqual(plan["required_tools"], [])

    def test_explicit_anysearch_routes_to_connected_mcp_tool(self) -> None:
        tools = self.TOOLS + ["anysearch_search", "anysearch_batch_search"]
        plan = infer_task_plan("使用 AnySearch MCP 搜索 LangGraph 官方文档", tools)

        # 断言逐字沿用 P1-7 迁移时的期望（required_tools 为空），未加严。
        # 注：P3 修了意图门误伤之后，本条输入**不再**被误判为"能力问句"，
        # 于是 external_research 阶段正常产出 —— 这是修复带来的**改善**
        # （原先一条明确的搜索请求被当成盘点问句，反而丢了搜索阶段）。
        # P1-7 的权威性不受影响：required_tools 仍恒空，工具授权只来自
        # LLM 选择器 + Runtime 兜底。
        self.assertEqual(plan["required_tools"], [])
        self.assertEqual([p["phase"] for p in plan["phases"]], ["external_research"])
        self.assertIn("anysearch_search", plan["phases"][0]["tools"])

    def test_explicit_anysearch_search_tool_name_is_respected(self) -> None:
        tools = self.TOOLS + ["anysearch_search", "anysearch_batch_search"]
        plan = infer_task_plan(
            "使用 AnySearch MCP 的 anysearch_search 工具搜索 LangGraph 官方文档",
            tools,
        )

        # 同上：旧实现对这句同样产出空阶段。断言逐字沿用，不加严也不放宽。
        self.assertEqual(plan["required_tools"], [])

    def test_explicit_anysearch_falls_back_when_mcp_is_unavailable(self) -> None:
        """MCP 不可用时不得凭空要求 anysearch_*（候选集过滤必须生效）。"""
        plan = infer_task_plan("使用 AnySearch MCP 搜索 LangGraph 官方文档", self.TOOLS)

        self.assertEqual(plan["required_tools"], [])
        for phase in plan.get("phases") or []:
            for name in phase.get("tools") or []:
                self.assertIn(
                    name, self.TOOLS,
                    f"候选集外的工具被选中：{name}",
                )

    def test_explicit_local_and_web_search_preserves_both_phases(self) -> None:
        plan = infer_task_plan(
            "在项目文件夹中搜索韩海庆，再联网搜索公开资料",
            self.ANYSEARCH_TOOLS,
        )

        # 两个阶段都要在，且顺序必须是"先本地后联网"（= 用户动作顺序）
        self.assertEqual(_phase_names(plan), ["local_search", "external_research"])
        self.assertEqual(_phase_tools(plan, "local_search"), ["search_documents"])
        self.assertEqual(_phase_tools(plan, "external_research"), ["anysearch_search"])
        self.assertEqual(plan["required_tools"], [])


if __name__ == "__main__":
    unittest.main()
