# Runtime package

This package contains Agent execution lifecycle, routing and policy boundaries, run/task state, evidence, persistence, and runtime observability.

## Keep here

- Run entry/orchestration components and state transitions.
- `conversation_workflow.py` uses LangGraph to orchestrate API analysis, Runtime plan refresh, one model/tool execution cycle, API review, and at most one supplement. Its callbacks are supplied by `AgentRuntime`.
- Tool registry, broker, router, approvals, execution evidence, persistence, and tracing adapters.
- Small runtime-owned helpers that have no independent product or external-provider boundary.
- `observability.py` and `trace_export.py` for OTel capture and Run trace export.
- `resilience.py` for shared retry, concurrency, and idempotency helpers used by the Runtime and integrations.
- `guardrails.py` for deterministic input/output safety and reply-shape checks.
- `compact.py` for session history reduction and summary state snapshots; generated summary files go under the runtime root.

## Keep elsewhere

- External-provider adapters belong in `integrations/`.
- CLI, web server, and application entrypoints remain at their current boundaries until their own move plan.
- Reusable benchmark cases and generated output belong in `benchmark/` and `var/`, respectively.

Mutable output paths must come from `runtime_paths`; moving a module must not move the project root or runtime data location.

LangGraph owns workflow transitions only. It does not authorize tools or write
execution evidence. All tool calls continue through the Runtime broker and
security gates; approvals, side-effect records, Run state, and terminal
decisions remain Runtime-owned. The graph is currently invocation-scoped; the
durable Run ledger remains the source of truth for recovery and audit.
