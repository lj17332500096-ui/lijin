# Progress

- [x] 检查 TaskManager task_events、RunContext、route_agent 与 tool invocation/audit 接口。
- [x] 新增 31 条固定 JSONL Layer 行为数据，含 direct/chat/search/file/code/memory/reminder/ask/multi/unsupported/discussion 样本。
- [x] 新增可复算 benchmark 脚本：dataset/checkpoint-config 指纹、backend、threshold、逐例 decision/confidence、整体与类别指标、coverage/abstain/FN/FP case IDs。
- [x] 在 Laya screen 暴露置信度/阈值/fallback details。
- [x] 使用既有 task_events 打通 routing.decision → model.call → tool.invocation → run.terminal 的 routing_decision_id；query 仅 SHA256，tool args 沿用脱敏。
- [x] 生成首份基线 `benchmark/layer_behavior/results/baseline_20260926.json`。
- [x] 改动 Python 文件已通过 py_compile；未运行 pytest/test suite。
- 2026-09-27 reconciled the stale phase checklist with this completed implementation. The earlier test-suite restriction is preserved for this plan; no Layer suite was run in this turn.

## Baseline

31 cases; 26 classified; 5 abstain; coverage 0.8387. Classified precision 1.0000, recall 0.5333, F1 0.6957; action recall including abstained cases as misses 0.4211. Misses: S03/S04/F03/C01/C02/M01/M02/R01/R02/Q02/Q03. This measures Laya screen only, not downstream Router compensation or end-to-end completion.

## Remaining evidence boundary

Trace schema hash fingerprints Agent tool definitions assembled in route_agent; it does not capture/verify the actual provider request body. The 31-case fixture is a starter set and needs independent label review/expansion before release thresholds.

2026-09-27: The task checklist now reflects the completed implementation. These limitations remain explicit rollout gates; the initial labels are not claimed to be independently adjudicated, and the schema fingerprint does not prove exact outbound request parity.
