"""Convergence hardening —— 多问题收敛 / Tool Router eligibility / DiscoveryTracker
semantic intent / Run budget / rejected-action memory / Completion partial 的确定性测试。

不依赖模型额度：全部是纯函数 + 轻量状态机的单元测试。
真实模型端到端（Test 1 原失败任务 / Test 9 Coding Agent）见实施报告，
此处覆盖能被确定性验证的收敛逻辑。
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TOOL_ROUTER"] = "on"

from runtime.readiness_gate import (
    DiscoveryTracker,
    semantic_search_intent,
    discovery_signature,
)
from runtime.tool_router import select_tool_names, _MEMORY_NOTE_TOOLS
from runtime.runctx import RunContext


def _all_tools():
    try:
        from agent import assistant_agent
        return [t.name for t in assistant_agent.tools]
    except Exception:
        # 保底：覆盖本套件依赖的核心工具名
        return ["web_search", "read_workspace_file", "list_workspace_files",
                "calculate", "get_current_datetime", "read_note", "list_notes",
                "save_note", "remember", "recall_memory", "forget_memory",
                "search_documents", "run_python", "write_code_file",
                "read_code_file", "list_code_files", "edit_project_file"]


class SemanticIntentTests(unittest.TestCase):
    """Test 2 — Semantic Query Rewrite：不同措辞归并为同一意图。"""

    def test_same_city_weather_normalized(self):
        # 同一实体 + 同一 time_scope + 同一请求字段 → 归并
        sem = {
            "北京现在多少度": "web_search|weather|beijing|today|temperature",
            "北京当前气温": "web_search|weather|beijing|today|temperature",
            "Beijing temperature today": "web_search|weather|beijing|today|temperature",
        }
        for q, expected in sem.items():
            got = semantic_search_intent("web_search", {"query": q})
            self.assertEqual(got, expected, f"{q!r} -> {got} != {expected}")

    def test_different_time_scope_not_merged(self):
        # 今天 vs 明天 → 不同 time_scope，不得归并
        a = semantic_search_intent("web_search", {"query": "北京今天天气"})
        b = semantic_search_intent("web_search", {"query": "北京明天天气"})
        self.assertNotEqual(a, b)

    def test_different_field_not_merged(self):
        # 温度 vs 空气质量 → 不同请求字段，不得归并
        a = semantic_search_intent("web_search", {"query": "北京今天多少度"})
        b = semantic_search_intent("web_search", {"query": "北京今天空气质量"})
        self.assertNotEqual(a, b)

    def test_different_topic_not_merged(self):
        self.assertNotEqual(
            semantic_search_intent("web_search", {"query": "上海天气"}),
            semantic_search_intent("web_search", {"query": "北京天气"}),
        )

    def test_non_search_tool_uses_exact(self):
        # 列目录/读文件走精确签名，不做语义归并
        self.assertEqual(
            semantic_search_intent("list_workspace_files", {"directory": "src"}),
            discovery_signature("list_workspace_files", {"directory": "src"}),
        )


class DiscoveryConvergenceTests(unittest.TestCase):
    """Test 3 — 同语义意图连续尝试 → 语义收敛（不是精确签名耗尽）。"""

    def test_semantic_intent_converges_after_limit(self):
        dt = DiscoveryTracker()
        qs = ["北京现在多少度", "北京当前气温", "Beijing temperature today"]
        for i, q in enumerate(qs, 1):
            ok, _ = dt.note("web_search", {"query": q})
            if i < 3:
                self.assertTrue(ok)
        # 同一语义意图（today+temperature）第 3 次 → 次数兜底收敛
        ok3, hint = dt.note("web_search", {"query": "北京今天气温"})
        self.assertFalse(ok3)
        self.assertIn("SEARCH_CONVERGENCE_REACHED", hint)
        # 收敛后换措辞重试 → 仍拦截（不同措辞不执行）
        ok4, _ = dt.note("web_search", {"query": "北京今天多少度"})
        self.assertFalse(ok4)

    def test_non_search_tool_uses_exact_not_semantic(self):
        # 列目录 3 次精确重复 → DISCOVERY_EXHAUSTED（保留原语义）
        dt = DiscoveryTracker()
        dt.note("list_workspace_files", {"directory": "src"})
        dt.note("list_workspace_files", {"directory": "src"})
        ok, hint = dt.note("list_workspace_files", {"directory": "src"})
        self.assertFalse(ok)
        self.assertIn("DISCOVERY_EXHAUSTED", hint)

    def test_different_topic_not_blocked(self):
        dt = DiscoveryTracker()
        for q in ("上海天气", "广州天气", "成都天气"):
            ok, _ = dt.note("web_search", {"query": q})
            self.assertTrue(ok)
        self.assertEqual(dt.blocked(), [])


class ToolRouterEligibilityTests(unittest.TestCase):
    """Test 5 — 天气/能力介绍请求不得暴露 note/memory 工具。"""

    def test_weather_request_omits_note_memory(self):
        names = select_tool_names("介绍你能做什么有哪些技能并告诉我北京天气", _all_tools())
        leak = [n for n in names if n in _MEMORY_NOTE_TOOLS]
        self.assertEqual(leak, [], f"note/memory 工具不应暴露: {leak}")

    def test_plain_weather_omits_memory(self):
        names = select_tool_names("北京天气", _all_tools())
        self.assertNotIn("save_note", names)
        self.assertNotIn("recall_memory", names)

    def test_explicit_save_request_keeps_save_note(self):
        names = select_tool_names("帮我记住我家的地址", _all_tools())
        # 明确保存/记忆意图 → 允许相应工具
        self.assertTrue(any(n in names for n in ("remember", "save_note")))


class RunBudgetTests(unittest.TestCase):
    """Test 8 — Run 级工具预算：真实执行上限 + web_search 上限。"""

    def test_web_search_budget_exhausted(self):
        rc = RunContext(run_id="r1")
        for _ in range(rc.max_web_search_executions):
            ok, _ = rc.can_execute_tool("web_search")
            self.assertTrue(ok)
            rc.note_executed("web_search")
        ok, reason = rc.can_execute_tool("web_search")
        self.assertFalse(ok)
        self.assertIn("达到本任务上限", reason)

    def test_total_execution_budget(self):
        rc = RunContext(run_id="r1")
        for i in range(rc.max_total_tool_executions):
            ok, _ = rc.can_execute_tool("read_workspace_file")
            self.assertTrue(ok)
            rc.note_executed("read_workspace_file")
        ok, _ = rc.can_execute_tool("run_python")
        self.assertFalse(ok)

    def test_rejected_action_memory(self):
        rc = RunContext(run_id="r1")
        self.assertFalse(rc.action_rejected("save_note"))
        rc.remember_rejected("save_note", "no_user_save_intent")
        self.assertTrue(rc.action_rejected("save_note"))
        # 被拒不计入执行预算
        self.assertEqual(sum(rc.tool_execution_counts.values()), 0)


class CodingNoRegressTests(unittest.TestCase):
    """Test 9 防护 — Coding Agent 多步不同探索不触发语义收敛/budget 误伤。"""

    def test_coding_progression_not_semantically_blocked(self):
        dt = DiscoveryTracker()
        # 真实的代码探索：搜代码 → 读多个不同文件 → 修改 → 测试(不同工具/不同主题)
        steps = [
            ("search_documents", {"query": "登录逻辑"}),
            ("search_documents", {"query": "token 刷新"}),
            ("read_workspace_file", {"path": "auth.py"}),
            ("read_workspace_file", {"path": "api.py"}),
            ("read_workspace_file", {"path": "store.py"}),
            ("run_python", {"filename": "test_auth.py"}),
            ("run_python", {"filename": "test_api.py"}),
        ]
        for name, args in steps:
            ok, _ = dt.note(name, args)
            self.assertTrue(ok, f"{name} {args} 不应被语义收敛拦截")
        self.assertEqual(dt.blocked(), [])

    def test_coding_multi_step_execution_within_budget(self):
        rc = RunContext(run_id="r1")
        # 正常编码任务可能调用较多工具（搜索/读/改/测），但应是不同真实执行
        names = ["read_workspace_file", "read_workspace_file", "read_workspace_file",
                 "write_code_file", "run_python", "run_python"]
        for n in names:
            ok, _ = rc.can_execute_tool(n)
            self.assertTrue(ok)
            rc.note_executed(n)
        self.assertEqual(sum(rc.tool_execution_counts.values()), len(names))


class ResultNoveltyTests(unittest.TestCase):
    """Test 3/13 — 真正的 result novelty：相同结果收敛，持续新增来源不误伤。"""

    def _res(self, *urls):
        return "\n".join(f"{i}. 来源\n   链接: {u}" for i, u in enumerate(urls, 1))

    def test_same_result_converges_after_low_novelty(self):
        dt = DiscoveryTracker()
        same = self._res("https://a.com", "https://b.com")
        r1, _ = dt.note("web_search", {"query": "北京天气1"}, result_text=same)
        r2, _ = dt.note("web_search", {"query": "北京天气2"}, result_text=same)
        self.assertTrue(r1)
        self.assertTrue(r2)  # 首次无 prev，第2次虽重叠但只为1次低新颖
        r3, hint = dt.note("web_search", {"query": "北京天气3"}, result_text=same)
        self.assertFalse(r3)
        self.assertIn("SEARCH_CONVERGENCE_REACHED", hint)

    def test_new_sources_not_blocked(self):
        dt = DiscoveryTracker()
        for i in range(1, 7):
            ok, _ = dt.note("web_search", {"query": f"技术问题 方案{i}"},
                            result_text=self._res(f"https://d{i}.com/run"))
            self.assertTrue(ok, f"持续新增来源不应在第{i}次被拦")
        self.assertEqual(dt.blocked(), [])

    def test_overlap_threshold_ignores_mostly_new(self):
        dt = DiscoveryTracker()
        # 第2次主要来源都新增（only 0.25 overlap）→ 视为有进展
        r1 = dt.note("web_search", {"query": "q1"}, result_text=self._res(
            "https://a.com", "https://b.com", "https://c.com"))[0]
        r2 = dt.note("web_search", {"query": "q2"}, result_text=self._res(
            "https://a.com", "https://x.com", "https://y.com", "https://z.com"))[0]
        self.assertTrue(r1)
        self.assertTrue(r2)


class ExternalFactGateTests(unittest.TestCase):
    """Test 6/7 — Completion Gate 外部事实证据约束（0 证据不 PASS，有证据放行）。"""

    def _gate(self):
        from runtime.completion import CompletionGate
        return CompletionGate()

    def _ev(self, calls=None):
        from runtime.completion import ExecutionEvidence
        return ExecutionEvidence(tool_calls=calls or [])

    def test_unsupported_temperature_not_passed(self):
        g = self._gate()
        v = g.evaluate({"kind": "answer", "content": "北京现在25度", "summary": "天气"},
                       self._ev(), request_text="北京天气")
        self.assertEqual(v.value, "fact_unsupported")

    def test_score_without_evidence_not_passed(self):
        g = self._gate()
        v = g.evaluate({"kind": "answer", "content": "比赛结果 3:1", "summary": "比分"},
                       self._ev(), request_text="比赛结果")
        self.assertEqual(v.value, "fact_unsupported")

    def test_capability_description_pass(self):
        g = self._gate()
        v = g.evaluate({"kind": "answer", "content": "我可以读取文件、修改代码和运行测试。",
                        "summary": "能力"}, self._ev(), request_text="你能做什么")
        self.assertEqual(v.value, "pass")

    def test_retrieval_evidence_pass(self):
        g = self._gate()
        v = g.evaluate({"kind": "answer", "content": "根据搜索，北京今天25度。", "summary": "天气"},
                       self._ev([{"name": "web_search", "status": "executed", "output_head": "北京天气"}]),
                       request_text="北京天气")
        self.assertEqual(v.value, "pass")

    def test_general_qa_without_specific_fact_pass(self):
        g = self._gate()
        v = g.evaluate({"kind": "answer", "content": "北京是中国的首都。", "summary": "京"},
                       self._ev(), request_text="北京是哪里")
        self.assertEqual(v.value, "pass")


if __name__ == "__main__":
    unittest.main()
