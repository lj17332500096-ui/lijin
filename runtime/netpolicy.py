"""run_python / code_loop 网络策略（Policy 层，产品化收口）。

重要（如实）：
- 本模块实现 **Policy/Approval 层**：deny → 工具执行前拦截；approval → 依赖既有
  Approval 门（run_python/code_loop 已在门内）；allow → 放行并记录风险。
- 静态源码标记（requests/socket/http/urllib 等）只做 **Risk Detection**，
  不构成安全隔离；Windows 本机无可靠 OS 级进程网络沙箱/容器，
  OS Network Enforcement 未实现 —— 单独列为下一阶段安全任务，不伪造“已隔离”。
- 每条判定写入运行事件/账本（tool.network_policy）：run_id/tool/policy/decision/risk。
"""

from __future__ import annotations

import os
import re
from typing import Any

#: 网络能力静态标记（Risk Detection 专用，非 enforcement）
NETWORK_MARKERS = re.compile(
    r"(?i)(requests\.|urllib|http\.client|socket\.|aiohttp|httpx|curl|wget|"
    r"https?://|DNS|resolve\()"
)

POLICIES = ("deny", "approval", "allow")

DEFAULT_POLICY = "approval"


def policy_for(tool_name: str) -> str:
    raw = os.getenv("FORGE_RUN_NETWORK_POLICY", "").strip().lower()
    if raw in POLICIES:
        return raw
    return DEFAULT_POLICY


def network_risk(text: str) -> bool:
    """静态判定文本是否出现网络能力/网络访问意图（仅风险检测）。"""
    return bool(text) and bool(NETWORK_MARKERS.search(text))


def _code_text(arguments: dict[str, Any]) -> str:
    return str(arguments.get("code") or "") + "\n" + str(arguments.get("args") or "")


def evaluate(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """对 run_python/code_loop 的一次网络策略判定。

    返回 {policy, decision, block: bool, reason, risk}：
    - policy=deny：仅当可静态判定网络意图（code/args 含标记）时 block=True；
      filename 运行（内容不可静态判定）→ decision=deny_unknown_block（block=True），
      因为 deny 语义下不允许“文件内可能联网”的模糊放行；
    - policy=approval：放行执行，标注 risk（工具级 Approval 门已存在）；
    - policy=allow：放行并记录 risk 供审计。
    """
    risk = network_risk(_code_text(arguments)) or bool(arguments.get("filename"))
    policy = policy_for(tool_name)
    if policy == "deny":
        if risk:
            return {
                "policy": policy, "decision": "blocked", "block": True,
                "risk": risk,
                "reason": ("网络策略 deny：本机子进程无 OS 级网络沙箱，"
                           "为避免代码静默联网，已按策略在工具执行前拦截。"
                           "如需联网运行，请切换到个人会话或调整 FORGE_RUN_NETWORK_POLICY。"),
            }
        return {"policy": policy, "decision": "allow", "block": False,
                "risk": False, "reason": "无可静态判定的网络意图"}
    if policy == "allow":
        return {"policy": policy, "decision": "allow", "block": False,
                "risk": risk,
                "reason": "网络策略 allow（风险已记录）" if risk else "网络策略 allow"}
    # approval：放行（run_python/code_loop 已在 Approval 门内），风险标注进审计
    return {"policy": policy, "decision": "approval", "block": False,
            "risk": risk,
            "reason": "网络策略 approval：执行经工具级 Approval 门；网络沙箱 OS 层未实现"}
