# -*- coding: utf-8 -*-
"""P3 中和实验（S6）：验证 P1-7 相关断言**有鉴别力**，不是空转断言。

方法（testing-basics §8.2 反假完成）：把被断言的行为改坏，看对应测试是否真会红。
只 monkeypatch / 临时改文件再还原，**不提交、不改生产代码语义**。
"""
from __future__ import annotations

import ast
import subprocess
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


class PlanRequiredToolsAlwaysEmptyTests(unittest.TestCase):
    """行为判据：required_tools 恒空。"""

    QUERIES = [
        "北京今天天气怎么样", "现在几点了", "帮我算一下 3+5 等于多少", "求平均值",
        "搜索一下最新的 AI 资讯", "读取 README.md 并总结", "分析这个 csv 表格",
        "把结果写入文件", "保存结果为笔记", "记住我 prefers 中文",
        "回忆一下我的记忆", "提醒我明天 9 点开会", "在项目文件夹里搜索文档",
    ]

    def test_required_tools_always_empty(self):
        offenders = [q for q in self.QUERIES if plan_required_tools(
            infer_task_plan(q, AVAILABLE))]
        self.assertEqual(offenders, [])

    def test_required_tools_empty_when_available_none(self):
        offenders = [q for q in self.QUERIES if plan_required_tools(
            infer_task_plan(q, None))]
        self.assertEqual(offenders, [])

    def test_required_capabilities_still_observable(self):
        """降级不能把可观测性也一起杀掉。"""
        plan = infer_task_plan("北京今天天气怎么样", AVAILABLE)
        self.assertTrue(plan["phases"], "phases 应保留作为提示")
        self.assertEqual(plan["match_kind"], "rule_hint")

    def test_rule_hint_off_switch_empties_phases(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {task_plan.RULE_HINT_ENV: "off"}):
            plan = infer_task_plan("北京今天天气怎么样", AVAILABLE)
        self.assertEqual(plan["phases"], [])
        self.assertEqual(plan["match_kind"], "none")


class RunnerCallSiteStructureTests(unittest.TestCase):
    """AST 级：runner 不得再把 plan_required_tools 并入授权集。"""

    def _runner_ast(self):
        return ast.parse((BASE / "runtime" / "runner.py").read_text(encoding="utf-8"))

    def test_runner_has_no_plan_required_tools_call(self):
        tree = self._runner_ast()
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "plan_required_tools":
                    hits.append(node.lineno)
                elif isinstance(f, ast.Attribute) and f.attr == "plan_required_tools":
                    hits.append(node.lineno)
        self.assertEqual(hits, [], f"runner 仍在调用 plan_required_tools: {hits}")

    def test_runner_required_names_is_literal_empty(self):
        """`_required_names: list[str] = []` 必须是字面量空，不能是表达式。"""
        tree = self._runner_ast()
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == "_required_names" and node.value is not None:
                found = node
        self.assertIsNotNone(found, "找不到 _required_names 赋值 —— 授权链形状变了")
        self.assertIsInstance(found.value, ast.List,
                             "_required_names 必须初始化为字面量空列表")
        self.assertEqual(found.value.elts, [], "_required_names 必须真的是空")


class NeutralizationExperimentTests(unittest.TestCase):
    """**中和实验**：把行为改坏，确认上面两条判据真会红。"""

    def test_neutralize_infer_task_plan_required_tools_makes_red(self):
        """把 required_tools 重新填回规则结果 -> test_required_tools_always_empty 必须红。"""
        real = task_plan.infer_task_plan

        def broken(query, available=None):
            plan = real(query, available)
            names = [n for ph in plan.get("phases") or [] for n in ph.get("tools") or []]
            plan["required_tools"] = names
            return plan

        import runtime.task_plan as tp
        orig = tp.infer_task_plan
        tp.infer_task_plan = broken
        try:
            offenders = [q for q in self.__class__.__mro__[0].__name__ and
                         PlanRequiredToolsAlwaysEmptyTests.QUERIES
                         if tp.plan_required_tools(tp.infer_task_plan(q, AVAILABLE))]
            self.assertNotEqual(offenders, [],
                                "把 required_tools 填回去竟然没被发现 -> 断言空转")
            print(f"[中和] required_tools 填回规则结果后，{len(offenders)}/{len(PlanRequiredToolsAlwaysEmptyTests.QUERIES)} 条被抓到 -> 断言有鉴别力")
        finally:
            tp.infer_task_plan = orig

    def test_neutralize_plan_required_tools_filter_makes_red(self):
        """把 plan_required_tools 改成返回全部 tools -> 必须被抓到。"""
        real = task_plan.plan_required_tools

        def broken(plan):
            if not isinstance(plan, dict):
                return []
            return [n for ph in (plan.get("phases") or []) for n in (ph.get("tools") or [])]

        import runtime.task_plan as tp
        tp.plan_required_tools = broken
        try:
            offenders = [q for q in PlanRequiredToolsAlwaysEmptyTests.QUERIES
                         if tp.plan_required_tools(tp.infer_task_plan(q, AVAILABLE))]
            self.assertNotEqual(offenders, [],
                                "plan_required_tools 失守竟然没被发现 -> 断言空转")
            print(f"[中和] plan_required_tools 改成透传后被抓到 {len(offenders)} 条 -> 断言有鉴别力")
        finally:
            tp.plan_required_tools = real


class CrossFileAssertionBiteTests(unittest.TestCase):
    """e13dff9 那条"跨文件漏迁移"断言的中和实验。

    那条用例是：
        plan = infer_task_plan("请读取工作区里的 README.md 并总结")
        assertEqual(plan["phases"][0]["phase"], "read_input")
        assertEqual(plan["phases"][0]["tools"], ["read_office_file"])
        assertEqual(plan["required_tools"], [])

    中和方式：把 `read_input` 规则**选错工具**（返回 read_spreadsheet 而非
    read_office_file），看它是否真会红。
    """
    QUERY = "请读取工作区里的 README.md 并总结"

    def test_baseline_passes(self):
        plan = infer_task_plan(self.QUERY, AVAILABLE)
        self.assertEqual(plan["phases"][0]["phase"], "read_input")
        self.assertEqual(plan["phases"][0]["tools"], ["read_office_file"])
        self.assertEqual(plan["required_tools"], [])

    def test_neutralize_chosen_tool_makes_the_assertion_red(self):
        """中和：把 read_input 的首选工具换成 read_spreadsheet（改磁盘源码 + try/finally 还原）。

        这是唯一可靠的中和方式 —— monkeypatch 拦不住，因为 preferred 分支的
        判定走编译期模块内常量。
        """
        import importlib
        tp_file = BASE / "runtime" / "task_plan.py"
        original = tp_file.read_text(encoding="utf-8")
        broken = original.replace(
            'else ("read_office_file", "read_workspace_file", "read_spreadsheet")',
            'else ("read_spreadsheet", "read_office_file", "read_workspace_file")',
        )
        self.assertNotEqual(broken, original, "\u6e90\u7801\u951a\u70b9\u5931\u6548\uff1a\u4e2d\u548c\u672a\u751f\u6548")
        tp_file.write_text(broken, encoding="utf-8")
        try:
            sys.modules.pop("runtime.task_plan", None)
            tp2 = importlib.import_module("runtime.task_plan")
            plan = tp2.infer_task_plan(self.QUERY, AVAILABLE)
            self.assertEqual(plan["phases"][0]["tools"], ["read_spreadsheet"],
                             "\u4e2d\u548c\u540e\u4ecd\u9009 read_office_file -> \u65e0\u6cd5\u9a8c\u8bc1\u9274\u522b\u529b")
            self.assertNotEqual(plan["phases"][0]["tools"], ["read_office_file"])
            print("[\u4e2d\u548c] read_input \u9009\u9519\u5de5\u5177\u540e\uff0ce13dff9 \u7684 assertEqual([read_office_file]) \u4f1a\u7ea2 -> \u65ad\u8a00\u6709\u9274\u522b\u529b")
        finally:
            tp_file.write_text(original, encoding="utf-8")
            sys.modules.pop("runtime.task_plan", None)
        self.assertEqual(tp_file.read_text(encoding="utf-8"), original, "\u6e90\u7801\u672a\u8fd8\u539f\uff01")

    def test_neutralize_dropping_read_phase_makes_red(self):
        """\u4e2d\u548c 2\uff1a\u8ba9 read_input \u89c4\u5219\u6574\u6761\u5931\u6548 -> phases[0] \u4e0d\u518d\u662f read_input\u3002"""
        import importlib
        tp_file = BASE / "runtime" / "task_plan.py"
        original = tp_file.read_text(encoding="utf-8")
        broken = original.replace(
            'r"\u8bfb\u53d6|\u8bfb\u5165|\u6253\u5f00|\u5206\u6790(?:\u8fd9\u4e2a|\u8fd9\u4efd|\u4e00\u4e0b)?(?:\u6587\u4ef6|\u8868\u683c|csv|excel)|"',
            'r"(?!x)x|"',
        )
        self.assertNotEqual(broken, original, "\u6e90\u7801\u951a\u70b9\u5931\u6548")
        tp_file.write_text(broken, encoding="utf-8")
        try:
            sys.modules.pop("runtime.task_plan", None)
            tp2 = importlib.import_module("runtime.task_plan")
            plan = tp2.infer_task_plan(self.QUERY, AVAILABLE)
            phases = plan.get("phases") or []
            if phases:
                self.assertNotEqual(phases[0].get("phase"), "read_input")
            print(f"[\u4e2d\u548c2] read_input \u89c4\u5219\u5931\u6548\u540e phases={phases} -> "
                  "assertEqual(phases[0].phase,'read_input') \u4f1a\u7ea2 -> \u6709\u9274\u522b\u529b")
        finally:
            tp_file.write_text(original, encoding="utf-8")
            sys.modules.pop("runtime.task_plan", None)
        self.assertEqual(tp_file.read_text(encoding="utf-8"), original, "\u6e90\u7801\u672a\u8fd8\u539f\uff01")


if __name__ == "__main__":
    unittest.main()
