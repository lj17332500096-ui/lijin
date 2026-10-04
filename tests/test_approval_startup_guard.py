"""P0-2 启动期护栏断言测试。

验收用例（P1 报告指定的用例名）：
    test_failclosed_blocks_startup_when_side_effect_tools_present

设计要点：
- 断言必须**显式声明**才允许关闭护栏（scheduled/daemon），不许靠启发式推断；
- 判据来自 runtime/startup_guard.py，不在测试里复刻一份规则。
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import startup_guard
from runtime.startup_guard import (
    UNATTENDED_ENV,
    approval_guard_verdict,
    enforce_approval_guard,
)


class StartupGuardEnvMixin:
    """每个用例都在干净环境上跑：清掉全部相关键，避免受开发者 .env 影响。"""

    MANAGED = (
        "APPROVAL",
        startup_guard.FORGE_APPROVAL_FAILCLOSED_ENV,
        UNATTENDED_ENV,
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.MANAGED}
        for k in self.MANAGED:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set(self, **env: str) -> None:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FailClosedTests(StartupGuardEnvMixin, unittest.TestCase):
    """FORGE_APPROVAL_FAILCLOSED=on（默认）时，护栏关闭必须拒绝启动。"""

    def test_failclosed_blocks_startup_when_side_effect_tools_present(self) -> None:
        """P0-2 主验收：APPROVAL=off + 存在 side_effect 工具 -> 拒绝启动。

        这是 P1 报告 §3.5 L-1 的直接对策：一行未跟踪配置不能再无声关闭护栏。
        """
        # 前提：确实存在受管的副作用工具（否则本用例没有鉴别力）
        tools = approval_guard_verdict().side_effect_tools
        self.assertTrue(
            tools,
            "GATED_DEFAULT 为空：本用例已失去鉴别力（无受管副作用工具可被绕过）",
        )

        self._set(APPROVAL="off")

        verdict = approval_guard_verdict()
        self.assertFalse(verdict.enabled, "APPROVAL=off 应被判定为护栏关闭")
        self.assertTrue(verdict.failclosed, "FORGE_APPROVAL_FAILCLOSED 默认应为 on")
        self.assertIsNone(
            verdict.unattended_mode, "未显式声明时不得推断出无人值守模式"
        )
        self.assertTrue(verdict.should_block, "关闭 + 有副作用工具 + failclosed -> 应阻断")

        with self.assertRaises(RuntimeError) as cm:
            enforce_approval_guard()
        self.assertIn("approval-guard", str(cm.exception))
        self.assertIn("拒绝启动", str(cm.exception))

    def test_default_is_failclosed_without_any_env(self) -> None:
        """完全无配置时：APPROVAL 缺省视为 on（护栏开），不阻断。"""
        verdict = approval_guard_verdict()
        self.assertTrue(verdict.enabled, "APPROVAL 缺省必须是开启态（fail-safe）")
        self.assertTrue(verdict.failclosed, "FORGE_APPROVAL_FAILCLOSED 缺省必须是 on")
        self.assertFalse(verdict.should_block)
        enforce_approval_guard()  # 不抛

    def test_no_side_effect_tools_means_no_block(self) -> None:
        """受管副作用工具为空时，即使护栏关闭也不阻断（避免误挡纯净部署）。"""
        self._set(APPROVAL="off")
        real = startup_guard._side_effect_tools
        startup_guard._side_effect_tools = lambda: []
        try:
            verdict = enforce_approval_guard()
            self.assertFalse(verdict.should_block)
        finally:
            startup_guard._side_effect_tools = real


class ExplicitUnattendedDeclarationTests(StartupGuardEnvMixin, unittest.TestCase):
    """无人值守必须由 .env 显式声明 —— 判据不许靠猜。"""

    def test_explicit_scheduled_declaration_allows_off(self) -> None:
        self._set(APPROVAL="off", **{UNATTENDED_ENV: "scheduled"})
        verdict = enforce_approval_guard()
        self.assertFalse(verdict.should_block, "显式声明 scheduled 应放行")
        self.assertEqual(verdict.unattended_mode, "scheduled")

    def test_explicit_daemon_declaration_allows_off(self) -> None:
        self._set(APPROVAL="off", **{UNATTENDED_ENV: "daemon"})
        verdict = enforce_approval_guard()
        self.assertFalse(verdict.should_block, "显式声明 daemon 应放行")
        self.assertEqual(verdict.unattended_mode, "daemon")

    def test_explicit_both_allows_off(self) -> None:
        self._set(APPROVAL="off", **{UNATTENDED_ENV: "both"})
        self.assertFalse(enforce_approval_guard().should_block)

    def test_illegal_declaration_value_does_not_disable_guard(self) -> None:
        """声明了但取值非法 -> 视为未声明，仍阻断（不做模糊匹配）。"""
        self._set(APPROVAL="off", **{UNATTENDED_ENV: "unattended-ish"})
        with self.assertRaises(RuntimeError) as cm:
            enforce_approval_guard()
        self.assertIn(UNATTENDED_ENV, str(cm.exception))
        self.assertIn("不是合法取值", str(cm.exception))

    def test_no_heuristic_inference_from_channel_names(self) -> None:
        """反启发式：像无人值守的渠道名 / run_id 不得被当作声明。

        这是判据的核心纪律 —— 靠猜会让"忘记配置"和"故意无人值守"无法区分。
        """
        for decoy in ("scheduled", "daemon", "cron", "sched_123", "unattended"):
            with self.subTest(decoy=decoy):
                self._set(APPROVAL="off")
                os.environ["FORGE_CHANNEL"] = decoy
                try:
                    with self.assertRaises(RuntimeError):
                        enforce_approval_guard()
                finally:
                    os.environ.pop("FORGE_CHANNEL", None)


class AuditAndLoggingTests(StartupGuardEnvMixin, unittest.TestCase):
    """关闭态必须留下可审计痕迹。"""

    class _FakeEvents:
        def __init__(self) -> None:
            self.rows: list[tuple[str, str, dict]] = []

        def add_event(self, task_id: str, event_type: str, payload: dict | None = None):
            self.rows.append((task_id, event_type, payload or {}))
            return object()

    def test_writes_audit_event_when_disabled(self) -> None:
        self._set(APPROVAL="off", **{UNATTENDED_ENV: "scheduled"})
        events = self._FakeEvents()
        enforce_approval_guard(events=events)
        self.assertEqual(len(events.rows), 1, "关闭态必须写一条审计事件")
        task_id, event_type, payload = events.rows[0]
        self.assertEqual(event_type, "approval.guard.disabled")
        self.assertEqual(task_id, "system")
        self.assertFalse(payload["approval_enabled"])
        self.assertEqual(payload["unattended_mode"], "scheduled")
        self.assertGreater(payload["side_effect_tool_count"], 0)

    def test_no_audit_event_when_enabled(self) -> None:
        """护栏开启是健康态，不写库（避免每次启动都产生事件）。"""
        self._set(APPROVAL="on")
        events = self._FakeEvents()
        enforce_approval_guard(events=events)
        self.assertEqual(events.rows, [])

    def test_audit_write_failure_does_not_break_verdict(self) -> None:
        """审计写是 best-effort：写失败不得改变启动判定。"""
        self._set(APPROVAL="off", **{UNATTENDED_ENV: "daemon"})

        class _Broken:
            def add_event(self, *_a, **_k):
                raise RuntimeError("db down")

        verdict = enforce_approval_guard(events=_Broken())
        self.assertFalse(verdict.should_block, "审计写失败不应影响判定结果")

    def test_raise_on_block_false_returns_verdict(self) -> None:
        """raise_on_block=False 时不抛，仅返回判定（供只读巡检用）。"""
        self._set(APPROVAL="off")
        verdict = enforce_approval_guard(raise_on_block=False)
        self.assertTrue(verdict.should_block)
        self.assertIn("fail-closed", verdict.reason)


class RunnerWiringTests(StartupGuardEnvMixin, unittest.TestCase):
    """护栏必须真的接在 Runtime 初始化路径上（否则只是一个没人调用的模块）。"""

    def test_runner_ensure_blocks_when_guard_fails_closed(self) -> None:
        from runtime.runner import AgentRuntime

        self._set(APPROVAL="off")
        rt = AgentRuntime(db_path="var/tmp/guard_wiring_probe.db")
        with self.assertRaises(RuntimeError) as cm:
            rt._ensure()
        self.assertIn("approval-guard", str(cm.exception))
        self.assertFalse(
            rt._initialized,
            "阻断后不得把 Runtime 标记为已初始化（否则后续调用会跳过护栏）",
        )

    def test_runner_ensure_allows_with_explicit_unattended(self) -> None:
        from runtime.runner import AgentRuntime

        self._set(APPROVAL="off", **{UNATTENDED_ENV: "scheduled"})
        rt = AgentRuntime(db_path="var/tmp/guard_wiring_probe2.db")
        rt._ensure()
        self.assertTrue(rt._initialized)
        self.assertFalse(rt.approval.enabled, "本用例确认放行后护栏确实是关闭态")

    def test_runner_ensure_ok_in_default_state(self) -> None:
        from runtime.runner import AgentRuntime

        self._set(APPROVAL="on")
        rt = AgentRuntime(db_path="var/tmp/guard_wiring_probe3.db")
        rt._ensure()
        self.assertTrue(rt.approval.enabled)

    def test_retry_after_block_does_not_skip_guard(self) -> None:
        """回归：阻断后重试不得因 _initialized 已置位而跳过护栏。

        这条对应一个真实缺陷——护栏最初被接在 `self._initialized = True` 之后，
        首次阻断虽抛了异常，但标志已置位，调用方重试会命中 _ensure 的早退分支
        直接放行，护栏形同虚设。
        """
        from runtime.runner import AgentRuntime

        self._set(APPROVAL="off")
        rt = AgentRuntime(db_path="var/tmp/guard_wiring_probe4.db")
        with self.assertRaises(RuntimeError):
            rt._ensure()
        # 第二次尝试：必须仍然被拦住
        with self.assertRaises(RuntimeError):
            rt._ensure()
        self.assertFalse(rt._initialized)


class GuardStaysInSyncWithApprovalTests(unittest.TestCase):
    """护栏判据必须与 approval 的真实派生结果同源，不得各算一套。"""

    def test_side_effect_tools_come_from_gated_default(self) -> None:
        from runtime.approval import GATED_DEFAULT

        self.assertEqual(startup_guard._side_effect_tools(), sorted(GATED_DEFAULT))

    def test_enabled_matches_approval_gate_enabled(self) -> None:
        """两处对"护栏是否开启"的判定必须一致（否则守卫与被守卫会打架）。"""
        from runtime.approval import ApprovalGate

        saved = os.environ.pop("APPROVAL", None)
        try:
            for raw, expected in (
                (None, True), ("on", True), ("off", False),
                ("false", False), ("0", False), ("OFF", False),
            ):
                with self.subTest(raw=raw):
                    if raw is None:
                        os.environ.pop("APPROVAL", None)
                    else:
                        os.environ["APPROVAL"] = raw
                    gate = ApprovalGate.__new__(ApprovalGate)
                    self.assertEqual(ApprovalGate.enabled.fget(gate), expected)
                    self.assertEqual(approval_guard_verdict().enabled, expected)
        finally:
            os.environ.pop("APPROVAL", None)
            if saved is not None:
                os.environ["APPROVAL"] = saved


if __name__ == "__main__":
    unittest.main()
