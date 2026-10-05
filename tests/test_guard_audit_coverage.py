"""启动期护栏的审计口径必须反映**实际生效**的审批名单（含运行期登记）。

## 缺陷

`startup_guard._side_effect_tools()` 直接返回 `GATED_DEFAULT`——那是**静态派生**
（`TOOL_CATALOG` 的 `side_effect` 元数据 + 三个集合），而 MCP / 技能类工具是在
**运行期**通过 `runtime.approval.register_gated_names()` 登记进 `_EXTRA_GATED` 的
（见 `integrations/mcp_bridge.py:362`：`policy == "approval"` 的 MCP 工具走这条）。

后果：护栏**阻断能力不受影响**（`ApprovalGate.should_gate` 读的是
`gated_names`，它正确并入了 `_EXTRA_GATED`），但**审计消息说谎** ——
`approval.guard.disabled` 事件里的 `side_effect_tool_count` /
`side_effect_tools` 只反映静态那 10 个，把运行期登记的高风险工具**漏报**。

这与项目里反复吃过的亏是同一类：**同一事实两处实现，必然漂移**。
`ApprovalGate.gated_names` 已经是权威实现，本文件要求审计复用它。

## 本文件锁的契约

1. `_side_effect_tools()` 的输出 == `_gate().gated_names`（同一事实，单一来源）。
2. 运行期登记一个工具后，审计输出**必须包含它**（这是缺陷本身）。
3. `APPROVAL_GATED_TOOLS` 的 env 覆盖（`all` / 前缀）同样要反映在审计里。
4. 登记后**阻断能力确实生效** —— 证明这不是"为了让审计好看"而牺牲安全性。
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import approval as approval_mod
from runtime import startup_guard
from runtime.approval import ApprovalGate
from runtime.startup_guard import approval_guard_verdict


def _gate() -> ApprovalGate:
    """造一个只用于读策略的 gate。

    `gated_names` / `enabled` / `should_gate` 都不碰 `manager`（它只用于
    pending 审批行的持久化），所以这里传None 安全 —— 用真实 TaskManager
    只会让本文件平白依赖数据库。
    """
    return ApprovalGate(None)  # type: ignore[arg-type]


class AuditReflectsRuntimeRegistrations(unittest.TestCase):
    def setUp(self) -> None:
        # 记录并恢复运行期登记，避免污染其它测试（该集合是模块级全局）
        self._extra_before = set(approval_mod._EXTRA_GATED)
        self._env_before = {
            k: os.environ.get(k) for k in
            ("APPROVAL", "APPROVAL_GATED_TOOLS", "FORGE_APPROVAL_FAILCLOSED",
             startup_guard.UNATTENDED_ENV)
        }

    def tearDown(self) -> None:
        approval_mod._EXTRA_GATED.clear()
        approval_mod._EXTRA_GATED.update(self._extra_before)
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_audit_matches_authoritative_gated_names(self) -> None:
        """**单一事实源**：审计输出必须等于 `_gate().gated_names`。

        不做「只查某个工具在不在」那种存在性断言 —— 那在多处出现时无效
        （删掉一处仍满足）。这里比的是**集合相等**。
        """
        audit = set(startup_guard._side_effect_tools())
        authoritative = set(_gate().gated_names)
        self.assertEqual(
            audit, authoritative,
            f"审计口径与权威口径不一致："
            f"仅审计有={sorted(audit - authoritative)}，"
            f"仅权威有={sorted(authoritative - audit)}。"
            f" 审计必须复用 ApprovalGate.gated_names，不要另读 GATED_DEFAULT。",
        )

    def test_runtime_registered_tool_appears_in_audit(self) -> None:
        """**缺陷本体**：运行期登记的高风险工具必须出现在审计里。"""
        marker = "mcp_zzz_test_side_effect_tool"
        self.assertNotIn(marker, startup_guard._side_effect_tools(),
                         "测试前提失效：该名字已天然在审计里")

        approval_mod.register_gated_names([marker])

        audit = startup_guard._side_effect_tools()
        self.assertIn(
            marker, audit,
            "运行期经 register_gated_names 登记的工具（MCP policy=approval 走这条）"
            "没进审计输出 —— 护栏的审计消息会漏报这些高风险工具。",
        )
        # 排序保证可复现（审计消息会被断言/落库）
        self.assertEqual(audit, sorted(audit), "审计输出必须排序，保证可复现")

    def test_env_wildcard_override_appears_in_audit(self) -> None:
        """`APPROVAL_GATED_TOOLS=all` 的语义也必须反映在审计里。"""
        os.environ["APPROVAL_GATED_TOOLS"] = "all"
        audit = set(startup_guard._side_effect_tools())
        gate_names = set(_gate().gated_names)
        self.assertEqual(
            audit, gate_names,
            "env 覆盖（all）下的审计口径与权威口径不一致",
        )

    def test_env_prefix_override_appears_in_audit(self) -> None:
        """前缀匹配覆盖同样要反映（`APPROVAL_GATED_TOOLS=run_*,sandbox_*`）。"""
        approval_mod.register_gated_names(["run_custom_thing"])
        os.environ["APPROVAL_GATED_TOOLS"] = "run_*,sandbox_*"
        audit = set(startup_guard._side_effect_tools())
        self.assertIn(
            "run_custom_thing", audit,
            "前缀匹配命中的运行期工具没进审计输出",
        )
        self.assertEqual(audit, set(_gate().gated_names))

    def test_registration_actually_gates(self) -> None:
        """反空转 + 安全性质：登记后**阻断能力必须真的生效**。

        这条保证本文件的其它断言不是在保护一个「审计好看但拦截失效」的实现。
        """
        marker = "mcp_zzz_gating_check"
        approval_mod.register_gated_names([marker])
        gate = _gate()
        os.environ["APPROVAL"] = "on"
        self.assertTrue(
            gate.should_gate(marker),
            f"登记后的工具 {marker} 未被审批门纳管 —— "
            f"若本条失败，说明 register_gated_names 失效，"
            f"那么「审计里有它」也只是巧合，本文件其余断言无意义。",
        )


if __name__ == "__main__":
    unittest.main()
