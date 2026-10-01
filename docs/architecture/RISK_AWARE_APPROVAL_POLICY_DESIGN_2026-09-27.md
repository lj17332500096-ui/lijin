# Risk-Aware Approval Strategy for FORGE

**Status:** Design proposal for review
**Scope:** Runtime tool execution, native tools, MCP/plugin tools, approval and unattended channels
**Code baseline:** `runtime/spec.py`, `runtime/approval.py`, `runtime/runner.py`, `runtime/readiness_gate.py`
**Configuration constraint:** Preserve the current user-selected `APPROVAL=off`; this proposal does not change `.env` or runtime behavior.

## 1. Goal

Make the agent choose the least disruptive safe outcome for each proposed tool call. The policy must distinguish ordinary reads, user-directed internal edits, code execution, external actions, and destructive actions. The API workflow and LangGraph selector may supply evidence or candidate tools, but neither grants permission.

The policy must produce one deterministic decision for each invocation and make that decision explainable in the Run trace.

## 2. Current design and gaps

The current implementation has strong ingredients but distributes authority:

- `ToolSpec` describes `risk`, `side_effect`, `destructive`, and `idempotent`. Unknown tools conservatively default to medium risk, side-effecting, and non-idempotent.
- `ApprovalGate` derives many gated tools from `side_effect`, but removes named exemptions. It also has explicit executable/real-file groups, env overrides, MCP runtime registration, a global `APPROVAL` switch, and a trusted-code-root bypass.
- MCP policy is attached to runtime tool objects. Readiness treats `allow` as discovery-safe and `approval` as side-effecting, while the approval decision is still made elsewhere.
- `APPROVAL=off` currently means `ApprovalGate.should_gate()` returns false for every tool. Consequently, an MCP tool labeled `approval` does not actually wait for approval.
- `ApprovalGate.check()` currently embeds a truncated JSON rendering of raw arguments in the block message. The approval UI/message path should redact secrets and sensitive personal values before displaying action details.
- Scheduled/daemon calls are auto-denied by ApprovalGate. Trusted code-root configuration can bypass interactive approval for code execution tools.

The main gap is not a missing prompt. It is the lack of one policy decision that composes tool metadata, target and data sensitivity, user intent, provider policy, channel, and explicit grants.

## 3. Responsibility boundaries

| Component | Owns | Must not own |
|---|---|---|
| Tool Registry / `ToolSpec` | Stable capability and impact facts: effect, destructiveness, reversibility, data class, execution surface, idempotency | Whether a particular Run is authorized |
| Intent / Readiness | Whether the request is sufficiently explicit and whether prerequisites are known | Approval, permission, or security bypass |
| Risk Policy | Deterministic `allow`, `require_approval`, `ask_user`, or `deny` decision and rationale | Executing tools or changing Run state |
| Approval Store / Gate | Recording and checking a user decision bound to one invocation | Reclassifying risk or widening the approved target |
| Tool wrapper | Enforcing the policy decision and independent FileScope/network/credential/WAL gates | Inferring authorization from model confidence or tool visibility |
| API workflow / LangGraph | Suggesting intent and candidate tools | Approving a side effect |
| TaskManager / ExecutionEvidence | Durable decision and execution facts | Reinterpreting policy after execution |

## 4. Risk facts

Keep `side_effect` as an execution-effect fact, not a synonym for risk. Extend metadata incrementally with these independent fields:

- `effect`: `none`, `internal_write`, `external_write`, `code_execution`.
- `destructive`: whether existing state can be removed, overwritten, or made unavailable.
- `reversibility`: `easy`, `recoverable`, `hard`, `irreversible`.
- `data_access`: `public`, `workspace`, `personal`, `credential`, `regulated`.
- `execution_surface`: `pure`, `sandbox`, `host`, `remote_service`.
- `idempotency`: `idempotent`, `keyed`, `non_idempotent`, `unknown`.
- `target_scope`: constrained by the invocation and independent FileScope/provider allowlists.

Keep the existing `risk=low|medium|high` as a derived human-readable summary during migration. Policy decisions should use the structured facts; no critical gate should depend on the summary label alone.

## 5. Decision contract

For each invocation, the Risk Policy returns exactly one of:

- `ALLOW`: execute if all independent execution gates also pass.
- `REQUIRE_APPROVAL`: create a pending approval bound to the invocation; suspend the Run.
- `ASK_USER`: required target, identity, scope, or intent is ambiguous. Do not create an approval for an underspecified action.
- `DENY`: policy prohibits this invocation. Do not offer an approval button as a way around the prohibition.

Suggested decision record:

```json
{
  "contract_version": 1,
  "decision_id": "...",
  "run_id": "...",
  "invocation_id": "...",
  "tool": "...",
  "source": "native|mcp|plugin",
  "provider_policy": "allow|approval|deny|unknown",
  "risk_class": "R0|R1|R2|R3|R4",
  "decision": "allow|require_approval|ask_user|deny",
  "reason_codes": ["explicit_user_intent", "external_effect"],
  "policy_version": "...",
  "target_digest": "...",
  "approval_id": null
}
```

Persist a `risk.policy_decision` event before the tool can run. Do not copy raw arguments into this event; use the existing redaction path and a stable digest for correlation.

## 6. Risk classes and default behavior

| Class | Typical operation | Interactive chat | Unattended scheduled/daemon |
|---|---|---|---|
| **R0: read-only** | Calculator, weather, public search, scoped workspace reads | Allow when target/readiness gates pass | Allow within configured source and budget |
| **R1: internal reversible write** | Create a requested note/document, write to an explicitly scoped project file, snapshot | Allow when the user explicitly requested the write and destination is in scope; otherwise ask | Allow only for pre-authorized output paths/task templates; otherwise deny |
| **R2: external bounded action** | Create an issue, upload a file, send a message, publish a comment, change a remote setting | Require approval for sending/publishing or external state changes; ask first if recipient/target is unclear | Deny by default; an explicit signed schedule grant may allow a narrow action |
| **R3: destructive or hard-to-reverse** | Delete/overwrite remote data, remove schedules/memory, rollback, broad database mutation | Require approval after showing exact target and consequence; destructive classes may be configured as non-overridable deny | Deny |
| **R4: privileged execution / sensitive access** | Host code execution, arbitrary browser evaluation, credential or regulated data access, financial/legal commitment | Require approval and enforce a constrained execution surface; certain actions (credential disclosure, unauthorized access) are hard deny | Deny unless a separately provisioned, tightly scoped machine policy explicitly grants it |

Examples are defaults, not name rules. Each registered tool must carry metadata and parameter-level target constraints. New or unknown tools default to `R4/unknown effect`, are unavailable for execution until classified, or require an explicit administrator policy entry. They must not silently inherit an ordinary read classification from their name.

### Internal artifact writes

Routine, user-requested writes to the agent's dedicated notes/exports or scoped code workspace can be `R1` and need not generate an approval prompt. The write still requires clear user intent, valid destination scope, durable invocation journaling, and evidence. Editing arbitrary project files is also `R1` only when the user explicitly asked for those edits and FileScope confirms the target; otherwise ask or require approval according to the project policy.

## 7. Policy precedence

Compose policy monotonically in this order; a lower layer cannot weaken a higher layer:

1. **Hard deny:** prohibited tool, provider, data class, destination, or action. This is not approval-overridable.
2. **Schema and target validation:** invalid/missing target becomes `ASK_USER` or `DENY`; no side effect runs.
3. **Provider policy:** MCP/plugin `deny` is a hard deny. `approval` is a minimum approval requirement for a side-effecting operation. `allow` means the provider permits connection/use; it does not override local risk rules or turn a writer into a read-only tool.
4. **Risk policy:** select the minimum action class using ToolSpec facts, validated parameters, explicit user intent, and channel.
5. **Explicit scoped grant:** may lower an approval requirement only for the exact tool, target, operation, data class, channel, expiry, and policy version it names. It cannot override hard deny, FileScope, network policy, or OS sandbox restrictions.
6. **Approval mode:** controls whether eligible approval-required actions prompt or are handled according to the selected mode. It never changes the underlying risk classification or hard-deny decision.
7. **Independent execution gates:** FileScope, network policy, authorization, budget, side-effect WAL, timeout, and cancellation remain mandatory after an `ALLOW` decision.

For MCP tools, effective policy is the stricter of provider policy and local policy. `deny` wins; `approval` cannot be downgraded by a provider `allow`; provider `allow` cannot bypass a local approval rule.

## 8. Meaning of `APPROVAL=off`

Keep the existing user setting unchanged. In the proposed design, separate the recorded policy decision from the interaction mode:

- Risk Policy still calculates and records `REQUIRE_APPROVAL` for the invocation.
- `APPROVAL=off` means no interactive approval prompt is created. For R0/R1 and explicitly trusted R2 operations, the policy may permit execution under an explicit off-mode profile.
- R3/R4 operations that are not safe under that profile become `DENY` or `ASK_USER`; they must not silently become `ALLOW` merely because the prompt mechanism is off.
- Hard-deny rules always remain active.

This gives the user a no-prompt mode without making the setting an implicit bypass of every independent safety boundary. Adopting this semantic change would require a separate implementation and migration decision; this design task does not change current behavior. Until implemented, document that the current `APPROVAL=off` bypasses all ApprovalGate checks.

## 9. Approval binding and lifecycle

An approval is a one-invocation grant, not general trust:

- Bind it to `run_id`, `invocation_id`, tool/provider identity, normalized argument digest, exact target, risk-policy version, and expiry.
- Show the action, target/recipient, data leaving the workspace, irreversible consequences, and whether it will execute code or contact an external service.
- Redact credentials, tokens, personal identifiers, and sensitive cell/file values from the approval card; show only the minimum detail needed to make the decision.
- Any change in target, recipient, scope, tool version, relevant arguments, or policy version invalidates the approval and requires a new decision.
- Approval consumption is atomic and at most once. Resume continues the same invocation identity; it must not create a fresh invocation to reuse a broad approval.
- Denied/expired approvals remain auditable and cannot be converted to allow by retrying with altered arguments.
- If a tool outcome is uncertain after the approval was consumed, do not replay automatically. Show `unknown` execution evidence and ask the user to inspect/resolve.
- Approval TTL is a maximum validity boundary; sensitive/high-impact actions may use a shorter class-specific TTL.

## 10. Exceptions and trust grants

Replace informal bypass lists over time with a versioned policy manifest. Every exception needs:

- owner and human-readable reason;
- exact tool and provider;
- allowed operation and target/path/recipient scope;
- allowed channel and execution surface;
- expiration or review date;
- maximum rate/budget and data classes;
- policy version and audit event.

`FORGE_TRUSTED_CODE_ROOTS` should not be treated as a broad approval bypass in production. A future grant should bind root identity, command/test target, runtime image, network mode, and write scope. Evaluation fixtures must be isolated from production grants. `SIDE_EFFECT_EXEMPT` should become a reviewed policy table with explicit rationale and tests, not an undocumented route around risk classification.

## 11. Rollout plan

1. **Inventory and shadow mode:** enrich ToolSpec and MCP metadata; compute decisions without changing execution. Log proposed class/reason and compare with current ApprovalGate behavior.
2. **Reconcile catalog:** classify every native, MCP, and plugin tool. Unknown tools remain fail-closed. Require owner, effect, destructive/reversibility, data access, idempotency, and provider mapping.
3. **Wire single policy owner:** have the wrapper request one decision before ApprovalGate/WAL; preserve FileScope and all execution constraints. LangGraph remains advisory; Runtime gates remain authoritative.
4. **Make approval invocation-bound:** migrate approval records with a compatibility path for existing pending rows; ensure resume and at-most-once behavior.
5. **Adopt explicit modes:** document `interactive`, `off-profile`, and `strict` semantics; preserve current `APPROVAL=off` until the user explicitly adopts new behavior.
6. **Enforce and observe:** compare effective policy in capability introspection, API/TUI approval cards, and run traces. Add mismatch alerts for catalog vs provider metadata.

## 12. Acceptance criteria for a later implementation

- Every exposed tool has source, effect, risk dimensions, idempotency, target scope, and policy owner.
- Every call yields exactly one durable policy decision before execution; blocked calls also have a reason code.
- MCP `deny` cannot execute; MCP `approval` cannot execute without the applicable approval/grant; MCP `allow` cannot bypass local policy.
- Ordinary direct conversation and explicit scoped internal writes do not cause repeated or unrelated approval prompts.
- Changing approved arguments or target invalidates the grant; resume executes at most once.
- Scheduled/daemon runs never wait forever for a human; disallowed actions are denied with an auditable reason.
- `APPROVAL=off` status is visible and its effective policy semantics are accurately described in TUI/API/introspection.
- Trusted roots and exemptions are scoped, expiring, and excluded from production unless explicitly provisioned.
- Hard deny, readiness, FileScope, provider allow/deny, ApprovalGate, WAL, cancellation, and uncertain-outcome cases are covered in isolated deterministic tests and production-path acceptance before enforcement rollout.

## 13. Decisions still needed before implementation

1. Should R3 operations be approvable, or should selected destructive classes be hard-deny only?
2. Which exact internal directories count as R1 output scopes, and may the user workspace be included?
3. Should off-mode automatically allow only R0/R1, or also selected R2 actions with an explicit target allowlist?
4. Should scheduled actions ever be approved in advance through signed grants, or remain deny-by-default for all R2+ operations?
5. Which MCP servers/tools may access personal or credential data, and what user-visible confirmation details are required?
