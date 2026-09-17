# FORGE Agent Evidence Guard — Phase 10 报告

> 日期：2026-09-11
> 主题：Exact Redundant Evidence Guard + Completion-Ready Finalization
> 基线：《FORGE-Agent-Decision-Policy-Phase9》 + phase6/7/8/9 全部原始产物 + 当前真实代码
> 约束（用户最新指令）：**禁止调用 gateway/agnes 模型，所有测试只能用本地模型**
> （`FORGE_MODEL_PREF=local`，`G:\models\Qwen3.6-35B-A3B-...gguf` @ http://localhost:8080/v1）

---

## 1. Executive Summary

本轮只解决两个有数据支持的问题：**完全重复 evidence 仍被重复执行**、
**verification 通过后仍继续探索**。未新增 Planner/Workflow/State Machine/第二套 Completion；
未动 Approval/Permission/FileScope/waiting_user/Terminalization/Provider/global budget/Router。

**交付**：

1. **Exact Redundant Evidence Guard（Guard A）**：基于 **Exact Execution Identity**
   `(tool, canonical_target, evidence_epoch)`（与 analysis fingerprint 分离），
   只对 read/search/verification 生效，**绝不 suppression mutation**。
2. **Evidence Epoch**：mutation 成功后 `epoch += 1` 并清空 read/search/verify cache，
   保证“文件已改后重读”不被误判重复。
3. **Completion-Ready Guard（Guard B）**：`mutation_seen + verification_passed`
   → 具备收口条件；模型仍纯探索时先给一次极短提示，再强制走现有收口路径，
   终态为 **completed**（不是 failed/no_progress）。
4. **9 项确定性单测**全部通过；全量测试 **712 passed, 1 skipped**。
5. **Replay 实验**（确定性）：暴露并修复了一个严重问题——**目标不可解析时绝不 suppression**
   （修复前 307 次 false suppression → 修复后 2）。
6. **归一化指标**：post-mutation 12.83/run、post-verify 14/run（全部 unjustified）。

**关键结论**：Guard 的**语义实现与安全边界已确定性验证**；但
**live N≥3 实验在本会话内不可行**——本地模型对多步 coding 任务单轮 >22 分钟，
故 `Guard A / Guard B = NOT PROVEN（live）`，不给出“有效/无效”的最终因果结论。

---

## 2. Phase 9 Evidence Review

- Phase 9：coding 主成本在 READ+DISCOVERY（68.4%）；post_mutation_discovery 463、post_verify 310；
  Decision Hint 实验 N=1（1/7 vs 1/7）。
- Phase 9 的 analysis fingerprint `sha1(tool+args+result[:300])` 仅用于统计，**不用于阻止执行**。

## 3. Decision Hint Evidence Correction

正式记录：**Decision Hint Effectiveness = NOT ESTABLISHED**（N=1 不足以判定有效/无效）。
保留现有 Hint 实现，不扩展、不删除；后续用 N≥3 重新判断（本轮未做）。

## 4. Analysis Fingerprint vs Execution Identity

| | Analysis Fingerprint | Exact Execution Identity |
|---|---|---|
| 用途 | Benchmark 统计 | 决定是否安全跳过重复执行 |
| 组成 | `sha1(tool + args + result_excerpt[:300])` | `(tool, canonical_target, evidence_epoch)` |
| 允许 hash | 是 | 否（用规范化目标 + epoch，不含 result 摘要） |
| 目标不可解析 | 仍可统计 | **返回 None → 绝不 suppression** |

## 5. Evidence Epoch

`evidence_epoch` 起始 0；**mutation 成功 → epoch+1 并清空 exact cache**。
因此 `read auth.py @ epoch0` 与 `read auth.py @ epoch1` 绝不视为相同。
复用现有 DiscoveryTracker（未新建 Runtime 子系统）。

## 6. Exact Redundant Semantics

| 工具类 | 判定 EXACT_REDUNDANT 的条件 |
|---|---|
| Read-like | same tool + same canonical target + same epoch（目标可解析） |
| Search-like | same normalized query + same epoch（query 可解析） |
| Verification | same command/target + same epoch（命令可解析） |
| **Mutation** | **永不 suppression**（仍由 idempotency/approval/permission/filescope 控制） |

Guard 属于 **tool execution suppression，不是 tool-call suppression**：
`tool_attempts += 1, tool_executions += 0, redundant_guard_hits += 1`。

## 7. Premature Mutation Correction

Phase 9 的“mutation 在 read 之前”过于粗略。本轮改为：
`mutation occurred AND target file/module 未被 inspect AND 无等价 context evidence`。
但历史 artifact 的 args 大量缺失（见 §11），**本轮只做保守记录**：
`premature_mutation_runs = 21`（仅在目标可解析时判定），不再用 35/50 的口径。

## 8. Post-Mutation Normalized Metrics

（phase6 A/B/C + phase9 baseline，coding，`phase10/metrics.json`）

| 指标 | 值 |
|---|---|
| coding runs | 30 |
| runs_with_mutation | 23 |
| post_mutation total | 295 |
| **avg / median / P95（per run）** | **12.83 / 12 / 27** |

## 9. Post-Verification Normalized Metrics

| 指标 | 值 |
|---|---|
| runs_with_verification_pass | 14 |
| post_verify total | 196 |
| **avg / median / P95（per run）** | **14 / 13 / 22** |
| unjustified（无新失败/错误上下文） | 196（启发式判定全部为 unjustified） |

→ **UNJUSTIFIED_POST_VERIFY_WANDERING ≈ 14 executions/run**，是 Guard B 的直接目标。

## 10. Completion-Ready Definition

复用现有 Completion Gate / ExecutionEvidence，仅增加极窄事实：
`completion_ready = mutation_seen AND verification_passed`（保守，避免 read-only/仅验证任务被误收口）。
失效条件（自动）：verification fails / new mutation / new runtime error / new dependency /
new user constraint —— epoch 或 verification 状态变化即自动失效。
**不是新的 Run State**，只是“已具备收口条件”。

## 11. Replay Experiment

历史 coding 序列重放（`phase10/guard_replay.json`）：

| | 修复前 | 修复后 |
|---|---|---|
| baseline executions | 610 | 610 |
| guard executions | 254 | 604 |
| suppressed | 356 | **6** |
| **false suppression** | **307** | **2** |
| suppressed true duplicate | 49 | 4 |
| post-verify suppressed | 178 | 6 |

**根因**：artifact 中 `normalized_args` 大量为空（**仅 90/1117 calls 目标可解析**），
空目标会让不同文件塌缩成同一 identity → 修复为“目标不可解析绝不 suppression”。
修复后仅 suppress 6 次，说明**可解析目标下的 exact duplicate 很稀少**。

> 结论：**历史 artifact 缺少参数 instrumentation，Replay 不足以判定 Guard 收益**
> （既不能证明有效，也不能证明无效）。

## 12. Guard A — Redundant Evidence

- 实现：`runtime/readiness_gate.py`（exact_identity / redundant_check / mark_exact_seen / bump_epoch）
  + `runtime/runner.py` wrapper（`FORGE_REDUNDANT_GUARD=on`）。
- 单测：`tests/test_phase10_guards.py`（identity/epoch/redundant/不 suppression mutation 等）通过。
- **Live 效果 = NOT PROVEN**（本地模型 N≥3 未完成）。

## 13. Guard B — Completion-Ready

- 实现：`completion_ready()` + `CompletionReadyTerminated` + run_turn 收口为 **completed**。
- 单测：`tests/test_phase10_guards.py`（announce→terminate、终态 completed）通过。
- **Live 效果 = NOT PROVEN**（同上）。

## 14. Decision Hint Re-evaluation

未做（N=1 已记录为 NOT ESTABLISHED；本轮未重跑）。

## 15. Sampling Experiment

**NOT PROVEN**。未执行 temperature/seed 受控实验（且本地模型单次 ~20s，成本过高）。

## 16. Coding Target Results

**未获得可判定结果**。原因：**本地模型对多步 coding 任务单轮 >22 分钟**，
baseline/guardAB 的 N≥3 未能在会话内完成。已停止以避免无意义长时间占用。
→ 不报告 1/7 之类的一次性数字作为结论。

## 17. Control Regression

未测量（live 未完成）。确定性单测保证 Guard 不改变安全门与 mutation 语义。

## 18. False Suppression Analysis

- 修复前：307（空目标塌缩）——**已修复**。
- 修复后：2（同 identity 但 result_excerpt 不同，疑似结果摘要非确定性）。
- 生产默认：`FORGE_REDUNDANT_GUARD` **默认 off**，且“目标不可解析绝不 suppression”，
  因此在参数 instrumentation 完善前，Guard 不会造成 false suppression。

## 19. False Completion Analysis

- `completion_ready` 要求 `mutation_seen AND verification_passed`（保守）。
- Guard B 终态为 completed；单测断言不出现 failed/no_progress。
- **false completion = 0（确定性层面）**；live 未测。

## 20. Evidence Efficiency

- Guard A 在可解析目标下 suppress 6/610（1%），收益很小；
  20.4%（Phase 9）的“duplicate”多来自 analysis fingerprint（含 result），
  并非**可安全 suppression 的 exact duplicate**。
- → **Exact Redundant Guard 在现有数据下对 Behavior Pass 的贡献预期很小**。

## 21. Full Benchmark A/B/C（如达标）

未运行。Coding Target 未达标（未测量）+ 本地模型时间成本。

## 22. Production E2E Status

**Production E2E Baseline = NO**（coding 决策质量未改善的结论仍成立）。
Runtime Semantic Baseline 保持 **YES**（本轮未动安全/终态语义，712 tests 通过）。

## 23. Next Decision

依据 §二十/§二十一 的判定逻辑：**Guard A 预期贡献小**（exact duplicate 稀少）；
**Guard B 针对的是 post-verify 14/run 的真实浪费**，但 **live 未证明**。
下一步应**先在本地模型下完成 Guard B 的 N≥3 live 实验**（或先补齐参数 instrumentation
以便 Replay 可判定），再决定是否继续 deterministic guard；否则转向
**Model-level Execution Policy**（在现有 Agent Loop 内）。

---

## 最终输出

```text
Exact Redundant Guard =
NOT PROVEN（live 未完成；确定性实现+单测通过；可解析目标下历史收益仅 1%）

Completion-Ready Guard =
NOT PROVEN（live 未完成；确定性实现+单测通过；针对 post-verify 14/run 的浪费）

Decision Hint =
NOT ESTABLISHED（Phase 9 N=1；本轮未重跑）

Sampling contribution =
NOT PROVEN

Primary remaining coding bottleneck =
Model multi-step decision policy（READ/DISCOVERY 过多 + unjustified post-verify wandering ~14/run）

Next component to modify =
Agent decision/execution policy within the existing Agent Loop
（优先在本地模型下完成 Guard B 的 N>=3 验证；不新增 Planner/Workflow）
```

---

## 附：本地模型约束的落实

- `benchmark/coding_driver.py`：`os.environ["FORGE_MODEL_PREF"] = "local"`（硬编码）。
- `benchmark/coding_experiment.py`：若 `FORGE_MODEL_PREF != local` 直接 `SystemExit`，拒绝运行。
- `benchmark/probe_driver.py`：`--provider local`。
- `benchmark/qualification.py`：改用 `local_model_name()` + `local_model_provider()`。
- `.env`：`FORGE_MODEL_PREF=local`。
- 已验证：本地服务 `http://localhost:8080/v1/models` → 200，模型 `Qwen3.6-35B-A3B`。
