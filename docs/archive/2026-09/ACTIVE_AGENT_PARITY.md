# ACTIVE_AGENT_PARITY

> FORGE Runtime Truth Audit · Phase 0（八）
> 核心问题：是否所有入口都用 `route_agent → active_agent`，没有任何路径退回 `global assistant_agent`。

## 逐入口核查（全部走 run_turn → route_agent）

| 入口 | 是否经 run_turn | active_agent 来源 | 退回 global? |
|---|---|---|---|
| **sync**（REPL/CLI） | ✅ `run_turn` | `route_agent(base=current_assistant_agent())` | ❌ 不退 |
| **stream**（CLI 消息平台） | ✅ `run_turn` | 同上 | ❌ 不退 |
| **async** | ✅ `run_turn` | 同上 | ❌ 不退 |
| **resume**（`run_turn(task_id=)`） | ✅ `run_turn` | 同上（同一 route_agent） | ❌ 不退 |
| **approval resume** | ✅ `run_turn(task_id=)` resume | 同上 + `_execute_approved_invocation` | ❌ 不退 |
| **benchmark** | ⚠️ 走 `execute_turn` | 看 benchmark 是否传 agent | **见下** |
| **scheduled task** | ✅ `run_turn(channel=scheduled)` | `route_agent`→`route_profile` 判 cheap | ❌ 不退 |

## 结论

**生产入口（sync/stream/async/resume/approval/scheduled）全部收敛到 `run_turn → route_agent`，没有直接退回 global `assistant_agent` 的裸调用。** `route_agent` 内部始终以 `current_assistant_agent()`（即 global `assistant_agent`）为**克隆基准**，但返回的是**克隆后的 active_agent**（经模型档位克隆 + Tool Router 子集裁剪），不是 global 本身。语义正确：global 是"原型"，active 是"本轮实例"。

**基准取 global 本身是对的**（避免绕过 CLI 安全开关），返回的是克隆后的 active_agent，不是 global 本身。生产入口收敛正确。

## benchmark 入口核查（已读源码，推翻 Phase 0 初判）
- `microbenchmark._run_case` → `rt.run_turn(prompt)` + 审批自动批准 + `run_turn(task_id=)` resume。**走生产同链（run_turn + 义务门 + 审批门）**，不退 global。
- `decision_qualification` → 部分走 `rt.run_turn`（L752/773），部分走 `rt.route_agent(...)`+`Runner.run`（L814/900，checkpoint 重放）。**重放路径不经 run_turn 收口门。**
- `decision_checkpoint` → `rt.route_agent(prompt)` + 裸 `Runner.run`（L114）。**不经 run_turn / 审批门 / 义务门 / 收口门。**

**判定（细化）：benchmark 不是整体 `BENCHMARK_NOT_PRODUCTION_PARITY`，而是"混合"——**
- ✅ `microbenchmark` 完全走生产 `run_turn` 链（等价）。
- ⚠️ `decision_*` 的 checkpoint/重放分支走 `route_agent + Runner.run`，绕过 run_turn 的 RunContext/审批/义务/收口门 → **该子集 `BENCHMARK_NOT_PRODUCTION_PARITY`**。

**真正的 P0 偏移不在入口，而在 feature flag 与判定逻辑**（详见 FORGE-RUNTIME-TRUTH-AUDIT.md 修正区）：
1. `microbenchmark._run_case` 硬置 `FORGE_COMPLETION_READY=off / FORGE_REDUNDANT_GUARD=off / FORGE_DECISION_HINT=off`，而生产默认这些是开/默认 → **benchmark 与生产 completion 行为不一致（BENCHMARK_NOT_PRODUCTION_PARITY 实锤）**。
2. `microbenchmark` L110 用字符串 `"退出码: 0"/"passed" in result_summary` **重新猜验证结果**，未用 `ExecutionEvidence.verification_passed()` → 违反第十七项（Verification Truth 必须来自真实执行证据，禁止字符串重猜）。
3. `FORGE_MODEL_PREF` 强制 `local`（microbenchmark L184、decision L130）→ benchmark 跑本地模型，生产默认 `gateway` → **模型都不一致**，评测结论不可平移生产。

## [待核实]
- `decision_qualification` 的 snapshot `model_config.FORGE_MODEL_PREF` 重放是否真绑了 provider/model fingerprint（L832 已在查，需确认 hash 校验）。
