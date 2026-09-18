"""Approval：高风险工具的异步审批门（不阻塞 Runner，任务进入 WAITING_APPROVAL）。

机制：
1. 包装高风险工具（按能力风险分类；可用 APPROVAL_GATED_TOOLS 覆盖、
   APPROVAL=off 关闭；默认：执行类 run_python/code_loop + 破坏/恢复类
   sandbox_rollback/forget_memory/schedule_remove）。
   code_loop 内部直接调用 run_python_impl，但因为 code_loop 本身在门内，
   整个循环（含其内部所有子执行）只会在批准后才运行——不会绕过审批。
2. 被包装工具被调用时：已批准(同任务同参数) → 直接执行；已拒绝 → 返回拒绝说明；
   未决定 → 若渠道为 scheduled 自动拒绝（常驻任务无人审批），否则落一条 pending 审批
   并把「需要审批」说明返回给模型，模型据此收尾，任务随后停在 WAITING_APPROVAL。
3. 用户 approve/deny（CLI 交互或网页按钮）→ 恢复任务重跑同一目标；
   已批准记录让门放行对应调用；拒绝则同样按记录直接拒绝，不再问第二遍。
"""

import asyncio
import json
import os
from typing import Any

from runtime.errors import AgentError
from runtime.task import Task

#: 能力风险分类（Approval 针对“实际能力”，不只是表面工具名）
EXECUTION_TOOLS = {"run_python", "code_loop", "run_tests"}
"""EXECUTION：执行任意代码 / 自动执行-修复循环。"""

DESTRUCTIVE_TOOLS = {"sandbox_rollback", "forget_memory", "schedule_remove"}
"""DESTRUCTIVE / RECOVERY：回滚、删除记忆、移除定时任务（改变既有状态且不可逆/需谨慎）。"""

REAL_FILE_EDIT_TOOLS = {"write_project_file", "edit_project_file"}
"""MUTATING（真实文件编辑，非 FORGE 数据目录）：仅在 APPROVAL_GATED_TOOLS=all/* 时纳入。"""

#: 默认审批清单 = 执行类 + 破坏/恢复类 + 真实文件编辑类
#: （P1-1：真实文件编辑默认纳管，防止 APPROVAL 默认开时无人值守静默写盘）
GATED_DEFAULT = EXECUTION_TOOLS | DESTRUCTIVE_TOOLS | REAL_FILE_EDIT_TOOLS

#: P2-6（2026-09-16）：APPROVAL_GATED_TOOLS 支持前缀匹配（如 "run_*,sandbox_*"）。
#: 精确名保持原语义；带 * 的条目按前缀匹配。默认仍为 GATED_DEFAULT。

#: APPROVAL_GATED_TOOLS=all/* 的语义 = 所有需要审批的风险操作
#: （执行 + 破坏/恢复 + 真实文件编辑）。不是“默认 3 件套”，更不是“所有工具”。
ALL_SEMANTICS = GATED_DEFAULT | REAL_FILE_EDIT_TOOLS

#: 运行时登记的高风险工具（MCP 策略映射等接入点追加；与 env 名单取并集）
_EXTRA_GATED: set[str] = set()


def register_gated_names(names: set[str] | list[str] | tuple[str, ...]) -> None:
    """把额外工具名登记进审批门（幂等；用于 MCP 工具权限映射等运行时接入）。"""
    _EXTRA_GATED.update(names)


AUTO_DENY_CHANNELS = {"scheduled", "daemon"}


def _run_id_default_auto_deny(run_id: str) -> bool:
    """run_id 形如 sched_* / cron_* / 含 daemon 标记时，缺省按无人值守渠道处理。"""
    rid = str(run_id or "").lower()
    return rid.startswith("sched") or rid.startswith("cron") or "daemon" in rid


BLOCK_TEXT = (
    "【需要审批】这个操作风险较高，已登记等待用户确认。"
    "不要再次调用本工具或换参数重试——请直接结束本轮，把上面这句原样告诉用户即可。"
)
REPEAT_TEXT = (
    "【仍在等待审批】同类操作已有一个或多个审批请求挂着，本轮已登记，请不要再调用本工具，"
    "直接结束本轮并等待用户处理。"
)
DENY_TEXT = "【审批拒绝】用户已拒绝执行该操作，请向用户说明并停止尝试。"


def _args_key(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _RunGateState:
    """单个 Run 的审批门状态（P1-C：不再用进程级单槽 mutable state）。"""

    __slots__ = ("channel", "pending", "denied", "pending_tools")

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.pending: list[str] = []
        self.denied: list[str] = []
        self.pending_tools: set[str] = set()


class ApprovalGate:
    """审批闸；绑定 TaskManager（持久化 approvals 行）。

    P1-C：状态按 run_id 隔离（_states[run_id]）；begin/end 只切换“本 Run 的活跃上下文”，
    check() 必须携带真实 run_id（来自 RunContext），并发 Run 互不污染。
    """

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._states: dict[str, _RunGateState] = {}
        self._last_key: str | None = None  # 兼容旧调用（测试/无 runctx 语境）的默认键

    # ---------- 配置 ----------

    @property
    def enabled(self) -> bool:
        return os.getenv("APPROVAL", "").strip().lower() not in ("off", "false", "0")

    @property
    def gated_names(self) -> set[str]:
        raw = os.getenv("APPROVAL_GATED_TOOLS", "").strip()
        if raw.lower() in ("all", "*"):
            # 明确语义：所有被标记为需要审批的风险操作（执行+破坏/恢复+真实文件编辑）
            base = set(ALL_SEMANTICS)
        elif raw:
            # P2-6：支持前缀匹配 —— "run_*,sandbox_*,gorden_ppt_apply"
            # 精确条目直接入集合；带 * 的条目按前缀在已登记工具名上展开。
            base: set[str] = set()
            prefixes: list[str] = []
            known = ALL_SEMANTICS | set(_EXTRA_GATED)
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                if item.endswith("*"):
                    prefixes.append(item[:-1])
                else:
                    base.add(item)
            if prefixes:
                for name in known:
                    if any(name.startswith(p) for p in prefixes):
                        base.add(name)
            # 前缀若匹配到尚未登记的工具，动态登记（MCP 策略等接入点）
            base |= set(_EXTRA_GATED)
        else:
            base = set(GATED_DEFAULT)
        return base | set(_EXTRA_GATED)

    def should_gate(self, tool_name: str) -> bool:
        if not self.enabled:
            return False
        if tool_name in self.gated_names:
            return True
        # P2-6：精确名未命中时，检查 env 前缀规则是否也命中
        raw = os.getenv("APPROVAL_GATED_TOOLS", "").strip()
        if raw and raw.lower() not in ("all", "*"):
            prefixes = [i.strip()[:-1] for i in raw.split(",") if i.strip().endswith("*")]
            if any(tool_name.startswith(p) for p in prefixes):
                return True
        return False

    # ---------- Run 上下文（按 run_id 隔离） ----------

    def _state(self, run_id: str | None) -> _RunGateState | None:
        if run_id is not None:
            return self._states.get(run_id)
        return self._states.get(self._last_key) if self._last_key else None

    def begin(self, task_id: str, channel: str = "chat") -> None:
        state = self._states.get(task_id)
        if state is None:
            state = _RunGateState(channel)
            self._states[task_id] = state
        state.channel = channel
        state.pending = []
        state.denied = []
        state.pending_tools = set()
        self._last_key = task_id

    def end(self) -> None:
        # 释放“活跃”只是语义标记：本轮结果（denied/pending）保留给 runner 读取，
        # 直到 run 关闭调用 clear_run(run_id)。兼容旧调用方。
        return

    def clear_run(self, task_id: str) -> None:
        self._states.pop(task_id, None)
        if self._last_key == task_id:
            self._last_key = None

    # ---------- 门：返回 None=放行执行，否则为返回给模型的文本 ----------

    async def check(self, tool_name: str, arguments: dict[str, Any],
                    run_id: str | None = None) -> str | None:
        if not self.should_gate(tool_name):
            return None
        # 受信评测代码根内的 run_python/code_loop：本地受控测试验证，无需审批。
        # 仅当 FORGE_TRUSTED_CODE_ROOTS 显式配置该目录时生效，不影响生产 Project 审批。
        # 判定用**规范化真实路径**（project 解析目录 / 绝对 filename），绝不扫描 code 文本，
        # 防止“注释里写受信根路径”或 sibling 前缀路径绕过 Approval。
        if tool_name in ("run_python", "code_loop"):
            try:
                from code_exec import trusted_root_for
                if trusted_root_for(
                    str((arguments or {}).get("project") or ""),
                    str((arguments or {}).get("filename") or ""),
                    str((arguments or {}).get("args") or ""),
                ) is not None:
                    return None
            except Exception:
                pass
        state = self._state(run_id)
        task_id = run_id or self._last_key
        if state is None or not task_id:
            # 无 Run 上下文：不静默放行高风险操作。
            # 交互渠道（CLI/web chat）→ 落 pending 让本轮挂起走审批流（不拒绝）；
            # 无人值守渠道（scheduled/daemon）→ 直接拒绝（原 AUTO_DENY 语义）。
            # 此前无差别返回 DENY_TEXT，导致裸调用（审批门未 begin）的 READY 工具被误拒。
            if run_id:
                state = _RunGateState("scheduled" if _run_id_default_auto_deny(run_id) else "chat")
                self._states[run_id] = state
            else:
                # 无 run_id：回落到 _last_key（测试/harness 语境）；仍无则按 chat 兜底
                if self._last_key:
                    state = self._states.get(self._last_key) or _RunGateState("chat")
                    self._states[self._last_key] = state
                    task_id = self._last_key
                else:
                    return DENY_TEXT
            if state is None:
                return DENY_TEXT
        key = _args_key(arguments)

        existing = self.manager.find_approval(task_id, tool_name, key)
        if existing is not None:
            if existing["status"] == "approved":
                return None
            if existing["status"] == "denied":
                state.denied.append(existing["id"])
                return DENY_TEXT
            # pending（同一参数已挂起）→ 不再重复建单
            return BLOCK_TEXT
        if tool_name in state.pending_tools:
            return REPEAT_TEXT  # 本轮已为该工具申请过（换参数也不重试，防循环）

        if state.channel in AUTO_DENY_CHANNELS:
            row_id = self.manager.create_approval(
                task_id, tool_name, key, arguments, status="denied", actor="policy"
            )
            state.denied.append(row_id)
            state.pending_tools.add(tool_name)
            return DENY_TEXT + f"（渠道 {state.channel} 自动拒绝高风险操作）"

        row_id = self.manager.create_approval(task_id, tool_name, key, arguments, status="pending")
        state.pending.append(row_id)
        state.pending_tools.add(tool_name)
        hint = json.dumps(arguments, ensure_ascii=False)[:200]
        block_msg = f"【需要审批】工具 {tool_name} 参数 {hint}\n" + BLOCK_TEXT
        # Phase 38: immediate suspension — raise to break tool loop immediately
        from runtime.errors import ApprovalRequired
        exc = ApprovalRequired(block_msg)
        exc.approval_id = row_id
        exc.tool_name = tool_name
        exc.arguments = arguments
        raise exc

    # ---------- 本轮结果（兼容旧属性读取：取最近一次 begin 的 run） ----------

    def pending_for(self, task_id: str) -> list[str]:
        state = self._state(task_id)
        return list(state.pending) if state else []

    def denied_for(self, task_id: str) -> list[str]:
        state = self._state(task_id)
        return list(state.denied) if state else []

    @property
    def pending_this_run(self) -> list[str]:
        state = self._state(None)
        return list(state.pending) if state else []

    @property
    def denied_this_run(self) -> list[str]:
        state = self._state(None)
        return list(state.denied) if state else []

    def clear_run_tracking(self) -> None:
        self._states.clear()
        self._last_key = None
