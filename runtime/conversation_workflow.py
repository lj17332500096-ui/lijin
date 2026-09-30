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
    plan: dict[str, Any]
    execution: dict[str, Any]
    review: dict[str, Any]
    supplement_count: int
    review_passes: int
    terminal: dict[str, Any]


NodeHandler = Callable[[ConversationState], Awaitable[Any]]


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
    plan: NodeHandler,
    execute: NodeHandler,
    review: NodeHandler,
) -> Any:
    """Build the bounded analyze/plan/execute/review graph.

    ``plan`` runs before every execution attempt. On the single supplement
    branch it receives the review gaps and may extend/rebuild the Runtime plan;
    it must return only Runtime-validated, already-routed tool candidates.
    ``execute`` and ``review`` return ``{"terminal": ...}`` to stop the graph
    for Runtime-owned outcomes such as approval pause, user clarification, or
    deterministic completion-gate failure.
    """

    async def analyze_node(state: ConversationState) -> dict[str, Any]:
        return _handler_update(await analyze(state), "analysis")

    async def plan_node(state: ConversationState) -> dict[str, Any]:
        return _handler_update(await plan(state), "plan")

    async def execute_node(state: ConversationState) -> dict[str, Any]:
        return _handler_update(await execute(state), "execution")

    async def review_node(state: ConversationState) -> dict[str, Any]:
        passes = int(state.get("review_passes", 0)) + 1
        update = _handler_update(await review(state), "review")
        update["review_passes"] = passes
        return update

    def route_execution(state: ConversationState) -> Literal["review", "end"]:
        if state.get("terminal") is not None:
            return "end"
        return "review"

    def route_continue(state: ConversationState) -> Literal["next", "end"]:
        return "end" if state.get("terminal") is not None else "next"

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
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("review", review_node)
    graph.add_node("supplement", supplement_node)

    graph.add_edge(START, "analyze")
    graph.add_conditional_edges(
        "analyze", route_continue, {"next": "plan", "end": END},
    )
    graph.add_conditional_edges(
        "plan", route_continue, {"next": "execute", "end": END},
    )
    graph.add_conditional_edges(
        "execute", route_execution, {"review": "review", "end": END},
    )
    graph.add_conditional_edges(
        "review", route_review, {"plan": "supplement", "end": END},
    )
    graph.add_edge("supplement", "plan")
    return graph.compile()


def initial_state(request: str) -> ConversationState:
    return {
        "request": str(request),
        "analysis": {},
        "plan": {},
        "execution": {},
        "review": {},
        "supplement_count": 0,
        "review_passes": 0,
    }
