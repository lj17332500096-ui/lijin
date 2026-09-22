"""任务级预算：把 RunBudget 落成可执行的约束。

当前强制维度：
- max_turns：合成到 Runner 的 max_turns（取更小值）；
- max_wall_seconds：delegate 超时护栏（超时 → BudgetExceeded，任务 failed）。

token/成本维度的精确统计依赖内循环钩子（后续闭环），本轮记录用法并在 checkpoint 上报。
"""

import asyncio
from typing import Any

from runtime.errors import BudgetExceeded, ProcessExitInterrupted
from runtime.task import RunBudget, Task


def resolve_budget(task: Task, requested: RunBudget | None, max_turns: int) -> tuple[RunBudget, int]:
    """合并任务预算与调用方参数：取更严格的 max_turns。"""
    budget = task.budget if requested is None else requested
    effective_turns = min(int(budget.max_turns), max_turns) if budget.max_turns else max_turns
    return budget, max(1, effective_turns)


async def _convert_process_exit(coro):
    """把执行体内的进程级异常（``SystemExit`` / ``KeyboardInterrupt``）转成普通异常。

    为什么必须在同一个 Task 内拦下来：asyncio 的 ``Task.__step`` 对
    ``(KeyboardInterrupt, SystemExit)`` 有特例 —— 它会直接 ``raise`` 出
    ``run_forever``，后果是：
      - 外层的 ``await`` 永远不会恢复（本函数所在协程被冻结在 await 点）；
      - 被测协程的 ``finally``（Run 终态兜底）整段被跳过；
      - Run 永久停在 ``running``，只能等下次进程启动的 auto_recover 收尸。
    在同一 Task 内先转成普通异常，两者就到不了 Task 边界，
    ``except Exception`` 能正常把它收口成 failed。

    只拦这两个，不笼统拦 ``BaseException``：``MemoryError`` 本身就是 ``Exception``
    子类，asyncio 对它没有特例，让它按普通异常路径自然传播即可，包一层反而掩盖了
    内存语义。

    收口之后是否把原异常重新抛出，由 ``ProcessExitInterrupted.must_reraise``
    决定（SystemExit 吞掉、KeyboardInterrupt 重抛），本函数不做这个判断 ——
    它只负责「让清理代码有机会跑」。
    """
    try:
        return await coro
    except (SystemExit, KeyboardInterrupt) as exc:
        raise ProcessExitInterrupted(exc) from exc


async def run_with_wall_limit(
    coro,
    budget: RunBudget,
    task: Task,
) -> Any:
    """按预算墙钟时间执行；超时抛 BudgetExceeded（已授权给调用方标记失败）。

    同时把执行体内的进程级异常转成普通异常（见 ``_convert_process_exit``），
    保证 Run 的终态写入路径不会被 asyncio 的 SystemExit 特例整段跳过。
    """
    limit = float(getattr(budget, "max_wall_seconds", 0) or 0)
    # P1-3（2026-09-22）：此前 limit >= 3600 会**静默跳过**墙钟兜底 —— 而 RunBudget
    # 的默认 max_wall_seconds 恰好就是 3600，等于默认配置下唯一的卡死兜底从未生效。
    # 只跳过「未配置」（<=0），不再用魔法阈值放行。
    if limit <= 0:
        return await _convert_process_exit(coro)
    try:
        return await asyncio.wait_for(_convert_process_exit(coro), timeout=limit)
    except (asyncio.TimeoutError, TimeoutError):
        raise BudgetExceeded(
            f"任务 {task.id} 超过墙钟预算 {limit:g}s"
        ) from None
