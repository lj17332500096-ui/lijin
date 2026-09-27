"""Terminalization（终态治理）——纯函数辅助，不引入第二套状态机。

目标：每个 Run 在有限路径内进入一个明确终态，且 **终态 / 回复 / 证据语义一致**。

- 真实 TaskState 以 ``runtime.task.TaskState`` 为准（不新增 DB 状态）：
  submitted / running / waiting_user / waiting_approval / paused / completed / failed / cancelled。
- provider 错误 / 墙钟超时当前映射为 ``failed``（现有等价终态），但保留结构化
  ``terminal.kind``（provider_error / timeout / …），供审计与 Benchmark 评测区分。
- 本模块只做分类与一致性检查，不做 I/O；状态写入仍集中在 runner。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from runtime.task import TaskState

TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
})

#: 可恢复的暂停态（跨重启保留；用户回答/批准后 resume）
RESUMABLE_STATES: frozenset[TaskState] = frozenset({
    TaskState.SUBMITTED, TaskState.PAUSED,
    TaskState.WAITING_USER, TaskState.WAITING_APPROVAL,
})

#: 语义终态类型（与 TaskState 正交，用于解释“为什么结束”）
KIND_COMPLETED = "completed"
KIND_NEEDS_USER_INPUT = "needs_user_input"
KIND_NEEDS_APPROVAL = "needs_approval"
KIND_REFUSED = "refused"
KIND_CANCELLED = "cancelled"
KIND_TIMEOUT = "timeout"
KIND_PROVIDER_ERROR = "provider_error"
KIND_PARSER_ERROR = "parser_error"
KIND_COMPLETION_REJECTED = "completion_rejected"
KIND_NO_PROGRESS = "no_progress"
KIND_BOUNDED_FAILURE = "bounded_failure"
KIND_FINAL_RESPONSE_FAILED = "final_response_failed"
KIND_TOKEN_BUDGET = "token_budget"  # 2026-09-19 ④ 成本闸门新增
KIND_UNKNOWN = "unknown"


class RunOutcome(StrEnum):
    """Stable user-facing result, separate from the Task lifecycle state.

    ``TaskState`` remains authoritative for whether a Run is active, resumable,
    or terminal. Outcome explains what this execution achieved. Cancellation is
    kept distinct from failure even though the original five-outcome proposal
    did not list it.
    """

    COMPLETED = "completed"
    NEEDS_USER = "needs_user"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunNextAction(StrEnum):
    ANSWER_QUESTIONS = "answer_questions"
    APPROVE = "approve"
    REVIEW_EFFECTS = "review_effects"
    RESUME = "resume"


def classify_run_outcome(
    state: TaskState,
    *,
    kind: str = "",
    side_effect_count: int = 0,
    unknown_side_effect_count: int = 0,
) -> tuple[RunOutcome, RunNextAction | None]:
    """Return (outcome, next_action) from durable state and execution evidence.

    Pending approval is represented as ``blocked`` with ``approve`` as the
    required action. Failed Runs with a completed or uncertain side effect are
    ``partial``; read-only tool use alone does not count as partial work.
    """
    if state == TaskState.COMPLETED:
        return RunOutcome.COMPLETED, None
    if state == TaskState.WAITING_USER:
        return RunOutcome.NEEDS_USER, RunNextAction.ANSWER_QUESTIONS
    if state == TaskState.WAITING_APPROVAL:
        return RunOutcome.BLOCKED, RunNextAction.APPROVE
    if state == TaskState.CANCELLED:
        return RunOutcome.CANCELLED, (
            RunNextAction.REVIEW_EFFECTS
            if side_effect_count or unknown_side_effect_count else None
        )

    if state == TaskState.FAILED:
        if side_effect_count > 0 or unknown_side_effect_count > 0:
            return RunOutcome.PARTIAL, RunNextAction.REVIEW_EFFECTS
        if kind in {KIND_REFUSED, KIND_NEEDS_APPROVAL, KIND_NEEDS_USER_INPUT,
                    KIND_NO_PROGRESS, KIND_COMPLETION_REJECTED}:
            return RunOutcome.BLOCKED, None
        return RunOutcome.FAILED, None

    # A RunResult should normally only be emitted after a stable state has
    # been selected. Treat unexpected active states conservatively.
    return RunOutcome.BLOCKED, RunNextAction.RESUME


@dataclass(frozen=True)
class TerminalReason:
    kind: str
    state: TaskState
    public_message: str = ""


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES


def classify_exception(exc: BaseException) -> TerminalReason:
    """把执行期异常映射为结构化终态原因（状态仍为 failed / cancelled）。"""
    name = type(exc).__name__
    text = str(exc)

    # 取消：由 spawn/finalize_cancelled 收口，这里只做分类
    try:
        import asyncio

        if isinstance(exc, asyncio.CancelledError):
            return TerminalReason(KIND_CANCELLED, TaskState.CANCELLED, "本轮任务已被取消。")
    except Exception:
        pass

    # 进程级退出信号（SystemExit 被 budget 层拦下后转成的普通异常）
    if name == "ProcessExitInterrupted":
        _code = getattr(exc, "exit_code", None)
        _orig = getattr(exc, "original_type", "SystemExit")
        return TerminalReason(
            KIND_BOUNDED_FAILURE, TaskState.FAILED,
            f"本轮执行被进程级退出信号中断（{_orig}"
            + (f"，code={_code}" if _code is not None else "")
            + "），已停止；已发生的操作仍然保留。",
        )

    # 墙钟预算 / 超时
    if name in ("BudgetExceeded", "TimeoutError") or "超过墙钟预算" in text:
        # BudgetExceeded 区分来源：reason == "token_budget" → 专走 KIND_TOKEN_BUDGET
        _reason = getattr(exc, "reason", "") or ""
        if _reason == "token_budget":
            return TerminalReason(KIND_TOKEN_BUDGET, TaskState.FAILED,
                                  "本次执行超出 token 预算，已停止（保留已发生的操作）。")
        return TerminalReason(KIND_TIMEOUT, TaskState.FAILED,
                              "本次执行超过时间上限，已停止。")

    # Provider / 网关错误
    provider_related = (
        hasattr(exc, "provider_kind")
        or hasattr(exc, "status_code")
        or type(exc).__module__.startswith("openai")
        or any(m in text for m in ("Error code:", "No available channel", "request id",
                                   "无效的令牌", "TokenPlan", "429", "503"))
    )
    if provider_related:
        return TerminalReason(KIND_PROVIDER_ERROR, TaskState.FAILED,
                              "模型服务暂时不可用，请稍后重试。")

    # 回复解析 / 最终回答
    if name == "FinalResponseFailed":
        return TerminalReason(KIND_FINAL_RESPONSE_FAILED, TaskState.FAILED,
                              "最终回答生成失败，已执行的操作仍然保留。")

    return TerminalReason(KIND_BOUNDED_FAILURE, TaskState.FAILED, text[:200])


#: 完成声明（与 completion.py 语义一致的最小集；仅用于一致性检查）
_CLAIM_RE = re.compile(
    r"已(?:经)?(?:修改|修复|完成|生成|保存|执行|运行|更新|创建|删除)|"
    r"(?:test|tests|check)\s+(?:passed|ok)|测试(?:已)?通过",
    re.IGNORECASE,
)


def consistency_errors(
    state: TaskState,
    *,
    assistant_text: str = "",
    completion_verdict: str = "",
    has_evidence: bool = False,
) -> list[str]:
    """检查「Run 状态 / 助手最终文案 / Completion 判定」是否语义一致。

    返回不一致项（空 = 一致）。只做记录，不改变状态。
    """
    errors: list[str] = []
    text = assistant_text or ""

    if state == TaskState.COMPLETED:
        if completion_verdict and completion_verdict not in ("pass", ""):
            errors.append(
                f"status=completed 但 completion_verdict={completion_verdict}"
            )
    if state == TaskState.FAILED:
        if _CLAIM_RE.search(text) and not has_evidence:
            errors.append("status=failed 但助手文案含无证据的完成声明")
    if state == TaskState.CANCELLED and _CLAIM_RE.search(text):
        errors.append("status=cancelled 但助手文案声称已完成")
    return errors
