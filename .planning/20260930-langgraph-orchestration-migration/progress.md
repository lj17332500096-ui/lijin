# Progress — LangGraph 对话编排迁移

## 2026-09-30

- Confirmed production entry and current responsibilities from `runtime/runner.py`, `runtime/task_plan.py`, and `runtime/runctx.py`.
- Confirmed the offline `runtime/orchestrator.py` is not used by the production TUI path.
- Confirmed LangGraph is absent from the project virtualenv; current model execution uses `openai-agents`.
- No live external calls or test suite runs performed.
- Added `langgraph>=1.0,<2` and installed LangGraph 1.2.12 into the project virtualenv.
- Added an async StateGraph with analysis, plan, execute, review, and a single supplement branch.
- Replaced the inline production analysis/review progression in `AgentRuntime.run_turn()` with graph invocation; Runtime still owns the broker, execution gates, approval handling, evidence, and terminalization.
- Added a sequential page-extraction phase for current/listing-style external research and Runtime-filtered supplement-phase extension.
- Added scalar-to-singleton normalization for known structured list fields such as `completion_criteria`.
- Added architecture notes to `runtime/README.md`.
- Updated `runtime/task_plan.py` so current/listing research plans include a sequential page-extraction stage when the routed schema contains an extraction tool.
- Added Runtime-only plan extension for the one supplement branch, and restore of supplement intent from task events when resuming a Run.
- Hardened structured API parsing for scalar strings returned in known list fields.
- Static checks passed: Python compilation for changed Runtime modules, LangGraph imports, and `git diff --check`.
- Did not run automated tests or live provider/MCP requests; production-path behavior remains dynamically unverified.

## Current phase

Phase 2: establish the graph module and callback/state contract.
