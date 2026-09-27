# Risk approval strategy findings

## Current code facts
- `runtime/spec.py` defines `ToolSpec.risk` (`low/medium/high`), `side_effect`, `destructive`, `idempotent`; unknown tools default conservatively to medium risk, side-effecting, and non-idempotent.
- `runtime/approval.py` derives most default gated tools from side-effect metadata, then subtracts `SIDE_EFFECT_EXEMPT`; executable tools and real-file edit tools are explicit gated groups.
- `APPROVAL=off` disables `ApprovalGate.should_gate` globally. Existing audit/acceptance documents confirm this is the user's selected persistent configuration and MCP `approval` labels are ineffective under the global off switch.
- MCP policy/candidate metadata is assembled in `runtime/tool_router.py`; it carries provider policy (`allow/approval/deny`) separately from ApprovalGate, so effective behavior needs an explicit composition rule.
- ApprovalGate binds decisions to run, tool, and redacted argument key; scheduled/daemon channels auto-deny; trusted code roots bypass approval for code execution tools.
- Approval is distinct from intent/readiness, file/network scope, side-effect WAL, and cancellation handling.

## Risks / design constraints to address
- Avoid treating `risk` and `side_effect` as interchangeable; destructive impact, sensitivity, external recipient, reversibility, and uncertainty need explicit dimensions or policy modifiers.
- Make MCP provider policy and tool-specific approval policy compose monotonically: deny wins; approval cannot be canceled by unrelated `APPROVAL=off` without an explicit documented override mode.
- Eliminate unbounded exemptions; every bypass needs scope, owner, reason, expiry/version, and auditable decision record.
- Keep routine user-directed internal artifact creation low-friction while requiring approval for external, destructive, financial, credential, broad code-execution, or irreversible actions.
- Approval must bind to immutable invocation details and expire on argument/scope changes; approval is not authorization for a different target or later invocation.

## Additional code-grounded findings
- Tool wrapper ordering is intent/readiness/pre-approval-completion guard, then ApprovalGate, then FileScope and side-effect WAL; risk policy should run before approval but after argument/schema normalization, and FileScope must remain an independent hard boundary.
- `classify_tool()` uses MCP `allow` as discovery-safe and `approval` as side-effecting for readiness. That is not the same as an approval decision: the `_mcp_policy` metadata must be composed by the new policy owner with ToolSpec and channel facts.
- `ApprovalGate.check()` currently bypasses all gates when `APPROVAL=off`, and also bypasses run_python/code_loop/run_tests under `FORGE_TRUSTED_CODE_ROOTS`. Both bypasses need explicit documented scope and must not suppress hard deny rules.
- Current `APPROVAL_GATED_TOOLS` supports exact names/prefixes/all; the proposal should phase this into compatibility input while avoiding it as the primary risk authority.
- Concrete risk examples from ToolSpec: `web_search/get_weather` read-only; save_note and Office artifact creation are internal writes; `write_project_file` is workspace mutation; `run_python/code_loop` execute code; forget_memory/sandbox_rollback/schedule_remove are destructive; MCP tools may be unknown and get conservative side-effect defaults.
- Existing global config is deliberately `APPROVAL=off`; preserving it is a constraint for this design. No persistent config was edited.
