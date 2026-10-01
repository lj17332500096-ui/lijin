# Laya Route Hint Contract

## Authority boundary

- Laya's binary `text_likely` / `tool_likely` classification is advisory trace
  data. It never returns an Agent with its tools cleared and never bypasses the
  Tool Router.
- `select_tool_names()` is authoritative for the tool candidates exposed to
  the model. Existing Runtime execution, permission, and completion gates remain
  authoritative for whether a candidate can actually run and whether the Run
  can complete.
- The optional trained Laya route head may suggest the order of one candidate
  already selected by the Tool Router for an eligible single-stage request. It
  cannot add or remove candidates. Multi-stage plans continue to skip this hint.
- Strong tool signals may skip the binary Laya inference to avoid unnecessary
  local model work. Skipping the hint does not change Tool Router behavior.

## Trace fields

`routing.decision.layer` records the raw classifier details, the normalized
`hint` when available, `decision_power: advisory_only`, and any optional
`route_hint_candidate`. `routing.decision.router_authority` remains
`keyword_tool_router` (or the capability inventory path), and
`router_candidates` records the selected tool set.

## Behavior examples

| Input | Laya hint | Tool set authority |
|---|---|---|
| Pure concept question | `text_likely` may be recorded | Tool Router may select no tools. |
| Weather / search / file request | Laya may be skipped or say `tool_likely` | Tool Router selects candidate tools. |
| Laya says `text_likely` for a tool request | Hint is recorded | The Tool Router's candidates remain available. |
| Optional route head suggests a candidate | Candidate is moved earlier in order | All Router-selected candidates remain exposed. |

TUI Laya service startup is a separate lifecycle concern. This contract changes
the authority of Laya decisions; it does not change the current TUI warm-up
behavior or make input available before warm-up completes.
