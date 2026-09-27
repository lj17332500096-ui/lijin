# Task Plan and Tool Router Boundary

## Ownership

- `runtime.task_plan` recognizes explicit actions, orders phases by the user's
  request, binds each phase to registered tool candidates, and records phase
  dependencies.
- `RunContext` owns per-Run plan progress. Runner enforces phase order,
  uncertainty recovery, and completion requirements.
- `runtime.tool_router.select_tool_names()` selects candidate tools from the
  Runtime registry. It may receive `plan_required_tools`, but it does not infer
  phases or task dependencies. It applies availability, intent, and tool-count
  policy to those candidates.
- The ordinary execution wrapper, action policy, approval policy, and
  Completion Gate remain authoritative for actual execution and completion.

## Data flow

```text
User request
    → Runtime builds TaskPlan from request + registry snapshot
    → Tool Router selects candidates and preserves eligible plan-required tools
    → RunContext tracks the current phase for this Run
    → Runner gates tool calls and phase transitions
    → Completion Gate checks the final evidence and unfinished phases
```

The TaskPlan schema remains version 1 and retains `required_tools`,
`required_capabilities`, `phases`, and `depends_on`. This keeps existing Run
trace and recovery readers compatible while relocating inference ownership.

`runtime.tool_router.infer_capability_plan()` remains as a lazy compatibility
shim for old callers. New Runtime code must import `infer_task_plan()` from
`runtime.task_plan`.
