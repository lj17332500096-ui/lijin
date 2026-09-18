"""Task 状态机：唯一允许改 state 的地方。

规则以转换表为准（文档第 21 节）；terminal 状态（completed/failed/cancelled）不可再转。
非法转换抛 AgentError，绝不静默放行。
"""

from runtime.errors import AgentError
from runtime.task import TaskState

ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.SUBMITTED: {TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.RUNNING: {
        TaskState.WAITING_USER,
        TaskState.WAITING_APPROVAL,
        TaskState.PAUSED,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.WAITING_USER: {TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.WAITING_APPROVAL: {TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.PAUSED: {TaskState.RUNNING, TaskState.CANCELLED},
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
}

# 允许被 runtime.resume 的中间状态（FAILED/CANCELLED 是终态：重试 = 建新任务）
RESUMABLE_FROM = {
    TaskState.SUBMITTED,
    TaskState.PAUSED,
    TaskState.WAITING_USER,
    TaskState.WAITING_APPROVAL,
}

TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}


def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def assert_transition(current: TaskState, target: TaskState) -> None:
    if not can_transition(current, target):
        raise AgentError(f"非法任务状态转换：{current.value} → {target.value}")
