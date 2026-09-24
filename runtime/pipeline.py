"""编排器接线层（P4）—— 把七阶段分层架构串成可调用入口。

默认**影子模式**（FORGE_ORCHESTRATOR=off）：不改变 run_turn 主链语义，
仅提供 `run_pipeline(query)` 供显式调用/评测/未来接入。

七阶段：
  ① 用户对话 → ② Laya 判别 → ③ 编排决策 decide() → ④ LLM 补全（need_llm 时）
  → ⑤ 工具执行 → ⑥ LLM 比对（三向裁决）→ ⑦ 记忆层（run_plan 过程记忆）

本模块只做「纯编排接线」，不 import 工具执行细节，工具执行由调用方注入回调。
这样可独立单测七阶段流程，且不破坏既有 runner 主链。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from runtime.laya_router import LayaDecision, laya_fast_screen, laya_router
from runtime.orchestrator import decide, Step
from runtime.loop_gate import LoopGate
from runtime import memory_layers


def orchestrator_enabled() -> bool:
    return os.getenv("FORGE_ORCHESTRATOR", "").strip().lower() in ("on", "true", "1")


@dataclass
class PipelineOutcome:
    """七阶段一次调用的最终结果。"""

    step: Step
    laya: LayaDecision
    loop_count: int
    verdict: str          # completed / redispatched / exhausted / fallback
    result: Any = None
    run_id: str = ""


def run_pipeline(
    query: str,
    run_id: str,
    *,
    tool_executor: Optional[Callable[[str, dict], Any]] = None,
    llm_completer: Optional[Callable[[str, str], dict]] = None,
    llm_comparer: Optional[Callable[[str, Any], str]] = None,
) -> PipelineOutcome:
    """执行七阶段流水线（供评测/显式接入；影子模式下不主动走此路径）。

    tool_executor(tool_name, args) -> result：工具执行回调（由调用方注入）
    llm_completer(query, tool) -> {"tool":..., "args":...}：LLM 补全回调
    llm_comparer(query, result) -> "done" | "redispatched" | "partial"：比对回调
    """
    gate = LoopGate()
    loop_count = 0

    while True:
        # ② Laya 判别
        fast = laya_fast_screen(query)
        decision = laya_router().classify(query) if orchestrator_enabled() else LayaDecision.fallback()

        # ③ 编排决策
        step = decide(
            decision.intent,
            decision.intent_conf,
            decision.completeness,
            decision.route,
            decision.route_conf,
        )
        # fast 二分类与三判别 intent 对齐：fast=direct_text 时也归 direct_text
        if step.kind == "llm_fallback" and fast == "direct_text":
            step = Step("direct_text")

        # 记录过程记忆
        memory_layers.record_step(
            run_id, loop_count + 1, step.kind,
            tool=step.tool, result_summary=None, loop_count=loop_count,
        )

        # ④/⑤ 执行
        if step.kind == "direct_text":
            return PipelineOutcome(step, decision, loop_count, "completed", result=None, run_id=run_id)

        if step.kind == "llm_fallback":
            return PipelineOutcome(step, decision, loop_count, "fallback", result=None, run_id=run_id)

        tool = step.tool
        args = step.args or {}

        # need_llm：先补全
        if step.kind == "need_llm" and llm_completer is not None:
            filled = llm_completer(query, tool or "")
            tool = filled.get("tool", tool)
            args = filled.get("args", args or {})

        # 执行工具
        result = None
        if tool_executor is not None and tool:
            result = tool_executor(tool, args)

        # ⑥ 比对
        verdict = "completed"
        if llm_comparer is not None and result is not None:
            verdict = llm_comparer(query, result)

        if verdict in ("done", "completed"):
            return PipelineOutcome(step, decision, loop_count, "completed", result=result, run_id=run_id)
        if verdict == "partial":
            return PipelineOutcome(step, decision, loop_count, "completed", result=result, run_id=run_id)

        # redispatched：回 Laya 再工作，但受循环闸限制
        loop_count += 1
        v = gate.can_continue(loop_count)
        if v.exhausted:
            return PipelineOutcome(step, decision, loop_count, "exhausted", result=result, run_id=run_id)
