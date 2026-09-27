# Layer Behavior Benchmark

This benchmark scores the current Laya `screen()` binary decision, independently
from Tool Router, Planner, provider calls, and tool execution. Its fixed input
set is `cases.v1.jsonl`; every result records dataset SHA-256, checkpoint path,
checkpoint config hash, backend, and confidence threshold.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe benchmark\layer_behavior\run_benchmark.py
```

Use `--output path.json` to write to a fixed location. Default runs create a
timestamped JSON report in `var/benchmark-runs/layer_behavior/`. The reviewed
starter baseline under `results/` remains the versioned reference artifact.

## Metric semantics

- Positive means the request requires an action/tool, including cases that
  should be clarified before execution.
- `direct_text` is a negative prediction; `tool_needed` is positive.
- `None` is abstention. Selective precision/recall/F1 use only classified
  cases; coverage and abstentions are reported alongside them.
- `tool_recall_over_all_action_cases` treats abstained action cases as misses,
  so they remain visible in the conservative recall denominator.
- Category subsets are small diagnostic slices, not statistically stable
  estimates. In particular, no single case's category F1 should be treated as
  a release-quality estimate.
- The dataset is a first fixed smoke baseline, not yet a reviewed production
  gold set. Expand it with independently reviewed paraphrases, multi-turn
  context cases, and enough examples per category before using thresholds for
  rollout decisions.
- A blind second-review sheet for the current 31 cases is available at
  [`review/cases.v1-blind-review.csv`](review/cases.v1-blind-review.csv); wait
  for the independent reviewer and adjudicate disagreements before versioning
  a larger production gold set.

## Initial baseline (2026-09-26)

Artifact: `results/baseline_20260926.json`
Dataset SHA-256: `1095f87f2fe7c57a8cd0c97c5bdb325ab45be4883b48d96ab4fddfd61ef8d52c`
Backend: torch; confidence threshold: 0.85.

| Metric | Result |
|---|---:|
| Cases | 31 |
| Classified / coverage | 26 / 83.87% |
| Abstentions | 5 (4 action, 1 non-action) |
| Tool precision (classified subset) | 1.00 |
| Tool recall (classified subset) | 0.533 |
| Tool recall (all action cases, abstain as miss) | 0.421 |
| Tool F1 (classified subset) | 0.696 |
| False positives | 0 |

Action misses: `S03`, `S04`, `F03`, `C01`, `C02`, `M01`, `M02`, `R01`, `R02`,
`Q02`, `Q03`. This is a classifier-only result; production's strong-tool-signal
guard and keyword Tool Router can compensate for some rows, so do not interpret
it as end-to-end success rate.

## Run trace events

Production `route_agent(..., run_id=...)` writes `routing.decision` into the
existing `task_events` store. The event records a query hash (not raw user text),
Layer enabled/skipped/result/confidence/threshold/fallback/backend, route mode,
allowed tool names, and a hash of the selected Agent tool schemas. `model.call`,
`tool.invocation`, and `run.terminal` events carry the same
`routing_decision_id`; they remain attached to their existing invocation and
Run records.

The schema hash represents the Agent tool definitions assembled by
`route_agent`. It does not independently prove the final provider request body;
provider-payload capture/parity remains a separate follow-up.
