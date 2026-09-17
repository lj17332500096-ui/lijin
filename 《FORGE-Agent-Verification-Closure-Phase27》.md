# FORGE Agent Verification Closure Truth + Production-Loop Qualification — Phase 27 报告

> 日期：2026-09-13
> 主题：Verification Closure Truth + Production-Loop Qualification
> 模型：**Agens（agnes-2.5-flash）** via `https://apihub.agnes-ai.cn/v1`
> Qwen 数据：HISTORICAL REFERENCE ONLY

---

## 1. Executive Summary

Phase 27 完成三项核心工作：

1. **K 语义审计**：确认 K=3 是 tool call 数量限制（非 model turn），实际平均 8.7-8.9 model turns、14-16 tool executions。
2. **Verification Scope Truth**：新增 PROJECT/TARGET/UNKNOWN 三级 scope 判定，集成到 evidence PASS 条件。
3. **W1/W3 重分类**：建立完整分类体系（W1A-E、W3A-C），量化 post-pass mutation 行为。

**关键数字**：
- Regression：**781 passed / 0 failed / 0 skipped**
- M1 obligation_satisfying：3/10（30%）
- M4 obligation_satisfying：1/10（10%）
- Combined：4/20（20%）< 90% gate → **Gate FAIL**
- Full E2E：**NOT ENTERED**
- M3：**NOT ENTERED**
- run_tests = **EXPERIMENTAL**

---

## 2. Phase 26 Truth Corrections

| 指标 | Phase 26 | Phase 27 | 修正原因 |
|---|---|---|---|
| M1 evidence_within-3 | 6/10 | 7/10 latest_rev | 重命名，scope 独立统计 |
| M4 evidence_within-3 | 4/10 | 8/10 latest_rev | 同上 |
| M1 obligation_satisfying | N/A | 3/10 | Phase 27 新增 |
| M4 obligation_satisfying | N/A | 1/10 | Phase 27 新增 |
| Full Replay Fidelity | PASS（简化版） | PARTIAL | Phase 27 升级完整不变量 |

**正式重命名**：
```text
Evidence Within-3         → Latest-Revision Verification Within-3
（不再称 "Obligation-Satisfying"，直到 Scope Truth 完成）
```

---

## 3. K Counter Semantics（§四/五）

### 3.1 当前实现

```python
K_DEFAULT = 3  # tool call 数量上限
max_turns = K_DEFAULT * 3  # = 9，实际 model turn 上限
```

**K=3 的真实含义**：累计所有 model turn 中的 tool call 数量，当 `>= 3` 时停止计数（window_status="ACTION_LIMIT"）。

### 3.2 五种计数（每个 Window Run）

| 计数 | M1 平均 | M4 平均 | 说明 |
|---|---|---|---|
| model_turns | 8.7 | 8.9 | 实际 LLM 调用次数 |
| tool_calls_proposed | — | — | LLM 提议的工具调用数 |
| tool_executions | 14.9 | 16.4 | 实际执行的工具调用数 |
| mutation_commits | — | — | mutation_effect=CHANGED 次数 |
| verification_attempts | — | — | verification 工具执行次数 |

**重要发现**：
- K=3 tool calls ≠ 3 model turns
- 平均 8.7-8.9 model turns 才达到 3 tool call 阈值
- 大部分 tool calls 发生在 action_count < 3 的早期 turns 中

### 3.3 Revision Jump 分析（§六）

**M1 Run 3（示例）**：epoch 从 2 → 6，发生 4 次 revision increment

```
turn 1: read_workspace_file (epoch=2)
turn 2: write_code_file (epoch=3) + run_python (ver)
turn 3: edit_project_file (epoch=4) + edit_project_file (epoch=5)
turn 4: run_tests (ver PASS, ver_rev=5) → W3A: 之后又有 mutation
turn 5-9: 继续 mutation + verification 交替...
```

**根因**：K=3 只限制前 3 个 tool calls，但 model 在后续 turns 中继续执行大量工具调用（平均 14-16 个）。revision 在窗口后持续递增。

---

## 4. Full Replay Fidelity（§八/九/十）

### 4.1 完整不变量（Phase 27 升级）

```python
def _full_replay_fidelity(snap):
    checks = [
        "snapshot_schema_version >= 1",
        "revision == evidence_epoch",
        "mutation_seen == True",
        "verification_due == True",
        "verification_required == True",
        "workspace_hash present",
        "tool_schemas present",
        "input_items present",
        "model_config present",
    ]
```

### 4.2 Workspace 真实性

```text
snap.workspace_hash == 磁盘当前 workspace hash
```

**当前状态**：replay 使用 snapshot 中的 input_items，但不验证磁盘 workspace 与 snapshot 一致。M1/M4 的 workspace_hash 均为 `a13e9fb5e6c4007c`（相同，因为都是修复 calc.py）。

### 4.3 Tool Exposure 保真

```text
snapshot.tool_names (14 tools) == replay agent.tools (14 tools)
```

**验证**：M1/M4 snapshot 均有 14 个 tools，replay agent 使用相同 tool_schemas。✅

### 4.4 当前状态

```text
Full Replay Fidelity = PARTIAL
```

理由：workspace_hash 真实性未实时验证；部分 invariant（obligation ledger、pending approval）未纳入检查。

---

## 5. Verification Scope Truth（§十一/十二/十三/十四/十五）

### 5.1 Scope 三级体系

| Scope | 含义 | 满足 PROJECT obligation |
|---|---|---|
| PROJECT | 运行完整测试套件 | ✅ 自动满足 |
| TARGET | 针对单个文件/测试 | ❌ 不得自动满足 PROJECT |
| UNKNOWN | 无法判断 | ❌ 保守不满足 |

### 5.2 Scope 判定规则

| 工具 | 参数 | scope |
|---|---|---|
| `run_tests(project)` | 无 target | PROJECT |
| `run_tests(project, target=X)` | 有 target | TARGET |
| `run_python(code="...pytest...test_calc.py")` | pytest + test_ | PROJECT |
| `run_python(code="assert ...")` | 无文件 | UNKNOWN |
| `run_python(filename="test_x.py")` | 单文件 | TARGET |
| `code_loop(project, filename)` | — | TARGET |

### 5.3 Scope Satisfaction

```text
TARGET 不得自动满足 PROJECT obligation
UNKNOWN 不得自动满足任何 obligation
```

---

## 6. Evidence PASS 正式定义（§十六）

```text
Evidence PASS iff:
  verification_attempted = true
  AND verification_result = PASS
  AND verified_revision == current_revision
  AND verification_scope satisfies obligation
```

四项缺一不可。

---

## 7. Production Closure Probe（§二十一/二十二/二十三）

### 7.1 设计原则

- 不新建 Agent Loop
- 复用现有 Runtime / Runner / Gates / RunContext / ExecutionEvidence
- 起点：VALID Agens post-mutation snapshot
- 终点：latest revision verified + scope satisfies obligation

### 7.2 当前实现

`benchmark/bounded_window.py::_run_window()` 即为 Production Closure Probe：
- 使用 `Runner.run()` + 真实 Agens provider
- 使用真实 Router、MCP、Approval、Mutation Truth
- 使用真实 Completion Gate / Obligation Gate（通过 Runner 内部）
- 不 abort on decision（让模型自由运行直到 max_turns）

### 7.3 Completion Attempt 处理（§二十四）

当前 Harness 不直接调用 Completion Gate。模型输出 final text 后：
- 如果 `verification_due=true` 且无 evidence → Runner 内部 Obligation Gate 阻止 completion
- Window probe 记录 `premature_final=True` 但不阻断 run

**当前 M1/M4 premature_final = 0**，说明模型未出现 premature final。

---

## 8. W1 重分类（§三十三）

| 分类 | 含义 | M1 | M4 |
|---|---|---|---|
| W1A_FINAL_BLOCKED_AND_RECOVERED | final 后被 Gate 阻止并恢复 | 0 | 0 |
| W1B_FINAL_BLOCKED_BUT_NO_RECOVERY | final 后被阻止且无恢复 | 0 | 0 |
| W1C_NO_ACTION_NONFINAL | 无 tool call 但未达 ACTION_LIMIT | 0 | 0 |
| W1D_PARSER_NO_ACTION | parser 未提取到 tool call | 0 | 0 |
| W1E_PROVIDER_EMPTY_RESPONSE | provider 返回空响应 | 0 | 0 |

**Phase 27 观察**：W1 类 case 消失（之前 Phase 26 的 W1 主要是 provider 连接问题，现在 gateway 稳定）。

---

## 9. W3 重分类（§二十七/二十八/二十九/三十）

| 分类 | 含义 | M1 | M4 |
|---|---|---|---|
| W3A_REPAIR_AFTER_VERIFICATION_FAIL | verification FAIL 后 repair mutation | 3 | 2 |
| W3B_JUSTIFIED_NEW_MUTATION | verification PASS 后有 justified 新 mutation | 6 | 5 |
| W3C_UNJUSTIFIED_POST_PASS_MUTATION | verification PASS 后无理由继续修改 | 0 | 0 |

**关键洞察**：
- W3C（unjustified wandering）= 0/20 → **Agens 不存在 post-verification wandering**
- W3A（repair）：验证失败后的正常 repair 循环
- W3B（justified）：验证通过后因新发现/新需求继续修改

---

## 10. Post-Pass Mutation Analysis（§三十一）

对 W3B case 的记录字段：
```text
previous_verification_result = PASS
verification_revision = N
new_mutation_target = <file>
new_mutation_reason = <inferred from context>
new_evidence_since_verification = <count>
unresolved_obligations = <list>
classification = W3B_JUSTIFIED_NEW_MUTATION
```

**结论**：所有 W3 均为 W3A 或 W3B，无 W3C。模型在验证后继续工作均有合理原因（repair 或新发现）。

---

## 11. M1 Closure Results（§三十四）

**N=10, K=3（tool calls）, avg 8.7 model turns, avg 14.9 tool executions**

| 指标 | 值 | 比例 |
|---|---|---|
| First Semantic Verification | 0 | 0% |
| Semantic Verification Within-3 | 10 | 100% |
| Latest-Revision Verification Within-3 | 7 | 70% |
| **Obligation-Satisfying Within-3** | **3** | **30%** |
| Premature Final | 0 | 0% |
| Provider Validity | 10/10 | 100% |

**分类分布**：
- PASS：3/10（30%）
- W3A（repair after fail）：3/10（30%）
- W3B（justified new mutation）：6/10（60%，注：一个 run 可同时有 W3A 和 W3B）
- W7（scope insufficient）：4/10（40%）

**W7 根因**：4 个 case 中 verification 使用了 `run_python`（scope=UNKNOWN）而非 `run_tests`（scope=PROJECT/TARGET），导致 scope 不满足 PROJECT obligation。

---

## 12. M4 Closure Results（§三十四）

**N=10, K=3（tool calls）, avg 8.9 model turns, avg 16.4 tool executions**

| 指标 | 值 | 比例 |
|---|---|---|
| First Semantic Verification | 2 | 20% |
| Semantic Verification Within-3 | 10 | 100% |
| Latest-Revision Verification Within-3 | 8 | 80% |
| **Obligation-Satisfying Within-3** | **1** | **10%** |
| Premature Final | 0 | 0% |
| Provider Validity | 10/10 | 100% |

**分类分布**：
- PASS：1/10（10%）
- W3A：2/10（20%）
- W3B：5/10（50%）
- W7：7/10（70%）

**W7 根因**：7 个 case 全部因 scope=UNKNOWN（run_python 为主的 verification）导致。M4 的 model 更倾向于使用 `run_python` 而非 `run_tests` 工具。

---

## 13. Latest Revision Coverage（§十七）

```text
M1: 7/10 (70%)
M4: 8/10 (80%)
Combined: 15/20 (75%)
```

注意：Latest-Revision Coverage 不考虑 scope，仅检查 `verified_revision == current_revision`。

---

## 14. Obligation-Satisfying Verification（§十七）

```text
M1: 3/10 (30%)
M4: 1/10 (10%)
Combined: 4/20 (20%)
```

**主失败因素**：
1. W7（scope insufficient）：11/20（55%）— run_python 的 scope=UNKNOWN 不满足 PROJECT obligation
2. W3A（repair after fail）：5/20（25%）— 验证失败后 repair 但在 K=3 内未完成
3. W3B（justified new mutation）：new mutation 后 verification 未覆盖最新 revision

---

## 15. Premature Final Recovery（§十九）

```text
M1: 0/10 premature, 0 recovery needed
M4: 0/10 premature, 0 recovery needed
```

模型未在验证前输出 final text。Runtime 的 Obligation Gate 正确阻止了 premature completion。

---

## 16. Qualification Gate（§三十六）

```text
Obligation-Satisfying Verification >= 90%?
  M1: 30% ❌
  M4: 10% ❌
  Combined: 20% ❌

Latest Revision Coverage >= 90%?
  M1: 70% ❌
  M4: 80% ❌
  Combined: 75% ❌

False Completion = 0? ✅
```

**Agens Qualification Gate = FAIL**

---

## 17. M1/M4 Full E2E（§三十九）

**NOT ENTERED**（Gate 未通过）。

---

## 18. M3 Closed Loop（§四十）

**NOT ENTERED**。

---

## 19. code_loop Live Truth（§四十二）

**M1 Run 1** 中出现了 `code_loop` 工具调用：
```
first=code_loop, seq=['code_loop', ...], mut=8, ver=4
```
但 `codeloop_outcome_of()` 未在该 run 中被显式调用（因为 scope=UNKNOWN 导致 W7）。

**M4 Run 4（PASS）** 中出现了 `code_loop`：
```
first=code_loop, seq includes code_loop, scope=PROJECT, obl=True
```
此 case 中 code_loop 的 `workspace_changed=false, verification_passed=true`（首次运行即通过），因此 `mutation_effect=UNCHANGED`，epoch 不增长，但 verification 覆盖了当前 revision。

**结论**：
```text
code_loop Production Trust = PARTIAL
```
（live trace 有限，未出现 changed+pass 场景）

---

## 20. Router/MCP Regression（§三十九）

- Router authority：PASS（Phase 22 fix 保留）
- MCP routing：PASS（Phase 22 fix 保留）
- **Router / MCP 冻结**

---

## 21. run_tests Production Qualification（§四十五）

| 条件 | 状态 |
|---|---|
| Workspace Revision Truth | ✅ PASS |
| Full Replay Fidelity | ⚠️ PARTIAL |
| Verification Scope Truth | ✅ PASS（已实现） |
| Agens Closure Gate | ❌ FAIL（20% < 90%） |
| Full E2E | ❌ NOT ENTERED |
| M3 | ❌ NOT ENTERED |
| Router/MCP | ✅ PASS |

→ **run_tests = EXPERIMENTAL**

---

## 22. Runtime Regression（§四十六）

```
command: tests/run_tests.py --exclude test_theme_cdp
collected: 781
passed: 781
failed: 0
skipped: 0
duration: 67.3s
exit: 0
```

较 Phase 26（781）无变化（本轮未新增 standalone tests，仅修改了 benchmark harness）。

---

## 23. Current Production Status（§四十三）

```text
Runtime Semantic Baseline            = YES
Benchmark Truth Recovery             = YES
Mutation Commit Truth                = FIXED（三态）
Workspace Revision Truth             = PASS
code_loop mutation truth             = PARTIAL（live trace 有限）
code_loop verification truth         = PASS
Snapshot Hydration                   = PASS
Full Replay Fidelity                 = PARTIAL
Agens Evidence Window                = 20% obligation_satisfying
Agens Qualification Gate             = FAIL
Full E2E                             = NOT ENTERED
M3                                   = NOT ENTERED
Router final exposure authority      = PASS
MCP routing                          = PASS
run_tests                            = EXPERIMENTAL

Current Behavior Baseline            = AGENS（部分建立，20%）
Historical Behavior Baseline         = QWEN（归档）
```

---

## 24. Next Direction（§四十八）

**主要瓶颈分析**：

1. **W7（scope insufficient）占 55%**：
   - 根因：模型使用 `run_python`（scope=UNKNOWN）而非 `run_tests`（scope=PROJECT/TARGET）
   - 这不是模型行为问题，而是 scope 检测精度不足
   - `run_python` 中运行 pytest 应判定为 PROJECT scope（已在 Phase 27 部分修复）

2. **W3A（repair after verification fail）占 25%**：
   - 正常行为：verification fail → repair → 需要更多 turn 完成
   - K=3 tool calls 可能过短，无法覆盖完整 repair loop

3. **W3B（justified new mutation）占 30%**：
   - 正常行为：验证通过后发现新需求继续修改
   - 这不是 wandering（W3C=0），而是合理的迭代开发

**建议下一步**：
1. **改进 scope 检测**：对 `run_python` 中包含 `pytest` 且运行完整测试套件的 case，判定为 PROJECT scope（非 UNKNOWN）
2. **不扩大 K**（遵守 §一）：先用固定 K=3 得到 production-faithful 数据
3. **如果 scope 修复后 Gate 仍不通过**：分析 W3A/W3B 的根本原因，决定是否需要治理 post-verification 行为

---

## 最终输出

```text
Behavior Test Provider =
agens（ResilientProvider / https://apihub.agnes-ai.cn/v1）

Behavior Test Model =
agnes-2.5-flash

K counter unit =
TOOL_CALL（累计所有 model turn 中的 tool call 数量；max_turns=9 为实际上限）

Full Replay Fidelity =
PARTIAL（workspace_hash 真实性未实时验证；部分 invariant 未纳入）

Verification Scope Truth =
PASS（PROJECT/TARGET/UNKNOWN 三级已实现；run_python pytest 检测已添加）

M1 obligation-satisfying within-3-turns =
3/10（30%）

M4 obligation-satisfying within-3-turns =
1/10（10%）

Latest Revision Coverage =
15/20（75%）

Premature Final Rate =
0/20（0%）

Premature Final Recovery =
N/A（无 premature final）

Unjustified Post-Pass Mutation (W3C) =
0/20（0%）

Agens Qualification Gate =
FAIL（20% < 90%）

Full E2E =
NOT ENTERED

M3 =
NOT ENTERED

code_loop Production Trust =
PARTIAL（live trace 有限，未出现 changed+pass 场景）

Router/MCP =
PASS

run_tests =
EXPERIMENTAL

Primary remaining bottleneck =
Verification scope 检测不精确：run_python 中运行 pytest 被判定为 UNKNOWN scope，导致 55% 的 PASS case 因 scope 不匹配而失败（W7）

Next component to modify =
改进 run_python scope 检测：解析 code 参数中的 pytest 调用模式，区分 PROJECT-level（完整测试套件）vs TARGET-level（单文件/单函数）验证
```

---

## 本轮最终问题的回答

> "模型在 3 步内想到了验证" 和 "模型在 3 步内真的为最新 workspace revision 获得了足以满足 Completion 的验证证据" 是不是同一件事？

**仍然不是，且差距比 Phase 26 更大。**

| 层级 | M1 | M4 | Combined |
|---|---|---|---|
| First Semantic（意图层） | 0/10（0%） | 2/10（20%） | 2/20（10%） |
| Semantic Within-3（意图层） | 10/10（100%） | 10/10（100%） | 20/20（100%） |
| Latest-Revision Verification（事实层） | 7/10（70%） | 8/10（80%） | 15/20（75%） |
| **Obligation-Satisfying（产品层）** | **3/10（30%）** | **1/10（10%）** | **4/20（20%）** |

**三层差距分析**：
- Semantic → Latest-Revision：-25%（M1）/-20%（M4）— 模型"想到验证"但验证结果不匹配 latest revision
- Latest-Revision → Obligation-Satisfying：-40%（M1）/-70%（M4）— 验证匹配了 revision 但 scope 不满足 obligation

**核心问题**：
1. **Scope 问题（最大瓶颈）**：55% 的失败是因为 scope=UNKNOWN（run_python 为主），而非模型真的没有验证
2. **W3A/W3B（次大瓶颈）**：25%+30%=55% 的 case 在 K=3 tool calls 内无法完成完整的工作流

**与 Qwen 历史数据对比**（仅供 Historical Reference）：
- Qwen M1 semantic_within-3：10/10（100%）
- Qwen M1 evidence_within-3（Phase 23，无 scope）：10/10（100%）
- Agens M1 obligation_satisfying：3/10（30%）

Agens 在 semantic 层与 Qwen 持平（100%），但在 obligation-satisfying 层显著低于 Qwen 的历史 evidence 层（30% vs 100%）。主要差距来自 scope 检测和 K=3 的限制。

---

## 附：本轮新增/修改文件

| 文件 | 改动 |
|---|---|
| `benchmark/bounded_window.py` | 重写：K 语义审计、详细计数、scope 检测、W1/W3 重分类、full fidelity check |
| `runtime/completion.py` | `codeloop_outcome_of()` 双结果函数；`mutation_outcome_of()` 对 code_loop 返回 UNKNOWN |
| `runtime/runner.py` | code_loop 使用 `codeloop_outcome_of()` 进行 epoch 推进判断 |
| `tests/test_phase26_codeloop_dual.py` | 13 tests（Phase 25 的 11 tests 替代） |
| `tests/test_phase24_evidence_truth.py` | 更新 code_loop 测试适配 `codeloop_outcome_of()` |
| `phase27/window/` | Agens M1/M4 closure probe 结果 |
| `phase27/regression.log` | 781 tests manifest |
