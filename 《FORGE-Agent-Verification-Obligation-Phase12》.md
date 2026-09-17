# FORGE Agent Verification Obligation — Phase 12 报告

> 日期：2026-09-11
> 主题：Verification Obligation Closure + Local Execution Reliability
> 基线：《FORGE-Agent-Local-Baseline-Completion-Proof-Phase11》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；所有行为实验使用本地 `Qwen3.6-35B-A3B`**（`FORGE_MODEL_PREF=local`）

---

## 1. Executive Summary

本轮唯一核心目标：**当任务明确要求 verification 时，让现有 Agent Loop 可靠意识到“验证义务未完成”，
并在最终回答前真正执行验证。**

**交付（复用现有组件，未新增 Planner/Workflow/State Machine/第二套 Completion）**：

1. **三态 Obligation 模型**：`required / not_required / unknown`，并修复关键词误判
   （“修改测试说明” ≠ 要求运行测试）。
2. **Obligation Ledger**（`RunContext.obligation_ledger()`）+ `obligation_deficits()` + `verification_due()`。
3. **Obligation Feedback（Variant A）**：mutation 后若 verification_due，在工具结果尾部追加极短义务提示。
4. **Completion Obligation Gate（Variant B）**：义务未满足时拒绝 completed；第一次反馈并继续，
   第二次进入 bounded failure `missing_required_verification`。
5. **`_succeed` 义务守卫**：防止 degraded / final-response-failure 路径绕过义务门。
6. **resume 修复**：`run_turn("")` 恢复时保留原始 goal 作为义务事实源。
7. **M1–M8 微型 fixture + 12 项确定性 Obligation 测试**；全量 **732 passed, 1 skipped**。

**关键实测（本地模型，N=1；Guards/Guard B OFF）**：

| Case | Baseline | Feedback | Gate |
|---|---|---|---|
| M1 修复+测试 | fail（**0 verification，completed**） | fail（0 verification） | **fail（0 verification，failed/missing_required_verification）** |
| M4 修复+验证 | fail（0 verification） | fail（0 verification） | fail（0 verification，failed） |
| M6 只读 | pass | pass | pass |
| M7 修改无测试 | pass | pass | pass |
| M8 “修改测试说明” | pass | pass | pass |

**结论**：

- **Verification Tool Availability = 100%**（M1/M4 的 Router 暴露 `run_python`/`code_loop`）→ **A 被排除**。
- **Verification Attempt Rate = 0%**（模型看到验证工具却不使用）→ **B 成立（主因）**。
- Baseline 出现 **false completion**（M1/M4 无验证却 completed）；Gate 修复后 **false completion = 0**
  （转为 bounded failure）→ **C 已闭合**。
- **Obligation Feedback = NEUTRAL**（模型忽略提示）。
- **Completion Obligation Gate = EFFECTIVE（消除 false completion）**，但对 **Behavior Pass 无提升**
  （模型仍不验证）。

---

## 2. Phase 11 Findings

Phase 11：本地 micro M1 0/3（0 次 VERIFICATION）、M6 3/3；Local-model Baseline = NOT ESTABLISHED。
根因指向：verification_required 存在于 Completion 语义，但**没有形成 execution obligation**。

## 3. Precision Metric Correction

Phase 11 的 “Completion Eligibility Precision = 100%” 不成立（positive eligibility = 0）。
正式改为：

```
Completion Eligibility Precision = NOT ESTIMABLE
positive eligibility decisions = 0
observed false completion = 0
```

## 4. Obligation Detection vs Enforcement

- **Detected Obligation**：由用户请求/意图推导（三态）。
- **Enforceable Obligation**：只有明确 `required` 时才阻止 finalization。
输出 `required / not_required / unknown`，不再只有 true/false。

## 5. Tri-State Obligation Model

`extract_obligations(text)` → `{"mutation": status, "verification": status}`。
验证判定：**执行动词（跑/运行/执行/run…）与验证名词同句出现**才算 `required`；
若验证名词处于“修改/阅读/解释…测试”内容语境 → `not_required`；否则 `unknown`。

## 6. Obligation Ledger

```
mutation:    {status, satisfied}
verification:{status, satisfied, required_revision, verified_revision}
```
`satisfied` 基于真实 execution evidence（非模型声明）。

## 7. Mutation Obligation

`mutation=required` 时，必须存在真实 MUTATION tool execution + 成功结果才算 satisfied。
模型说“已经修改”无证据 → 不算。

## 8. Verification Obligation

只能由 `tool_capability=VERIFICATION + 真实 execution + 成功 result +
verified_revision == current mutation revision` 满足。`claim ≠ evidence`。

## 9. Verification Due

`verification_due = verification_required AND mutation_seen AND NOT latest_revision_verified`。
mutation 后 `required_revision` 自动更新，旧 PASS 立即失效。

## 10. Obligation Feedback

Variant A：mutation 后若 verification_due，在**最近一次 mutation tool result 后**追加极短提示
（不指定具体工具，让模型自行选择）。非每一步都打扰。

## 11. Completion Obligation Gate

Variant B：`gate_verdict==PASS` 但存在 deficits → 第一次反馈缺失义务并继续正常 Agent Loop；
第二次（仍无新证据）→ bounded failure `missing_required_verification`。
另在 `_succeed` 加义务守卫，覆盖 degraded/final-response-failure 路径。

## 12. Bounded Missing-Verification Failure

终态：`failed` + `terminal.kind=bounded_failure` + `event_reason=missing_required_verification`。
不伪造 completed，也不无限 loop（最多一次 repair）。

## 13. Verification Tool Availability

| Case | exposed tools | verification tools |
|---|---|---|
| M1 | 13 | **run_python, code_loop** |
| M4 | 13 | **run_python, code_loop** |
| M6 | 8 | （无，正确） |
| M7 | 13 | run_python, code_loop（不强制） |
| M8 | 13 | run_python, code_loop（不强制） |

→ **Verification Tool Availability = 100%**（需要验证的 case）。问题不在 Router（A 排除）。

## 14. Verification Selection Accuracy

| 指标 | Baseline | Feedback | Gate |
|---|---|---|---|
| Verification Availability（M1,M4） | 100% | 100% | 100% |
| Verification Attempt Rate | **0%** | **0%** | **0%** |
| Verification Success Rate | 0% | 0% | 0% |
| Latest Revision Coverage | 0% | 0% | 0% |

## 15. M1–M8 Fixtures

`micro_fixture/`（`calc.py` + `test_calc.py` + `README.md`，可重置）：
M1 单文件修复+测试、M2 双文件+测试、M3 fail→修复→再验证、M4 修改+验证、M5 先读后改、
M6 只读、M7 修改无测试、M8 “修改测试说明”（关键词误判防护）。
本轮 live 运行覆盖 M1/M4/M6/M7/M8（受本地模型速度限制，M2/M3/M5 未 live）。

## 16. Deterministic Obligation Tests

`tests/test_phase12_obligations.py`（12 项）：
M1/M2/M3/M4/M5 → verification=required；M6 → not_required；M7 → not_required；
**M8（“修改测试说明”）→ ≠required**；verification_due / ledger / completion gate 行为。

## 17. Local Baseline

本地模型，Guards/Guard B OFF，obligation enforcement OFF（N=1）：

| Case | Behavior | Verification | State |
|---|---|---|---|
| M1 | fail | 0 | completed（false completion） |
| M4 | fail | 0 | waiting_user |
| M6 | pass | — | completed |
| M7 | pass | — | completed |
| M8 | pass | — | completed |

Behavior Pass = **3/5 = 60%**；verification required cases 的 Attempt Rate = **0%**。

## 18. Variant A — Feedback Only

Feedback ON、Gate OFF：M1/M4 仍 **0 verification**；M6/M7/M8 pass。
→ **Obligation Feedback = NEUTRAL**（本地模型忽略提示）。

## 19. Variant B — Feedback + Completion Gate

Gate ON（+ `_succeed` 守卫 + resume 修复）：M1/M4 从 **completed（false completion）** 变为
**failed / missing_required_verification**；M6/M7/M8 pass。
→ **Completion Obligation Gate = EFFECTIVE（消除 false completion）**；Behavior Pass 不变（3/5）。

## 20. Verification Attempt Rate

Baseline / Feedback / Gate 均为 **0%**。→ 模型看到验证工具但从不使用。

## 21. Latest Revision Coverage

0%（无任何 verification attempt，故无覆盖）。

## 22. False Verification Claims

**0**。模型没有声称验证通过（Completion Gate 的既有 claim 语义继续有效）。

## 23. Completion Eligibility Precision / Recall

```
Completion Eligibility Precision = NOT ESTIMABLE
positive eligibility decisions (leading to finalization) = 0
observed false completion (Baseline) = 2 (M1,M4)
observed false completion (Gate)     = 0
Recall = NOT ESTIMABLE
```

## 24. Behavior Pass

Local micro：Baseline 3/5、Feedback 3/5、Gate 3/5。**未达 80%**；M1 未达 2/3（本轮 N=1）。

## 25. Control Regression

M6/M7/M8（只读 / 修改无测试 / 关键词误判）在三种条件下均 pass → **Control regression = 0**。
M8 未被强制 verification（三态检测正确）。

## 26. Runtime Regression

全量 **732 passed, 1 skipped**（含 Phase 12 新增 13 项）。Runtime Semantic Baseline 保持 YES。
本轮修复：verification 检测正则（含路径）、resume request_text 保留、`_succeed` 义务守卫。

## 27. Guard B Re-entry Decision

Guard B（Completion-Ready）保持 **OFF**：verification coverage 仍为 0，尚无 completion_eligible 正样本，
Guard B 无实验意义。待 Verification Attempt Rate 提升后再评估。

## 28. Local Coding Benchmark Decision

**暂不进入** 5~10 个真实 coding case（micro 未达 80%）。先解决模型不验证的问题。

## 29. Production Status

```
Runtime Semantic Baseline      = YES
Gateway-era Production E2E     = HISTORICAL ONLY
Local Microbenchmark Baseline  = NO（3/5，verification attempt 0%）
Local Coding Benchmark Baseline= NOT ESTABLISHED
Local Full Production E2E      = NOT ESTABLISHED
```

## 30. Next Direction

主因是 **B：模型看到了 verification 工具但不使用**（A 已排除，C 已由 Gate 闭合，D 已修复，E 无尝试）。
下一步（Phase 13）应聚焦 **Model-level Execution Policy（在现有 Agent Loop 内）**：
在 `verification_due` 时用更强的**结构化义务反馈**（如首轮把义务作为必答项/工具选择提示，
仍不指定具体工具、不替模型猜命令），并在本地 micro 上验证 **Verification Attempt Rate ≥ 90%**；
只有该指标达标后，才重新评估 Guard B 与真实 coding case。

---

## 最终输出

```text
Verification Tool Availability =
100%（M1/M4 暴露 run_python/code_loop）

Verification Attempt Rate =
0%（Baseline / Feedback / Gate）

Latest Revision Verification Coverage =
0%

False Verification Claims =
0

Completion Eligibility Precision =
NOT ESTIMABLE（positive eligibility = 0）

Completion Eligibility Recall =
NOT ESTIMABLE

Obligation Feedback =
NEUTRAL

Completion Obligation Gate =
EFFECTIVE（消除 false completion：Baseline 2 → Gate 0），但对 Behavior Pass 无提升

Local Microbenchmark Baseline =
NO（3/5 = 60%，verification attempt 0%）

Primary remaining bottleneck =
B — 模型看到了 verification 工具却不执行（model execution policy）

Next component to modify =
现有 Agent Loop 内的 obligation-driven execution policy（提升 Verification Attempt Rate），
不新增 Planner / 不替模型拼命令
```

## 附：A–E 判定

| 选项 | 判定 | 证据 |
|---|---|---|
| A. Router 没给 verification tool | **排除** | M1/M4 暴露 run_python/code_loop |
| B. 模型看到 tool 但不用 | **成立（主因）** | Attempt Rate 0% |
| C. Completion 允许无验证结束 | **已闭合** | Baseline false completion 2 → Gate 0 |
| D. Obligation detection 判断错 | **已修复** | 路径夹带导致漏判；三态+动词/名词修正；M8 无误报 |
| E. 调用但执行失败 | **不适用** | 从未调用 |

## 附：本轮修改文件

| 文件 | 改动 |
|---|---|
| `runtime/completion.py` | 三态 obligation；`detect_verification_obligation`；`_NO_MUTATION_RE`；`evaluate_completion_eligibility` 适配 |
| `runtime/runctx.py` | `obligation_ledger` / `obligation_deficits` / `verification_due`；`completion_eligibility` 适配 |
| `runtime/runner.py` | mutation 后 obligation feedback；Completion Obligation Gate；`_succeed` 义务守卫；resume 保留 goal |
| `benchmark/microbenchmark.py` | M1–M8 + verification 指标 + feedback/gate 模式 |
| `micro_fixture/` | 微型 fixture（calc.py / test_calc.py / README.md） |
| `tests/test_phase12_obligations.py` | 13 项确定性测试 |
