# Findings — LangGraph 对话编排迁移

- Production TUI calls `AgentRuntime.run_turn()` directly; `runtime/orchestrator.py` is only imported by the offline `runtime/pipeline.py` path.
- `run_turn()` currently contains API analysis, an inline `for _repair in range(6)` model execution loop, result review, and one supplement `continue` branch.
- Ordered tool capability plan is currently inferred in `route_agent()` via `runtime.task_plan.infer_task_plan()` and copied into `RunContext`; tool gates in `runner.py` and `runctx.py` remain the actual execution authority.
- A recent run showed API schema validation failure when `completion_criteria` was a string rather than a list.
- The same run showed that the one-phase inferred plan became completed after `web_search`, so later extraction and the API-requested supplement were blocked. Graph integration must keep the active capability plan synchronized with any approved follow-up phase without bypassing tool policy.
- Project virtualenv has `openai-agents==0.22.0`; `langgraph`, `langchain-core`, and `langchain-openai` are not installed. Only `langgraph` is needed if LangGraph nodes wrap existing async Runtime callbacks.
- `requirements.txt` is the active dependency manifest; no `pyproject.toml` was found.
- Worktree was clean before this change.
- LangGraph now owns the production graph transitions. The graph is invocation-scoped and does not enable a LangGraph checkpointer; the existing Runtime event/Run ledger remains the persistence and audit source.
- The runtime graph has a shared six-attempt execution cap across its initial execution and one supplement branch, plus the existing one-supplement limit.
- Runtime plan refresh is constrained to tool names already present in the selected Agent tool schema; it does not bypass the broker, approval, or execution gates.
