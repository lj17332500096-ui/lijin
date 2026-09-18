"""Phase 12 义务门（Obligation Gate）独立单测。

义务门是 `_succeed` / completion 循环里的守卫：请求被判定为"必需 mutation /
verification"，但 RunContext 的 DiscoveryTracker 里没有对应执行证据时，收口会被拦下
（missing_required_verification → bounded_failure）。

关键点（历史上多起"明明做完了却失败"的根因）：
  义务门以 **RunContext 的 DiscoveryTracker** 为准，而不是工具包装器账本 `_run_ledger`。
  只往 `_run_ledger` 塞数据不会让义务门放行，必须走 `RunContext.note_progress()`。

判定链：
  RunContext.note_progress(name, args, result_text)
    → DiscoveryTracker.observe()
        · mutation 工具 + 结果命中成功标记 → mutation_seen = True
          （FAILED / UNKNOWN 三态语义：失败或无法判定都不置位）
        · verification 工具 + 结果含"退出码: 0" → verification_passed = True
          且 verified_revision = 当前 evidence_epoch（后续 mutation 会 bump epoch，
          使旧验证结果失效）
  RunContext.obligation_ledger() / obligation_deficits()

集成测试（收口/降级/并发隔离）已在 setUp 显式关闭义务门，判定语义集中在此锁定。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.runctx import RunContext  # noqa: E402
from runtime.runner import (  # noqa: E402
    _log_obligation_block,
    obligation_gate_enabled,
)

# 义务解析样本（与 test_phase12_obligations 的 M1/M6/M7 对齐）
REQ_BOTH = "修复这个 bug 并确保测试通过"          # mutation + verification 必需
REQ_MUTATION_ONLY = "修改 calc.py 的 add 函数，让它返回两数之和。"  # 仅 mutation
REQ_READ_ONLY = "只阅读 calc.py，解释 add 函数，不要修改任何文件。"  # 无义务

WRITE_OK = "已写入 hello.py"
VERIFY_OK = "退出码: 0 ｜ 用时: 0.1s\n[stdout]\n3 passed"
VERIFY_FAIL = "退出码: 1 ｜ [stderr]\nAssertionError"


def _ctx(request_text: str) -> RunContext:
    """构造一个最小 RunContext（dataclass 默认值即可，无需真实 Run）。"""
    return RunContext(request_text=request_text)


class ObligationDeficitTests(unittest.TestCase):
    """obligation_deficits() 的判定语义（义务门的核心决策）。"""

    def test_read_only_request_never_has_deficit(self):
        """纯只读请求：零执行也不得产生义务缺口。"""
        rc = _ctx(REQ_READ_ONLY)
        self.assertEqual(rc.obligation_deficits(), [])

    def test_required_obligations_without_execution_are_all_missing(self):
        rc = _ctx(REQ_BOTH)
        self.assertEqual(rc.obligation_deficits(), ["mutation", "verification"])

    def test_committed_mutation_clears_only_mutation(self):
        rc = _ctx(REQ_BOTH)
        rc.note_progress("write_code_file", {"filename": "a.py"}, WRITE_OK)
        self.assertEqual(rc.obligation_deficits(), ["verification"])

    def test_mutation_then_passed_verification_clears_all(self):
        rc = _ctx(REQ_BOTH)
        rc.note_progress("write_code_file", {"filename": "a.py"}, WRITE_OK)
        rc.note_progress("run_python", {"filename": "a.py"}, VERIFY_OK)
        self.assertEqual(rc.obligation_deficits(), [])

    def test_failed_mutation_does_not_clear_deficit(self):
        """结果含失败标记 → FAILED，不得误判为已修改。"""
        rc = _ctx(REQ_BOTH)
        rc.note_progress("edit_project_file", {"path": "a.py"}, "写入失败：权限不足")
        self.assertEqual(rc.obligation_deficits(), ["mutation", "verification"])
        self.assertFalse(rc._t().mutation_seen)

    def test_unknown_mutation_outcome_does_not_clear_deficit(self):
        """三态语义：无法判定的结果不保守默认成功（否则假修改会蒙混过关）。"""
        rc = _ctx(REQ_BOTH)
        rc.note_progress("edit_project_file", {"path": "a.py"}, "done")
        self.assertIn("mutation", rc.obligation_deficits())

    def test_error_status_mutation_does_not_clear_deficit(self):
        rc = _ctx(REQ_BOTH)
        rc.note_progress("edit_project_file", {"path": "a.py"}, WRITE_OK, status="error")
        self.assertIn("mutation", rc.obligation_deficits())

    def test_failed_verification_keeps_verification_deficit(self):
        rc = _ctx(REQ_BOTH)
        rc.note_progress("write_code_file", {"filename": "a.py"}, WRITE_OK)
        rc.note_progress("run_python", {"filename": "a.py"}, VERIFY_FAIL)
        self.assertEqual(rc.obligation_deficits(), ["verification"])

    def test_stale_verification_invalidated_by_new_mutation(self):
        """先验证通过，之后又改了代码（epoch bump）→ 旧验证结果失效。"""
        rc = _ctx(REQ_BOTH)
        rc.note_progress("write_code_file", {"filename": "a.py"}, WRITE_OK)
        rc.note_progress("run_python", {"filename": "a.py"}, VERIFY_OK)
        self.assertEqual(rc.obligation_deficits(), [])

        rc.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
        rc._t().bump_epoch()  # runtime 在 mutation 成功后调用
        self.assertEqual(rc.obligation_deficits(), ["verification"])

    def test_mutation_only_request_does_not_require_verification(self):
        rc = _ctx(REQ_MUTATION_ONLY)
        rc.note_progress("edit_project_file", {"path": "calc.py"}, "已修改 calc.py")
        self.assertEqual(rc.obligation_deficits(), [])

    def test_non_mutation_tool_never_clears_mutation_deficit(self):
        """forget_memory 等不在 mutation 工具集内：记了账也置不了 mutation_seen。

        这是并发集成用例 `@forget 删除记忆` 被义务门拦下的真实原因，语义锁定在此。
        """
        rc = _ctx(REQ_BOTH)
        rc.note_progress("forget_memory", {"key": "k"}, "已删除")
        self.assertIn("mutation", rc.obligation_deficits())


class ObligationLedgerTests(unittest.TestCase):
    """obligation_ledger() 的结构与 revision 记账。"""

    def test_ledger_marks_not_applicable_when_not_required(self):
        rc = _ctx(REQ_READ_ONLY)
        ledger = rc.obligation_ledger()
        self.assertEqual(ledger["mutation"]["satisfied"], "not_applicable")
        self.assertEqual(ledger["verification"]["satisfied"], "not_applicable")

    def test_ledger_records_verified_revision(self):
        rc = _ctx(REQ_BOTH)
        rc.note_progress("write_code_file", {"filename": "a.py"}, WRITE_OK)
        epoch_before = rc._t().evidence_epoch
        rc.note_progress("run_python", {"filename": "a.py"}, VERIFY_OK)
        ledger = rc.obligation_ledger()
        self.assertEqual(ledger["verification"]["satisfied"], "satisfied")
        self.assertEqual(ledger["verification"]["verified_revision"], epoch_before)
        self.assertEqual(ledger["verification"]["required_revision"], epoch_before)

    def test_ledger_reports_required_revision_when_unverified(self):
        rc = _ctx(REQ_BOTH)
        ledger = rc.obligation_ledger()
        self.assertEqual(ledger["verification"]["satisfied"], "unsatisfied")
        self.assertIsNone(ledger["verification"]["verified_revision"])
        self.assertEqual(ledger["verification"]["required_revision"], rc._t().evidence_epoch)


class ObligationGateSwitchTests(unittest.TestCase):
    """FORGE_OBLIGATION_GATE 开关契约（集成测试依赖它在 setUp 里关闭义务门）。"""

    def _enabled(self, raw):
        with patch.dict(os.environ, {"FORGE_OBLIGATION_GATE": raw}):
            return obligation_gate_enabled()

    def test_explicit_off_values_disable_gate(self):
        for raw in ("off", "OFF", "0", "false", " off "):
            self.assertFalse(self._enabled(raw), f"{raw!r} 应关闭义务门")

    def test_explicit_on_values_enable_gate(self):
        for raw in ("on", "1", "true", "TRUE"):
            self.assertTrue(self._enabled(raw), f"{raw!r} 应开启义务门")

    def test_default_is_enabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_OBLIGATION_GATE", None)
            self.assertTrue(obligation_gate_enabled())

    def test_unrecognized_value_disables_gate(self):
        """白名单语义：非 on/1/true 的值一律视为关闭（与历史实现保持一致）。"""
        self.assertFalse(self._enabled("yes"))
        self.assertFalse(self._enabled(""))


class ObligationBlockObservabilityTests(unittest.TestCase):
    """拦截必须留下可查证据（否则"明明做完了却失败"无法归因）。"""

    def _runtime_stub(self):
        class _Tasks:
            def __init__(self):
                self.events = []

            def add_event(self, task_id, event_type, payload):
                self.events.append((task_id, event_type, payload))

        class _Runtime:
            def __init__(self):
                self.tasks = _Tasks()

        return _Runtime()

    def test_block_emits_event_and_warning_log(self):
        rt = self._runtime_stub()
        rc = _ctx(REQ_BOTH)
        task = type("T", (), {"id": "task-1"})()
        with self.assertLogs("runtime.runner", level="WARNING") as logs:
            _log_obligation_block(rt, task, rc, ["mutation", "verification"], where="unit")
        self.assertEqual(len(rt.tasks.events), 1)
        _, event_type, payload = rt.tasks.events[0]
        self.assertEqual(event_type, "completion.obligation_blocked")
        self.assertEqual(payload["missing"], ["mutation", "verification"])
        self.assertIn("ledger", payload)       # 账本快照：区分"没执行"和"执行了没记账"
        self.assertIn("request", payload)
        self.assertTrue(any("义务门拦截" in line for line in logs.output))

    def test_emit_event_false_skips_event_but_still_logs(self):
        rt = self._runtime_stub()
        with self.assertLogs("runtime.runner", level="WARNING"):
            _log_obligation_block(rt, type("T", (), {"id": "t"})(), _ctx(REQ_BOTH),
                                  ["verification"], where="completion_loop", emit_event=False)
        self.assertEqual(rt.tasks.events, [])

    def test_event_write_failure_does_not_raise(self):
        class _BrokenTasks:
            def add_event(self, *a, **kw):
                raise RuntimeError("db down")

        rt = type("RT", (), {"tasks": _BrokenTasks()})()
        with self.assertLogs("runtime.runner", level="WARNING"):
            _log_obligation_block(rt, type("T", (), {"id": "t"})(), _ctx(REQ_BOTH),
                                  ["mutation"])

    def test_rc_none_still_records_block(self):
        """RunContext 未绑定时（降级场景）也要留痕，不能静默。"""
        rt = self._runtime_stub()
        with self.assertLogs("runtime.runner", level="WARNING"):
            _log_obligation_block(rt, type("T", (), {"id": "t"})(), None, ["mutation"])
        self.assertEqual(rt.tasks.events[0][2]["missing"], ["mutation"])


if __name__ == "__main__":
    unittest.main()
