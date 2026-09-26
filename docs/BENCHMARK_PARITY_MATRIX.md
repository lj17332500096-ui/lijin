# BENCHMARK PARITY MATRIX

> Phase 1 交付物 3。逐 benchmark entrypoint 比对 12 个维度，正式定义
> **Benchmark Entry Parity = MIXED**（纠正 Phase 0 的"全部绕 run_turn"）。
> 证据：各 driver 源码 grep + 真实 `run_turn` 调用点定位。

## 1. 逐 entrypoint 真相表

| ENTRYPOINT | CALL PATH | RUN_TURN? | ROUTER? | APPROVAL? | PERMISSION? | OBLIGATION? | COMPLETION? | RUNTIME CONFIG SOURCE | MODEL SOURCE | VERIFICATION SOURCE |
|---|---|---|---|---|---|---|---|---|---|---|
| production (CLI/web) | `run_turn` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `.env`+代码默认 | gateway=agnes | `ExecutionEvidence` |
| microbenchmark | `rt.run_turn` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **硬置 3 flag + local** ❌ | **local=Spark** ❌ | **字符串重猜** ❌ |
| decision_qualification (capture+replay) | `rt.run_turn`（L752/773） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `.env`+代码默认 | gateway | `ExecutionEvidence` |
| decision_checkpoint | `route_agent + Runner.run`（L86/114） | ❌ 绕收口门 | ✅（仅 route） | ❌ 绕过 | ❌ 绕过 | ❌ 绕过 | ❌ 绕过 | `.env`+代码默认 | gateway | `ExecutionEvidence` |
| bounded window / M3 harness | [待核实: 读 benchmark/decision_qualification.py 内 M3 分支] | ? | ? | ? | ? | ? | ? | ? | ? | ? |

> **注**：`decision_replay.py` / `decision_capture.py` 不存在，实际文件是
> `decision_checkpoint.py`（绕收口门）+ `decision_qualification.py`（走
> `run_turn` + 用 `_RunnerProxy` 非侵入观测）。

## 2. 12 维度漂移矩阵（仅列"与生产不一致"的行）

| 维度 | production | microbenchmark | decision_checkpoint | 漂移 |
|---|---|---|---|---|
| 入口 | `run_turn` | `run_turn` ✅ | `route_agent+Runner.run` ❌ | `decision_checkpoint` 绕收口门 |
| model | gateway(agnes) | **local(Spark)** ❌ | gateway ✅ | `microbenchmark` 强制 local |
| `FORGE_COMPLETION_READY` | 代码默认 | **off** ❌ | 代码默认 | `microbenchmark` 硬置 |
| `FORGE_REDUNDANT_GUARD` | on | **off** ❌ | on | `microbenchmark` 硬置 |
| 验证源 | `ExecutionEvidence` | **字符串重猜** ❌ | `ExecutionEvidence` | `microbenchmark` L110 |

## 3. 正式判定

- **Benchmark Entry Parity = MIXED**：不是"全部绕 run_turn"（Phase 0 错判），
  而是"两条走 run_turn、一条绕收口门"。
- **BENCHMARK_NOT_PRODUCTION_PARITY = CONFIRMED**，根因定位到两个文件：
  - `benchmark/microbenchmark.py`（3 flag 硬置 + 强制 local + 字符串验证）
  - `benchmark/decision_checkpoint.py`（直接 `route_agent + Runner.run`，
    绕过 `run_turn` 的审批/义务/完成收口门）
- `decision_qualification` 与 `microbenchmark` 的**入口**同为 `run_turn`
  （✅ 等价），但 `microbenchmark` 的 flag/model/验证源三维漂移使**评测
  语义**不等价。

## [待核实]
- bounded window / M3 harness 的入口路径（`decision_qualification.py` 内 M3
  分支或独立 driver）需读源码确认（本阶段不修改，仅记录）。
