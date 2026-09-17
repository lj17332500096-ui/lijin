# FORGE Agent Verification Selection — Phase 13 报告

> 日期：2026-09-11
> 主题：Verification Tool Selection Policy + Local Execution Closure
> 基线：《FORGE-Agent-Verification-Obligation-Phase12》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**（`FORGE_MODEL_PREF=local`）

---

## 1. Executive Summary

本轮唯一目标：查清 **为什么 `verification_due=true` 且验证工具已暴露时，本地模型仍不选择
verification capability**，并在 A2（语义可见性）/ B（知道但不选）/ 工具竞争 / 模型 action selection
之间定因。

**审计与实验结论**：

1. **模型真正看到的 `run_python` description（旧版）不表达验证语义**：
   “在沙箱里运行 Python 代码并返回结果”，未提测试/验证；且 schema 要求
   `project+filename+code+args+timeout` 全填 → **A2 假设成立（语义可见性不足）**。
2. 据此实施 **Variant B**（改进模型可见 description，加入“运行项目测试/验证修改”），
   并实施 **Variant A**（更高显著度 Verification-Due Runtime Fact）。
3. 但本地实测：**Variant A（Fact）与 Variant B（Description）与 Variant C（Verification-Focused
   抑制无关能力）均未使 M1/M4 产生任何 verification attempt**（仍 0%）。
4. 因此 **A2 与工具竞争均被排除为“充分原因”**；剩余唯一成立的是
   **模型自身的 action selection 限制**：工具已暴露、语义已改善、义务已高显著提示、无关能力已抑制，
   本地模型仍不选择 VERIFICATION。

**最终定因**：`Primary cause = model-level action selection limitation`
（A 排除；A2 已修但不充分；C 已修但不充分；E 未发生）。

---

## 2. Phase 12 Baseline

- 旧 `run_python` description：无测试/验证语义；Verification Attempt Rate = 0%。
- Completion Obligation Gate：EFFECTIVE（消除 false completion），Behavior 无提升。
- Obligation Feedback：N=1，观察改进 = 0 → 本轮正式改为 **NOT ESTABLISHED**。

## 3. A–E + A2 Hypothesis

```
A  = Router 未暴露 verification tool           → 已排除（Availability 100%）
A2 = 暴露但 name/description/schema 无法表达验证语义 → 审计成立；已修（Variant B）
B  = 明确可用但模型仍不选                        → 本轮主因
C  = Completion 允许无验证结束                   → Phase 12 已闭合
D  = Obligation detection 错误                   → Phase 12 已闭合
E  = 调用但执行失败                              → 未发生
```

## 4. Model-Visible Tool Definitions

真实链路（Registry → Router → SDK tool definition → 本地模型）读取：

**run_python（旧）**
```
description: 在沙箱里运行 Python 代码并返回运行结果（stdout/stderr/退出码）。
             filename 指定要运行的文件，code 直接给代码…
schema properties: project, filename, code, args, timeout
required: project, filename, code, args, timeout   ← 全必填
```
**code_loop**
```
description: 代码验证闭环（Codex 循环）… 反复写代码、失败则自动修复（最多 3 次）…
```
→ `run_python` 未向模型表达“可运行项目测试/验证修改”；schema 全必填进一步降低可用性。

## 5. Verification Semantic Discoverability

- 旧版：`run_python` 语义仅“运行 Python 代码” → **Discoverability 低**。
- 新版（Variant B）：明确“运行项目测试（pytest/unittest）/修改后验证” → 语义改善。
- 但实测 **Attempt Rate 仍 0%** → 语义改善**必要但不充分**。

## 6. Verification-Due Runtime Fact

Variant A 实现（`FORGE_OBLIGATION_FEEDBACK=on`）：
```
【执行义务 / Execution obligation】
verification = REQUIRED
status = UNSATISFIED
revision = <current>
在最终完成前，必须为本 revision 获得真实验证证据（自行选择验证工具）。
```
只在 verification_due 的 mutation 结果后出现；**不指定具体工具/命令**。

## 7. Verification Decision Turn

记录 mutation 后第一个非 blocked 动作（capability + tool）。实测：

| 条件 | M1 first action | M4 first action |
|---|---|---|
| Baseline（新 desc） | DISCOVERY `list_workspace_files` | DISCOVERY `list_workspace_files` |
| Focus（Variant C） | READ `read_workspace_file` | DISCOVERY `list_workspace_files` |

**从未出现 VERIFICATION**。

## 8. First Decision Accuracy

按“preparation 必须与如何执行 verification 直接相关（读 package.json/pyproject/pytest.ini/CI 配置）”
的严格定义，观测到的首次动作是**通用工作区探索**（list/read 任意文件），**不是**验证准备。
→ **First Verification Decision Accuracy = 0%**。

## 9. Baseline

Phase 13 baseline（**新 description**、Fact OFF、Completion Gate ON，本地 N=1）：

| Case | Behavior | Verification | State |
|---|---|---|---|
| M1 | fail | 0 | failed（gate 阻断 false completion） |
| M4 | fail | 0 | failed |
| M6/M7/M8 | pass | — | completed |

Behavior Pass = 3/5；Attempt Rate（M1/M4）= **0%**。

## 10. Variant A — Runtime Fact

Fact ON（本地，M1/M4 等）：实测 M1/M4 **仍 0 verification**（Phase 12 feedback 亦为 0）。
→ **Variant A = NOT ESTABLISHED / no observed improvement**。

## 11. Variant B — Tool Description

改进 `run_python` 模型可见 description（加入测试/验证语义）。Phase 13 baseline 即运行于新
description 下，M1/M4 **仍 0 verification**。
→ **Variant B = 语义改善成立，但行为无改善**。

## 12. Variant C — Focused Exposure

实现：`verification_due` 时抑制 MEMORY / EXTERNAL_FACT / COMMUNICATION（保留
VERIFICATION/READ/DISCOVERY/MUTATION；不指定命令）。实测 M1/M4 **仍 0 verification**。
→ **Variant C = no observed improvement**。

## 13. Tool Choice Constraint

未执行（A/B/C 已足够定因；且本地模型无统一 tool_choice 约束路径，实验成本高）。

## 14. Verification Attempt Rate

| 条件 | M1/M4 Attempt |
|---|---|
| Phase 12 baseline（旧 desc） | 0% |
| Phase 13 baseline（新 desc） | 0% |
| Variant A（Fact） | 0% |
| Variant C（Focus） | 0% |

→ **Verification Attempt Rate = 0%（全部条件）**。

## 15. Verification Preparation

观测到的首次动作（list/read 任意工作区文件）**不构成**合法 verification preparation
（未指向测试/构建配置）。→ Preparation Rate ≈ 0%。

## 16. Verification Failure Recovery

未发生（从未调用 verification），M3 闭环无法 live 验证。

## 17. M3 End-to-End Closure

**未达成**。前提（Attempt Rate 提升）未满足，按 §二十三 未进入 M3 live。

## 18. Latest Revision Coverage

0%（无 verification attempt）。

## 19. False Completion

**0**（Completion Obligation Gate 始终 ON；Baseline 的 false completion 在 Phase 12 已由 Gate 闭合）。

## 20. False Verification Claims

**0**。

## 21. Control Regression

M6（只读）/ M7（修改无测试）/ M8（“修改测试说明”）在 baseline/focus 均 **pass**，
且未被强制 verification → **Control regression = 0**，Unnecessary Verification Rate 未上升。

## 22. Local Microbenchmark Result

Behavior Pass = 3/5（M1/M4 fail，M6/M7/M8 pass）；verification-required case Attempt = 0%。
→ **Local Microbenchmark Baseline = NO**。

## 23. Local Coding Benchmark Decision

**不进入** 5~10 个真实 coding case（micro 未达 80%，Attempt 0%）。

## 24. Production Status

```
Runtime Semantic Baseline      = YES
Gateway-era Production E2E     = HISTORICAL ONLY
Local Microbenchmark Baseline  = NO（3/5；verification attempt 0%）
Local Coding Benchmark Baseline= NOT ESTABLISHED
Local Full Production E2E      = NOT ESTABLISHED
```

## 25. Next Direction

A/B/C 均已排除或不足，唯一成立的是 **模型自身 action selection 限制**。
下一步不再堆 Prompt / description / exposure；按 §三十，研究
**structured action commitment 或 capability-level tool choice constraint**
（在现有 Agent Loop 内做实验，不进入生产），验证是否能让本地模型在 verification_due 时
强制提交一个 verification action；同时保持 Completion Obligation Gate（不得为提分重新允许
false completion）。

---

## 最终输出

```text
Verification Tool Availability =
100%

Verification Tool Semantic Discoverability =
旧版低；Variant B 已改善语义，但行为仍无改善（必要不充分）

First Verification Decision Accuracy =
0%

Verification Attempt Rate =
0%

Latest Revision Coverage =
0%

False Completion =
0

False Verification Claims =
0

Primary cause =
model-level action selection limitation
（A 排除；A2 已修不充分；C 已修不充分；E 未发生）

Winning Variant =
NONE

Local Microbenchmark Baseline =
NO

Next component to modify =
现有 Agent Loop 内的 structured action commitment / capability-level tool choice constraint
（实验性质；不指定具体测试命令；保持 Completion Obligation Gate）
```

## 附：为什么本地模型不验证（最终回答）

| 候选 | 结论 | 证据 |
|---|---|---|
| 工具语义不清（A2） | **已修，但不充分** | 旧 description 无验证语义；改进后仍 0 attempt |
| obligation 不够显著 | **已提高，但不充分** | 高显著 Runtime Fact 后仍 0 attempt |
| 工具竞争过大 | **已抑制，但不充分** | Focus 抑制无关能力后仍 0 attempt |
| 模型 action selection 能力不足 | **成立（主因）** | 工具已暴露+语义已改善+义务已高显著+无关能力已抑制，仍不选 VERIFICATION |

## 附：本轮修改文件

| 文件 | 改动 |
|---|---|
| `code_exec.py` | `run_python` 模型可见 description 加入测试/验证语义（Variant B） |
| `runtime/runner.py` | 高显著度 Verification-Due Runtime Fact；Variant C verification-focused 抑制 |
| `benchmark/microbenchmark.py` | first-verification-decision trace；fact/focus 模式；Gate 常开 |
| `benchmark/micro_driver.py` | Phase 13 条件驱动 |
