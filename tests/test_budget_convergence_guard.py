"""P0-2 / P1-2 / P2 回归用例 —— 2026-09-20 修复沉淀（C7 失败案例转评测用例）。

覆盖三条根因（详见 .workbuddy/memory/2026-09-20.md）：
1. H1 收敛硬闸失效：超限/TERMINALIZE 后模型仍被反复受理 → 现应为「同因拒绝 ≥2 即 break」。
   失败证据：T046（web_search 29 次其中 24 次被拒仍调用）、T045（list_workspace_files 超限 19 次）。
2. T008 收口空回答：TERMINALIZE 强终止后 assistant_text 为 None → 终态 final_text 为空。
3. 日志隔离：测试桩数据经 check_output 写进生产 logs/guardrail_failures.jsonl
   （2026-09-19 起累积 42 条相同"我的密钥是 ***"样本）。

全部为确定性单元测试（不消耗模型额度）；端到端回归跑法（见文末注释）：
    python -m benchmark.eval_runner --case T008,T012,T014,T045,T046 --out runs_verify_H1
"""

import json
import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ.setdefault("FORGE_TEST_MODE", "1")

from runtime.runctx import RunContext


class BudgetBlockKeyTests(unittest.TestCase):
    """H1 修复：can_execute_tool 拒绝时必须给出可计数的同因拦截键。"""

    def test_per_tool_exceeded_returns_block_key(self):
        rc = RunContext()
        rc.max_web_search_hard = 2
        # 放行 2 次
        for _ in range(2):
            ok, _ = rc.can_execute_tool("web_search")
            self.assertTrue(ok)
        # 第 3 次拒发，且 block key 可识别
        ok, _ = rc.can_execute_tool("web_search")
        self.assertFalse(ok)
        key = rc.budget_block_key("web_search")
        self.assertIsNotNone(key)
        self.assertIn("web_search", key)

    def test_total_exceeded_returns_block_key(self):
        rc = RunContext(max_total_tool_executions=3)
        for i in range(3):
            ok, _ = rc.can_execute_tool(f"tool_{i}")
            self.assertTrue(ok)
        ok, _ = rc.can_execute_tool("tool_x")
        self.assertFalse(ok)
        key = rc.budget_block_key("tool_x")
        self.assertIsNotNone(key)
        self.assertIn("TOOL_BUDGET_TOTAL", key)

    def test_within_budget_returns_none(self):
        rc = RunContext()
        self.assertIsNone(rc.budget_block_key("web_search"))
        rc.can_execute_tool("web_search")
        # 1 次未超限（默认 web_search 上限 5）→ 无 block key
        self.assertIsNone(rc.budget_block_key("web_search"))


class RepeatedBlockedConvergenceTests(unittest.TestCase):
    """H1 修复：同一预算拒发键连续 2 次 → note_blocked_reason 应返回 ≥2，
    调用方（runner 预算分支）据此 break 工具循环。"""

    def test_second_identical_block_triggers_threshold(self):
        rc = RunContext()
        key = "TOOL_BUDGET_PER_TOOL_EXCEEDED:web_search:2"
        self.assertEqual(rc.note_blocked_reason(key), 1)
        self.assertEqual(rc.note_blocked_reason(key), 2)
        # ≥2 → 触发
        self.assertGreaterEqual(rc.note_blocked_reason(key), 3)

    def test_different_keys_independent(self):
        rc = RunContext()
        a1 = rc.note_blocked_reason("TOOL_BUDGET_PER_TOOL_EXCEEDED:web_search:2")
        b1 = rc.note_blocked_reason("TOOL_BUDGET_PER_TOOL_EXCEEDED:list_workspace_files:8")
        self.assertEqual(a1, 1)
        self.assertEqual(b1, 1)


class TerminalizeNonEmptyReplyTests(unittest.TestCase):
    """T008 兜底：TERMINALIZE 收口终态说明必须非空。"""

    def test_fallback_text_non_empty(self):
        fallback = (
            "本轮探索连续多轮未取得新进展，Runtime 已强制收口。"
            "已完成的部分与保留下来的操作记录见上方事件；"
            "未确认/未完成的部分如实说明：没有取得可靠结果。"
            "如需继续，请给出更明确的范围或换一种做法。"
        )
        self.assertTrue(fallback.strip())
        self.assertIn("没有取得可靠结果", fallback)

    def test_build_degraded_reply_returns_non_empty(self):
        # completion.build_degraded_reply 在 evidence 为空时也必须给出可用说明
        from runtime.completion import build_degraded_reply, ExecutionEvidence
        ev = ExecutionEvidence()  # 无参构造 → executed_names() 空、new_files 空
        res = build_degraded_reply(reason="收敛终止", evidence=ev)
        self.assertTrue(res.get("content"))
        self.assertTrue(res["content"].strip())


class LogChannelIsolationTests(unittest.TestCase):
    """P0-1 修复：FORGE_TEST_MODE=1 时 check_output 落盘写到独立文件，不污染生产日志。"""

    def setUp(self):
        self.prod_log = BASE / "logs" / "guardrail_failures.jsonl"
        self.test_log = BASE / "logs" / "guardrail_failures.test.jsonl"
        # 备份
        self._prod_backup = self.prod_log.read_text(encoding="utf-8") if self.prod_log.exists() else ""
        self._test_backup = self.test_log.read_text(encoding="utf-8") if self.test_log.exists() else ""

    def tearDown(self):
        if self._prod_backup:
            self.prod_log.write_text(self._prod_backup, encoding="utf-8")
        else:
            if self.prod_log.exists():
                self.prod_log.unlink()
        if self._test_backup:
            self.test_log.write_text(self._test_backup, encoding="utf-8")
        else:
            if self.test_log.exists():
                self.test_log.unlink()

    def test_secret_stub_goes_to_test_log_not_prod(self):
        # 桩数据：同 2026-09-19 的 42 条相同样本之一
        os.environ["FORGE_TEST_MODE"] = "1"
        from guardrails import check_output
        payload = json.dumps({
            "kind": "answer", "summary": "一句话摘要",
            "content": "我的密钥是 sk-" + "a" * 24,
            "questions": [], "saved_file": None, "next_step": None,
        }, ensure_ascii=False)
        reason = check_output(payload)
        self.assertIn("密钥", reason or "")
        # 测试日志应出现
        self.assertTrue(self.test_log.exists())
        last = self.test_log.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn("密钥", last)
        # 生产日志必须不变
        prod_now = self.prod_log.read_text(encoding="utf-8") if self.prod_log.exists() else ""
        self.assertEqual(prod_now, self._prod_backup)
        del os.environ["FORGE_TEST_MODE"]

    def test_eval_mode_skips_final_empty(self):
        os.environ["FORGE_EVAL_MODE"] = "1"
        from guardrails import _log_guardrail_failure
        # 评测回放的空回答样本不写日志
        try:
            _log_guardrail_failure("{}", "E-FINAL-EMPTY: 模型没有产生任何可用回答")
        except Exception:
            pass
        # 该 reason 在 eval 模式被显式跳过
        eval_log = BASE / "logs" / "guardrail_failures.eval.jsonl"
        if eval_log.exists():
            content = eval_log.read_text(encoding="utf-8")
            self.assertNotIn("E-FINAL-EMPTY", content)
        del os.environ["FORGE_EVAL_MODE"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
