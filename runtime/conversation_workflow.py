"""LangGraph orchestration for one conversation Run.

This graph owns only workflow transitions. Handlers are supplied by
``AgentRuntime`` and remain responsible for Runtime policy, tool execution,
evidence, persistence, approvals, and terminalization.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class ConversationState(TypedDict, total=False):
    request: str
    analysis: dict[str, Any]
    tool_selection: dict[str, Any]
    plan: dict[str, Any]
    execution: dict[str, Any]
    review: dict[str, Any]
    supplement_count: int
    review_passes: int
    execution_attempts: int
    completion_repairs: int
    obligation_feedback_signatures: list[str]
    retry_instruction: str
    supplement_plan_applied: bool
    terminal: dict[str, Any]


NodeHandler = Callable[[ConversationState], Awaitable[Any]]
MAX_EXECUTION_ATTEMPTS = 6


def _handler_update(result: Any, key: str) -> dict[str, Any]:
    if isinstance(result, Mapping):
        payload = dict(result)
        terminal = payload.pop("terminal", None)
        update = {key: payload}
        if terminal is not None:
            update["terminal"] = dict(terminal)
        return update
    return {"terminal": {"runtime_result": result}}


def build_conversation_workflow(
    *,
    analyze: NodeHandler,
    select_tools: NodeHandler | None = None,
    plan: NodeHandler,
    execute: NodeHandler,
    review: NodeHandler,
    retry_exhausted: NodeHandler,
) -> Any:
    """Build the bounded analyze/plan/execute/review graph.

    The graph owns candidate tool selection, bounded execution retry
    transitions, and the single review supplement. A supplement re-runs tool
    selection against the full registered catalog before Runtime extends its
    ordered capability plan.
    ``execute`` and ``review`` return ``{"terminal": ...}`` to stop the graph
    for Runtime-owned outcomes such as approval pause or user clarification.
    A Runtime completion rejection returns a typed retry request; the graph
    applies the retry budget and asks Runtime to terminalize exhaustion.
    """

    async def analyze_node(state: ConversationState) -> dict[str, Any]:
        return _handler_update(await analyze(state), "analysis")

    async def select_tools_node(state: ConversationState) -> dict[str, Any]:
        if select_tools is None:
            return {"tool_selection": {}}
        return _handler_update(await select_tools(state), "tool_selection")

    async def plan_node(state: ConversationState) -> dict[str, Any]:
        update = _handler_update(await plan(state), "plan")
        if int(state.get("supplement_count", 0)) > 0:
            update["supplement_plan_applied"] = True
        return update

    async def execute_node(state: ConversationState) -> dict[str, Any]:
        update = _handler_update(await execute(state), "execution")
        update["execution_attempts"] = int(state.get("execution_attempts", 0)) + 1
        return update

    async def review_node(state: ConversationState) -> dict[str, Any]:
        passes = int(state.get("review_passes", 0)) + 1
        update = _handler_update(await review(state), "review")
        update["review_passes"] = passes
        return update

    def route_execution(
        state: ConversationState,
    ) -> Literal["review", "retry", "exhausted", "end"]:
        if state.get("terminal") is not None:
            return "end"
        retry = (state.get("execution") or {}).get("retry")
        if isinstance(retry, dict):
            kind = str(retry.get("kind") or "")
            if int(state.get("execution_attempts", 0)) >= MAX_EXECUTION_ATTEMPTS:
                return "exhausted"
            if kind == "completion_repair":
                return "retry" if int(state.get("completion_repairs", 0)) < 1 else "exhausted"
            if kind == "obligation_feedback":
                signature = str(retry.get("signature") or "")
                seen = list(state.get("obligation_feedback_signatures") or [])
                return "retry" if signature and signature not in seen else "exhausted"
            return "exhausted"
        return "review"

    async def retry_node(state: ConversationState) -> dict[str, Any]:
        retry = (state.get("execution") or {}).get("retry") or {}
        kind = str(retry.get("kind") or "")
        update: dict[str, Any] = {
            "retry_instruction": str(retry.get("prompt") or "")[:6000],
        }
        if kind == "completion_repair":
            update["completion_repairs"] = int(state.get("completion_repairs", 0)) + 1
        elif kind == "obligation_feedback":
            update["obligation_feedback_signatures"] = (
                list(state.get("obligation_feedback_signatures") or [])
                + [str(retry.get("signature") or "")]
            )[-12:]
        return update

    async def retry_exhausted_node(state: ConversationState) -> dict[str, Any]:
        return _handler_update(await retry_exhausted(state), "execution")

    def route_continue(state: ConversationState) -> Literal["next", "end"]:
        return "end" if state.get("terminal") is not None else "next"

    def route_plan(state: ConversationState) -> Literal["execute", "exhausted", "end"]:
        if state.get("terminal") is not None:
            return "end"
        if int(state.get("execution_attempts", 0)) >= MAX_EXECUTION_ATTEMPTS:
            return "exhausted"
        return "execute"

    def route_after_selection(state: ConversationState) -> Literal["plan", "end"]:
        return "end" if state.get("terminal") is not None else "plan"

    def route_review(state: ConversationState) -> Literal["plan", "end"]:
        if state.get("terminal") is not None:
            return "end"
        result = state.get("review") or {}
        if (result.get("verdict") == "supplement"
                and int(state.get("supplement_count", 0)) == 0
                and str(result.get("supplement_prompt") or "").strip()):
            return "plan"
        return "end"

    async def supplement_node(state: ConversationState) -> dict[str, Any]:
        review_result = state.get("review") or {}
        # The count is advanced in graph state before the next plan/execution;
        # no route can schedule a second supplement.
        return {
            "supplement_count": 1,
            "review": {},
            "analysis": {
                **(state.get("analysis") or {}),
                "supplement_prompt": str(review_result.get("supplement_prompt") or "")[:3000],
                "supplement_gaps": [str(gap)[:300] for gap in
                                     list(review_result.get("gaps") or [])[:12]],
                "supplement_requires_tools": bool(
                    review_result.get("supplement_requires_tools", False)
                ),
            },
        }

    graph = StateGraph(ConversationState)
    graph.add_node("analyze", analyze_node)
    graph.add_node("select_tools", select_tools_node)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("review", review_node)
    graph.add_node("supplement", supplement_node)
    graph.add_node("retry", retry_node)
    graph.add_node("retry_exhausted", retry_exhausted_node)

    graph.add_edge(START, "analyze")
    graph.add_conditional_edges(
        "analyze", route_continue, {"next": "select_tools", "end": END},
    )
    graph.add_conditional_edges(
        "select_tools", route_after_selection, {"plan": "plan", "end": END},
    )
    graph.add_conditional_edges(
        "plan", route_plan,
        {"execute": "execute", "exhausted": "retry_exhausted", "end": END},
    )
    graph.add_conditional_edges(
        "execute", route_execution,
        {"review": "review", "retry": "retry", "exhausted": "retry_exhausted", "end": END},
    )
    graph.add_edge("retry", "plan")
    graph.add_edge("retry_exhausted", END)
    graph.add_conditional_edges(
        "review", route_review, {"plan": "supplement", "end": END},
    )
    graph.add_edge("supplement", "select_tools")
    return graph.compile()


def initial_state(request: str) -> ConversationState:
    return {
        "request": str(request),
        "analysis": {},
        "tool_selection": {},
        "plan": {},
        "execution": {},
        "review": {},
        "supplement_count": 0,
        "review_passes": 0,
        "execution_attempts": 0,
        "completion_repairs": 0,
        "obligation_feedback_signatures": [],
        "retry_instruction": "",
        "supplement_plan_applied": False,
    }
