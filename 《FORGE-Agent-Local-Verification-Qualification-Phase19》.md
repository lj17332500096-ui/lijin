# FORGE Agent Local Verification Qualification — Phase 19 报告

> 日期：2026-09-11
> 主题：Local Decision Qualification Harness + Verification Reliability Acceptance
> 基线：《FORGE-Agent-Finalization-Repair-Phase18》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**；默认不改生产 Runtime。

---

## 1. Executive Summary

本轮交付一个 **Decision Qualification Harness（checkpoint-based decision probe）**：
在**不跑完整 coding run** 的前提下，重建“mutation 已完成、`verification_due=true`”的真实 checkpoint
（真实 RunContext / Tool Router / tool definitions / system prompt / 真实 mutation 工具结果 /
真实 obligation feedback），让**真实本地模型**做一次真实 decision turn，只观察第一项动作。

**核心结果（N=10/checkpoint）**：

| Checkpoint | run_tests 选择 | Verification Rate |
|---|---:|---:|
| M1-post-mutation | 3/10 | **30%** |
| M4-post-mutation | 6/10 | **60%** |
| Combined | 9/20 | **45%** |

→ **在真实 decision point，verification selection 不可靠**（single-tool Raw API/SDK 曾达 100%）。
即 **full-loop action selection** 是剩余瓶颈（不是 schema、不是抽象、不是 function calling）。

**其余**：Controls M6/M7/M8 N=2 全 pass、0 误触发；M4 E2E N=1 本轮漂移未验证（F4 路径未观测）；
M3 未执行。→ `run_tests` 保持 **EXPERIMENTAL**；**L1 Structured Action Commitment = YES（下一阶段）**。

---

## 2. Phase 18 Truth Corrections

| Phase 18 报告 | 修正为 |
|---|---|
| `M1 verification = 0/0` | **Observed M1 verification = 0/1；Qualification status = INSUFFICIENT SAMPLE** |
| `M3 closed loop = 0/3` | **M3 closed loop = NOT RUN**（未执行不得写 0/N） |

## 3. Qualification Harness Design

`benchmark/decision_checkpoint.py`：
- 用真实 `AgentRuntime.get_default().route_agent(prompt)` 得到真实 agent（真实 Tool Router + 真实工具定义 + 真实 system prompt）。
- 用真实 `write_code_file` 工具执行一次真实 mutation（`micro_fixture/calc.py`），得到真实工具结果。
- 用真实 `RunContext`（obligation ledger）计算 `evidence_epoch`、`verification_due`。
- 用真实 runtime obligation feedback 文本构造 checkpoint。
- `Runner.run(agent, input=checkpoint, hooks=<record first tool>, max_turns=1)` → 真实模型一次决策；
  只记录**第一项 tool start**（`RunHooks.on_tool_start`）。
- **不 mock 模型、不 scripted response、不 fake tool definitions、不提示具体工具。**

## 4. Checkpoint Semantics

Checkpoint = `[user prompt, function_call(write_code_file), function_call_output(mutation_result + obligation_feedback)]`。
绑定真实 RunContext；位于 `verification_due=true` 且模型尚未决策的位置。

## 5. Checkpoint Validation

构建时断言：`verification_due=True`（10/10 成立）、`revision=1`、mutation 工具结果为真实执行结果。
**与 Full Run 的交叉验证（§十四）仅部分完成**：M4 E2E 本轮漂移（无 mutation），未取得可对比的
first-post-mutation action → Checkpoint validity = **PARTIAL（未完全交叉验证）**。

## 6. M1 Checkpoint

first action 分布（N=10）：`run_tests 3`、`list_code_files 4`、`write_code_file 1`、
`run_python 1`、`list_workspace_files 1`。
→ run_tests = **3/10**；DISCOVERY/READ 漂移 = 5/10。

## 7. M4 Checkpoint

first action 分布（N=10）：`run_tests 6`、`run_python 3`、`read_workspace_file 1`。
→ run_tests = **6/10**；若把 run_python 计为 indirect verification，则 9/10。

## 8. Decision Probe Results

| 指标 | M1 | M4 |
|---|---|---|
| Verification (run_tests) | 3/10 | 6/10 |
| run_python（indirect） | 1/10 | 3/10 |
| Discovery drift | 5/10 | 1/10 |
| Premature final | 0/10 | 0/10 |
| Provider error | 0/10 | 0/10 |
| Avg latency | 21.1s | 11.8s |

## 9. Verification Selection Stability

**NOT RELIABLE**（M1 30%、M4 60%；combined 45%，远低于 90%）。
→ 在真实 decision point，本地模型仍常选 DISCOVERY/READ 或 run_python，而非 `run_tests`。

## 10. M1/M4 Difference Analysis

M4 明显高于 M1（60% vs 30%）。M1 漂移以 `list_code_files` 为主（重新探索代码）；
M4 漂移以 `run_python`（indirect verification）为主。说明 M1 的 context 更易触发再探索。
**两者都低（<90%）** → 属 §十七：full-loop action selection 正式确认；L1 留待下一阶段。

## 11. Provider Reliability

| 指标 | M1 | M4 |
|---|---|---|
| model calls | 10 | 10 |
| successful（返回 tool call 或 final） | 10 | 10 |
| provider error | 0 | 0 |
| MaxTurnsExceeded（max_turns=1 预期） | 10 | 10 |
| avg latency | 21.1s | 11.8s |

无 provider 失败；慢是本地推理延迟，非错误。

## 12. M4 Full E2E

N=1：MUTATION 0、VERIFICATION 0、DISCOVERY 5、READ 3、state=failed。
→ 本轮模型连 mutation 都未做（漂移），**未进入 F4 路径**。

## 13. F4 Live Regression

**NOT OBSERVED**（本轮 M4 E2E 未发生 mutation/verification，无法观测 obligation block → verify → final）。
F4 的修复仍由 Phase 18 确定性回归（5/5）保证。

## 14. M1 Full E2E

**NOT RUN**（Decision Probe 已显示 M1 selection 30%，按 §二十不浪费长 Run）。

## 15. M3 Full E2E

**NOT RUN**（进入条件未满足）。

## 16. M3 Failure Classification

不适用（未执行）。

## 17. Repair Semantics Live Proof

未取得（F4 路径未观测）。确定性层面：Phase 18 回归证明 obligation block 不消耗 completion repair。

## 18. Controls

M6/M7/M8 N=2：**全部 pass**，VERIFICATION 0，state=completed。
→ Control regression = **0**；Unnecessary Verification Rate = **0**；无 obligation false positive。

## 19. False Completion

**0**（Completion Obligation Gate 保持 ON）。

## 20. False Failure

满足义务后的 false failure = **0（确定性回归）**；live 未观测。

## 21. run_tests Qualification

| 条件 | 状态 |
|---|---|
| Tool Contract Security | ✅ PASS |
| Execution Boundary | ✅ KNOWN ACCEPTED LIMITATION |
| Raw API / Minimal SDK semantic | ✅ 100% |
| **Decision selection reliable（M1/M4 ≥90%）** | ❌ 30% / 60% |
| M4 live finalization no false failure | ❌ 未观测 |
| M3 ≥2/3 | ❌ NOT RUN |
| False Completion / Claim | ✅ 0 |
| Control regression | ✅ 0 |

→ **run_tests = EXPERIMENTAL**（仅差 decision selection 可靠性与 M4/M3 live 验证）。

## 22. Local Microbenchmark Baseline

**NO**（M1/M4 decision selection 未达门槛；M3 未执行）。

## 23. L1 Entry Decision

**YES**：Decision Probe 证明 `verification_due=true` + `run_tests` available 时，
本地模型仍稳定地（M1 70%、M4 40%）选择非 verification action → 满足 §十七 进入 L1 的条件。
**但本轮不实现 L1**（按 §二十四/§十七，留待下一阶段）。

## 24. Production Status

```
Runtime Semantic Baseline            = YES
run_tests Tool Contract Security     = PASS
run_tests Execution Boundary         = KNOWN ACCEPTED LIMITATION
Full Agent Verification Baseline     = NO（M1 3/10、M4 6/10）
Verification Repair Loop             = PASS（确定性；live NOT OBSERVED）
Finalization Closure                 = PASS（确定性）；live NOT OBSERVED
Local Microbenchmark Baseline        = NO
```

## 25. Next Direction

**L1 Structured Action Commitment**（下一阶段，实验性质，不新增 Planner）：
在 `verification_due=true` 的 decision turn 要求模型先提交 `next_action_class ∈
{VERIFY, VERIFICATION_PREPARATION, REVISE, BLOCKED}`（禁 FINAL），工具仍由模型自选；
验证能否把 M1/M4 decision selection 提升到 ≥90%。同时用 Checkpoint Harness 快速迭代，
并补齐与 Full Run 的交叉验证。

---

## 最终输出

```text
M1 decision verification rate =
3/10

M4 decision verification rate =
6/10

Combined verification selection =
9/20（45%）

Checkpoint validity =
PARTIAL（构建断言成立；与 Full Run 交叉验证未完成）

M4 Full E2E =
0/1（N=1 漂移，无 mutation/verification）

F4 live regression =
NOT OBSERVED

M1 Full E2E =
NOT RUN

M3 closed loop =
NOT RUN

False Completion =
0

False Failure after satisfied obligations =
0（确定性）

Tool Contract Security =
PASS

Execution Boundary Security =
KNOWN ACCEPTED LIMITATION

run_tests =
EXPERIMENTAL

Local Microbenchmark Baseline =
NO

L1 Structured Commitment needed =
YES

Primary remaining bottleneck =
full-loop verification action selection（decision point：verification_due + run_tests available 仍漂移）

Next component to modify =
L1 Structured Action Commitment（下一阶段实验；不新增 Planner；不指定具体命令）
```

## 附：修改/新增文件

| 文件 | 改动 |
|---|---|
| `benchmark/decision_checkpoint.py` | **新增** Decision Qualification Harness（真实 agent/工具/模型/obligation feedback） |
| `《FORGE-Agent-Finalization-Repair-Phase18》.md` | 修正真值口径（M1 0/1 INSUFFICIENT；M3 NOT RUN） |
