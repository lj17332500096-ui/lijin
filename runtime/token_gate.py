"""④ 成本闸门：单 Run 累计 token 预算（与墙钟闸门并列的第二道刹车）。

设计原则
--------
- **零改主链路**：``TokenBudgetGate`` 是一个挂在 Run 上的轻量记账器，
  由内循环（每次 provider 调用后）喂入本调用 token，超预算时抛
  ``BudgetExceeded("token_budget")``，由 Runner 的既有错误处理路径
  （``_fail`` + ``run.terminal`` kind=``token_budget``）收口，不新建分支。
- **默认值**：``FORGE_TOKEN_BUDGET``（缺省 50_000）；显式 0 关闭。
- **记账口径**：provider 返回的 input_tokens + output_tokens（含 system
  prompt 与工具 schema 的 input，这是「负债」的真实口径）。
- **降级**：超预算时不回退模型/工具，只收口为 ``token_budget`` 终态，
  保留已发生的事实（mutation/验证证据），避免「悄悄降级导致静默丢事」。

用法
----
    gate = TokenBudgetGate(budget, task_id)
    for each provider call:
        gate.note(input_tokens, output_tokens)
        # note 内部在超预算时直接抛 BudgetExceeded
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from runtime.errors import BudgetExceeded
from runtime.task import Task, TaskUsage

_logger = logging.getLogger("runtime.token_gate")


def resolve_token_budget() -> int:
    """读 env 拿单 Run token 预算。0 表示关闭成本闸门。

    优先级：``FORGE_TOKEN_BUDGET`` > 默认 50_000。负数按 0 处理。
    """
    raw = (os.getenv("FORGE_TOKEN_BUDGET", "50000") or "").strip()
    try:
        v = int(raw)
    except ValueError:
        v = 50_000
    return max(0, v)


@dataclass
class TokenBudgetGate:
    """挂在 Run 上的 token 累计记账器（超预算即抛）。"""

    budget: int            # 单 Run 总 token 预算；0 = 关闭
    task_id: str
    usage: TaskUsage = field(default_factory=TaskUsage)

    # ---- 记账 ----
    def note(self, input_tokens: int, output_tokens: int) -> None:
        """每次 provider 调用后记账；超预算立即抛（不吞）。"""
        if self.budget <= 0:
            return
        self.usage.input_tokens += int(input_tokens or 0)
        self.usage.output_tokens += int(output_tokens or 0)
        total = self.usage.tokens_total()
        if total > self.budget:
            # 超预算：抛 BudgetExceeded；Runner 错误路径收口为 token_budget 终态
            raise BudgetExceeded(
                f"任务 {self.task_id} 累计 token {total} 超过预算 "
                f"{self.budget}（input={self.usage.input_tokens} "
                f"output={self.usage.output_tokens}）；"
                f"已发生的 mutation/验证证据保留，Run 收口为 token_budget。"
            )

    # ---- 便捷 ----
    def tokens_total(self) -> int:
        return self.usage.tokens_total()

    def remaining(self) -> int:
        return max(0, self.budget - self.tokens_total())

    def is_open(self) -> bool:
        return self.budget > 0


def attach_to_task(task: Task, budget: int | None = None) -> TokenBudgetGate:
    """建一个与 task 绑定的 TokenBudgetGate（budget=None 时读 env 默认）。

    返回 (gate, usage_dict)，调用方需把 usage_dict 存到 task.metadata["token_usage"]，
    内循环在每次 provider 调用后读 usage_dict 累计并调 gate.note(...)。
    """
    if budget is None:
        budget = resolve_token_budget()
    gate = TokenBudgetGate(budget=budget, task_id=task.id, usage=task.usage)
    usage_dict = {"input": 0, "output": 0, "note_count": 0}
    task.metadata["token_budget"] = budget
    task.metadata["token_budget_gate"] = "open" if gate.is_open() else "closed"
    task.metadata["token_usage"] = usage_dict
    return gate, usage_dict
