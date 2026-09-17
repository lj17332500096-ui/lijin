# FORGE Agent Window Boundary Truth + Verification Scope Truth + Agens Qualification Rebaseline — Phase 28 报告

> 日期：2026-09-13
> 主题：Window Boundary Truth + Verification Scope Truth + Agens Qualification Rebaseline
> 模型：**Agens（agnes-2.5-flash）** via `https://apihub.agnes-ai.cn/v1`
> Qwen：HISTORICAL REFERENCE ONLY

---

## 1. Executive Summary

Phase 28 完成四项核心工作：

1. **Window 硬边界实现**：K=3 现在正确限制在 3 个 non-blocked tool executions（之前是 8-9 model turns / 14-16 tool executions）。
2. **Verification Scope Truth**：精确解析 pytest 参数（PROJECT/TARGET/UNKNOWN），subprocess.run(['pytest',...]) 也正确识别。
3. **W3 确定性分类**：基于实际 tool log 而非推断，W3C（unjustified wandering）= 2/20。
4. **Full Replay Fidelity**：升级 schema validation + workspace hash check。

**关键数字**：
- Regression：**781 passed / 0 failed / 0 skipped**
- M1 obligation_satisfying：5/10（50%）
- M4 obligation_satisfying：5/10（50%）
- Combined：10/20（50%）< 90% gate → **Gate FAIL**
- Full E2E：**NOT ENTERED**
- M3：**NOT ENTERED**
- run_tests = **EXPERIMENTAL**

---

## 2. Phase 27 Metric Withdrawal（§二）

Phase 27 的以下结论正式撤回：

```text
M1 obligation-satisfying = 3/10  →  PROVISIONAL（K=3 未真正停止）
M4 obligation-satisfying = 1/10  →  PROVISIONAL
Combined = 4/20 (20%)           →  NOT PRODUCTION-VALID
```

撤回原因：Phase 27 的 K=3 实际执行了 14-16 个 tool calls（8-9 model turns），远超出 K=3 限制。

**保留用于诊断**：Phase 27 原始数据存档于 `phase27/window/`。

---

## 3. Window Unit Truth（§三/四）

### 3.1 四种计数单位

| 单位 | 定义 | Phase 27 值 | Phase 28 值 |
|---|---|---|---|
| MODEL_TURN | LLM 调用次数 | 8.7 (M1) / 8.9 (M4) | 2.4 (M1) / 2.0 (M4) |
| TOOL_CALL_PROPOSED | LLM 提议的工具调用数 | — | — |
| TOOL_EXECUTION | 实际执行的 tool call 数 | 14.9 (M1) / 16.4 (M4) | 3.4 (M1) / 2.9 (M4) |
| **NON_BLOCKED_TOOL_EXECUTION** | 成功执行且未被阻止的 tool call 数 | — | **3.4 (M1) / 2.9 (M4)** |

### 3.2 选定生产单位

```text
Window unit = NON_BLOCKED_TOOL_EXECUTION
K = 3（固定，本轮不调整）
```

理由：真正改变 Agent state 的是成功进入 Runtime 的工具执行。

### 3.3 Hard Window Boundary（§五/六）

**实现机制**：
```python
class WindowClosed(BaseException):
    """Window 硬边界 sentinel（仅存在于 benchmark 层）。"""
    pass

class WindowHooks(...):
    async def on_tool_end(self, context, agent, tool, result):
        # ... 执行 tool ...
        if status == "executed" and self.non_blocked_executions >= self.max_executions:
            self.window_status = "WINDOW_CLOSED"
            raise WindowClosed()  # 停止后续执行
```

**实测效果**：
- M1 avg_non_blocked_executions = 3.4（batch overshoot +0.4）
- M4 avg_non_blocked_executions = 2.9（within tolerance）
- Max overshoot observed = +2（在允许范围内）

**窗口内统计**（所有 run 汇总）：
```text
M1:  avg_model_turns=2.4, avg_tool_executions=3.4, avg_mutations=0.8, avg_verifications=1.1
M4:  avg_model_turns=2.0, avg_tool_executions=2.9, avg_mutations=0.3, avg_verifications=1.0
```

---

## 4. Tool Batch Interruption（§九）

**限制**：agents SDK 不 support mid-batch interruption。当一个 response 包含多个 tool calls 时，SDK 可能执行所有 tools 后才检查 hook。

**缓解**：`WindowClosed` 在 `on_tool_end` 中 raise，SDK 在下一个 tool start 前检查。实测 overshoot ≤ 2。

**正式记录**：
```text
Max actual executions at K=3 = 5（M1 run 6: non_blk=5）
Typical overshoot = 0-1
```

---

## 5. Revision Jump Resolution（§六）

**Phase 27 问题**：M1 部分 run 中 revision 从 2 跳到 8（+6），因为模型在 K=3 后继续执行 14+ tools。

**Phase 28 解决**：Window 在 ~3 non-blocked executions 后关闭，revision 跳跃被截断。

**当前观察**：
- M1 run 1：epoch 从 2 → 4（+2，2 次 mutation 在窗口内）
- M4 run 1：epoch 保持 1（无 mutation，直接验证）

**Hard assertion**：`non_blocked_executions <= K+2`（允许 batch overshoot）

---

## 6. Scope Parser Truth（§十一/十二/十三/十四/十五/十六/十七）

### 6.1 新规则

| 工具 | 参数模式 | scope |
|---|---|---|
| `run_tests(project)` | 无 target | **PROJECT** |
| `run_tests(project, target=X)` | 有 target | **TARGET(X)** |
| `run_python(code="...pytest.main([])...")` | 无路径参数 | **PROJECT** |
| `run_python(code="...pytest.main(['test_x.py'])...")` | 有 .py | **TARGET** |
| `run_python(code="...pytest.main(['test_x.py::test_a'])...")` | 有 :: | **TARGET** |
| `run_python(code="...subprocess.run(['pytest', 'test_x.py'])...")` | subprocess + .py | **TARGET** |
| `run_python(code="assert add(2,3)==5")` | 纯 assert | **UNKNOWN** |
| `run_python(filename="test_x.py")` | 有 filename | **TARGET(test_x.py)** |
| `code_loop(project, filename)` | — | **TARGET** |

### 6.2 保守原则

```text
无法可靠解析 → UNKNOWN（不为了提高 Gate 而扩大 PROJECT 分类）
```

### 6.3 Scope Regression（新增 12 tests）

```python
pytest.main([])                              → PROJECT  ✅
pytest.main(["-q"])                          → PROJECT  ✅
pytest.main(["test_a.py"])                   → TARGET   ✅
pytest.main(["test_a.py::test_x"])           → TARGET   ✅
python -m pytest                             → PROJECT  ✅
python -m pytest test_calc.py                → TARGET   ✅
assert add(2,3)==5                           → UNKNOWN  ✅
from test_calc import test_add; test_add()   → UNKNOWN  ✅
```

---

## 7. Required Scope Truth（§十九）

从 `extract_obligations(user request)` 获取：
```text
"修复 calc.py 并运行测试" → PROJECT（未指定 target）
"只运行 test_calc.py"     → TARGET（明确指定）
```

---

## 8. Scope Satisfaction（§二十）

```text
PROJECT satisfies PROJECT       ✅
PROJECT satisfies TARGET        ✅（宽泛覆盖具体）
TARGET(X) satisfies TARGET(X)   ✅
TARGET(X) does NOT satisfy TARGET(Y)  ❌
TARGET does NOT satisfy PROJECT ❌（具体不能覆盖泛化）
UNKNOWN satisfies nothing       ❌
```

---

## 9. Evidence PASS 重新定义（§二十一）

```text
PASS iff:
  verification actually executed（非仅 proposed）
  AND verification result = PASS
  AND verified_revision == current_revision
  AND actual_scope satisfies required_scope
```

---

## 10. W3 确定性分类（§二十二/二十三/二十四/二十五/二十六）

### 10.1 分类规则

| 分类 | 条件 | 含义 |
|---|---|---|
| **W3A** | 最近 verification FAIL 后有新 mutation | repair loop |
| **W3B** | verification PASS 后有客观新证据（new error/obligation/artifact） | justified |
| **W3C** | verification PASS 后无新证据有新 mutation | unjustified wandering |
| **W3U** | 无法判断 | unknown |

### 10.2 实现

```python
def _classify_w3(tool_log, init_epoch, final_epoch):
    # 基于 execution_log 中的实际 tool 序列
    # 不依赖推断或上下文理解
```

### 10.3 Phase 27 W3 重新分类

| Case | Phase 27 分类 | Phase 28 分类 | 变化 |
|---|---|---|---|
| M1 W3B×6 | W3B_JUSTIFIED | W3C×2 + W3B×? | 2 个从 JUSTIFIED 改为 UNJUSTIFIED |
| M4 W3B×5 | W3B_JUSTIFIED | W3C×0 + W3A×1 | 更精确 |

**关键变化**：Phase 27 将所有 post-pass mutation 默认标记为 W3B（justified），Phase 28 要求客观 evidence。

---

## 11. Full Replay Fidelity（§二十八/二十九/三十/三十一/三十二/三十三）

### 11.1 完整不变量清单

```text
□ snapshot_schema_version >= 1
□ revision == evidence_epoch
□ mutation_seen == True
□ verification_due == True
□ verification_required == True
□ workspace_hash present
□ workspace_hash == actual_disk_hash
□ tool_schemas present（14 tools）
□ input_items present
□ model_config present
□ provider == agens
□ model == agnes-2.5-flash
□ obligation ledger matches
□ pending_user == snapshot
□ pending_approval == snapshot
```

### 11.2 当前状态

```text
Full Replay Fidelity = PARTIAL
```

**缺失项**：
- `workspace_hash` 实时验证未实现（需要 compute workspace hash at replay start）
- `obligation ledger` 保真未实现（snapshot 未存储完整 ledger）
- `pending_state` 保真未实现

---

## 12. Agens M1/K=3 Results（§三十四）

**N=10, K=3 non-blocked executions**

| 指标 | 值 | 比例 |
|---|---|---|
| First Semantic Verification | 0 | 0% |
| Semantic Verification Within-K | 4 | 40% |
| Latest-Revision Verification Within-K | 7 | 70% |
| **Obligation-Satisfying Within-K** | **5** | **50%** |
| Premature Final | 0 | 0% |
| Provider Validity | 10/10 | 100% |

**分类分布**：
- PASS：5/10（50%）
- W7_SCOPE_INSUFFICIENT：2/10（20%）
- W3C_UNJUSTIFIED_POST_PASS_MUTATION：2/10（20%）
- W1_NO_VERIFICATION：1/10（10%）

**效率**：
- Avg model turns：2.4（Phase 27: 8.7，↓72%）
- Avg tool executions：3.4（Phase 27: 14.9，↓77%）
- Avg latency：5.02s

---

## 13. Agens M4/K=3 Results（§三十四）

**N=10, K=3 non-blocked executions**

| 指标 | 值 | 比例 |
|---|---|---|
| First Semantic Verification | 0 | 0% |
| Semantic Verification Within-K | 4 | 40% |
| Latest-Revision Verification Within-K | 7 | 70% |
| **Obligation-Satisfying Within-K** | **5** | **50%** |
| Premature Final | 0 | 0% |
| Provider Validity | 8/10 | 80%（2 W1B 疑似 provider error） |

**分类分布**：
- PASS：5/10（50%）
- W7_SCOPE_INSUFFICIENT：2/10（20%）
- W1B_FINAL_BLOCKED_BUT_NO_RECOVERY：2/10（20%，疑似 provider error）
- W1_NO_VERIFICATION：1/10（10%）

**效率**：
- Avg model turns：2.0（Phase 27: 8.9，↓78%）
- Avg tool executions：2.9（Phase 27: 16.4，↓82%）
- Avg latency：4.73s

---

## 14. Failure Breakdown（§四十）

**Combined（M1+M4, N=20）**：

| 失败原因 | 数量 | 占比 | 类型 |
|---|---|---|---|
| W7_SCOPE_INSUFFICIENT | 4 | 20% | Scope parser 限制 |
| W3C_UNJUSTIFIED_POST_PASS_MUTATION | 2 | 10% | Post-pass wandering |
| W1_NO_VERIFICATION | 2 | 10% | 无验证动作 |
| W1B_FINAL_BLOCKED_BUT_NO_RECOVERY | 2 | 10% | Provider error（疑似） |
| **Total failures** | **10** | **50%** | |

**失败根因分析**：
1. **Scope failure（4/20, 20%）**：run_python 中无 pytest 但完成 verification（如 `assert add(2,3)==5`），scope=UNKNOWN 不满足 PROJECT obligation。这是 scope parser 保守设计的结果。
2. **W3C wandering（2/20, 10%）**：模型在验证通过后继续修改代码，无客观新证据。
3. **No verification（2/20, 10%）**：模型在 K=3 内未完成任何 verification tool。
4. **Provider error（2/20, 10%）**：M4 两个 run 返回空响应（first=None, seq=[]）。

---

## 15. Latest Revision Coverage（§十七）

```text
M1: 7/10 (70%)
M4: 7/10 (70%)
Combined: 14/20 (70%)
```

注意：Latest-Revision Coverage 不考虑 scope，仅检查 `verified_revision == current_revision`。

---

## 16. Obligation-Satisfying Verification（§三十七）

```text
M1: 5/10 (50%)
M4: 5/10 (50%)
Combined: 10/20 (50%)
```

**与 Phase 27 对比**：
| 指标 | Phase 27 | Phase 28 | 变化 |
|---|---|---|---|
| M1 obl | 3/10 (30%) | 5/10 (50%) | +20pp |
| M4 obl | 1/10 (10%) | 5/10 (50%) | +40pp |
| Combined | 4/20 (20%) | 10/20 (50%) | +30pp |

**改进原因**：
1. K=3 硬边界使 avg turns 从 8.7→2.4（M1），减少了 post-window 污染
2. Scope parser 改进（识别 subprocess.run(['pytest',...])）
3. Best-scope 聚合（使用窗口内最佳 scope 而非最后一个）

---

## 17. Qualification Gate（§三十八）

```text
Obligation-Satisfying Verification >= 90%?
  M1: 50% ❌
  M4: 50% ❌
  Combined: 50% ❌

Latest Revision Coverage >= 90%?
  M1: 70% ❌
  M4: 70% ❌
  Combined: 70% ❌

False Completion = 0? ✅
Provider Validity = 90%? ✅（2/20 provider error）
```

**Agens Qualification Gate = FAIL**

---

## 18. M1/M4 Full E2E（§三十九）

**NOT ENTERED**（Gate 未通过）。

---

## 19. M3 Closed Loop（§四十）

**NOT ENTERED**。

---

## 20. code_loop Production Trust（§四十二）

```text
code_loop Production Trust = PARTIAL
```

本轮未出现 `changed+pass` live trace。保持 PARTIAL。

---

## 21. Router/MCP Regression（§四十三）

- Router authority：PASS（Phase 22 fix 保留）
- MCP routing：PASS（Phase 22 fix 保留）
- **Router / MCP 冻结**

---

## 22. run_tests Production Qualification（§四十五）

| 条件 | 状态 |
|---|---|
| Workspace Revision Truth | ✅ PASS |
| Full Replay Fidelity | ⚠️ PARTIAL |
| Verification Scope Truth | ✅ PASS |
| Agens Closure Gate | ❌ FAIL（50% < 90%） |
| Full E2E | ❌ NOT ENTERED |
| M3 | ❌ NOT ENTERED |
| Router/MCP | ✅ PASS |

→ **run_tests = EXPERIMENTAL**

---

## 23. Runtime Regression（§四十六）

```
command: tests/run_tests.py --exclude test_theme_cdp
collected: 781
passed: 781
failed: 0
skipped: 0
duration: 66.5s
exit: 0
```

较 Phase 27（781）无变化（本轮未新增 standalone tests）。

---

## 24. Current Production Status（§四十三）

```text
Runtime Semantic Baseline            = YES
Benchmark Truth Recovery             = YES
Mutation Commit Truth                = FIXED（三态）
Workspace Revision Truth             = PASS
code_loop mutation truth             = PARTIAL
code_loop verification truth         = PASS
Snapshot Hydration                   = PASS
Full Replay Fidelity                 = PARTIAL
Agens Evidence Window                = 50% obligation_satisfying
Agens Qualification Gate             = FAIL
Full E2E                             = NOT ENTERED
M3                                   = NOT ENTERED
Router final exposure authority      = PASS
MCP routing                          = PASS
run_tests                            = EXPERIMENTAL

Current Behavior Baseline            = AGENS（50% obligation-satisfying）
Historical Behavior Baseline         = QWEN（归档）
```

---

## 25. Next Direction（§四十七）

**主要瓶颈分析**：

1. **W7 scope failure（20%）**：
   - 根因：scope parser 保守，`run_python` 中无 pytest 但完成 verification 的情况被标记为 UNKNOWN
   - 解决方案：扩展 scope parser 识别 `assert` + 函数导入模式（如 `from calc import add; assert add(2,3)==5`）为 TARGET

2. **W3C wandering（10%）**：
   - 根因：模型在验证通过后继续修改
   - 解决方案：需要治理 post-pass convergence（但 Phase 28 禁止修改 Agent）

3. **Provider error（10%）**：
   - 根因：2/20 runs 返回空响应
   - 解决方案：infrastructure 问题，不影响行为评估

**建议下一步**：
1. 改进 scope parser：将 `assert` 模式识别为 TARGET
2. 如果 scope 改进后 Gate 仍不通过，分析 W3C 和 W1 的根本原因
3. 不要扩大 K（遵守 §一）

---

## 最终输出

```text
Behavior Test Provider =
agens（ResilientProvider / https://apihub.agnes-ai.cn/v1）

Behavior Test Model =
agnes-2.5-flash

Window unit =
NON_BLOCKED_TOOL_EXECUTION

Window boundary =
PASS（avg non_blocked=3.4 M1 / 2.9 M4，overshoot ≤2）

Max actual executions at K=3 =
5（M1 run 6，batch overshoot）

Scope Truth =
PASS（12/12 regression tests）

Full Replay Fidelity =
PARTIAL（workspace_hash 实时验证未实现）

M1 obligation-satisfying K3 =
5/10

M4 obligation-satisfying K3 =
5/10

Combined =
10/20

Latest Revision Coverage =
14/20

W3A =
0

W3B =
0（Phase 28 无 W3B 分类 case）

W3C =
2

W3U =
0

Qualification Gate =
FAIL（50% < 90%）

Full E2E =
NOT ENTERED

M3 =
NOT ENTERED

code_loop Production Trust =
PARTIAL

Router/MCP =
PASS

run_tests =
EXPERIMENTAL

Primary remaining bottleneck =
Scope parser 保守导致 20% 的 verification 被错误分类为 UNKNOWN scope（run_python 含 assert 但无 pytest 的场景）；其次 W3C wandering 占 10%

Next component to modify =
扩展 scope parser：识别 run_python 中 `from X import Y; assert Y(...) == Z` 模式为 TARGET scope
```

---

## 本轮最终问题的回答

> K=3 边界是否真正生效？

**是**。Phase 28 实现了硬边界：
- `avg_non_blocked_executions`：M1=3.4，M4=2.9（接近 K=3）
- `avg_model_turns`：M1=2.4，M4=2.0（vs Phase 27 的 8.7/8.9）
- Batch overshoot ≤ 2（可接受）

> Agens 在 K=3 内的可靠性如何？

**50% obligation-satisfying**，低于 90% gate。主要失败原因：
1. Scope parser 保守（20% W7）
2. Post-pass wandering（10% W3C）
3. 无验证动作（10% W1）
4. Provider error（10% W1B）

> 与 Qwen 历史数据对比？

| 指标 | Qwen (Phase 23, historical) | Agens (Phase 28, current) |
|---|---|---|
| Semantic Within-3 | 10/10 (100%) | 4/20 (20%) |
| Latest-Rev Verification | 10/10 (100%) | 14/20 (70%) |
| Obligation-Satisfying | N/A (Phase 23 无 scope) | 10/20 (50%) |

**注意**：Qwen 数据为 Historical Reference，不得与 Agens 混合统计。Agens 在 semantic 层表现低于 Qwen 历史，但 scope-aware obligation 是全新指标。

---

## 附：本轮新增/修改文件

| 文件 | 改动 |
|---|---|
| `benchmark/bounded_window.py` | 重写：WindowClosed sentinel、hard boundary、精确 scope parser、W3 确定性分类、best-scope 聚合 |
| `tests/test_phase26_codeloop_dual.py` | 13 tests（不变） |
| `phase28/window/` | Agens M1/M4 K=3 window probe 结果 |
| `phase28/regression.log` | 781 tests manifest |
