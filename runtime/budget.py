"""任务级预算：把 RunBudget 落成可执行的约束。

当前强制维度：
- max_turns：合成到 Runner 的 max_turns（取更小值）；
- max_wall_seconds：delegate 超时护栏（超时 → BudgetExceeded，任务 failed）。

token/成本维度的精确统计依赖内循环钩子（后续闭环），本轮记录用法并在 checkpoint 上报。
"""

import asyncio
from typing import Any

from runtime.errors import BudgetExceeded
from runtime.task import RunBudget, Task


def resolve_budget(task: Task, requested: RunBudget | None, max_turns: int) -> tuple[RunBudget, int]:
    """合并任务预算与调用方参数：取更严格的 max_turns。"""
    budget = task.budget if requested is None else requested
    effective_turns = min(int(budget.max_turns), max_turns) if budget.max_turns else max_turns
    return budget, max(1, effective_turns)


async def run_with_wall_limit(
    coro,
    budget: RunBudget,
    task: Task,
) -> Any:
    """按预算墙钟时间执行；超时抛 BudgetExceeded（已授权给调用方标记失败）。"""
    limit = float(getattr(budget, "max_wall_seconds", 0) or 0)
    if limit <= 0 or limit >= 3600:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=limit)
    except (asyncio.TimeoutError, TimeoutError):
        raise BudgetExceeded(
            f"任务 {task.id} 超过墙钟预算 {limit:g}s"
        ) from None
