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


#: 各工具的**独立**执行上限 —— 不依赖参数，专门兜住"参数漂移"式空转。
#:
#: 为什么需要它：单 Run 总上限（TOOL_BUDGET_TOTAL，默认 20）是最后一道闸，
#: 但它落得太晚 —— 实测 T038 一共 35 次调用，其中 `list_workspace_files` 独占 19 次，
#: 且每次都换一个目录写法（`benchmark_fixture/app` → `my_creative_agent` → …），
#: 任何"按目标去重"的护栏都看不见它，只能等到 20 次总上限才收场。
#: 这里给高频探索类工具设更早、更窄的闸。
#:
#: ⚠️ web_search 的硬上限（TOOL_BUDGET_WEB_SEARCH，默认 5）与软预算见下方两级逻辑；
#: 调整请用环境变量 TOOL_BUDGET_PER_TOOL（格式 `工具名=上限,工具名=上限`），不要改这里。
_DEFAULT_PER_TOOL_BUDGETS: dict[str, int] = {
    "list_workspace_files": 8,
    "list_code_files": 6,
    "list_notes": 6,
    "list_sandbox_snapshots": 6,
    "index_workspace": 3,
    "search_documents": 8,
    "search_sources": 8,
    # P1-B(2)（2026-09-22）：read_workspace_file 设 per-tool cap=8。
    # 实测 T024 撞 TOOL_BUDGET_TOTAL=20（read 13 + list 7 碎片堆叠），
    # 给 read 设更早的独立上限（8），防反复读同一批文件消耗总预算。
    # coding 任务合法读取一般 ≤6 个文件，cap=8 留余量；
    # 调整用 TOOL_BUDGET_PER_TOOL=read_workspace_file=N 覆盖。
    "read_workspace_file": 8,
}

#: web_search 两级预算（软/硬）。
#: 软预算：达到时不拒绝，返回 policy feedback 让模型自判是否值得继续；
#: 硬预算：达到时拒绝继续 web_search，要求基于已有证据收口。
#: 默认 soft=3 / hard=5 —— 第一阶段不引入任务分类（simple/normal/multi-part），
#: 保留 SEARCH_BUDGET_PROFILE 扩展接口，后续可接任务分类器切换档位。
_SEARCH_SOFT_DEFAULT = 3
_SEARCH_HARD_DEFAULT = 5  # 与历史 TOOL_BUDGET_WEB_SEARCH 默认值对齐


def _search_soft_limit() -> int:
    """web_search 软预算：达到时给 policy feedback（不拒绝）。"""
    return int(_env_int("TOOL_BUDGET_WEB_SEARCH_SOFT", _SEARCH_SOFT_DEFAULT))


def _search_hard_limit() -> int:
    """web_search 硬预算：达到时拒绝。默认与 TOOL_BUDGET_WEB_SEARCH 一致（5）。"""
    return int(_env_int("TOOL_BUDGET_WEB_SEARCH", _SEARCH_HARD_DEFAULT))


_SEARCH_SOFT_FEEDBACK = (
    "【搜索预算提示】web_search 已达到本次任务的建议搜索预算。"
    "请先判断现有证据是否已经足够回答；只有在仍缺少一个明确且新的关键证据，"
    "并且下一次搜索与已有搜索目的不同的情况下，才继续搜索。"
    "不要仅通过改写关键词重复查询。"
)

_SEARCH_HARD_FEEDBACK = (
    "【搜索预算硬上限】web_search 已达到本次任务硬上限。"
    "请基于已有证据完成回答；无法确认的内容明确说明无法确认，"
    "不要继续换关键词搜索。"
)


def _env_per_tool_budgets() -> dict[str, int]:
    """解析 TOOL_BUDGET_PER_TOOL，格式 `工具名=上限,工具名=上限`（亦接受分号分隔）。"""
    raw = __import__("os").getenv("TOOL_BUDGET_PER_TOOL", "").strip()
    out: dict[str, int] = {}
    if not raw:
        return out
    for chunk in raw.replace(";", ",").split(","):
        name, sep, cap = chunk.partition("=")
        name, cap = name.strip(), cap.strip()
        if not sep or not name:
            continue
        try:
            n = int(cap)
        except ValueError:
            continue
        if n > 0:
            out[name] = n
    return out


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
    # 默认值可被环境变量覆盖（TOOL_BUDGET_TOTAL / TOOL_BUDGET_WEB_SEARCH /
    # TOOL_BUDGET_PER_TOOL），收敛主要靠 semantic intent + result novelty + no-progress，
    # 绝对次数只是 safety cap。
    max_total_tool_executions: int = field(
        default_factory=lambda: int(_env_int("TOOL_BUDGET_TOTAL", 20))
    )
    #: web_search 两级预算：soft 达到给 feedback（不拒绝），hard 达到拒绝。
    max_web_search_soft: int = field(default_factory=_search_soft_limit)
    max_web_search_hard: int = field(default_factory=_search_hard_limit)
    #: 兼容字段：保留 max_web_search_executions 供读取（= hard），
    #: 新代码请用 search_budget_state() 获取两级状态。
    max_web_search_executions: int = field(default_factory=_search_hard_limit)
    #: 工具名 -> 独立上限（覆盖 _DEFAULT_PER_TOOL_BUDGETS 中的同名项）。
    max_per_tool_executions: dict = field(default_factory=_env_per_tool_budgets)
    tool_execution_counts: dict[str, int] = field(default_factory=dict)
    rejected_actions: dict[str, str] = field(default_factory=dict)  # tool/intent -> reason
    constraints: dict[str, bool] = field(default_factory=dict)  # {allow_write, allow_delete}
    # ---- needs_user_input：确定性控制状态（缺必需信息后禁止一切真实工具）----
    needs_user_input: bool = False
    pending_questions: list[str] = field(default_factory=list)
    needs_user_blocked_count: int = 0
    # ---- P0-C：think / scratchpad（模型→自己的过程地板；只存最近 20 条，不落库、
    #      不进主 model context，避免污染 token；Completion Gate 不把它当 evidence）----
    think_notes: list[str] = field(default_factory=list, repr=False)
    # ---- Phase 5：TERMINALIZE 强制收口（一次收口机会，之后 break SDK 工具循环）----
    terminalize_announced: bool = False
    convergence_forced: bool = False
    # ---- B7：模型轮次计数（tool_calls.turn_number 的真实来源）----
    # 语义：本 Run 内**成功返回**的模型响应条数（从 1 开始计）。
    # 只由 provider 网关在尝试记录 kind ∈ {"success", "ok"} 时递增 —— 重试/限流不算一轮，
    # 因为它们没有产出任何模型输出，也就不会产生工具调用。
    # ⚠️ 生产侧写的是 "success"（见 provider_gateway.record_attempt 的 kind 取值）；
    #    "ok" 只是历史/兜底写法，判据必须同时认这两个，否则本字段恒为 0（静默失效）。
    model_turns: int = 0
    model_latency_ms: list[float] = field(default_factory=list, repr=False)
    # ---- D4：重复调用护栏状态（原 tools._last_repeat_calls 是**进程级全局**）----
    # 语义：tool_name -> {"key": 关键参数指纹, "ts": 时间戳, "count": 次数}
    # 为什么必须 Run 级：进程级全局会让 A 轮搜过的关键词把 B 轮的**第一次**调用判成重复
    # （跨 Run 污染）；测试里得手动 `tools._last_repeat_calls.clear()` 就是征兆。
    # 为什么**不落库**：它是纯过程状态——只在 10 秒窗口内有意义，Run 结束后无任何审计价值；
    # resume 会产生新 run_id，计数本就该重置。落库只会引入 schema 变更与无用的写放大。
    repeat_calls: dict[str, dict] = field(default_factory=dict, repr=False)
    routing_decision_id: str | None = None  # route_agent 决策与本 Run 工具调用的关联键
    # Explicit multi-action requests are enforced as Run-scoped ordered phases.
    capability_plan: dict[str, Any] = field(default_factory=dict, repr=False)
    capability_phase_index: int = 0
    capability_phase_inflight: str | None = None
    capability_plan_uncertain: bool = False

    def configure_capability_plan(self, plan: dict[str, Any] | None) -> None:
        plan = plan if isinstance(plan, dict) else {}
        phases = plan.get("phases")
        self.capability_plan = plan if isinstance(phases, list) and len(phases) > 1 else {}
        self.capability_phase_index = 0
        self.capability_phase_inflight = None
        self.capability_plan_uncertain = False

    def claim_capability_phase_tool(self, tool_name: str) -> tuple[bool, dict[str, Any]]:
        """Atomically reserve the only tool allowed in the current plan phase."""
        phases = self.capability_plan.get("phases") or []
        if len(phases) < 2 or self.capability_phase_index >= len(phases):
            return True, {}
        phase = phases[self.capability_phase_index]
        phase_name = str(phase.get("phase") or f"phase_{self.capability_phase_index + 1}")
        details = {"phase_index": self.capability_phase_index,
                   "phase": phase_name, "tools": list(phase.get("tools") or [])}
        if self.capability_plan_uncertain:
            return False, {**details, "reason": "previous_phase_outcome_unknown",
                           "message": "前一阶段结果不确定，需先由用户核对后才能继续。"}
        if tool_name not in details["tools"]:
            expected = " / ".join(details["tools"]) or "当前阶段指定工具"
            return False, {**details, "reason": "out_of_order_tool",
                           "message": (f"多阶段任务当前处于第 {self.capability_phase_index + 1} 步“{phase_name}”，"
                                       f"应先成功执行 {expected}；当前工具 {tool_name} 属于后续或无关步骤。")}
        if self.capability_phase_inflight is not None:
            return False, {**details, "reason": "phase_tool_already_inflight",
                           "message": "当前阶段已有工具调用在执行，请等待其结果后再继续。"}
        self.capability_phase_inflight = tool_name
        return True, details

    def finish_capability_phase_tool(self, tool_name: str, *, success: bool,
                                     uncertain: bool = False) -> dict[str, Any] | None:
        if self.capability_phase_inflight != tool_name:
            return None
        phases = self.capability_plan.get("phases") or []
        index = self.capability_phase_index
        phase = phases[index] if index < len(phases) else {}
        details = {"phase_index": index,
                   "phase": str(phase.get("phase") or f"phase_{index + 1}"),
                   "tool": tool_name}
        self.capability_phase_inflight = None
        if success:
            self.capability_phase_index += 1
            details["status"] = "completed"
        else:
            self.capability_plan_uncertain = bool(uncertain)
            details["status"] = "unknown" if uncertain else "failed"
            details["uncertain"] = bool(uncertain)
        return details

    def mark_capability_phase_uncertain(self, phase_index: int) -> None:
        """Fail closed when durable phase evidence could not be written."""
        self.capability_phase_index = min(
            self.capability_phase_index, max(0, int(phase_index))
        )
        self.capability_phase_inflight = None
        self.capability_plan_uncertain = True

    def restore_capability_phase_events(self, events: list[Any]) -> None:
        """Restore ordered phase progress; an unmatched start is treated as unknown."""
        if not self.capability_plan:
            return
        latest: dict[int, str] = {}
        for event in events:
            kind = getattr(event, "event_type", None)
            payload = getattr(event, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            try:
                index = int(payload.get("phase_index"))
            except (TypeError, ValueError):
                continue
            if kind == "capability.phase.started":
                latest[index] = "started"
            elif kind == "capability.phase.completed":
                latest[index] = "completed"
            elif kind == "capability.phase.failed":
                latest[index] = "unknown" if payload.get("uncertain") else "failed"
        self.capability_phase_index = 0
        while latest.get(self.capability_phase_index) == "completed":
            self.capability_phase_index += 1
        self.capability_plan_uncertain = any(
            state in ("started", "unknown") for state in latest.values()
        )
        self.capability_phase_inflight = None

    def note_model_turn(self) -> int:
        """记一次成功返回的模型响应，返回递增后的轮次。"""
        self.model_turns = int(self.model_turns or 0) + 1
        return self.model_turns

    def note_repeat_call(self, tool_name: str, key: str, *,
                         window: float = 10.0, now: float | None = None) -> bool:
        """记一次工具调用，并按「同工具 + 同关键参数 + 时间窗口」判定是否构成重复。

        返回 True = 构成重复，调用方应拒绝执行并回 `_REPEAT_HINTS`。
        状态是 **Run 级**，跨 Run 不共享。

        `now` 仅供测试注入时间，生产不传（取 time.time()）。
        """
        import time as _time

        ts = _time.time() if now is None else float(now)
        rec = self.repeat_calls.get(tool_name)
        if rec is not None and rec.get("key") == key and ts - rec.get("ts", 0.0) < window:
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["ts"] = ts
            return rec["count"] >= 2
        self.repeat_calls[tool_name] = {"key": key, "ts": ts, "count": 1}
        return False

    def note_think(self, text: str) -> int:
        """记一条 think 笔记到 Run 级 scratchpad，返回当前条数。

        与 decision_hint（运行时→模型）互补：think 补「模型→自己」的地板。
        只保留最近 20 条（防无限增长）；不落库、不进 context（纯过程记录）。
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return len(self.think_notes)
        self.think_notes.append(cleaned[:500])
        if len(self.think_notes) > 20:
            self.think_notes.pop(0)
        return len(self.think_notes)

    def think_notes_snapshot(self) -> list[str]:
        """返回 think 笔记快照（只读副本）。"""
        return list(self.think_notes)

    def enter_needs_user_input(self, questions: list[str] | None = None) -> None:
        self.needs_user_input = True
        if questions:
            for q in questions:
                if q and q not in self.pending_questions:
                    self.pending_questions.append(q)

    def per_tool_budget(self, name: str) -> int | None:
        """该工具的独立执行上限；None 表示只受单 Run 总上限约束。

        优先级：web_search 的 hard 上限（max_web_search_hard / TOOL_BUDGET_WEB_SEARCH）
        > TOOL_BUDGET_PER_TOOL 显式覆盖 > _DEFAULT_PER_TOOL_BUDGETS。

        ⚠️ web_search 是两级：hard 是拒绝线，soft 只给 feedback。
        per_tool_budget 返回 hard 用于拒绝判断；soft 线请用 search_budget_state()。
        """
        if name == "web_search":
            return self.max_web_search_hard
        if name in self.max_per_tool_executions:
            return self.max_per_tool_executions[name]
        return _DEFAULT_PER_TOOL_BUDGETS.get(name)

    def search_budget_state(self) -> tuple[str, int, int, int]:
        """web_search 两级预算状态。

        返回 (state, used, soft, hard)：
        - state in {"normal", "soft_reached", "hard_reached"}
        - 调用方按 state 决定：
          * normal        → 放行
          * soft_reached  → 放行但返回 policy feedback（不拒绝）
          * hard_reached  → 拒绝，返回 hard feedback

        保留扩展接口：后续可接任务分类器（simple/normal/multi-part）
        通过 SEARCH_BUDGET_PROFILE 环境变量切换 soft/hard 档位。
        当前默认 simple+normal=3/5，multi-part=4/6 走 TOOL_BUDGET_WEB_SEARCH 覆盖。
        """
        used = self.tool_execution_counts.get("web_search", 0)
        soft, hard = self.max_web_search_soft, self.max_web_search_hard
        if used >= hard:
            return "hard_reached", used, soft, hard
        if used >= soft:
            return "soft_reached", used, soft, hard
        return "normal", used, soft, hard

    def search_budget_feedback(self) -> str | None:
        """web_search 两级预算对应的 feedback 文案（normal 时返回 None）。"""
        state, used, soft, hard = self.search_budget_state()
        if state == "hard_reached":
            return _SEARCH_HARD_FEEDBACK
        if state == "soft_reached":
            return _SEARCH_SOFT_FEEDBACK
        return None

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
        cap = self.per_tool_budget(name)
        if cap is not None:
            used = self.tool_execution_counts.get(name, 0)
            if used >= cap:
                # web_search 两级：拒绝（达到 hard 上限）时走硬上限文案，
                # 与 search_budget_state() 的 hard_reached 语义对齐。
                if name == "web_search":
                    return False, _SEARCH_HARD_FEEDBACK
                return False, (
                    f"{name} 已执行 {used} 次，达到本任务上限。请不要再重复调用同一工具，"
                    "换用其它能力或直接基于已获得的信息作答，并说明当前的限制。"
                )
        # 原子预留
        self.tool_execution_counts[name] = self.tool_execution_counts.get(name, 0) + 1
        return True, None

    def budget_block_key(self, name: str) -> str | None:
        """预算拒发（can_execute_tool 返回 False 的场景）对应的"同因拦截"计数键。

        2026-09-20 P0-2（H1 修复）：超限/TERMINALIZE 后的调用仍被继续受理，
        模型在拒绝提示下反复打同一工具（T046 实测 29 次 web_search 其中 24 次被拒），
        纯拒绝段烧掉上百秒回合。调用方对每个预算拒发调用 `note_blocked_reason(key)`，
        返回 ≥2 时应 break 工具循环进终态收口（见 runner 预算分支）。
        """
        total = sum(self.tool_execution_counts.values())
        if total >= self.max_total_tool_executions:
            return f"TOOL_BUDGET_TOTAL_EXCEEDED:{total}"
        cap = self.per_tool_budget(name)
        if cap is not None and self.tool_execution_counts.get(name, 0) >= cap:
            return f"TOOL_BUDGET_PER_TOOL_EXCEEDED:{name}:{cap}"
        return None

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
        if name in ("sandbox_rollback", "forget_memory", "schedule_remove"):
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


def bind(ctx: RunContext | None) -> contextvars.Token:
    """为当前 Task 设置 RunContext，并返回可用于精确恢复的 Token。"""
    return _current.set(ctx)


def reset(token: contextvars.Token) -> None:
    """恢复 bind 前的上下文，支持嵌套 Run 与异常收尾。"""
    _current.reset(token)
