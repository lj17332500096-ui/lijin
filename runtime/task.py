"""Task：运行时的一等公民（领域模型）。

约定：Task 代表"一个有目标、有生命周期的工作单元"，不是单轮 LLM 调用——
一个 Task 内部可能包含多次 LLM turn 与多次工具调用。
任务状态与 LLM turn 状态分离（turn 属于 execution，Task 属于 work）。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class RunBudget:
    """单 Task 的运行预算（本轮先落字段，检查逻辑随 Runner 迁移启用）。"""

    max_turns: int = 20
    max_tool_calls: int = 100
    max_wall_seconds: int = 3600
    max_failures: int = 3
    max_output_tokens: int | None = 4096


@dataclass(slots=True)
class TaskUsage:
    """Task 累计用量（审计/评估用；增量统计随内循环钩子逐步接入）。"""

    turns: int = 0
    tool_calls: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True)
class Task:
    id: str
    session_id: str
    goal: str
    state: TaskState = TaskState.SUBMITTED
    agent_name: str = "assistant"
    parent_task_id: str | None = None
    budget: RunBudget = field(default_factory=RunBudget)
    usage: TaskUsage = field(default_factory=TaskUsage)
    metadata: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(slots=True)
class TaskEvent:
    task_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
