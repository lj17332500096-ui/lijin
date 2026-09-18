"""RunContext：把 Run 级可变执行状态从“进程级单例”迁移到 contextvars。

原则（P1-C）：所有按 run 变化的执行状态（memory_scope、文件授权范围、审批目标、
工具账本归属、channel）都必须来自当前任务的 contextvar，而不是模块全局/实例最后一个值。

- contextvars 在 asyncio 的 Task 间天然隔离：并发 run_turn 各自持有自己的副本；
- 同一 Task 内同步/异步工具调用都能读到调用方设置的上下文；
- 通过 context 覆盖模块级 fallback（测试/旧调用方仍可用）。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.filescope import FileScope
from runtime.readiness_gate import DiscoveryTracker


def _env_int(name: str, default: int) -> int:
    try:
        v = int(__import__("os").getenv(name, "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class RunContext:
    """一轮 Run 的可变状态（全部由 Runtime 注入，不信任模型输入）。"""

    run_id: str | None = None
    container_id: str | None = None
    session_id: str | None = None
    channel: str = "chat"
    memory_scope: str | None = None      # project_only / global / None(=legacy global)
    profile: str | None = None           # model route profile（default/cheap/reasoning）
    requested_model: str | None = None   # 本轮实际请求的模型名（供 audit 兜底）
    request_text: str | None = None       # 本轮原始用户请求（工具意图授权的事实源）
    file_scope: FileScope | None = None
    activity: Any = field(default=None, repr=False)  # Public projection, never serialized in context.
    # ---- Task Readiness（v2 收口：Runtime 在工具调用前可读的确定性状态）----
    readiness_status: str | None = None     # READY / DISCOVERABLE / NEEDS_USER / UNKNOWN / None
    readiness_missing: list[dict] = field(default_factory=list)  # [{name, reason}]
    mutation_counts: dict[str, int] = field(default_factory=dict)
    _discovery: DiscoveryTracker | None = field(default=None, repr=False)
    # ---- Convergence hardening（Run-level budget + rejected-action memory）----
    # 默认值可被环境变量覆盖（TOOL_BUDGET_TOTAL / TOOL_BUDGET_WEB_SEARCH），
    # 收敛主要靠 semantic intent + result novelty + no-progress，绝对次数只是 safety cap。
    max_total_tool_executions: int = field(
        default_factory=lambda: int(_env_int("TOOL_BUDGET_TOTAL", 20))
    )
    max_web_search_executions: int = field(
        default_factory=lambda: int(_env_int("TOOL_BUDGET_WEB_SEARCH", 5))
    )
    tool_execution_counts: dict[str, int] = field(default_factory=dict)
    rejected_actions: dict[str, str] = field(default_factory=dict)  # tool/intent -> reason
    constraints: dict[str, bool] = field(default_factory=dict)  # {allow_write, allow_delete}
    # ---- needs_user_input：确定性控制状态（缺必需信息后禁止一切真实工具）----
    needs_user_input: bool = False
    pending_questions: list[str] = field(default_factory=list)
    needs_user_blocked_count: int = 0
    # ---- Phase 5：TERMINALIZE 强制收口（一次收口机会，之后 break SDK 工具循环）----
    terminalize_announced: bool = False
    convergence_forced: bool = False

    def enter_needs_user_input(self, questions: list[str] | None = None) -> None:
        self.needs_user_input = True
        if questions:
            for q in questions:
                if q and q not in self.pending_questions:
                    self.pending_questions.append(q)

    def can_execute_tool(self, name: str) -> tuple[bool, str | None]:
        """Run 级工具预算：超限返回 False；放行时**原子预留**一个名额。

        关键：模型可能在一轮里并发发起多个工具调用。若只在执行后才计数，
        并发批会同时通过检查从而突破上限。这里在检查通过时立即占位（事件循环
        单线程、检查与占位之间无 await），保证预算真正封顶。
        """
        total = sum(self.tool_execution_counts.values())
        if total >= self.max_total_tool_executions:
            return False, (
                f"本任务已执行 {total} 次工具调用，达到单 Run 执行上限。请停止继续调用工具，"
                "直接基于已获得的信息给出最终回答；无法完成的部分说明限制。"
            )
        if name == "web_search":
            used = self.tool_execution_counts.get("web_search", 0)
            if used >= self.max_web_search_executions:
                return False, (
                    f"联网搜索已执行 {used} 次，达到本任务上限。请不要再搜索，"
                    "直接基于已有信息作答，或改用其它可用能力，说明当前的限制。"
                )
        # 原子预留
        self.tool_execution_counts[name] = self.tool_execution_counts.get(name, 0) + 1
        return True, None

    def note_executed(self, name: str) -> None:
        """兼容旧调用：预算已在 can_execute_tool 预留，这里不再重复计数。"""
        return None

    def remember_rejected(self, action: str, reason: str) -> None:
        """记录一次被 Runtime 拒绝的动作（非真实执行，不计入执行预算）。"""
        self.rejected_actions.setdefault(action, (reason or "")[:160])

    def action_rejected(self, action: str) -> bool:
        """同一 Run 内是否已经拒绝过该动作（无新上下文时模型不得再试）。"""
        return action in self.rejected_actions

    # ---- Persistent write 幂等（同一 Run 内 save_note/remember 只真实执行一次）----
    _persistence_done: set = None  # type: ignore

    def persistence_already_done(self, name: str) -> bool:
        if self._persistence_done is None:
            return False
        return name in self._persistence_done

    def mark_persistence_done(self, name: str) -> None:
        if self._persistence_done is None:
            self._persistence_done = set()
        self._persistence_done.add(name)

    # ---- 拦截后强制收敛：同一 blocked reason 连续达到阈值 → 结束本轮 ----
    _blocked_counts: dict = None  # type: ignore

    def note_blocked_reason(self, reason: str) -> int:
        """记录一次同因拦截，返回累计次数。≥2 表示应强制收敛。"""
        key = (reason or "blocked")[:60]
        if self._blocked_counts is None:
            self._blocked_counts = {}
        self._blocked_counts[key] = self._blocked_counts.get(key, 0) + 1
        return self._blocked_counts[key]

    def constraint_allows(self, name: str) -> bool:
        """按用户显式约束判断某动作是否允许（默认允许；仅当前 turn）。"""
        if name in ("write_project_file", "edit_project_file", "write_code_file", "save_note"):
            return self.constraints.get("allow_write", True)
        if name in ("delete_task", "sandbox_rollback", "forget_memory"):
            return self.constraints.get("allow_delete", True)
        return True

    def budget_summary(self) -> dict:
        return {
            "total_executed": sum(self.tool_execution_counts.values()),
            "execution_counts": dict(self.tool_execution_counts),
            "rejected_actions": list(self.rejected_actions),
        }

    def set_readiness(self, status: str | None,
                      missing: list[dict] | None = None) -> None:
        self.readiness_status = status
        if missing is not None:
            self.readiness_missing = list(missing)

    def note_discovery(self, name: str, arguments: dict | None,
                       result_text: object | None = None) -> tuple[bool, str | None]:
        """记录一次只读探索（计数 + exact/语义兜底判断 + 已收敛意图拦截）。

        result_text 可选：传入时用于结果新颖度判断（执行后调用）；执行前判断
        用 discovery_blocked 避免提前计数。此方法保证：执行前调用会识别
        blocked_intents 与次数兜底；执行后调用（带 result）会更新新颖度。
        """
        if self._discovery is None:
            self._discovery = DiscoveryTracker()
        return self._discovery.note(name, arguments, result_text=result_text)

    def discovery_blocked(self, name: str, arguments: dict | None) -> tuple[bool, str | None]:
        """执行前只读判断：是否已被收敛/耗尽（不计数、不更新 result）。
        返回 (is_blocked, 引导文本)。"""
        if self._discovery is None:
            return False, None
        return self._discovery.is_blocked(name, arguments)

    def discovery_novelty(self, name: str, arguments: dict | None, result_text: object) -> None:
        """执行后：把本次 result 用于结果新颖度判断（更新 fingerprint + 低新颖计数）。"""
        if self._discovery is None:
            self._discovery = DiscoveryTracker()
        self._discovery.note_result(name, arguments, result_text)

    def discovery_hard_stopped(self) -> bool:
        """连续耗尽信号达到阈值后，只读探索整体硬停止（防止换参数继续空转）。"""
        if self._discovery is None:
            return False
        return self._discovery.hard_stopped()

    def note_mutation(self, name: str, limit: int = 3) -> bool:
        """记录同一类写工具调用；超过单 Run 上限时返回 False。"""
        count = self.mutation_counts.get(name, 0) + 1
        self.mutation_counts[name] = count
        return count <= limit

    def discovery_summary(self) -> dict:
        if self._discovery is None:
            return {}
        return self._discovery.as_dict()

    # ---- Phase 4：Run 级 Progress / Convergence ----

    def _tracker(self) -> DiscoveryTracker:
        if self._discovery is None:
            self._discovery = DiscoveryTracker()
        return self._discovery

    def note_progress(self, name: str, arguments: dict | None,
                      result_text: object | None = None, *,
                      status: str = "executed",
                      error_class: str | None = None):
        """记录一次动作的进展判定（所有工具，不只搜索）。返回 ProgressSignal。"""
        return self._tracker().observe(
            name, arguments, result_text, status=status, error_class=error_class,
        )

    def convergence_level(self) -> str:
        if self._discovery is None:
            return "NORMAL"
        return self._discovery.convergence_level()

    def is_duplicate_action(self, name: str, arguments: dict | None) -> bool:
        if self._discovery is None:
            return False
        return self._discovery.is_duplicate_action(name, arguments)

    def progress_summary(self) -> dict:
        if self._discovery is None:
            return {"convergence_level": "NORMAL"}
        return self._discovery.progress_summary()

    # ---- Phase 9：Evidence-Driven Decision Hint（复用 DiscoveryTracker）----

    def saturation_summary(self) -> dict:
        if self._discovery is None:
            return {}
        return self._discovery.saturation_summary()

    def decision_hint(self) -> str | None:
        if self._discovery is None:
            return None
        return self._discovery.decision_hint()

    # ---- Phase 10：Exact Redundant Guard + Completion-Ready ----

    def _t(self) -> DiscoveryTracker:
        return self._tracker()

    def redundant_check(self, name: str, arguments: dict | None) -> tuple[bool, str]:
        return self._t().redundant_check(name, arguments)

    def mark_exact_seen(self, name: str, arguments: dict | None) -> None:
        self._t().mark_exact_seen(name, arguments)

    def bump_evidence_epoch(self) -> None:
        self._t().bump_epoch()

    def completion_ready(self) -> bool:
        return self._t().completion_ready()

    def note_redundant_suppressed(self, name: str) -> None:
        t = self._t()
        t.redundant_guard_hits += 1
        if name in ("read_workspace_file", "read_code_file", "read_note"):
            t.suppressed_read += 1
        elif name in ("web_search", "search_documents", "search_sources",
                      "list_workspace_files", "list_code_files", "index_workspace"):
            t.suppressed_search += 1
        elif name in ("run_python", "code_loop"):
            t.suppressed_verification += 1

    def guard_summary(self) -> dict:
        if self._discovery is None:
            return {}
        return self._discovery.guard_summary()

    # ---- Phase 11：Per-Target 重复调用护栏（near-identical loop guard）----

    def note_repeat_target(self, name: str, arguments: dict | None) -> int:
        """执行前记录一次目标调用；返回该目标的累计次数（无可解析目标 = 0）。"""
        if self._discovery is None:
            return 0
        return self._discovery.note_repeat_target(name, arguments)

    def repeat_target_saturated(self, name: str, arguments: dict | None) -> tuple[bool, int]:
        """该逻辑目标是否已达 cap（达到 = 应拦截重定向）；不计入计数。"""
        if self._discovery is None:
            return False, 0
        return self._discovery.target_saturated(name, arguments)

    def mark_repeat_target_hit(self) -> None:
        if self._discovery is not None:
            self._discovery.mark_repeat_target_hit()

    def mark_completion_ready_announced(self) -> None:
        self._t().completion_ready_announced = True

    def completion_ready_announced(self) -> bool:
        if self._discovery is None:
            return False
        return self._discovery.completion_ready_announced

    def obligation_ledger(self) -> dict:
        """轻量义务账本（三态 status + satisfied），复用现有状态，不新建 Planner。"""
        from runtime.completion import (
            OBLIGATION_REQUIRED, OBLIGATION_UNKNOWN, extract_obligations,
        )

        t = self._t()
        ob = extract_obligations(self.request_text)
        mutation_satisfied = bool(t.mutation_seen)
        latest_verified = bool(t.verification_passed
                               and t.verified_revision is not None
                               and t.verified_revision == t.evidence_epoch)
        ledger = {
            "mutation": {
                "status": ob["mutation"],
                "satisfied": ("satisfied" if mutation_satisfied else "unsatisfied")
                if ob["mutation"] == OBLIGATION_REQUIRED else "not_applicable",
            },
            "verification": {
                "status": ob["verification"],
                "satisfied": ("satisfied" if latest_verified else "unsatisfied")
                if ob["verification"] == OBLIGATION_REQUIRED else "not_applicable",
                "required_revision": t.evidence_epoch,
                "verified_revision": t.verified_revision,
            },
        }
        return ledger

    def obligation_deficits(self) -> list[str]:
        """返回未满足的必需义务（结构化的 missing 列表）。"""
        from runtime.completion import OBLIGATION_REQUIRED

        ledger = self.obligation_ledger()
        missing: list[str] = []
        for key in ("mutation", "verification"):
            item = ledger[key]
            if item["status"] == OBLIGATION_REQUIRED and item["satisfied"] == "unsatisfied":
                missing.append(key)
        return missing

    def obligation_deficit_signature(self) -> str:
        """轻量 deficit 签名：判断是否仍是“上一次完全相同的缺口”（用于 bounded obligation feedback）。"""
        from runtime.completion import extract_obligations

        t = self._t()
        ob = extract_obligations(self.request_text)
        return "|".join([
            f"rev={t.evidence_epoch}",
            f"mut={ob['mutation']}:{int(bool(t.mutation_seen))}",
            f"ver={ob['verification']}:{int(bool(t.verification_passed))}:{t.verified_revision}",
            f"user={int(bool(self.needs_user_input))}",
        ])

    def verification_due(self) -> bool:
        """mutation 成功且 verification 义务尚未在当前 revision 满足。"""
        from runtime.completion import OBLIGATION_REQUIRED, extract_obligations

        t = self._t()
        if extract_obligations(self.request_text)["verification"] != OBLIGATION_REQUIRED:
            return False
        if not t.mutation_seen:
            return False
        latest = bool(t.verification_passed and t.verified_revision is not None
                      and t.verified_revision == t.evidence_epoch)
        return not latest

    def completion_eligibility(self) -> dict:
        """基于现有 RunContext/DiscoveryTracker 的轻量完成资格 + 可解释 reasons。"""
        from runtime.completion import OBLIGATION_REQUIRED, extract_obligations

        t = self._t()
        ob = extract_obligations(self.request_text)
        reasons: dict = {}
        ok = True
        if ob["mutation"] == OBLIGATION_REQUIRED:
            reasons["mutation_requirement"] = bool(t.mutation_seen)
            ok = ok and t.mutation_seen
        else:
            reasons["mutation_requirement"] = None
        if ob["verification"] == OBLIGATION_REQUIRED:
            ran = bool(t.verification_passed)
            latest = bool(ran and t.verified_revision is not None
                          and t.verified_revision == t.evidence_epoch)
            reasons["verification_requirement"] = ran
            reasons["latest_revision_verified"] = latest
            ok = ok and latest
        else:
            reasons["verification_requirement"] = None
            reasons["latest_revision_verified"] = None
        reasons["pending_user"] = bool(self.needs_user_input)
        reasons["pending_approval"] = False  # 有 pending approval 时本轮已暂停
        if self.needs_user_input:
            ok = False
        if not t.mutation_seen and not t.verification_seen:
            reasons["execution_evidence"] = False
            ok = False
        else:
            reasons["execution_evidence"] = True
        reasons["obligation_deficits"] = self.obligation_deficits()
        # 无明确必需义务时保守：必须 mutation + verification 都发生，才可能 eligible。
        if (ob["mutation"] != OBLIGATION_REQUIRED
                and ob["verification"] != OBLIGATION_REQUIRED):
            fallback = bool(t.mutation_seen and t.verification_passed)
            reasons["fallback_requires_mutation_and_verification"] = fallback
            ok = ok and fallback
        return {"eligible": bool(ok), "reasons": reasons}

    def note_execution_identity(self, name: str, arguments: dict | None) -> None:
        """执行成功后：mutation → epoch+1（使 read/search/verify 缓存失效）；其它 → 标记 exact seen。"""
        from runtime.readiness_gate import _P9_MUTATION_TOOLS

        t = self._t()
        if name in _P9_MUTATION_TOOLS:
            t.bump_epoch()
        else:
            t.mark_exact_seen(name, arguments)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "container_id": self.container_id,
            "session_id": self.session_id,
            "channel": self.channel,
            "memory_scope": self.memory_scope,
            "profile": self.profile,
            "requested_model": self.requested_model,
            "request_text": self.request_text,
            "readiness_status": self.readiness_status,
        }


_current: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "forge_run_context", default=None
)


def current() -> RunContext | None:
    """返回当前 Task 的 RunContext（无则 None，表示非 Run 内调用）。"""
    return _current.get()


def bind(ctx: RunContext | None) -> None:
    """为当前 Task 设置 RunContext（每次 run_turn 开始调用一次即覆盖旧值）。"""
    _current.set(ctx)
