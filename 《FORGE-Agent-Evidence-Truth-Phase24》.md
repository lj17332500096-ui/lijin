# FORGE Agent Evidence Truth + Workspace Revision — Phase 24 报告

> 日期：2026-09-13
> 主题：Workspace Revision Truth + Verification Evidence Qualification
> 基线：《FORGE-Agent-Mutation-Truth-Phase23》 + 当前真实代码
> 模型：**Qwen3.6-35B-A3B**（`FORGE_MODEL_PREF=local`，`localhost:8080`）
> 本轮不增加 Agent 能力。只修正 truth 语义。

---

## 1. Executive Summary

Phase 24 完成三项核心工作：

1. **Mutation Outcome 三态化**：`_mutation_result_ok()` 从 bool 升级为 `COMMITTED / FAILED / UNKNOWN`，UNKNOWN 不再默认推进 revision。
2. **Workspace Revision Truth**：tool execution ≠ workspace changed；no-op mutation 不得递增 epoch。
3. **Evidence-Based Window**：bounded window 从"出现验证意图"升级为"产生真实 latest-revision verification evidence"。

**关键数字**：
- Mutation tri-state 回归套件：**30 tests PASS**（Phase 23: 15 + Phase 24: 15 new）
- 完整 regression：**768 passed / 0 failed / 0 skipped / exit 0**
- **Live Window Probe（M1 首跑）**：W1（无 evidence 产生），后续因模型服务器停机未完成
- Full E2E：**NOT ENTERED**（实验未完成）
- run_tests = **EXPERIMENTAL**

---

## 2. Phase 23 Truth Review

Phase 23 已建立：
- Mutation Commit Truth 修复（runtime + benchmark 统一）
- Bounded Verification Window（K=3）
- Corrected M1/M4 snapshots

本轮在此基础上升级 truth 粒度：从"mutation 成功"到"workspace 实际变更"。

---

## 3. Mutation Outcome 三态（§四）

**旧语义**：`_mutation_result_ok()` → `bool`（True/False），UNKNOWN 默认 True

**新语义**：
```python
def _mutation_result_ok(name: str, result_text: str) -> str:
    """返回 'COMMITTED' | 'FAILED' | 'UNKNOWN'"""
```

| 返回 | 含义 | epoch 推进 | mutation_seen |
|---|---|---|---|
| `COMMITTED` | workspace 确定变更 | ✅ bump | ✅ set |
| `FAILED` | workspace 确定未变更 | ❌ 不推进 | ❌ 不设置 |
| `UNKNOWN` | runtime 无法推断 | ❌ 不推进 | ❌ 不设置 |

**修改文件**：
- `runtime/runner.py::_mutation_result_ok()`（lines 59–77）
- `runtime/runner.py::_make_invoke`（lines 743–767）
- `runtime/readiness_gate.py::observe()`（lines 724–730）
- `runtime/completion.py::mutation_outcome_of()`（lines 222–244）

---

## 4. Mutation Tool Contracts（§九/十二）

**已覆盖工具的成功/失败标记**：

| tool | COMMITTED 标记 | FAILED 标记 |
|---|---|---|
| `edit_project_file` | "已在...替换" | "错误：没有找到" |
| `write_project_file` | "已写入"/"已覆盖" | "错误：路径不存在" |
| `write_code_file` | "已写入" | "错误"/"permission" |
| `save_note` | "已保存" | "错误"/"失败" |
| `save_word_doc` | "已生成"/"已保存" | "错误" |
| `sandbox_rollback` | "已回滚" | "错误"/"失败" |
| `code_loop` | "✅ 通过" | "❌ 失败" |

**新增标记**（本轮）：`"覆盖"`、`"回滚"`、`"✅ 通过"`、`"❌ 失败"`

---

## 5. Unknown Outcome Semantics（§五/六）

**正式规则**：
```text
UNKNOWN → 不推进 evidence_epoch
UNKNOWN → 不设置 mutation_seen
UNKNOWN → 不满足 mutation obligation
UNKNOWN → 不得用于 Completion 宣称"修改已完成"
```

**审计保留**：
```text
mutation_outcome = unknown  → 记录在 ExecutionEvidence 中
Completion 不得基于 unknown mutation 宣称 state change
```

---

## 6. No-Op Mutation（§十三）

**edit_project_file 已有防护**：
```python
if not old_string or old_string == new_string:
    return "错误：old_string 不能为空，且不能与 new_string 相同"
```

**new→unknown 路径**：如果工具返回无标记结果（如某些边界情况），`_mutation_result_ok` 返回 `UNKNOWN`，epoch 不推进。

**正式固定**：
```text
successful operation ≠ state change
```

---

## 7. Partial Mutation（§十四）

**当前状态**：各 mutation 工具为原子操作（先备份→写→成功/失败），不存在部分写入后返回 error 的情况。

**审计结论**：当前工具实现满足"execution_success=true → workspace_changed=true"的一致性。未来如引入非原子写入，需单独处理。

---

## 8. Workspace Revision Truth（§八）

**真正决定 revision 的事实**：`workspace state changed`

**Phase 24 实现**：
- `evidence_epoch` 仅在 `_mut_outcome == "COMMITTED"` 时递增
- `verified_revision` 仅在 `verification_passed && tool in VERIFY_TOOLS` 时设置
- `verification_due()` 检查 `verified_revision == evidence_epoch`

**与 Phase 23 的差异**：
- Phase 23：`_mut_result_ok=True` → bump epoch（bool 判断）
- Phase 24：`_mut_outcome=="COMMITTED"` → bump epoch（三态判断）

---

## 9. ExecutionEvidence Integration（§二十）

**新增方法**（`runtime/completion.py`）：
- `mutation_outcomes()` → `list[tuple[str, str]]`：每个 mutation 调用的 (tool_name, outcome)
- `mutation_committed_count()` → `int`：workspace 确定变更的次数
- `mutation_any_failed()` → `bool`：是否存在至少一次确定失败

**使用方式**：
```python
evidence = runner.get_evidence()
for name, outcome in evidence.mutation_outcomes():
    print(f"{name}: {outcome}")  # COMMITTED / FAILED / UNKNOWN
```

---

## 10. code_loop Compound Audit（§十五/十六/十七）

**Phase 23 结论**：`code_loop = NOT RELIABLE`（外部无法判断内部是否实际修改了 workspace）

**Phase 24 增强**：
- code_loop 结果文本附带 `[Phase24-outcome] workspace_changed=X verification_passed=Y`
- `mutation_outcome_of()` 可解析 `✅ 通过` → COMMITTED，`❌ 失败` → FAILED
- 但仍无法区分"首次通过（无 write）"和"修复后通过（有 write）"

**正式结论**：
```text
code_loop mutation truth = NOT RELIABLE（无法确定 workspace_changed）
code_loop verification truth = RELIABLE（✅ 通过 / ❌ 失败 可判定）
```

---

## 11. code_loop Outcome Contract

**当前输出格式**（已修改）：
```
【循环结果】✅ 通过（1/3 次尝试，用时 2.1s）
...
[Phase24-outcome] workspace_changed=false verification_passed=true

【循环结果】❌ 失败（尝试 2/3 次后停止）
...
[Phase24-outcome] workspace_changed=true verification_passed=false
```

**限制**：`workspace_changed` 仅在 `_safe_write` 成功时设为 `true`，首次通过时为 `false`。

---

## 12. Benchmark/Runtime Truth（§十九）

**保持统一**：benchmark 不重新拥有独立 predicate。
- `benchmark/bounded_window.py` 使用 `runtime.completion.mutation_outcome_of()` 解析 evidence
- `runtime.readiness_gate.DiscoveryTracker` 使用 `runtime.runner._mutation_result_ok()` 更新 epoch

**双真值系统已消除**（Phase 23 建立，Phase 24 强化）。

---

## 13. Workspace Revision Regression（§二十一）

**`tests/test_phase24_evidence_truth.py`：30 tests，全部 PASS**

| 类别 | 测试数 | 覆盖场景 |
|---|---|---|
| MutationOutcomeTriState | 12 | edit/write/code_loop/rollback 的三态判定 |
| NoOpMutation | 2 | no-op 不得推进 revision |
| UnknownDoesNotBumpEpoch | 2 | UNKNOWN 不 bump epoch |
| IntegrationRevisionFlow | 3 | success→noop, success→verify, verify→failed |
| CodeLoopOutcome | 3 | code_loop 结构化解析 |
| ExecutionEvidenceMutation | 3 | mutation_outcomes 方法 |
| EvidenceWindowClassification | 5 | W1/W2/W3/W4/PASS 分类 |

**Phase 23 测试同步更新**：15 tests PASS（断言从 `assertTrue/assertFalse` 改为 `assertEqual(..., "COMMITTED"/"FAILED"/"UNKNOWN")`）

---

## 14. Verification Window Truth Correction（§二十二）

**Phase 23 定义**（已重命名）：
```text
Semantic Verification Within-3 =
M1 10/10（100%）
M4 10/10（100%）
```
含义：窗口内出现 semantic verification tool（run_tests / run_python with pytest）。

**Phase 24 新定义**：
```text
Obligation-Satisfying Verification Within-3 =
NOT YET ESTABLISHED（实验未完成）

Actual Latest Revision Coverage =
NOT YET ESTABLISHED（实验未完成）
```

**PASS 条件**（Phase 24）：
```text
verification_attempted = true
verification_passed = true
verified_revision == current_revision
verification scope satisfies obligation
```

---

## 15. Evidence-Based Window Design（§二十三/二十四/二十五）

**新分类体系**（§二十七）：
| 分类 | 含义 |
|---|---|
| `PASS` | verified_revision == epoch AND verification_passed |
| `W1_NO_VERIFICATION` | 无 verification 动作 |
| `W2_VERIFICATION_FAILED` | verification 执行但未通过 |
| `W3_NEW_MUTATION_UNVERIFIED` | 新 mutation 发生后未验证 |
| `W4_EXPLORATION_ONLY` | 仅探索/读取 |
| `W5_TOOL_BLOCKED_OR_ERROR` | 工具被阻断或异常 |
| `W6_PROVIDER_FAILURE` | 模型服务故障 |
| `W7_SCOPE_INSUFFICIENT` | 验证范围不满足义务 |

**实现位置**：`benchmark/bounded_window.py::_categorize_window()`

---

## 16. M1 Window Results（§二十八）

**实验状态**：**部分完成**（模型服务器在第一次运行后停机）

```
[WM1] M1 [1/10] cat=W1 first=run_tests seq=['run_tests','list_workspace_files',...]
      actions=8 epoch=0 ver_rev=0 evid_pass=False lat=171.5s
[WM1] M1 [2-10/10] cat=W5 first=None seq=[] actions=0  (API connection error)
```

**分析**：
- Run 1：模型输出了 run_tests + list_workspace_files 等 8 个动作，但 evidence_pass=False
  - 原因：replay agent 的 `DiscoveryTracker` 在模拟 observe 时，run_tests 被识别为 verification tool，但 `evidence_epoch` 保持为 0（snapshot 的 epoch=0，且窗口内无 mutation）
  - **关键发现**：Phase 23 的 "PASS" 是语义层面的（出现了 run_tests），Phase 24 的 "PASS" 需要 `verified_revision == evidence_epoch > 0`
  - 由于 snapshot 的 `evidence_epoch=0`（valid post-mutation checkpoint 可能有不同设置），窗口内没有新的 mutation → epoch 不增长 → verification 无法匹配

**需要澄清**：
- M1 snapshot 的 `evidence_epoch` 值是多少？
- 如果 snapshot 已有 `evidence_epoch=1`，则窗口内的 verification 应能匹配

---

## 17. M4 Window Results（§二十八）

**未完成**（服务器停机）。

---

## 18. Actual Latest Revision Coverage（§三十）

**待实验完成**。初步分析（Run 1）：
- M1 snapshot 的 `evidence_epoch=0` → 窗口内无 mutation → epoch 保持 0
- verification 工具执行 → `verified_revision` 设为 0（== epoch）
- **但**：snapshot 可能已有 `verification_passed=True`（pre-existing），所以 `verification_due()` 可能返回 False

需要重新检查 snapshot 内容来确定预期行为。

---

## 19. Verification Scope（§二十三）

**当前实现**：窗口内 verification 不检查 scope（OBLIGATION_REQUIRED vs OBLIGATION_NONE）。
**限制**：replay agent 不提供真实的 obligation 上下文，仅基于 snapshot 的 `verification_due` 字段。

**Full E2E 中**：scope 由 `extract_obligations(request_text)` 决定，window probe 暂不涉及。

---

## 20. Window Failure Classification（§二十七）

**已实现的分类**：PASS / W1 / W2 / W3 / W4 / W5 / W6 / W7

**本轮观察到的分类**：
- M1 Run 1: W1（有 verification 工具但 epoch=0，不满足 evidence_pass 条件）
- M1 Runs 2-10: W5（API 连接错误）

---

## 21. Qualification Gate Decision（§三一）

**新 gate 条件**（证据based）：
```text
Obligation-Satisfying Verification Within-3 >= 90%   → NOT EVALUATED（实验未完成）
Actual Latest Revision Coverage >= 90%               → NOT EVALUATED
False Completion = 0                                  → PASS（回归证明）
```

**旧 gate**（已废弃）：
```text
First Verification Decision >= 90%   → M1 80% / M4 70%（combined 75%）
```

**结论**：无法正式判定 gate，因为 live 实验未完成。

---

## 22. M1/M4 Full E2E（§三二/三三）

**NOT ENTERED**。

理由：
1. Evidence-based window 实验未完成（服务器停机）
2. 即使完成，M1 Run 1 显示 W1（evidence_pass=False），暗示 gate 可能不通过
3. 需要重新设计 window probe 的 epoch 初始值逻辑（见 §23）

---

## 23. M3 Closed Loop（§三五）

**NOT ENTERED**。

---

## 24. Router/MCP Regression（§二八）

- Router authority：PASS（Phase 22 fix 保留，本轮未修改）
- MCP routing：PASS（Phase 22 fix 保留，本轮未修改）
- **Router / MCP 冻结**（§三八）

---

## 25. run_tests Production Qualification（§三七）

| 条件 | 状态 |
|---|---|
| Mutation Commit Truth | ✅ PASS（三态，30 tests） |
| Workspace Revision Truth | ✅ PASS（no-op 不递增） |
| Evidence-Based Window | ⚠️ PARTIAL（M1 Run 1=W1，后续未完成） |
| Full E2E | ❌ NOT ENTERED |
| M3 | ❌ NOT ENTERED |

→ **run_tests = EXPERIMENTAL**

---

## 26. Runtime Regression（§四十一）

```
command: tests/run_tests.py --exclude test_theme_cdp
collected: 768
passed: 768
failed: 0
skipped: 0
duration: ~70s
exit: 0
```

较 Phase 23 baseline（738）+30 = `tests/test_phase24_evidence_truth.py`（30 tests）
Phase 23 tests（15）已更新适配三态语义，继续 PASS。

**修改文件清单**：
| 文件 | 改动 |
|---|---|
| `runtime/runner.py` | `_mutation_result_ok()` 返回三态；observe 路径使用三态 |
| `runtime/readiness_gate.py` | `observe()` 仅 COMMITTED 设 mutation_seen |
| `runtime/completion.py` | 新增 `MUTATION_TOOLS`、`mutation_outcome_of()`、`ExecutionEvidence.mutation_outcomes()` |
| `runtime/codex_loop.py` | 输出 `[Phase24-outcome] workspace_changed=X verification_passed=Y` |
| `benchmark/bounded_window.py` | 重写为 evidence-based PASS；三层指标 |
| `tests/test_phase23_mutation_truth.py` | 更新断言适配三态 |
| `tests/test_phase24_evidence_truth.py` | **新增** 30 tests |

---

## 27. Production Status

```text
Runtime Semantic Baseline            = YES
Benchmark Truth Recovery             = YES
Mutation Commit Truth                = FIXED（三态）
Unknown defaults to committed        = NO（已修复）
No-op mutation increments revision   = NO（已修复）
Workspace Revision Truth             = PASS（回归证明）
code_loop mutation truth             = NOT RELIABLE（已知限制）
M1 first semantic verification       = 10/10（Phase 23，保留）
M1 semantic within-3                 = 10/10（Phase 23，保留）
M1 evidence within-3                 = NOT ESTABLISHED（实验未完成）
M4 first semantic verification       = 10/10（Phase 23，保留）
M4 semantic within-3                 = 10/10（Phase 23，保留）
M4 evidence within-3                 = NOT ESTABLISHED（实验未完成）
Actual Latest Revision Coverage      = NOT ESTABLISHED
Qualification Gate                   = NOT EVALUATED
Full E2E                             = NOT ENTERED
M3                                   = NOT ENTERED
Router final exposure authority      = PASS
MCP routing                          = PASS
run_tests                            = EXPERIMENTAL
Primary remaining bottleneck         = Evidence-based window 实验未完成（服务器停机）+ M1 snapshot epoch=0 导致 W1 分类
Next component to modify             = 重启模型服务器后重跑 M1/M4 window probe；或调整 window probe 的 epoch 初始化逻辑
```

---

## 28. Next Direction

1. **重启模型服务器**：`llama.cpp` server 需要在 GPU 上重新启动（`localhost:8080`）
2. **重跑 M1/M4 window probe**：N=10 each，收集三层指标
3. **审查 snapshot epoch 初始化**：M1 Run 1 的 `epoch=0` 可能是预期行为（valid post-mutation checkpoint 可能 epoch=0 表示"mutation 已完成但未在 snapshot 中记录 epoch"），需要确认
4. **如果 gate 通过（>=90% evidence）**：进入 Full E2E N=3 for M1/M4
5. **如果 gate 不通过**：分析 W1/W2/W3 的根本原因，决定下一步

---

## 最终输出

```text
Unknown mutation defaults to committed =
NO（已修复：UNKNOWN 不推进 epoch）

No-op mutation increments revision =
NO（已修复：edit old==new 直接报错；unknown 结果不 bump epoch）

Workspace Revision Truth =
PASS（30 regression tests；768 total regression）

code_loop mutation truth =
NOT RELIABLE（无法从外部判定 workspace_changed；verification 可靠）

M1 first semantic verification =
10/10（Phase 23，保留）

M1 semantic within-3 =
10/10（Phase 23，保留）

M1 evidence within-3 =
NOT ESTABLISHED（实验未完成；Run 1=W1）

M4 first semantic verification =
10/10（Phase 23，保留）

M4 semantic within-3 =
10/10（Phase 23，保留）

M4 evidence within-3 =
NOT ESTABLISHED（实验未完成）

Actual Latest Revision Coverage =
NOT ESTABLISHED

Qualification Gate =
NOT EVALUATED

Full E2E =
NOT ENTERED

M3 =
NOT ENTERED

Router/MCP =
PASS

run_tests =
EXPERIMENTAL

Primary remaining bottleneck =
Evidence-based window 实验因模型服务器停机未完成；M1 Run 1 显示 W1（epoch=0 导致 evidence_pass=False）

Next component to modify =
重启 localhost:8080 模型服务器后重跑 window probe；或修正 window probe 的 epoch 初始值逻辑以匹配 valid post-mutation checkpoint 语义
```

---

## 本轮最终问题的回答

> "模型在 3 步内想到了验证" 和 "模型在 3 步内真的为最新 workspace revision 获得了足以满足 Completion 的验证证据" 是不是同一件事？

**不是同一件事。Phase 24 正式拆分了这两个概念。**

| 指标 | Phase 23 | Phase 24 |
|---|---|---|
| 名称 | Semantic Verification Within-3 | Obligation-Satisfying Verification Within-3 |
| 判定 | 窗口内出现 run_tests 等工具 | 工具执行 + verification_passed=true + verified_revision==epoch |
| M1（Phase 23） | 10/10（100%） | 待实验（Run 1=W1） |
| M4（Phase 23） | 10/10（100%） | 待实验 |

**关键差异**：
- "想到验证" = 模型选择了验证工具（意图层）
- "获得验证证据" = 工具执行了 + 结果通过 + 匹配最新 revision（事实层）

Phase 23 的 100% 是意图层的可靠；Phase 24 正在验证事实层是否同样可靠。

---

## 附：本轮新增/修改文件

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `runtime/runner.py` | 修改 | `_mutation_result_ok()` 返回三态；observe 路径更新 |
| `runtime/readiness_gate.py` | 修改 | `observe()` 仅 COMMITTED 设 mutation_seen |
| `runtime/completion.py` | 新增方法 | `MUTATION_TOOLS`、`mutation_outcome_of()`、`ExecutionEvidence.mutation_outcomes()` |
| `runtime/codex_loop.py` | 修改 | 输出 `[Phase24-outcome]` 结构化标签 |
| `benchmark/bounded_window.py` | 重写 | evidence-based PASS；三层指标；W1-W7 分类 |
| `tests/test_phase23_mutation_truth.py` | 更新 | 断言适配三态 |
| `tests/test_phase24_evidence_truth.py` | **新增** | 30 tests |
| `phase24/regression.log` | **新增** | 768 tests manifest |
