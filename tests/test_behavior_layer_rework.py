"""FORGE 行为层收敛整改（2026-09-21）回归用例。

覆盖 4 块改动（详见 .workbuddy/memory/2026-09-21.md）：
1. web_search 两级预算（soft=3 / hard=5）：
   - soft_reached 给 policy feedback 不拒绝；
   - hard_reached 拒绝并返回 hard feedback；
   - 保留 max_web_search_executions 兼容字段（= hard）。
2. Clarification Gate 新增拦截类（文件发送 / 查天气 / 模糊执行意图）：
   - 命中各对抗用例钉死 9 类拦截；
   - 命中已有 6 类（航班/提醒/删除/部署/批量删/发送）不回归。
3. benchmark trace 补 normalized_args / result_fingerprint / progress_event /
   canonical_target / workspace_epoch 字段。
4. 行为断言指标：search_call_count / duplicate_tool_calls / no_progress_calls /
   replan_count / clarification_required / clarification_happened。

Phase 2a（#19）追加：Clarification Gate 9 类拦截的对抗回归矩阵
（test_matrix_pins_all_nine_gate_classes + 各单类正反对照），
把 Phase 1 的拦截能力固化为可重复运行的回归防线。
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

    def test_weather_with_city_name_ending_in_zhou_not_blocked(self):
        from runtime.readiness_gate import missing_required_fields, required_questions
        for text in ("锦州今天的天气怎么样", "锦州的天气怎么样", "辽宁锦州市今天实时天气"):
            with self.subTest(text=text):
                missing, _ = missing_required_fields(text, "get_weather", {"city": "锦州"})
                self.assertFalse(missing)
                self.assertEqual(required_questions(text), [])

    def test_weather_without_location_still_asks(self):
        from runtime.readiness_gate import missing_required_fields, required_questions
        text = "今天的天气怎么样"
        missing, _ = missing_required_fields(text, "get_weather", {})
        self.assertTrue(missing)
        self.assertTrue(any("城市" in q for q in required_questions(text)))

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


# ---------------------------------------------------------------------------
# Phase 2a（#19）：Clarification Gate 9 类拦截的对抗回归矩阵
# ---------------------------------------------------------------------------
# 把 Phase 1 的 9 类拦截固化成可重复运行的回归防线：
#   - 第 1~6 类（航班缺出发地 / 提醒缺时间 / 删文件缺标准 / 部署缺环境 /
#     破坏性批量删 / 发送确认）—— 已有 6 类不回归；
#   - 第 7 类：文件发送缺「文件 + 接收对象」（T007「把这个报告发给李总」对抗）；
#   - 第 8 类：查天气缺「位置」（T011「北京现在天气怎么样」不回归的对称）；
#   - 第 9 类：模糊执行意图缺「具体测试目标」（T044 对抗）。
# 每条钉死「拦截 + 提示文案点名缺失字段」，外加同类的「不拦」正面对照，
# 防止后续改 gate 时把拦截误伤成误杀、或漏掉某一类。

from runtime.readiness_gate import missing_required_fields as _mrf


class ClarificationGateNineClassMatrixTests(unittest.TestCase):
    """9 类拦截对抗矩阵（#19）。

    每条钉死 2 项：
    1. missing_required_fields 返回 True（拦截进 waiting_user）；
    2. 提示文案点名缺失字段（精确追问，非「请补充更多信息」式空泛措辞）。
    同类附「正面对照」（给全必要字段 → 不拦），防止拦截被改过头误杀。
    """

    def _assert_blocked_with_hint(self, text: str, tool: str,
                                  args: dict | None,
                                  must_contain: tuple[str, ...]) -> None:
        missing, hint = _mrf(text, tool, args)
        self.assertTrue(missing, f"应拦截：{text!r}")
        self.assertIsNotNone(hint, f"提示文案缺失：{text!r}")
        for frag in must_contain:
            self.assertIn(frag, hint, f"提示文案应点名 {frag!r}，实际：{hint!r}")

    def _assert_not_blocked(self, text: str, tool: str, args: dict | None) -> None:
        missing, _ = _mrf(text, tool, args)
        self.assertFalse(missing, f"不应拦截：{text!r}")

    # ---- 第 1 类：航班/行程缺出发地 ----
    def test_1_flight_missing_departure_blocked(self):
        # 对称对照：带「从北京」不拦
        self._assert_blocked_with_hint(
            "帮我查一下去上海的航班", "web_search", {},
            ("出发",))
        self._assert_not_blocked(
            "帮我查一下从北京去上海的航班", "web_search", {})

    # ---- 第 2 类：提醒缺时间 ----
    def test_2_remind_missing_time_blocked(self):
        self._assert_blocked_with_hint(
            "提醒我去交资料", "schedule_add", {},
            ("什么时候", "时间"))
        self._assert_not_blocked(
            "提醒我明天早上 9 点去交资料", "schedule_add", {})

    # ---- 第 3 类：删「没用」文件缺标准 ----
    def test_3_delete_useless_missing_criteria_blocked(self):
        self._assert_blocked_with_hint(
            "把没用的文件删掉", "write_project_file", {},
            ("标准",))

    # ---- 第 4 类：部署缺目标环境 ----
    def test_4_deploy_missing_target_env_blocked(self):
        self._assert_blocked_with_hint(
            "帮我部署一下", "run_python", {},
            ("环境",))
        self._assert_not_blocked(
            "帮我部署到本地", "run_python", {})

    # ---- 第 5 类：破坏性批量删缺清单 ----
    def test_5_batch_delete_missing_list_blocked(self):
        self._assert_blocked_with_hint(
            "删除所有项目里的文件", "write_project_file", {},
            ("确认",))

    # ---- 第 6 类：发送/转发确认（非文件场景兜底）----
    def test_6_send_non_file_confirm_blocked(self):
        # 「发给李总」—— 非文件发送场景，走第 6 类兜底确认（收件人+内容）
        self._assert_blocked_with_hint(
            "把这段内容发给李总", "send_message",
            {"content": "x", "to": "李总"},
            ("收件人",))

    # ---- 第 7 类：文件发送缺「文件 + 接收对象」（T007 对抗）----
    def test_7_file_send_missing_both_blocked(self):
        # T007「把这个报告发给李总」：报告=词表文件但无扩展名，李总=接收人
        # → 缺「具体文件引用」，拦截进 waiting_user，且文案同时点名两项
        self._assert_blocked_with_hint(
            "把这个报告发给李总", "send_message", {},
            ("文件", "接收对象"))
        # 给全（带扩展名文件 + 邮箱收件人）→ 不拦
        self._assert_not_blocked(
            "把 report.xlsx 发给 zhang@example.com", "send_message",
            {"to": "zhang@example.com", "file": "report.xlsx"})
        # arguments 已提供 file + receiver 的豁免路径
        self._assert_not_blocked(
            "把报告发出去", "send_message",
            {"file": "report.docx", "to": "li@example.com"})
        # 只缺接收对象 → 文案点名「接收对象」
        self._assert_blocked_with_hint(
            "把 report.xlsx 发给某人", "send_message",
            {"file": "report.xlsx"},
            ("接收对象",))

    # ---- 第 8 类：查天气缺「位置」（T011 对称）----
    def test_8_weather_missing_location_blocked(self):
        # T011「北京现在天气怎么样」带位置 → 不拦
        self._assert_not_blocked(
            "北京现在天气怎么样", "web_search", {})
        # 缺位置 → 拦截，文案点名「城市」
        self._assert_blocked_with_hint(
            "查一下明天天气", "web_search", {"query": "明天天气"},
            ("城市",))
        # 带「我这里」视为有位置 → 不拦
        self._assert_not_blocked(
            "查一下我这里的天气", "web_search", {})

    # ---- 第 9 类：模糊执行意图缺「具体测试目标」（T044 对抗）----
    def test_9_vague_exec_missing_target_blocked(self):
        # T044「我今天想先用 Python 3.13 测一下」—— 无目标文件/函数/命令
        self._assert_blocked_with_hint(
            "我今天想先用 Python 3.13 测一下", "run_python", {},
            ("目标",))
        # 带诊断/修复目标的 coding case（T049 类）不拦
        self._assert_not_blocked(
            "找出这个测试偶发失败的原因并修复", "run_python", {})
        # 带具体文件目标 → 不拦
        self._assert_not_blocked(
            "测一下 app/auth.py 的登录逻辑", "run_python", {})

    # ---- 汇总矩阵：9 类逐条钉死 ----
    def test_matrix_pins_all_nine_gate_classes(self):
        """一张对照表把 9 类拦截钉死。后续改 gate 时任何一条翻红即回归失败。"""
        cases: list[tuple[int, str, str, dict | None, tuple[str, ...]]] = [
            (1, "帮我查一下去上海的航班", "web_search", {}, ("出发",)),
            (2, "提醒我去交资料", "schedule_add", {}, ("时间",)),
            (3, "把没用的文件删掉", "write_project_file", {}, ("标准",)),
            (4, "帮我部署一下", "run_python", {}, ("环境",)),
            (5, "删除所有项目里的文件", "write_project_file", {}, ("确认",)),
            (6, "把这段内容发给李总", "send_message",
             {"content": "x", "to": "李总"}, ("收件人",)),
            (7, "把这个报告发给李总", "send_message", {}, ("文件", "接收对象")),
            (8, "查一下明天天气", "web_search", {"query": "明天天气"}, ("城市",)),
            (9, "我今天想先用 Python 3.13 测一下", "run_python", {}, ("目标",)),
        ]
        for idx, text, tool, args, frags in cases:
            self._assert_blocked_with_hint(text, tool, args, frags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
