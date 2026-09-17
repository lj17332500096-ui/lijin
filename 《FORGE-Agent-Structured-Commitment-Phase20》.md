# FORGE Agent Structured Commitment — Phase 20 报告

> 日期：2026-09-12
> 主题：Checkpoint Fidelity + Verification Intent Truth + L1 Structured Action Commitment Causal Proof
> 基线：《FORGE-Agent-Local-Verification-Qualification-Phase19》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**；本轮不修改生产 Runtime。
> 模型入口：`FORGE_MODEL_PREF=local`，`FORGE_LOCAL_MODEL_BASE_URL=http://localhost:8080/v1`，
> 模型 `Qwen3.6-35B-A3B`（llama.cpp / IQ3_XXS）。

---

## 1. Executive Summary

本轮把 Phase 19 的三个真值缺口全部补齐，并完成 L1 Structured Action Commitment 的因果实验：

1. **Verification Selection 指标拆分**：正式区分
   - `Preferred Verification Tool Selection`（`selected_tool == run_tests`）
   - `Semantic Verification Intent`（真实 verification 语义，含 `run_python` 中的 pytest/unittest/assert/测试函数）
   - `Obligation-Satisfying Verification`（ExecutionEvidence + Completion Ledger 认定）。
2. **Phase 19 run_python 语义重分类**：Phase 19 artifact **没有持久化 tool arguments**（harness 缺陷），
   因此用 Phase 20 harness 以相同口径重跑并记录真实 args 后分类（`phase20/reclass`）。
3. **Harness 停止语义修复**：用 benchmark-only `DecisionCaptured(BaseException)` sentinel
   在捕获第一项模型动作后主动停止；`MaxTurnsExceeded` 不再作为正常 probe 结果。
4. **Live Snapshot**：用真实 Full Agent run + `RunHooks.on_llm_start` 抓取
   “mutation 成功、`verification_due=true`、下一次模型调用尚未开始”的真实模型输入
   （system prompt + 真实 message history + 真实 tool definitions + RunContext + revision + workspace hash）。

**核心结论（LIVE_SNAPSHOT，N=10）**：

| Checkpoint | Preferred (`run_tests`) | Semantic Verification Intent | Discovery/Read Drift |
|---|---:|---:|---:|
| M1-post-mutation | **0/10** | **0/10** | 4/10 |
| M4-post-mutation | **4/10** | **5/10** | 5/10 |
| Combined | **4/20 (20%)** | **5/20 (25%)** | 9/20 (45%) |

**Checkpoint Fidelity**：synthetic 与 live 存在系统偏差（M1 synthetic 40–60% vs live 0%）→
**Checkpoint validity = PARTIAL**；synthetic 不再作为生产行为真值。

**L1 因果结论（Case D）**：即使 benchmark-only **Oracle Commitment = VERIFY**，
M1 的 verification action 仍为 **0/5**，M4 为 **2/5**；commitment 不能约束随后真实 tool choice。
→ **L1 causal effect = NOT PROVEN；L1 production candidate = NO。**
→ 下一方向不是 Action Commitment，而是 **Capability-Focused Exposure / Choice Constraint**。

**其余**：Controls M6/M7/M8 N=2 全 pass（Unnecessary Verification = 0、L1 activation = 0）；
Runtime 回归 **PASS（715 tests, exit 0）**；False Completion = 0；M3 未执行；M1/M4 Full E2E 未执行。

---

## 2. Phase 19 Metric Correction

Phase 19 把 `Verification Selection` 直接等同于 `run_tests selection`，口径不完整。Phase 20 正式拆分：

| 口径 | 定义 | 用途 |
|---|---|---|
| **A. Preferred Verification Tool Selection** | `selected_tool == run_tests` | 首选纯 verification capability |
| **B. Semantic Verification Intent** | `run_tests` ∪ 真实 verification 语义的 `run_python`（pytest/unittest/assert/test 函数/验证脚本） | 模型是否“想验证” |
| **C. Obligation-Satisfying Verification** | ExecutionEvidence + Completion Ledger 认定的真实 evidence | 生产完成资格 |

三者禁止混用。§六明确：**run_tests 仍是首选**，`run_python` verification 属 **indirect verification**，
不因 Phase 20 重新分类回 `VERIFICATION`（`spec.capability_of("run_python") == "CODE_EXECUTION"` 保持不变）。

---

## 3. run_python Semantic Classification

Phase 19 artifact（`phase19/partial.json`）**只记录 `first_tool` / `capability`，未记录 tool arguments**，
因此无法离线分类；这是 Phase 19 harness 的真值缺陷，本轮已修复（`Decision Probe` 现在记录
`tool_name / tool_capability / normalized_args / semantic_action_class`）。

以相同 checkpoint 口径重跑（`phase20/reclass`，N=10）后，`run_python` 的真实 arguments 与分类：

| 来源 | case | args（节选） | 分类 |
|---|---|---|---|
| reclass | M1#6 | `sys.path.insert(0,'.'); from calc import add; print(add(2,3))`，filename=`test_calc.py` | **SEMANTIC_VERIFICATION** |
| reclass | M1#10 | `filename=calc.py`（无 code/args） | OTHER |
| reclass | M4#3 | `from calc import add,multiply; print("Testing ...")` | DISCOVERY |
| reclass | M4#7 | `assert add(2,3)==5; assert multiply(2,3)==6` | **SEMANTIC_VERIFICATION** |
| reclass | M4#9 | `sys.exit(__import__('pytest').main(['-q','-x']))` | **SEMANTIC_VERIFICATION** |
| live baseline | M1#2 | `from calc import add,multiply; print(...)` | DISCOVERY |
| live baseline | M4#2 | `assert add(2,3)==5 ... print("所有测试通过")` | **SEMANTIC_VERIFICATION** |

→ 结论：`run_python` 既可能是 verification，也可能是 discovery；**必须看 arguments**，不能只看 tool_name。

---

## 4. Preferred vs Semantic Verification

**Phase 19 口径重跑（SYNTHETIC，N=10）**：

| Checkpoint | Preferred (`run_tests`) | Semantic Verification Intent | Discovery Drift |
|---|---:|---:|---:|
| M1 | 6/10 (60%) | **7/10 (70%)** | 1/10 |
| M4 | 6/10 (60%) | **8/10 (80%)** | 1/10 |
| Combined | 12/20 (60%) | **15/20 (75%)** | 2/20 |

**Phase 20 正式基线（LIVE_SNAPSHOT，N=10）**：

| Checkpoint | Preferred (`run_tests`) | Semantic Verification Intent | Discovery Drift |
|---|---:|---:|---:|
| M1 | 0/10 | **0/10** | 4/10 |
| M4 | 4/10 | **5/10** | 5/10 |
| Combined | 4/20 (20%) | **5/20 (25%)** | 9/20 (45%) |

两者差异巨大（synthetic 75% vs live 25%）→ **Synthetic checkpoint 高估 verification intent**。

---

## 5. Decision Harness Stop Semantics

Phase 19 使用 `Runner.run(..., max_turns=1)`，每次 probe 最终抛 `MaxTurnsExceeded`，
把 provider/runtime 指标污染成“异常”。Phase 20 修复：

- 新增 benchmark-only `DecisionCaptured(BaseException)` sentinel；
- `ProbeHooks.on_llm_end` 捕获模型 response 中的 `function_call` 后立即 `raise DecisionCaptured()`，
  在**真实工具执行之前**停止；
- `max_turns=12`（仅作安全上限，不再作为正常完成方式）；
- **不修改**生产 `Runner` 的 `MaxTurnsExceeded` / terminal 语义；sentinel 只存在于 benchmark 进程。

结果：本轮所有 qualification probe 的 `status_counts` 均为 `DECISION_CAPTURED`（或对照的 `NO_ACTION`），
**`MaxTurnsExceeded = 0`**。

---

## 6. Live Snapshot Design

`benchmark/decision_qualification.py::capture_live_snapshot`：

1. 真实 `AgentRuntime.run_turn(prompt, mode="async", max_turns=16)`（真实 Tool Router / 真实工具 / 真实本地模型）；
2. benchmark 层 monkeypatch `main.Runner` → 注入 `SnapshotHooks`（生产 runtime 代码不变）；
3. `on_tool_end` 跟踪真实执行工具；`on_llm_start` 读取 `runtime.runctx.current()`，
   当 `verification_due()==True` 且 `mutation_seen==True` 时：
   - 保存 `system_prompt`、`input_items`（真实 message history + 真实 tool output）、
     `tool_schemas`、`runctx.as_dict()`、`revision`、`workspace_hash`、`model_config`；
   - 抛 `SnapshotCaptured`，**在下一次模型调用真正开始前**停止整个 Run（省去 20+ 分钟 full run）。
4. auto-approve 待审批工具后 resume，保证能走到 mutation 之后。

**不保存任何 benchmark-only 内容**；保存的全部来自真实 model input。

---

## 7. Checkpoint Fidelity

Live Snapshot 保存项（`phase20/snapshots/M{1,4}.json`）：

```text
user goal / system prompt / message history / actual tool-call history
actual tool outputs / RunContext relevant fields / obligation ledger
current revision / verification_due / tool definitions / tool schema hashes
tool exposure list / runtime facts / model configuration / workspace hash
```

重新加载并校验（`validate_fidelity`）：`goal_hash / system_prompt_hash / message_history_hash /
tool_definitions_hash / revision / verification_obligation / tool_exposure / model_config_local /
workspace_hash` → **M1 9/9、M4 9/9 全部一致**（`phase20/ab/result.json` → `fidelity`）。

但发现一个真实 fidelity 差异：**synthetic checkpoint 工具暴露 = 14，live snapshot = 120**
（live 真实 run 里 MCP/技能工具被 materialize，模型实际面对 120 个工具）。
A/B 已通过“synthetic 复用 live 的 system prompt + tool_schemas”隔离该 confound。

---

## 8. Synthetic vs Live Comparison

`phase20/ab`（每 checkpoint N=5，受控：同一 system prompt + 同一工具暴露，只比较 message history 重建）：

| Checkpoint | Synthetic pref | Synthetic sem | Live pref | Live sem | Discovery drift Δ |
|---|---:|---:|---:|---:|---:|
| M1 | 2/5 (40%) | 2/5 (40%) | 0/5 | 0/5 | 0.6 vs 0.6 |
| M4 | 2/5 (40%) | 2/5 (40%) | 2/5 (40%) | 3/5 (60%) | 0.6 vs 0.4 |

**Checkpoint validity**：M1 = **PARTIAL**（pref Δ=0.40）；M4 = **PASS**（最大 Δ=0.20）。
**Overall = PARTIAL**。→ 生产行为因果结论**只用 LIVE_SNAPSHOT**；synthetic 保留仅作快速回归。

---

## 9. Final Baseline

以 LIVE_SNAPSHOT（N=10）为 Phase 20 正式 Baseline：

| 指标 | M1 | M4 | Combined |
|---|---:|---:|---:|
| Preferred Verification Tool Rate (`run_tests`) | 0/10 | 4/10 | 4/20 (20%) |
| Semantic Verification Intent Rate | 0/10 | 5/10 | 5/20 (25%) |
| Discovery Drift Rate | 4/10 | 5/10 | 9/20 (45%) |
| Read Drift Rate | 0/10 | 0/10 | 0/20 |
| Mutation Rate（首个动作即再修改） | 5/10 | 0/10 | 5/20 (25%) |
| Final Attempt Rate | 0/10 | 0/10 | 0/20 |
| Provider error / timeout | 0/10 | 0/10 | 0/20 |
| Decision latency avg / P50 / P95 (s) | 38.3 / 16.61 / 143.72 | 14.64 / 11.7 / 25.89 | — |

M1 首动作分布：`write_project_file 4、edit_project_file 1、run_python 1、read_workspace_file 3、list_workspace_files 1`。
M4 首动作分布：`run_tests 4、run_python 1、list_workspace_files 5`。

→ **Baseline semantic verification = 5/20 (25%)，远低于 90%**。

---

## 10. L1 Entry Decision

§十八/§十九进入条件：LIVE checkpoint 的 Semantic Verification Intent 明显低于 90% → **满足**（25%）。
因此本轮正式执行 L1 Structured Action Commitment（harness-only）。

---

## 11. Structured Action Commitment Design

- Action enum 严格限定：`VERIFY / VERIFICATION_PREPARATION / REVISE / BLOCKED`；`verification_due=true` 时禁止 `FINAL`。
- 禁止生成步骤列表 / 计划 / 文件列表 / 命令 / 工具名 / patch —— 只允许一个 action class（不是 Planner）。
- 两步实验（§二十三，因果实验，非生产方案）：
  - **Step 1**：同一本地模型（tools=[]）只输出 `next_action_class`；
  - **Step 2**：把 `[Runtime Fact] Committed next action: <class>` 加入**相同 checkpoint**，
    再用真实 decision probe 观察第一项真实 tool choice。
- Oracle 对照（benchmark-only，非产品行为）：固定 `Committed next action: VERIFY`。

---

## 12. Self Commitment

`phase20/l1`，Self，N=5：

| case | commitments | VERIFY commit rate |
|---|---|---:|
| M1 | `null×2, REVISE×2, VERIFICATION_PREPARATION×1` | **0/5** |
| M4 | `VERIFY×2, VERIFICATION_PREPARATION×2, null×1` | **2/5** |
| Combined | — | **2/10 (20%)** |

注：M1 有 2 次 commitment 解析失败（模型未输出合法 action class），属 Step 1 parser 局限。

---

## 13. Oracle Commitment

`phase20/l1`，Oracle = 强制 `VERIFY`，N=5：

| case | commitment | verification action | follow-through |
|---|---|---:|---:|
| M1 | VERIFY 5/5 | **0/5** | 0/5 |
| M4 | VERIFY 5/5 | **2/5** | 2/5 |
| Combined | 10/10 | **2/10 (20%)** | 2/10 |

→ **即使明确 commit=VERIFY，M1 仍全部选择 `read_workspace_file`**；Oracle 无法把 verification 拉到 90%。

---

## 14. Commitment Follow-Through

- Self：`commit==VERIFY` 仅出现在 M4（2 次）→ honored 1/2（run#2 run_tests=honored；run#3 list_workspace_files=violated）。
  **Self follow-through = 1/2 (50%)**。
- Oracle：`VERIFY 10/10` → honored 2/10（全部来自 M4 的 run_tests）。**Oracle follow-through = 2/10 (20%)**。
- M1 无论 Self 还是 Oracle，follow-through 均为 0（动作全是 discovery/read）。

---

## 15. M1 Results

| 条件 | Preferred | Semantic | VERIFY commit | Follow-through |
|---|---:|---:|---:|---:|
| Live Baseline (N=10) | 0/10 | 0/10 | n/a | n/a |
| Self L1 (N=5) | 0/5 | 0/5 | 0/5 | 0/0 |
| Oracle VERIFY (N=5) | 0/5 | 0/5 | 5/5 | 0/5 |

→ M1 在 baseline 已经 0% verification；L1 无法改善。首动作集中在 `write_project_file` / `read_workspace_file`。

---

## 16. M4 Results

| 条件 | Preferred | Semantic | VERIFY commit | Follow-through |
|---|---:|---:|---:|---:|
| Live Baseline (N=10) | 4/10 | 5/10 | n/a | n/a |
| Self L1 (N=5) | 4/5 | 4/5 | 2/5 | 1/2 |
| Oracle VERIFY (N=5) | 2/5 | 2/5 | 5/5 | 2/5 |

→ M4 Self 看似 80%，但主要是模型本身就在选 `run_tests`（与 commitment 弱相关）；
Oracle 反而只有 40%（低于 baseline 50%）。**无稳定因果收益**。

---

## 17. Latency Cost

| 条件 | commit latency avg | decision latency avg | total latency avg |
|---|---:|---:|---:|
| Live Baseline M1 / M4 | — | 38.3 / 14.64 | 38.3 / 14.64 |
| Self L1 M1 / M4 | 55.18 / 56.58 | 77.94 / 95.04 | **133.12 / 151.62** |
| Oracle L1 M1 / M4 | 0.0 | 15.56 / 18.03 | 15.56 / 18.03 |

**Additional latency（Self，每 run）**：M1 ≈ +94.8s，M4 ≈ +137.0s，**combined ≈ +116s/run**，
其中额外的 commitment 模型调用本身 ≈ **+56s**。本地模型本已慢，此成本不可忽略。

---

## 18. Controls

`phase20/controls`，M6/M7/M8，N=2，`verification_due=false`：

| case | first tools | Unnecessary Verification | L1 activation |
|---|---|---:|---:|
| M6（read-only） | `NO_ACTION ×2` | 0/2 | 0/2 |
| M7（mutation, no verify） | `run_python ×2`（DISCOVERY/OTHER） | 0/2 | 0/2 |
| M8（keyword false positive） | `read_workspace_file ×2` | 0/2 | 0/2 |

→ **Unnecessary Verification = 0；L1 activation = 0；Control regression = 0**。
`verification_due=false` 时 harness 不注入 commitment，也未出现多余 verification。

---

## 19. Full E2E Decision

**NOT RUN**。§三十四：只有 L1 Decision Probe 达标（≥90%）才跑 M1/M4 Full E2E N=3。
L1 未达标（Oracle 20%），故**不浪费长 Run**。

---

## 20. M3 Decision

**NOT RUN**。§三十五：只有 M1/M4 verification selection 在 Baseline 或 L1 达到可靠标准才进入 M3；
当前 25%（live）远未达标。

---

## 21. Production Candidate

**NO**。L1 不满足 §三十一生产采用条件：

```text
Semantic Verification >= 90%   -> 20%（Oracle）/ 40%（Self），FAIL
Follow-Through >= 90%          -> 20% / 50%，FAIL
Control regression = 0         -> PASS
False Completion = 0           -> PASS
额外 latency 可接受            -> +116s/run，FAIL
```

→ L1 仅保留实验结论；**不进入生产，不 merge 进主 Runtime**。

---

## 22. Runtime Regression

- 本轮**未修改** `runtime/`（仅新增 `benchmark/decision_qualification.py` 与 `phase20/` artifacts）。
- 确定性回归：`regression.ps1` → **PASS（ran=715, skipped=0, exit 0）**。
  （脚本内置基线为 538；实际 715 为测试集增长导致的基线漂移 warning，不影响 exit 0。）
- Completion Gate / Obligation Gate / Repair / Approval / FileScope / Tool Budget / run_tests 实现均未触碰。
- **False Completion = 0**；**False Failure after satisfied obligations = 0（确定性）**。

---

## 23. Local Microbenchmark Status

**NO**。M1/M4 live semantic verification = 25%，未达门槛；M3 未执行；L1 无因果收益。
`run_tests` 保持 **EXPERIMENTAL**。

---

## 24. Next Direction

因果实验明确指向 **Case D**（§二十八）：

> Oracle VERIFY 仍低 → 不要继续 L1；后续转向 **Capability-Focused Exposure / Choice Constraint**。

具体机制：在 `verification_due=true` 的 decision turn，**收缩暴露的工具集**为
verification-capable 集合（如 `run_tests` / `run_python`）+ 必要的 `REVISE`，
而不是给模型 120 个工具让它自由漂移。Live snapshot 显示：**工具暴露从 14 膨胀到 120** 与
M1 的 0% verification 高度相关，这是比 action commitment 更上游的瓶颈。

---

## 最终输出

```text
M1 preferred run_tests rate =
0/10（LIVE_SNAPSHOT，N=10；synthetic 重跑为 6/10）

M1 semantic verification rate =
0/10（LIVE_SNAPSHOT，N=10；synthetic 重跑为 7/10）

M4 preferred run_tests rate =
4/10（LIVE_SNAPSHOT，N=10；synthetic 重跑为 6/10）

M4 semantic verification rate =
5/10（LIVE_SNAPSHOT，N=10；synthetic 重跑为 8/10）

Checkpoint validity =
PARTIAL（M1 PARTIAL：synthetic 40% vs live 0%；M4 PASS；工具暴露 14 vs 120）

Baseline semantic verification =
5/20（LIVE，M1 0/10 + M4 5/10 = 25%）

Self-Commit VERIFY rate =
2/10（M1 0/5 + M4 2/5）

Self-Commit verification rate =
4/10（M1 0/5 + M4 4/5）

Oracle VERIFY verification rate =
2/10（M1 0/5 + M4 2/5）

Commitment follow-through =
self 1/2（50%）；oracle 2/10（20%）

Additional latency =
~+116s/run（Self：M1 +94.8s、M4 +137.0s；其中额外 commitment 调用 ~+56s）

L1 causal effect =
NOT PROVEN（Case D：Oracle VERIFY 仍仅 20%，commitment 不约束真实 tool choice）

L1 production candidate =
NO

Full E2E needed =
NO

Primary remaining bottleneck =
verification_due=true 时模型面对全量工具暴露（120 个）发生 tool-selection drift；
即使显式 commit=VERIFY，真实 tool choice 仍不受约束（action-class 与 tool action 脱钩）

Next component to modify =
Capability-Focused Exposure / Choice Constraint
（verification_due 时收缩暴露工具集为 verification-capable，而非继续 Action Commitment）
```

---

## 附：本轮新增/修改文件

| 文件 | 改动 |
|---|---|
| `benchmark/decision_qualification.py` | **新增** Phase 20 Harness：语义分类 / DecisionCaptured sentinel / args 捕获 / LIVE_SNAPSHOT / Fidelity / L1 Self+Oracle / Controls |
| `phase20/snapshots/M1.json`、`M4.json` | **新增** LIVE_SNAPSHOT_CHECKPOINT（真实 Full Agent model-turn input） |
| `phase20/reclass/` | **新增** Phase 19 口径重跑 + run_python 真实 args 分类 |
| `phase20/ab/` | **新增** Synthetic vs Live A/B + Checkpoint validity + 工具暴露对比 |
| `phase20/baseline/` | **新增** Phase 20 LIVE Baseline（N=10） |
| `phase20/l1/` | **新增** L1 Self / Oracle Commitment |
| `phase20/controls/` | **新增** M6/M7/M8 controls |

> 生产 `runtime/` 无任何改动。`run_tests` 保持 EXPERIMENTAL。
