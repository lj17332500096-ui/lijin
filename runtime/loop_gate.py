"""循环界限闸（LoopGate）—— 七阶段架构的「循环有界限」硬约束。

职责：
- 对「LLM 比对 → 再派发回 Laya」的回环施加硬上限，防止空转失控。
- 双闸：max_loops（次数）+ budget（token/墙钟）。
- 超限后返回 LoopExhausted，编排器据此走 LOOP_EXHAUSTED 收口。

设计纪律：
- 纯函数式、无 IO、无模型依赖，可离线单测。
- 不 import runner / completion，避免循环依赖；由编排器在收口时调用
  completion 的 LOOP_EXHAUSTED verdict。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class LoopVerdict:
    """循环闸判定结果。"""

    can_continue: bool
    reason: str = ""
    loop_count: int = 0
    max_loops: int = 0
    spent_tokens: int = 0
    token_budget: int = 0

    @property
    def exhausted(self) -> bool:
        return not self.can_continue


class LoopGate:
    """循环界限闸。"""

    def __init__(
        self,
        max_loops: Optional[int] = None,
        token_budget: Optional[int] = None,
        wall_budget_sec: Optional[float] = None,
    ):
        self.max_loops = max_loops if max_loops is not None else _env_int("FORGE_LOOP_MAX", 3)
        self.token_budget = token_budget if token_budget is not None else _env_int("FORGE_LOOP_BUDGET", 20000)
        self.wall_budget_sec = wall_budget_sec if wall_budget_sec is not None else _env_float("FORGE_LOOP_WALL_SEC", 300.0)

    def can_continue(
        self,
        loop_count: int,
        spent_tokens: int = 0,
        elapsed_sec: float = 0.0,
    ) -> LoopVerdict:
        """判定能否再派发一轮。

        任一闸超限即收口：次数 / token / 墙钟。
        """
        if loop_count >= self.max_loops:
            return LoopVerdict(
                can_continue=False,
                reason="max_loops",
                loop_count=loop_count,
                max_loops=self.max_loops,
                spent_tokens=spent_tokens,
                token_budget=self.token_budget,
            )
        if spent_tokens >= self.token_budget:
            return LoopVerdict(
                can_continue=False,
                reason="token_budget",
                loop_count=loop_count,
                max_loops=self.max_loops,
                spent_tokens=spent_tokens,
                token_budget=self.token_budget,
            )
        if elapsed_sec >= self.wall_budget_sec:
            return LoopVerdict(
                can_continue=False,
                reason="wall_clock",
                loop_count=loop_count,
                max_loops=self.max_loops,
                spent_tokens=spent_tokens,
                token_budget=self.token_budget,
            )
        return LoopVerdict(
            can_continue=True,
            reason="ok",
            loop_count=loop_count,
            max_loops=self.max_loops,
            spent_tokens=spent_tokens,
            token_budget=self.token_budget,
        )
