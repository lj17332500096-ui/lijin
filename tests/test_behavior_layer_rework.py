"""FORGE 行为层收敛整改（2026-09-21）回归用例。

覆盖 4 块改动（详见 .workbuddy/memory/2026-09-21.md）：
1. web_search 两级预算（soft=3 / hard=5）：
   - soft_reached 给 policy feedback 不拒绝；
   - hard_reached 拒绝并返回 hard feedback；
   - 保留 max_web_search_executions 兼容字段（= hard）。
2. Clarification Gate 新增 2 类拦截：
   - 文件发送缺「文件 + 接收对象」即拦；
   - 查天气/气温缺「位置」即拦；
   - 命中已有 6 类（航班/提醒/删除/部署/批量删/发送）不回归。
3. benchmark trace 补 normalized_args / result_fingerprint / progress_event /
   canonical_target / workspace_epoch 字段。
4. 行为断言指标：search_call_count / duplicate_tool_calls / no_progress_calls /
   replan_count / clarification_required / clarification_happened。
"""

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ.setdefault("FORGE_TEST_MODE", "1")


class SearchTwoLevelBudgetTests(unittest.TestCase):
    """web_search soft/hard 两级预算。"""

    def test_soft_reached_gives_feedback_not_reject(self):
        from runtime.runctx import RunContext
        rc = RunContext()
        # 默认 soft=3 / hard=5；放行 3 次
        for _ in range(3):
            ok, reason = rc.can_execute_tool("web_search")
            self.assertTrue(ok)
            self.assertIsNone(reason)
        # 第 4 次：used=3 已达 soft 线 → 仍放行但给 soft feedback
        state, used, soft, hard = rc.search_budget_state()
        self.assertEqual(state, "soft_reached")
        self.assertEqual(used, 3)
        fb = rc.search_budget_feedback()
        self.assertIsNotNone(fb)
        self.assertIn("建议搜索预算", fb)
        # 放行（soft 不拒绝）
        ok, _ = rc.can_execute_tool("web_search")
        self.assertTrue(ok)

    def test_hard_reached_rejects(self):
        from runtime.runctx import RunContext
        rc = RunContext()
        for _ in range(5):
            ok, _ = rc.can_execute_tool("web_search")
            self.assertTrue(ok)
        # 第 6 次：used=5 已达 hard 线 → 拒绝
        ok, reason = rc.can_execute_tool("web_search")
        self.assertFalse(ok)
        self.assertIn("硬上限", reason or "")
        state, used, soft, hard = rc.search_budget_state()
        self.assertEqual(state, "hard_reached")
        self.assertEqual(used, 5)
        self.assertEqual(hard, 5)
        # 拒绝后进 convergence 语义（同因拦截键可识别）
        key = rc.budget_block_key("web_search")
        self.assertIsNotNone(key)
        self.assertIn("web_search", key)

    def test_compat_field_equals_hard(self):
        from runtime.runctx import RunContext
        rc = RunContext()
        self.assertEqual(rc.max_web_search_executions, rc.max_web_search_hard)

    def test_soft_hard_overridable_via_env(self):
        import importlib
        os.environ["TOOL_BUDGET_WEB_SEARCH_SOFT"] = "2"
        os.environ["TOOL_BUDGET_WEB_SEARCH"] = "4"
        try:
            import runtime.runctx as rctx_mod
            importlib.reload(rctx_mod)
            rc = rctx_mod.RunContext()
            self.assertEqual(rc.max_web_search_soft, 2)
            self.assertEqual(rc.max_web_search_hard, 4)
        finally:
            os.environ.pop("TOOL_BUDGET_WEB_SEARCH_SOFT", None)
            os.environ.pop("TOOL_BUDGET_WEB_SEARCH", None)
            import runtime.runctx as rctx_mod
            importlib.reload(rctx_mod)


class ClarificationGateNewCasesTests(unittest.TestCase):
    """Clarification Gate 新增 2 类拦截。"""

    def test_file_send_missing_both_blocked(self):
        from runtime.readiness_gate import missing_required_fields
        missing, hint = missing_required_fields(
            "帮我把文件发给他", "send_message", {"content": "x"},
        )
        self.assertTrue(missing)
        self.assertIn("文件", hint or "")
        self.assertIn("接收对象", hint or "")

    def test_file_send_with_file_and_receiver_not_blocked(self):
        from runtime.readiness_gate import missing_required_fields
        missing, _ = missing_required_fields(
            "把 report.xlsx 发给 zhang@example.com", "send_message",
            {"to": "zhang@example.com", "file": "report.xlsx"},
        )
        self.assertFalse(missing)

    def test_file_send_with_file_only_blocked(self):
        from runtime.readiness_gate import missing_required_fields
        missing, hint = missing_required_fields(
            "把 report.xlsx 发给某人", "send_message",
            {"file": "report.xlsx"},
        )
        self.assertTrue(missing)
        self.assertIn("接收对象", hint or "")

    def test_weather_missing_location_blocked(self):
        from runtime.readiness_gate import missing_required_fields
        missing, hint = missing_required_fields(
            "查一下明天天气", "web_search", {"query": "明天天气"},
        )
        self.assertTrue(missing)
        self.assertIn("城市", hint or "")

    def test_weather_with_location_not_blocked(self):
        from runtime.readiness_gate import missing_required_fields
        missing, _ = missing_required_fields(
            "查一下明天北京天气", "web_search", {"query": "北京明天天气"},
        )
        self.assertFalse(missing)

    def test_existing_six_rules_still_fire(self):
        """回归：原有 6 类（航班/提醒/删除/部署/批量删/发送）不破坏。
        ⚠️ 「提醒」类正则把 早上/下午/晚上/点/明天/今天/周/分钟后/上午/整点/日期 视为
        时间已提供，所以带相对时间的提醒文案不拦（既有设计，非本轮改动破坏）。
        本回归只覆盖 6 类中真正缺字段、且既有正则能命中的 4 条。"""
        from runtime.readiness_gate import missing_required_fields
        cases = [
            ("帮我查一下去上海的航班", "web_search"),       # 缺出发地（航班类）
            ("帮我部署一下", "run_python"),                   # 缺目标环境（部署类）
            ("把没用的文件删掉", "write_project_file"),       # 缺「没用」标准（删除类）
            ("删除所有项目里的文件", "write_project_file"),   # 破坏性批量删
        ]
        for text, tool in cases:
            missing, _ = missing_required_fields(text, tool, {})
            self.assertTrue(missing, f"应拦截：{text!r}")


class TraceFieldTests(unittest.TestCase):
    """trace 补字段：normalized_args / result_fingerprint / progress_event /
    canonical_target / workspace_epoch 进入 tool_calls。"""

    def test_call_record_has_new_fields(self):
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T999",
            tool_calls=[
                {
                    "name": "web_search", "status": "executed",
                    "reason": "", "output_head": "结果1",
                    "normalized_args": json.dumps({"query": "北京天气"}),
                    "result_fingerprint": "abc123",
                    "progress_event": "web_search|weather|beijing|today|temperature",
                    "canonical_target": "query:北京天气",
                    "workspace_epoch": 0,
                },
            ],
        )
        # search_call_count = 1
        self.assertEqual(obs.search_call_count(), 1)
        # duplicate = 0（单次调用）
        self.assertEqual(obs.duplicate_tool_calls(), 0)
        # no_progress = 0
        self.assertEqual(obs.no_progress_calls(), 0)
        # 旧字段不丢
        self.assertEqual(obs.tool_calls[0]["name"], "web_search")
        self.assertEqual(obs.tool_calls[0]["result_excerpt"]
                         if "result_excerpt" in obs.tool_calls[0] else
                         obs.tool_calls[0]["output_head"], "结果1")


class BehaviorAssertionMetricsTests(unittest.TestCase):
    """行为断言指标：search_call_count / duplicate / no_progress / replan /
    clarification_required / clarification_happened。"""

    def test_search_call_count_counts_executed_only(self):
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T1",
            tool_calls=[
                {"name": "web_search", "status": "executed"},
                {"name": "web_search", "status": "executed"},
                {"name": "web_search", "status": "blocked"},  # 不算
                {"name": "read_workspace_file", "status": "executed"},
            ],
        )
        self.assertEqual(obs.search_call_count(), 2)

    def test_duplicate_counts_same_fp_no_epoch_change(self):
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T2",
            tool_calls=[
                {"name": "web_search", "status": "executed",
                 "result_fingerprint": "X1", "workspace_epoch": 0},
                {"name": "web_search", "status": "executed",
                 "result_fingerprint": "X1", "workspace_epoch": 0},  # dup
                {"name": "web_search", "status": "executed",
                 "result_fingerprint": "X2", "workspace_epoch": 0},  # 不同 fp
            ],
        )
        self.assertEqual(obs.duplicate_tool_calls(), 1)

    def test_duplicate_not_counted_when_epoch_changes(self):
        """read→edit→read 合理重读：epoch 变了不算重复。"""
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T3",
            tool_calls=[
                {"name": "read_workspace_file", "status": "executed",
                 "result_fingerprint": "F1", "workspace_epoch": 0,
                 "canonical_target": "a.py"},
                {"name": "edit_project_file", "status": "executed",
                 "result_fingerprint": "E1", "workspace_epoch": 0},
                {"name": "read_workspace_file", "status": "executed",
                 "result_fingerprint": "F1", "workspace_epoch": 1,  # epoch 变了
                 "canonical_target": "a.py"},
            ],
        )
        self.assertEqual(obs.duplicate_tool_calls(), 0)

    def test_no_progress_counts_same_sig_same_fp(self):
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T4",
            tool_calls=[
                {"name": "web_search", "status": "executed",
                 "progress_event": "web_search|weather|beijing",
                 "result_fingerprint": "P1", "workspace_epoch": 0},
                {"name": "web_search", "status": "executed",
                 "progress_event": "web_search|weather|beijing",
                 "result_fingerprint": "P1", "workspace_epoch": 0},  # no-progress
            ],
        )
        self.assertEqual(obs.no_progress_calls(), 1)

    def test_replan_count_from_blocked_reasons(self):
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T5",
            blocked_reasons=[
                "【重复调用护栏】你已对同一目标调用 3 次",
                "【搜索预算硬上限】web_search 已达到本次任务硬上限",
                "审批拒绝",  # 不算
            ],
        )
        self.assertEqual(obs.replan_count(), 2)

    def test_clarification_happened_on_waiting_user(self):
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T6",
            final_status="waiting_user",
            terminal_state="waiting_user",
        )
        self.assertTrue(obs.clarification_happened())

    def test_clarification_happened_on_questions_reply(self):
        from benchmark.evaluator import Observation

        obs = Observation(
            case_id="T7",
            final_status="completed",
            reply_kind="questions",
        )
        self.assertTrue(obs.clarification_happened())

    def test_as_row_with_behavior_metrics_shape(self):
        from benchmark.evaluator import Observation, ExpectedBehavior, evaluate_case

        obs = Observation(
            case_id="T8",
            tool_calls=[
                {"name": "web_search", "status": "executed",
                 "result_fingerprint": "Z1", "workspace_epoch": 0},
            ],
            final_status="completed",
            final_text="北京明天 25 度",
        )
        expected = ExpectedBehavior(outcome=("completed",), behavior="direct_answer")
        result = evaluate_case(expected, obs)
        row = result.as_row_with_behavior_metrics(obs)
        # 新增字段都在
        for key in ("search_call_count", "duplicate_tool_calls",
                    "no_progress_calls", "replan_count",
                    "clarification_required", "clarification_happened"):
            self.assertIn(key, row)
        self.assertEqual(row["search_call_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
