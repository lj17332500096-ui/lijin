# Run Outcome Contract

Version: 1

## Purpose

`TaskState` describes the Run lifecycle. `RunOutcome` describes what the latest
execution achieved. They are separate fields so a resumable wait state is not
mistaken for successful completion, and a failed Run with a committed side
effect is not described as if nothing happened.

## Lifecycle and outcome mapping

| Durable `TaskState` | `RunOutcome` | `next_action` | Meaning |
|---|---|---|---|
| `completed` | `completed` | — | Completion Gate accepted the result. |
| `waiting_user` | `needs_user` | `answer_questions` | The Run can resume after the user supplies required information. |
| `waiting_approval` | `blocked` | `approve` | The Run can resume after the pending action is approved. |
| `failed` with a committed or uncertain side effect | `partial` | `review_effects` | Some effects may already exist; do not imply rollback. |
| `failed` after refusal/no-progress with no side effect | `blocked` | — | Runtime or policy stopped execution before an effect was committed. |
| other `failed` | `failed` | — | Execution failed without evidence of a committed or uncertain side effect. |
| `cancelled` | `cancelled` | `review_effects` when effects may exist | Cancellation is distinct from failure; completed effects remain. |

`RunOutcome` values are `completed`, `needs_user`, `partial`, `blocked`,
`failed`, and `cancelled`. `cancelled` is retained as an explicit outcome even
though the initial five-outcome proposal omitted it.

`RunNextAction` values are `answer_questions`, `approve`, `review_effects`, and
`resume`. The action is optional. A caller must continue to use `TaskState` to
decide whether a Run is resumable; `RunOutcome` alone is not a lifecycle state.

## Persistence and API contract

- `RunResult.outcome` and `RunResult.next_action` are the in-process result
  contract. `RunResult.ok` remains for compatibility and is not authoritative
  for task status.
- New `run.terminal` events include `contract_version: 1`, `outcome`, and
  `next_action`, while retaining the existing `state`, `kind`, and evidence
  fields. Existing event readers that only use `state` or `kind` remain valid.
- `partial` is derived from ToolSpec side-effect evidence, durable file or
  persistence evidence, or an uncertain side-effect invocation. Read-only tool
  calls alone do not make a failed Run partial.
- The Task database state enum is unchanged; this contract requires no schema
  migration. Historical terminal events without `outcome` remain readable and
  may be interpreted from their existing state/kind fields.

## Consumer behavior

- TUI renders the outcome label instead of always displaying `DONE`.
- CLI emits a status label for non-completed outcomes before rendering the
  assistant response.
- Schedulers treat any outcome other than `completed` as a non-success result.
- Approval and clarification flows continue to use their existing resumable
  `TaskState` and flow-specific data.
