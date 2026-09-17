# FORGE Agent Local Baseline + Completion Proof — Phase 11 报告

> 日期：2026-09-11
> 主题：Local Model Baseline + Completion Eligibility Proof + Trace Instrumentation
> 基线：《FORGE-Agent-Evidence-Guard-Phase10》 + phase6~10 全部原始产物 + 当前真实代码
> 约束：**禁止调用 gateway/agnes；所有测试只用本地模型**
> （`FORGE_MODEL_PREF=local`，`Qwen3.6-35B-A3B` @ http://localhost:8080/v1）

---

## 1. Executive Summary

本轮解决三个基础问题：**模型基线边界**、**completion_ready 语义过粗**、**tool args instrumentation 不完整**。

**交付**：

1. **Tool Invocation Instrumentation**：每个工具请求稳定记录
   `run_id / invocation_id / tool_name / tool_capability / normalized_args /
   canonical_target / workspace_epoch / mutation_revision / blocked / blocked_reason /
   execution_status / result_fingerprint / result_summary / evidence_kind / at`
   （事件 `tool.invocation`）。`canonical_target` 无法解析时写 **null**，不写空串。
2. **Capability 分类**：复用现有 `runtime/spec.py` 元数据，新增 `capability_of(name)`
   → DISCOVERY / READ / MUTATION / VERIFICATION / EXTERNAL_FACT / MEMORY / COMMUNICATION / OTHER。
3. **Mutation Revision + Verification Coverage**：mutation → `evidence_epoch+1`；
   verification 记录 `verified_revision` 与 `verification_coverage`。
4. **Completion Eligibility（可解释）**：来自现有 Completion Gate 语义 +
   `extract_obligations(request_text)`（mutation_required / verification_required）；
   输出 `eligible + reasons`（mutation_requirement / verification_requirement /
   latest_revision_verified / pending_user / pending_approval / unresolved_blockers /
   execution_evidence）。
5. **Guard B 语义修正**：改用 `completion_eligibility().eligible`，不再用
   `mutation_seen && verification_passed`。
6. **Local Coding Microbenchmark**（真实本地模型 + 真实 Runtime + 微型 fixture）。
7. **测试**：新增 Phase 11 单测 8 项；全量 **720 passed, 1 skipped**。

**核心实测结论（本地模型，N=3）**：

| Case | Baseline | Guard B |
|---|---|---|
| M1 单文件修复+测试 | **0/3 pass**（每次都 0 次 VERIFICATION） | 0/3 pass（0 次 VERIFICATION） |
| M6 只读解释 | 3/3 pass | 3/3 pass |

→ 本地模型 coding 失败的**直接原因不是 redundant evidence、也不是 completion 后 wandering，
而是模型在明确要求“跑测试”时不执行 verification**。Guard B 因此从不触发（NEUTRAL）。

---

## 2. Model Baseline Boundary

```
Runtime Semantic Baseline = YES
Gateway-era Production Baseline = HISTORICAL REFERENCE
Local-model Production Baseline = NOT ESTABLISHED（本轮 micro 仅 50%，未达 80% 门槛）
```
禁止再用 Phase 6~10 的 gateway 数据描述“当前生产模型表现”。

## 3. Gateway vs Local Separation

| | Gateway era（Phase 6–10） | Local（Phase 11） |
|---|---|---|
| 模型 | agnes-2.5-flash | Qwen3.6-35B-A3B（本地） |
| 用途 | 架构/行为模式历史参考 | 当前生产基线（进行中） |
| coding 主问题 | READ/DISCOVERY 过多、post-verify wandering | **不执行 verification** |

## 4. Tool Instrumentation

已实现（`runtime/runner.py::_record_tool` → 事件 `tool.invocation`）。
字段见 §1；`canonical_target` 用 `canonical_target_of()`，不可解析 → `null`。
本轮 micro 的 metrics 即由 `tool.invocation` 事件驱动（capability 计数、verified 判定）。

## 5. Capability Metadata

复用 `runtime/spec.py`（未新建 Registry），新增 `capability_of()`；单测覆盖
read/list/edit/run_python/web_search/remember 六类。

## 6. Mutation Revision

`evidence_epoch`：Run 起始 0，**mutation 成功 +1**；read/search/verification 的 exact identity
与 verification 的 `verified_revision` 都带 epoch。mutation 后旧 verification 立即失效
（`verified_revision != evidence_epoch`）。

## 7. Verification Coverage

verification 记录 `{tool, passed, verified_revision}` 列表（`verification_coverage`），
并保留 `verified_revision`。未新建 Verification Engine，仅完善 ExecutionEvidence。

## 8. Task Obligation Semantics

`extract_obligations(request_text)` → `{mutation_required, verification_required}`：
- mutation：修复/修改/新增/删除/替换…；
- verification：测试/验证/校验/跑测试/运行测试/lint/build…。
多目标（“修复 A 和 B”）当前**无法可靠识别** → 记录为 unknown/unresolved，
**不因“一个 mutation + 一个 test PASS”自动认为多目标任务完成**（见 §15 局限）。

## 9. Completion Eligibility

```
completion_ready =
  Completion Gate 语义 eligible
  AND required execution evidence satisfied
```
实现：`RunContext.completion_eligibility()`（复用现有状态），
无明确义务时**保守要求 mutation + verification 都发生**。

## 10. Completion Eligibility Explainability

返回 `reasons`：`mutation_requirement / verification_requirement / latest_revision_verified /
pending_user / pending_approval / unresolved_blockers / execution_evidence`
（+ 无义务时的 `fallback_requires_mutation_and_verification`）。

## 11. Historical Post-Verify Re-evaluation

**NOT EVALUABLE**。历史 artifact 的 `normalized_args` 大量缺失
（仅 **90/1117** calls 目标可解析），且无 `verified_revision` / `completion_eligible` 记录，
无法判定 Phase 10 的 196 次 post-verify exploration 中“真正发生在 completion-eligible 之后”的数量。
**不根据缺失参数补猜**；等待新 instrumentation 数据。

## 12. Local Coding Microbenchmark

微型 fixture（`micro_fixture/`，可重置）：
- `calc.py`（含 bug 的 `add`）+ `test_calc.py`；
- 覆盖：单文件修复+测试（M1）、只读解释（M6）。
仍使用真实 local model / Agent Loop / Runner / Router / Permission / Completion / Tools。

## 13. Local Baseline

本地模型，N=3，Guards OFF：

| Case | pass | executions（capability） | states |
|---|---|---|---|
| M1 | **0/3** | DISCOVERY 1–2, READ 1–2, MUTATION 1–3, **VERIFICATION 0** | failed / waiting_user / completed |
| M6 | 3/3 | READ 1 | completed |

**Local Microbenchmark Behavior Pass = 3/6 = 50%**；**VERIFICATION coverage on M1 = 0%**。

## 14. Guard B Design Correction

Guard B 现只在 `completion_eligibility().eligible == true` 时生效；
M1 因从未 verification → 永不 eligible → **Guard B 从不触发**。
第一次触发返回极短提示（`completion_guard_warning=1`）；第二次（无新 blocker）进入
现有 final response 路径，终态 `completed`。

## 15. False Completion Tests

确定性单测（`tests/test_phase11_completion.py`）覆盖：
- **Case 2**（mutation → PASS → 新 mutation）：旧 PASS 失效 ✅
- **Case 4**（verification PASS + pending approval）：不 eligible ✅
- **Case 5**（verification PASS + waiting_user）：不 eligible ✅
- 无义务时保守要求 mutation+verification ✅
- **Case 1（多目标：改了 A、B 未改）**：**当前无法可靠识别 required_targets → 局限**，
  记为 unknown，不做自动完成判定。
- **Case 3（unit test PASS 但还要求 build）**：verification scope 尚未细分到 kind → 局限。

## 16. Guard B A/B

| | Baseline | Guard B |
|---|---|---|
| M1 pass | 0/3 | 0/3 |
| M6 pass | 3/3 | 3/3 |
| Guard B 触发次数 | — | **0**（M1 从未 completion_eligible） |

→ **Completion-Ready Guard = NEUTRAL（本地 micro）**：不是 Guard 无效，而是**前置条件从未满足**。

## 17. Completion Eligibility Precision

- Guard B 强制收口次数 = 0 → **未发生任何 false completion**。
- **Completion Eligibility Precision = 100%（在 0 次强制收口下为空真）**；
  Recall = 0（从未收口）。按 §二十二“优先 Precision”，当前无 false completion regression。

## 18. Post-Completion-Ready Wandering

本地 micro 中 M1 从未进入 completion-eligible，故 **post-completion-ready wandering = 0**。
真正的问题是 **pre-verification**：模型不执行验证。

## 19. Guard A Re-evaluation

历史修复后仅 suppression 6/610（~1%），且 instrumentation 不完整 →
**KEEP OFF**。不因旧的 20.4% analysis duplicate 重新打开。

## 20. Runtime Regression

全量测试 **720 passed, 1 skipped**（含 Phase 11 新增 8 项）。Runtime Semantic Baseline 保持 YES。

## 21. Local Production Status

```
Runtime Semantic Baseline = YES
Gateway-era Production E2E = HISTORICAL ONLY
Local-model Production E2E = NOT ESTABLISHED
```
原因：micro Behavior Pass 50%（<80% 门槛），M1 稳定 0/3；但**不是 Guard 问题**，
而是**本地模型执行策略：被明确要求跑测试却不调用 verification**。

## 22. Next Decision

下一阶段应聚焦 **Model-level Execution Policy（在现有 Agent Loop 内）**：
在 `verification_required` 义务未满足时，用**确定性 obligation 反馈**引导模型执行验证
（复用 Decision Hint / Completion Gate 的 obligation 通道，不新增 Planner），
并在本地 micro 上验证 M1 的 VERIFICATION coverage 提升后，再扩到 5~10 个真实 coding case。

---

## 最终输出

```text
Runtime Semantic Baseline =
YES

Gateway-era Production E2E =
HISTORICAL ONLY

Local-model Production E2E =
NOT ESTABLISHED

Completion Eligibility Precision =
100%（0 次强制收口；无 false completion；Recall=0）

Completion-Ready Guard =
NEUTRAL（本地 micro：M1 从未 completion_eligible，Guard 未触发）

Exact Redundant Guard =
KEEP OFF

Primary remaining bottleneck =
本地模型在执行义务中的缺失：被明确要求 verification 却不执行（M1 VERIFICATION coverage=0%）

Next component to modify =
现有 Agent Loop 内的 obligation-driven execution policy（确保 verification_required 被满足）
```

---

## 附：本轮修改/新增文件

| 文件 | 改动 |
|---|---|
| `runtime/spec.py` | `capability_of()`（复用现有目录元数据） |
| `runtime/completion.py` | `has_mutation_intent` / `extract_obligations` / `CompletionEligibility` / `evaluate_completion_eligibility` |
| `runtime/readiness_gate.py` | `canonical_target_of`；`verified_revision` / `verification_coverage` |
| `runtime/runctx.py` | `completion_eligibility()`（可解释） |
| `runtime/runner.py` | `tool.invocation` instrumentation；Guard B 改用 completion eligibility |
| `benchmark/microbenchmark.py` / `micro_driver.py` | 本地微型 benchmark |
| `micro_fixture/` | 可重置微型 fixture |
| `tests/test_phase11_completion.py` | 8 项确定性测试 |
