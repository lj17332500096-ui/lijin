"""Checkpoint：turn 边界的恢复加速点（最小实现）。

原则（与修改建议文档一致）：
- checkpoint 不是唯一真相源——真实数据仍在 tasks/task_events/messages 里；
- 每个 Task 结束时写一条快照（目标/摘要或错误/用量/元数据），用于恢复提示与审计；
- 进程崩溃后 RUNNING 超时任务由 recover_stale_tasks 标记 failed（可重试=新建任务）。
"""

from runtime.task import Task

SCHEMA_VERSION = 1


def save_success_checkpoint(manager, task: Task, summary: str) -> None:
    """任务成功后写 checkpoint（含摘要与用量）。"""
    snapshot = {
        "goal": task.goal[:1000],
        "session_id": task.session_id,
        "final_summary": (summary or "")[:1000],
        "state": task.state.value,
        "usage": {
            "turns": task.usage.turns,
            "tool_calls": task.usage.tool_calls,
            "input_tokens": task.usage.input_tokens,
            "output_tokens": task.usage.output_tokens,
        },
        "metadata": dict(task.metadata),
    }
    manager.write_checkpoint(task.id, SCHEMA_VERSION, snapshot)


def save_failure_checkpoint(manager, task: Task, error: str) -> None:
    """任务失败后写 checkpoint（含错误，便于复盘/重试）。"""
    snapshot = {
        "goal": task.goal[:1000],
        "session_id": task.session_id,
        "error": (error or "")[:1000],
        "state": task.state.value,
        "metadata": dict(task.metadata),
    }
    manager.write_checkpoint(task.id, SCHEMA_VERSION, snapshot)
