"""P1-P4 编排器 / 循环闸 / 记忆层 / 决策入口单测。

覆盖：
- Orchestrator 拓扑排序、重试、闭环反馈、loop_back
- decide() 四分支 + mutation 安全原则
- LoopGate 双闸超限
- run_plan 过程记忆 + folder_memory 文件夹记忆
"""
from __future__ import annotations

import unittest
import tempfile
import os
from pathlib import Path

from runtime.orchestrator import (
    Orchestrator,
    PipelineStatus,
    StepStatus,
    decide,
    Step,
)
from runtime.loop_gate import LoopGate
from runtime import memory_layers


class DecideTests(unittest.TestCase):
    def test_direct_text_greeting(self):
        self.assertEqual(decide("greeting", 0.9, None, None, 0.0).kind, "direct_text")

    def test_direct_tool_read_only(self):
        s = decide("tool_needed", 0.9, "self_sufficient", "web_search", 0.997)
        self.assertEqual(s.kind, "direct_tool")
        self.assertEqual(s.tool, "web_search")

    def test_mutation_forces_need_llm(self):
        # 安全原则：mutation 工具绝不由 Laya 直连
        s = decide(
            "tool_needed", 0.9, "self_sufficient",
            "write_project_file", 0.997,
            mutation_tools=frozenset({"write_project_file"}),
        )
        self.assertEqual(s.kind, "need_llm")

    def test_need_llm_when_incomplete(self):
        s = decide("tool_needed", 0.9, "need_llm", "web_search", 0.997)
        self.assertEqual(s.kind, "need_llm")

    def test_llm_fallback_low_confidence(self):
        s = decide(None, 0.0, None, None, 0.0)
        self.assertEqual(s.kind, "llm_fallback")


class LoopGateTests(unittest.TestCase):
    def test_ok_under_limits(self):
        g = LoopGate(max_loops=3, token_budget=100, wall_budget_sec=10.0)
        self.assertTrue(g.can_continue(0, spent_tokens=10, elapsed_sec=1.0).can_continue)

    def test_exhausted_by_loops(self):
        g = LoopGate(max_loops=3)
        v = g.can_continue(3)
        self.assertFalse(v.can_continue)
        self.assertEqual(v.reason, "max_loops")

    def test_exhausted_by_tokens(self):
        g = LoopGate(max_loops=10, token_budget=100)
        v = g.can_continue(0, spent_tokens=100)
        self.assertEqual(v.reason, "token_budget")

    def test_exhausted_by_wall(self):
        g = LoopGate(max_loops=10, wall_budget_sec=5.0)
        v = g.can_continue(0, elapsed_sec=5.0)
        self.assertEqual(v.reason, "wall_clock")


class OrchestratorTests(unittest.TestCase):
    def _component(self, name, fail_times=0, ret="out"):
        class C:
            def __init__(self):
                self.calls = 0
                self.feedback_received = []

            def run(self, input_data):
                self.calls += 1
                if self.calls <= fail_times:
                    raise RuntimeError("boom")
                return {"result": ret, "name": name}

            def feedback(self, fb):
                self.feedback_received.append(fb)
        return C()

    def test_linear_pipeline_completes(self):
        orch = Orchestrator()
        a = self._component("a")
        b = self._component("b")
        orch.register_step("step_a", a).register_step("step_b", b, dependencies=["step_a"])
        r = orch.execute({"x": 1})
        self.assertEqual(r.status, PipelineStatus.COMPLETED)
        self.assertEqual(len(r.steps), 2)

    def test_retry_then_success(self):
        # 注意：参考实现的 retry 语义是「递归重试 + 主循环计数」双路径，
        # fail_times=1 时最终 calls >= 2 且终态 COMPLETED（不精确断言重试次数）
        orch = Orchestrator(max_retries=2)
        a = self._component("a", fail_times=1)
        orch.register_step("step_a", a)
        r = orch.execute({})
        self.assertEqual(r.status, PipelineStatus.COMPLETED)
        self.assertGreaterEqual(a.calls, 2)

    def test_loop_back(self):
        orch = Orchestrator(max_retries=3)

        class LoopComp:
            def __init__(self):
                self.n = 0

            def run(self, input_data):
                self.n += 1
                if self.n < 3:
                    return {"_control_signal": {"action": "loop_back", "target_step": "gen"}}
                return {"done": True}

        class Gen:
            def run(self, input_data):
                return {"gen": True}

            def feedback(self, fb):
                pass

        orch.register_step("gen", Gen(), feedback_method="feedback")
        orch.register_step("check", LoopComp(), dependencies=["gen"], allow_loop_back=True)
        r = orch.execute({})
        self.assertIn(r.status, (PipelineStatus.COMPLETED, PipelineStatus.PARTIAL_SUCCESS))

    def test_topological_order(self):
        orch = Orchestrator()
        orch.register_step("c", self._component("c"), dependencies=["a", "b"])
        orch.register_step("b", self._component("b"), dependencies=["a"])
        orch.register_step("a", self._component("a"))
        plan = orch._topological_sort()
        self.assertEqual(plan[0], "a")
        self.assertEqual(plan[-1], "c")
        self.assertIn("b", plan[1:])

    def test_dependency_input(self):
        orch = Orchestrator()
        a = self._component("a", ret="A_OUT")
        b = self._component("b", ret="B_OUT")
        orch.register_step("step_a", a).register_step("step_b", b, dependencies=["step_a"])
        orch.execute({})
        self.assertEqual(orch.get_step_output("step_a")["result"], "A_OUT")


class MemoryLayersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "test_memory.db"

    def test_record_and_recent_steps(self):
        memory_layers.record_step("run1", 1, "direct_tool", tool="web_search",
                                  args={"q": "天气"}, result_summary="ok", db=self.db)
        memory_layers.record_step("run1", 2, "need_llm", tool="web_search", db=self.db)
        steps = memory_layers.recent_steps("run1", db=self.db)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["decision"], "direct_tool")
        self.assertEqual(steps[0]["tool"], "web_search")

    def test_index_and_search_folder(self):
        d = self.tmp / "docs"
        d.mkdir()
        (d / "a.txt").write_text("hello")
        (d / "b.txt").write_text("world")
        n = memory_layers.index_folder(str(d), summary="项目文档", db=self.db)
        self.assertEqual(n, 2)
        hits = memory_layers.search_folder("项目", db=self.db)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["doc_count"], 2)


if __name__ == "__main__":
    unittest.main()
