"""P1-7：`task_plan` 不得再用正则决定工具集（授权权收回 Runtime）。

# 这条测试要拦住什么

`runtime/task_plan.py` 曾是一个**用正则实现的工具选择器**：13 条中文关键词规则
决定工具集，且结果被 `runner._workflow_select_tools` 并入 LLM 已选中的名字。
后果是 LLM 的选择被规则**悄悄否决** —— 迁移宣称的"语义判断从规则迁到 LLM"
在授权链最外层失效；同时直接违反 `AGENTS.md` 分工宪法
（面向业务语义的判断绝不能用关键字/正则）。

设计说明：02-design/P1_7_TASK_PLAN_BOUNDARY_20261004.md

# 判据怎么写才算数

不写成"读源码里有没有 re.search"——那只是**实现细节的复述**，
把正则换个马甲就能绕过。真正的判据是**行为**：
  - `plan_required_tools(...)` 对 `infer_task_plan` 的结果**恒为空**；
  - 即便有人重新把规则结果塞进 `required_tools`，合并进授权集的工具也必须
    不受它影响（反向用例）。

另加一条 AST 级断言锁住调用点：runner 不得再把 plan_required_tools 的结果
**并入**授权集。行为 + 结构双保险，两者缺一都可能被绕过。
"""

import ast
import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import task_plan
from runtime.task_plan import infer_task_plan, plan_required_tools

AVAILABLE = [
    "get_weather", "get_current_datetime", "calculate", "anysearch_search",
    "anysearch_batch_search", "anysearch_extract", "read_workspace_file",
    "read_office_file", "read_spreadsheet", "search_documents", "save_note",
]

#: 曾经的规则命中样本 —— 每条都曾产出 required_tools。
RULE_HIT_QUERIES = [
    "北京今天天气怎么样",
    "现在几点了",
    "帮我算一下 3+5 等于多少",
    "求平均值",
    "搜索一下最新的 AI 资讯",
    "联网查一下 Rust 最近的进展",
    "读取 README.md 并总结",
    "分析这个 csv 表格",
    "把结果写入文件",
    "保存结果为笔记",
    "记住我 prefers 中文",
    "回忆一下我的记忆",
    "提醒我明天 9 点开会",
    "在项目文件夹里搜索文档",
]


class RequiredToolsAreNeverRuleDerivedTests(unittest.TestCase):
    """核心判据：规则不得产出授权级工具集。"""

    def test_required_tools_empty_for_every_rule_hit(self) -> None:
        """所有曾命中规则的输入，required_tools 都必须为空。"""
        offenders: list[str] = []
        for q in RULE_HIT_QUERIES:
            plan = infer_task_plan(q, AVAILABLE)
            if plan_required_tools(plan):
                offenders.append(f"{q!r} -> {plan_required_tools(plan)}")
        self.assertEqual(
            offenders, [],
            "正则仍在决定工具集（P1-7 回归）：\n  " + "\n  ".join(offenders),
        )

    def test_required_tools_empty_even_without_available_filter(self) -> None:
        """available=None（无候选过滤）时同样必须为空。

        防"只在有候选时为空"这种半吊子实现。
        """
        for q in RULE_HIT_QUERIES:
            self.assertEqual(
                plan_required_tools(infer_task_plan(q, None)), [],
                f"{q!r}：无候选过滤时也不得由规则产出工具",
            )

    def test_plan_required_tools_never_reads_phases(self) -> None:
        """即使有人把工具塞进 phases，plan_required_tools 也不得把它当授权。"""
        forged = {
            "schema_version": 2,
            "required_tools": ["get_weather"],  # 手工伪造
            "phases": [{"phase": "external_fact", "tools": ["get_weather"]}],
        }
        # 本函数按契约读取 required_tools；伪造值会被原样返回 ——
        # 所以这里断言的是"真实 infer_task_plan 永不产出它"（上一条已覆盖），
        # 本条只确认函数不越权去 phases 里翻。
        self.assertEqual(plan_required_tools(forged), ["get_weather"])
        self.assertEqual(plan_required_tools({"phases": [{"tools": ["x"]}]}), [])


class RuleHintsStillWorkTests(unittest.TestCase):
    """降级不等于删除：阶段提示能力必须保留（否则白改）。"""

    def test_phases_still_produced_under_default(self) -> None:
        saved = os.environ.pop(task_plan.RULE_HINT_ENV, None)
        try:
            plan = infer_task_plan("搜索一下最新的 AI 资讯", AVAILABLE)
            self.assertTrue(plan["phases"], "规则提示被误删：phases 应仍产出阶段建议")
            self.assertEqual(plan["match_kind"], "rule_hint")
            self.assertEqual(plan["required_tools"], [])
        finally:
            if saved is not None:
                os.environ[task_plan.RULE_HINT_ENV] = saved

    def test_non_action_query_has_no_phases(self) -> None:
        """普通业务任务不该被硬塞阶段（提示也不能是噪声）。"""
        saved = os.environ.pop(task_plan.RULE_HINT_ENV, None)
        try:
            plan = infer_task_plan("把 3 条缺陷按严重程度分类", AVAILABLE)
            self.assertEqual(plan["phases"], [], "无关任务被规则误判出阶段")
            self.assertEqual(plan["match_kind"], "none")
        finally:
            if saved is not None:
                os.environ[task_plan.RULE_HINT_ENV] = saved

    def test_rollback_switch_empties_phases(self) -> None:
        """回退档：FORGE_TASK_PLAN_RULE_HINT=off -> 连提示也不给。"""
        saved = os.environ.get(task_plan.RULE_HINT_ENV)
        os.environ[task_plan.RULE_HINT_ENV] = "off"
        try:
            plan = infer_task_plan("搜索一下最新的 AI 资讯", AVAILABLE)
            self.assertEqual(plan["phases"], [], "回退档未生效")
            self.assertEqual(plan["required_tools"], [])
            self.assertEqual(plan["match_kind"], "none")
        finally:
            if saved is None:
                os.environ.pop(task_plan.RULE_HINT_ENV, None)
            else:
                os.environ[task_plan.RULE_HINT_ENV] = saved

    def test_structure_is_backward_compatible(self) -> None:
        """签名与返回结构不变（runner 的消费面不得因此崩）。"""
        plan = infer_task_plan("读取 README.md 并总结", AVAILABLE)
        for key in ("schema_version", "required_tools",
                    "required_capabilities", "phases", "match_kind"):
            self.assertIn(key, plan, f"返回结构缺少 {key}：消费方可能崩")


class RunnerDoesNotMergeRuleToolsTests(unittest.TestCase):
    """AST 级：runner 不得再把 plan_required_tools 并入授权集。

    行为断言之外再加这条结构断言。行为能证明"现在没越权"，
    结构能证明"没有第二条隐蔽路径把规则结果塞回来"。
    """

    @staticmethod
    def _select_tools_node() -> ast.AST:
        src = (BASE / "runtime" / "runner.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 注意：`_workflow_select_tools` 是 **async** 嵌套函数（AsyncFunctionDef），
        # 只匹配 FunctionDef 会漏掉它 —— 那会让本断言"什么都没查"却全绿。
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "_workflow_select_tools":
                return node
        raise AssertionError("未找到 _workflow_select_tools（P1-7 验收锚点失效）")

    def test_required_names_not_sourced_from_plan_required_tools(self) -> None:
        """`_required_names` 不得再由 plan_required_tools 决定授权并集。"""
        node = self._select_tools_node()
        offenders: list[str] = []
        for sub in ast.walk(node):
            # 形如: _required_names = [...] plan_required_tools(...)
            if isinstance(sub, ast.Assign):
                targets = [t.id for t in sub.targets if isinstance(t, ast.Name)]
                if "_required_names" in targets:
                    seg = ast.dump(sub.value)
                    if "plan_required_tools" in seg:
                        offenders.append(ast.unparse(sub)[:120])
        self.assertEqual(
            offenders, [],
            "runner 又把 plan_required_tools 并入授权集（P1-7 回归）：\n  "
            + "\n  ".join(offenders),
        )

    def test_authorization_still_falls_back_to_all_tools(self) -> None:
        """收窄授权后，三层 fail-safe 必须在源码里仍在（防误伤成"无工具可用"）。"""
        src = (BASE / "runtime" / "runner.py").read_text(encoding="utf-8")
        for guard in ("selection_error_full_tools_fallback",
                      "empty_selection_full_tools_fallback"):
            self.assertIn(
                guard, src,
                f"兜底 {guard} 消失：规则退出后，LLM 空选会导致 Run 无工具可用",
            )


if __name__ == "__main__":
    unittest.main()
