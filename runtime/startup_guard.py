"""P0-2 启动期护栏断言：让"审批总开关"有强制生效点。

# 要解决的问题（L-1）

`runtime/approval.py` 的 `ApprovalGate.enabled` 读 `os.getenv("APPROVAL")`。
这个总开关派生出的 `GATED_DEFAULT` 再严密，也抵不过**派生结果没有强制生效点**：
一个未被 git 跟踪的 `.env` 就能把整个护栏关掉，且不产生任何告警——
本轮实测正是如此（`.env:86 APPROVAL=off` 让 18 条审批断言静默失去鉴别力）。

本模块提供那个缺失的生效点，三件事：

1. **审计事件**：关闭态下启动，写一条 `approval.guard.disabled` 事件到事件流；
2. **结构化日志**：同一条事实走 `slog`（运维可 grep），与事件流互补；
3. **fail-closed**：`FORGE_APPROVAL_FAILCLOSED=on`（**默认 on**）时，
   生产 profile 下 `APPROVAL=off` **直接拒绝启动**。

# 判据：显式声明，不许靠猜

无人值守（定时任务 / 常驻 daemon）确实需要关闭审批——此时没有人类能点批准。
但**"我猜它是无人值守"是不允许的**：本模块只认 `.env` 里的**显式声明**
`FORGE_UNATTENDED=scheduled` / `daemon` / `both`，或显式关掉 fail-closed。
任何启发式推断（按渠道名、按 run_id 前缀、按有没有 MCP）都不作为依据。

`runtime/approval.py:98` 的 `AUTO_DENY_CHANNELS` 是**运行期**按渠道自动拒绝，
与本模块的**启动期**声明是两层，不互相替代：运行期自动拒绝仍然生效，
但要跳过启动断言，必须在配置里明说。
"""

from __future__ import annotations

import os
from typing import Any

from runtime.errors import StartupGuardBlocked
from runtime.structured_log import slog

__all__ = [
    "FORGE_APPROVAL_FAILCLOSED_ENV",
    "UNATTENDED_ENV",
    "UNATTENDED_MODES",
    "GuardVerdict",
    "StartupGuardBlocked",
    "approval_guard_verdict",
    "audit_approval_guard",
    "enforce_approval_guard",
    "format_block_message",
]

#: fail-closed 总开关。默认 on —— 护栏被关掉时宁可起不来，也不要静默裸奔。
FORGE_APPROVAL_FAILCLOSED_ENV = "FORGE_APPROVAL_FAILCLOSED"

#: 无人值守场景的**显式声明**键。取值必须是 UNATTENDED_MODES 之一。
UNATTENDED_ENV = "FORGE_UNATTENDED"

#: 合法的无人值守声明取值。scheduled=定时任务，daemon=常驻服务，both=两者。
UNATTENDED_MODES = frozenset({"scheduled", "daemon", "both"})

_TRUTHY = ("on", "true", "1", "yes")
_FALSY = ("off", "false", "0", "no")


def _flag(name: str, default: str) -> str:
    """取配置值并小写去空白；缺省用 default。"""
    return (os.getenv(name) or default).strip().lower()


def _is_truthy(value: str) -> bool:
    return value in _TRUTHY


def _side_effect_tools() -> list[str]:
    """当前**实际生效**的受管副作用工具名（排序保证可复现）。

    必须复用 `ApprovalGate.gated_names` 这个权威实现，**不能**直接读
    `GATED_DEFAULT`：后者只是静态派生（工具目录的 side_effect 元数据），
    而 MCP / 技能类工具是在**运行期**经 `register_gated_names` 登记进
    `_EXTRA_GATED` 的（`integrations/mcp_bridge.py:362`，
    `policy == "approval"` 的 MCP 工具走这条），env 覆盖
    （`APPROVAL_GATED_TOOLS=all` / 前缀）也只有 `gated_names` 才认。

    阻断能力本来就不受影响（`should_gate` 读的是 `gated_names`），旧写法坏的是
    **审计口径**：事件里的 `side_effect_tool_count` / `side_effect_tools`
    会漏报运行期登记的高风险工具，让「护栏关闭时到底有多少工具失去保护」
    这一关键信息失真。同一事实两处实现必然漂移 —— 这里收敛到单一来源。
    """
    from runtime.approval import ApprovalGate

    return sorted(ApprovalGate(None).gated_names)  # type: ignore[arg-type]


class GuardVerdict:
    """启动断言结论。

    字段全部显式，调用方不得依赖"从字符串里再解析一次"——
    判定逻辑只允许存在这一处（与 P1-6 同一条纪律：语义判断不重复实现）。
    """

    __slots__ = ("enabled", "failclosed", "unattended_mode", "side_effect_tools", "reason")

    def __init__(
        self,
        *,
        enabled: bool,
        failclosed: bool,
        unattended_mode: str | None,
        side_effect_tools: list[str],
        reason: str,
    ) -> None:
        self.enabled = enabled
        self.failclosed = failclosed
        self.unattended_mode = unattended_mode
        self.side_effect_tools = side_effect_tools
        self.reason = reason

    @property
    def should_block(self) -> bool:
        """是否拒绝启动。

        四个条件同时成立才阻断：护栏关闭 + 存在受管副作用工具
        + fail-closed 生效 + **没有显式声明无人值守**。
        最后一项是判据的核心：显式声明才放行，未声明（含非法取值）一律阻断。
        """
        return (
            (not self.enabled)
            and bool(self.side_effect_tools)
            and self.failclosed
            and self.unattended_mode is None
        )

    def payload(self) -> dict[str, Any]:
        """审计事件体（不含任何密钥值，只含配置判定与工具名清单）。"""
        return {
            "approval_enabled": self.enabled,
            "failclosed": self.failclosed,
            "unattended_mode": self.unattended_mode,
            "side_effect_tool_count": len(self.side_effect_tools),
            "side_effect_tools": self.side_effect_tools,
            "should_block": self.should_block,
            "reason": self.reason,
        }

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return (
            f"GuardVerdict(enabled={self.enabled}, failclosed={self.failclosed}, "
            f"unattended_mode={self.unattended_mode!r}, "
            f"side_effect_tools={len(self.side_effect_tools)}, should_block={self.should_block})"
        )


def approval_guard_verdict() -> GuardVerdict:
    """纯函数式判定当前启动是否合规。**不抛异常、不写文件、不读 .env**。

    显式不做的事（防"启发式"渗入）：
      - 不按渠道名 / run_id 前缀推断无人值守（那是 approval.py 运行期的事）；
      - 不按"当前有没有连接 MCP"推断安全性；
      - 不因"测试环境"就自动放行 —— 测试要放行必须自己设 FORGE_UNATTENDED。
    """
    enabled = _flag("APPROVAL", "on") not in _FALSY
    failclosed = _flag(FORGE_APPROVAL_FAILCLOSED_ENV, "on") in _TRUTHY

    raw_unattended = (os.getenv(UNATTENDED_ENV) or "").strip().lower()
    unattended_mode: str | None = None
    if raw_unattended:
        if raw_unattended in UNATTENDED_MODES:
            unattended_mode = raw_unattended
        else:
            # 声明了但取值非法 —— 按"未声明"处理，且在 reason 里留痕。
            # 不做模糊匹配：写错声明不该换来护栏静默关闭。
            unattended_mode = None
            if not enabled:
                return GuardVerdict(
                    enabled=False, failclosed=failclosed, unattended_mode=None,
                    side_effect_tools=_side_effect_tools(),
                    reason=(
                        f"{UNATTENDED_ENV}={raw_unattended!r} 不是合法取值"
                        f"（合法：{sorted(UNATTENDED_MODES)}），视为未声明无人值守"
                    ),
                )

    tools = _side_effect_tools()

    if enabled:
        reason = "审批门开启，启动断言通过"
    elif unattended_mode is not None:
        reason = (
            f"审批门关闭，但 {UNATTENDED_ENV} 显式声明了无人值守模式 "
            f"{unattended_mode!r}（运行期 AUTO_DENY_CHANNELS 仍会兜底拒绝）"
        )
    elif not failclosed:
        reason = (
            f"审批门关闭且未声明无人值守，但 {FORGE_APPROVAL_FAILCLOSED_ENV} 已显式关闭，"
            "按部署者要求放行（放行事实已审计）"
        )
    else:
        reason = (
            "审批门关闭、存在受管副作用工具、且未通过配置显式声明无人值守场景 —— "
            f"fail-closed 拒绝启动（要放行请设 {UNATTENDED_ENV}=scheduled|daemon|both "
            f"或 {FORGE_APPROVAL_FAILCLOSED_ENV}=off）"
        )

    return GuardVerdict(
        enabled=enabled,
        failclosed=failclosed,
        unattended_mode=unattended_mode,
        side_effect_tools=tools,
        reason=reason,
    )


def format_block_message(exc: BaseException) -> str:
    """把护栏阻断渲染成给人看的多行提示（CLI / Web / daemon 共用）。

    单独抽出来的理由：护栏异常是 `BaseException` 子类，正常 `except Exception`
    捕不到，所以**必须**由入口显式接住并给出可读原因 —— 否则用户看到的是
    一段 Python 崩栈，而不是"审批护栏拒绝了启动、缺哪个声明"。
    """
    reason = getattr(exc, "reason", "") or str(exc)
    hint = getattr(exc, "hint", "")
    head = "审批护栏拒绝启动（approval guard blocked startup）"
    lines = [head, "", f"原因：{reason}"]
    if hint:
        lines += ["", hint]
    return "\n".join(lines)


def enforce_approval_guard(
    *,
    events: Any = None,
    raise_on_block: bool = True,
) -> GuardVerdict:
    """启动期执行护栏断言。

    参数
    ----
    events:
        可选的 ``TaskManager``（需有 ``add_event``）。给定时把判定写一条
        ``approval.guard.disabled`` 事件，使"护栏被关"在事件流里**可审计**。
        审计写是 best-effort：写失败只记日志，绝不影响启动判定。
    raise_on_block:
        True（默认）时，fail-closed 命中会抛 ``RuntimeError`` 拒绝启动。

    返回 GuardVerdict，调用方可在 TaskManager 就绪后用
    :func:`audit_approval_guard` 补写审计事件。
    """
    verdict = approval_guard_verdict()

    if verdict.enabled:
        # 护栏开启是健康态；不写审计事件，避免每次启动都写库。
        return verdict

    slog.warning(
        "审批护栏在启动时处于关闭态",
        **verdict.payload(),
    )
    audit_approval_guard(verdict, events)

    if verdict.should_block and raise_on_block:
        raise StartupGuardBlocked(
            verdict.reason,
            reason=verdict.reason,
            hint=(
                f"这是审批护栏的启动期强制断言（{FORGE_APPROVAL_FAILCLOSED_ENV} 默认 on）。\n"
                f"  当前 APPROVAL 取值：{os.getenv('APPROVAL') or '(未设置，按 on 处理)'} —— "
                f"关闭态下 {len(verdict.side_effect_tools)} 个有副作用的工具不受审批保护。\n"
                f"  若这是**无人值守**部署（定时任务 / 常驻服务），请在 .env 显式声明：\n"
                f"      {UNATTENDED_ENV}=scheduled   （或 daemon / both）\n"
                f"  若需要人工审批，把 APPROVAL 设回 on。\n"
                f"  紧急放行（不推荐）：{FORGE_APPROVAL_FAILCLOSED_ENV}=off"
            ),
        )

    return verdict


def audit_approval_guard(verdict: GuardVerdict, events: Any) -> None:
    """把"护栏关闭"这一事实写进事件流（best-effort，永不影响启动判定）。

    与 :func:`enforce_approval_guard` 拆成两步的原因：启动断言必须发生在
    ``Runtime._initialized = True`` **之前**（否则一次阻断后重试会跳过护栏），
    而事件流要等 TaskManager 建好才可用。故"先判定、后落审计"。
    """
    if verdict.enabled or events is None:
        return
    try:
        events.add_event("system", "approval.guard.disabled", verdict.payload())
    except Exception as exc:  # noqa: BLE001 - 审计写不得阻断启动判定
        slog.warning("approval guard 审计事件写入失败", error=repr(exc))
