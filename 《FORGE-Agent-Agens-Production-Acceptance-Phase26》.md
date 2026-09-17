# FORGE Agent Agens Production Acceptance — Phase 26 报告

> 日期：2026-09-13
> 主题：Compound Outcome Truth + Agens Production Acceptance
> 模型：**Agens（agnes-2.5-flash）** via `https://apihub.agnes-ai.cn/v1`
> Qwen 数据正式归档为 Historical Reference

---

## 1. Executive Summary

Phase 26 完成四项核心工作：

1. **Agens Provider Preflight**：确认 `agnes-2.5-flash` 可用，tool calling 正常。
2. **code_loop 双结果语义**：`mutation_effect`（CHANGED/UNCHANGED/UNKNOWN）与 `verification_result`（PASS/FAIL/UNKNOWN）彻底分离。
3. **Agens Evidence Window**：M1 6/10 PASS，M4 4/10 PASS（combined 10/20 = 50%）。
4. **Full E2E**：**NOT ENTERED**（Gate 50% < 90%）。

**关键数字**：
- Regression：**781 passed / 0 failed / 0 skipped**
- Agens Provider：✅ AVAILABLE（DNS/TCP/TLS/HTTP 401→认证通过→模型响应正常）
- M1 evidence_within_k：6/10（60%）
- M4 evidence_within_k：4/10（40%）
- Combined：10/20（50%）< 90% gate → **Gate FAIL**
- Full E2E：**NOT ENTERED**
- M3：**NOT ENTERED**
- run_tests = **EXPERIMENTAL**

---

## 2. Agens Provider Preflight（§二/三）

### 2.1 分层诊断

| 层级 | 测试 | 结果 |
|---|---|---|
| DNS | `Resolve-DnsName apihub.agnes-ai.cn` | ✅ 解析到 27.128.219.126 |
| TCP | `Test-NetConnection :443` | ✅ Connected |
| TLS | Python `ssl.create_default_context()` | ✅ Handshake OK |
| HTTP /v1/models | `GET /v1/models` without auth | ❌ 401 Token not provided |
| HTTP /v1/models | `GET /v1/models` with Bearer key | ✅ 返回模型列表 |
| HTTP /v1/chat/completions | POST with agnes-2.5-flash | ✅ 正常响应 |
| Tool Calling | `calculate(2+2)` | ✅ 返回 "2 + 2 = 4" |

**故障层**：NONE（全部正常）

### 2.2 模型 Identity

```text
Behavior Test Provider      = agens (ResilientProvider / OpenAI-compatible)
Behavior Test Model         = agnes-2.5-flash
Provider Base Endpoint      = https://apihub.agnes-ai.cn/v1
Tool Calling Mode           = standard function_calling
Temperature                 = unspecified（SDK default）
Max Tokens                  = 4096
```

---

## 3. Provider Failure Classification（§三）

```text
Agens Provider = AVAILABLE
Provider failure layer = NONE
```

所有网络层（DNS/TCP/TLS/HTTP/Auth/Model）均正常。

---

## 4. Model Identity（§一）

**Current Behavior Test Model**：Agens（`agnes-2.5-flash`）
**Historical Reference Model**：Qwen（`Qwen3.6-35B-A3B`，`localhost:8080`）

禁止混合统计。

---

## 5. code_loop Dual Outcome（§六/七）

### 5.1 旧实现（Phase 25，错误）

```python
# mutation_outcome_of() 用单一值表示：
workspace_changed=true + verification_passed=false → FAILED  ❌
```

问题：`FAILED` 混淆了 "mutation 失败" 和 "verification 失败"。

### 5.2 新实现（Phase 26）

```python
def codeloop_outcome_of(call) -> tuple[str, str, bool | None]:
    """返回 (mutation_effect, verification_result, final_verification_after_last_change)"""
```

**四种核心组合**：

| workspace_changed | verification_passed | mutation_effect | verification_result | final_after_change |
|---|---|---|---|---|
| false | true | **UNCHANGED** | PASS | None |
| true | true | **CHANGED** | PASS | True |
| true | false | **CHANGED** | FAIL | False |
| false | false | **UNCHANGED** | FAIL | None |

**无结构化标签时**：`mutation_effect = UNKNOWN`，`verification_result` 从关键词推断。

### 5.3 Simple Mutation Tools 保持兼容

`edit_project_file`、`write_project_file` 等原子工具继续使用 `mutation_outcome_of()` → `COMMITTED/FAILED/UNKNOWN`。

---

## 6. code_loop Mutation Effect（§八/九）

**正式废弃** `FAILED` 作为 mutation_effect 值。

固定三态：
```text
CHANGED   — workspace 确实发生了变更
UNCHANGED — workspace 未变更
UNKNOWN   — runtime 无法推断
```

---

## 7. code_loop Verification Result（§八/十二）

独立于 mutation_effect：
```text
PASS   — verification 通过
FAIL   — verification 未通过
UNKNOWN — 无法判断
```

---

## 8. Final Verification After Last Change（§十三/十四）

**关键新增字段**：`final_verification_after_last_change`

| workspace_changed | verification_passed | final_after_change | 含义 |
|---|---|---|---|
| true | true | True | 最终验证发生在最后修改之后 → verified_revision = new revision |
| true | true | False | 最终验证发生在最后修改之前 → verified_revision != new revision, verification_due = true |
| false | true | None | 无修改 → verified_revision = current revision（验证当前状态） |

**Rule**：只有 `CHANGED + PASS + final_after_change=True` 才允许 `verified_revision = new_revision`。

---

## 9. Workspace Revision Truth（§十七）

### 9.1 Simple Mutation Tools

| 工具 | committed 标记 | failed 标记 |
|---|---|---|
| edit_project_file | "已在...替换" | "错误：没有找到" |
| write_project_file | "已写入"/"已覆盖" | "错误：路径不存在" |
| write_code_file | "已写入" | "错误"/"permission" |
| save_note | "已保存" | "错误"/"失败" |
| sandbox_rollback | "已回滚" | "错误"/"失败" |

### 9.2 code_loop Compound Tool

| 场景 | mutation_effect | verification_result | epoch 推进 |
|---|---|---|---|
| unchanged + pass | UNCHANGED | PASS | ❌ 不推进 |
| changed + pass | CHANGED | PASS | ✅ 推进（if final_after_change=True） |
| changed + fail | CHANGED | FAIL | ✅ 推进（verification_due=true） |
| unchanged + fail | UNCHANGED | FAIL | ❌ 不推进 |
| no marker | UNKNOWN | PASS/FAIL | ❌ 不推进 |

### 9.3 总状态

```text
Workspace Revision Truth = PARTIAL
```

理由：simple mutation tools PASS，但 code_loop 的 `final_verification_after_last_change` 目前保守设为 None（无结构化标签时）或 True（有标签且 changed+pass 时）。Live Agens 数据尚未证明 code_loop 的 final_after_change 准确性。

---

## 10. Full Replay Fidelity（§十八/十九）

### 10.1 Hydration Invariant（升级）

```python
def _hydrate_invariant_check(snap, initial_epoch, mutation_seen):
    errors = []
    # 1. epoch 一致性
    if snap.get("revision") is not None and initial_epoch != snap.get("revision"):
        errors.append("epoch 不匹配")
    # 2. mutation_seen 一致性
    if snap.get("mutation_seen") is not None and mutation_seen != bool(snap.get("mutation_seen")):
        errors.append("mutation_seen 不匹配")
    # 3. verified_revision 存在性
    if snap.get("verified_revision") is not None and initial_epoch == 0:
        errors.append("verified_revision 非空但 epoch=0")
    return errors
```

### 10.2 Agens Snapshots 验证

| Snapshot | epoch | mutation_seen | verified_revision | inv_errors |
|---|---|---|---|---|
| Agens-M1 | 2 | True | None | [] ✅ |
| Agens-M4 | 1 | True | None | [] ✅ |

### 10.3 Schema Version 强制

```text
snapshot_schema_version >= 1: 缺关键字段 → INVALID_SCHEMA（不 fallback）
legacy snapshot（version=missing）: 允许 revision fallback
```

Agens Current Baseline 只使用 schema v1+ snapshots。

---

## 11. Snapshot Schema Validation（§二十/二十一）

### 11.1 Schema v1 必填字段

```json
{
  "snapshot_schema_version": 1,
  "revision": int,
  "evidence_epoch": int,
  "mutation_seen": bool,
  "verified_revision": int | null,
  "verification_passed": bool,
  "verification_due": bool,
  "verification_required": bool,
  "workspace_hash": str,
  "model_config": {...}
}
```

### 11.2 Legacy vs Current 分离

| 类别 | 来源 | 用途 |
|---|---|---|
| Legacy（phase22/snapshots_asyncfix/） | Qwen | Historical Reference only |
| Current（phase26/snapshots_agens/） | Agens | Current Baseline / Qualification |

---

## 12. Agens M1/M4 Snapshots（§二十二/二十三）

### 12.1 Capture 条件（均已满足）

```text
model = agnes-2.5-flash          ✅
provider = agens                 ✅
mutation_effect = CHANGED        ✅（M1 revision=2, M4 revision=1）
current_revision > 0             ✅
verification_required = true     ✅
verification_due = true          ✅
verification_passed = false      ✅
hydration full fidelity = PASS   ✅
```

### 12.2 Snapshot 详情

**M1**（revision=2，Agens 比 Qwen 多一步 mutation）：
- 57 input_items（含 reasoning summaries）
- 14 tools exposed
- workspace_hash: `a13e9fb5e6c4007c`

**M4**（revision=1）：
- 多次 attempt 后才成功 capture（前 5 次无 post-mutation llm_start）
- 14 tools exposed
- 相同 workspace_hash

---

## 13. Verification Scope（§二十七/二十八/二十九/三十/三十一）

### 13.1 当前实现

Window probe 的 `_analyze_window_evidence()` 使用 `DiscoveryTracker` 模拟 observe，但不检查 verification scope（PROJECT vs TARGET）。

### 13.2 限制

- `run_tests(project)` → scope=PROJECT（默认）
- `run_tests(project, target=...)` → scope=TARGET
- `run_python` → scope=UNKNOWN（无法从工具名判断）

**Phase 26 未实现 scope 检查**，evidence_pass 判定基于 `verified_revision == epoch` 而不考虑 scope 是否满足 obligation。

---

## 14. M1 Evidence Window（§二十四/二十五）

**N=10, K=3**

| 指标 | 值 | 比例 |
|---|---|---|
| First Semantic Verification | 0 | 0% |
| Semantic Verification Within-3 | 8 | 80% |
| **Evidence Within-3** | **6** | **60%** |

**分类分布**：PASS=6, W3=4

**W3 分析**（4 个 case）：
- 模型在窗口内产生了 verification（run_tests 通过）
- 但后续又产生了新的 mutation，导致 latest revision 未被验证
- 这表明 Agens 的 multi-step workflow 超出 K=3 限制

---

## 15. M4 Evidence Window（§二十四/二十五）

**N=10, K=3**

| 指标 | 值 | 比例 |
|---|---|---|
| First Semantic Verification | 1 | 10% |
| Semantic Verification Within-3 | 5 | 50% |
| **Evidence Within-3** | **4** | **40%** |

**分类分布**：W1=3, PASS=5, W3=2

**W1 分析**（3 个 case）：
- 模型在 K=3 步内未产生任何 verification 动作
- first=None（无 tool call），可能是快速 final text

**W3 分析**（2 个 case）：
- 同 M1：verification 后又有新 mutation

---

## 16. Latest Revision Coverage（§二十三）

| Case | revision_before | revision_after | verified_revision | coverage |
|---|---|---|---|---|
| M1 PASS (6/10) | 2 | 4-8 | == epoch | ✅ |
| M1 W3 (4/10) | 2 | 3-8 | != epoch | ❌ |
| M4 PASS (5/10) | 1 | 1-5 | == epoch | ✅ |
| M4 W3 (2/10) | 1 | 2-5 | != epoch | ❌ |
| M4 W1 (3/10) | 1 | 1 | None | ❌ |

**Actual Latest Revision Coverage**：10/20 = 50%

---

## 17. Qualification Gate（§二十六）

```text
M1 evidence_within_k >= 9/10?    6/10 = 60%  ❌
M4 evidence_within_k >= 9/10?    4/10 = 40%  ❌
Combined >= 18/20?               10/20 = 50% ❌
False Completion = 0?             ✅（0 false completion）
```

**Agens Qualification Gate = FAIL**

---

## 18. M1/M4 Full E2E（§二十八/二十九）

**NOT ENTERED**（Gate 未通过）。

---

## 19. M3 Closed Loop（§三十五）

**NOT ENTERED**。

---

## 20. Router/MCP Regression（§三十九）

- Router authority：PASS（Phase 22 fix 保留）
- MCP routing：PASS（Phase 22 fix 保留）
- **Router / MCP 冻结**

---

## 21. run_tests Production Qualification（§三十三）

| 条件 | 状态 |
|---|---|
| Workspace Revision Truth | ⚠️ PARTIAL（code_loop final_after_change 待验证） |
| Agens Evidence Window Gate | ❌ FAIL（50% < 90%） |
| Full E2E | ❌ NOT ENTERED |
| M3 | ❌ NOT ENTERED |
| Router/MCP | ✅ PASS |

→ **run_tests = EXPERIMENTAL**

---

## 22. Runtime Regression（§四十一）

```
command: tests/run_tests.py --exclude test_theme_cdp
collected: 781
passed: 781
failed: 0
skipped: 0
duration: 63.9s
exit: 0
```

**新增 tests（本轮）**：
- `tests/test_phase26_codeloop_dual.py`：13 tests（code_loop 双结果）
- `tests/test_phase25_codeloop_truth.py`：已移除（被 phase26 替代）
- Phase 24 tests：30 tests（更新 code_loop 测试适配新 API）
- Phase 23 tests：15 tests（不变）

**净增**：+2 vs Phase 25（779 → 781）

---

## 23. Current Production Status（§四十三）

```text
Runtime Semantic Baseline            = YES
Benchmark Truth Recovery             = YES
Mutation Commit Truth                = FIXED（三态）
Unknown defaults to committed        = NO
No-op mutation increments revision   = NO
Workspace Revision Truth             = PARTIAL（simple tools PASS, code_loop 待 live 验证）
code_loop mutation truth             = PASS（结构化输出已添加）
code_loop verification truth         = PASS（验证结果独立）
Snapshot Hydration                   = PASS（M1/M4 invariant check）
Agens Evidence Window                = 60%/40%（M1/M4 evidence_within_k）
Agens Qualification Gate             = FAIL（50% < 90%）
Full E2E                             = NOT ENTERED
M3                                   = NOT ENTERED
Router final exposure authority      = PASS
MCP routing                          = PASS
run_tests                            = EXPERIMENTAL

Current Behavior Baseline            = AGENS（部分建立）
Historical Behavior Baseline         = QWEN（归档）
```

---

## 24. Next Direction（§二十六）

**主要瓶颈**：Agens 在 K=3 步内仅 50% 产生 obligation-satisfying verification evidence。

**根本原因分析**：
1. **W3（40% of failures）**：模型在完成验证后又进行新 mutation，导致 latest revision 未验证。这是多步工作流超出 K=3 限制的表现。
2. **W1（M4 30%）**：部分 case 无 tool call（first=None），可能是模型快速输出 final text 或未正确解析 snapshot 的 input_items。

**建议下一步**：
1. **扩大 K**：测试 K=5 或 K=7 是否显著提高 evidence_within_k
2. **分析 W1 cases**：检查为什么部分 run 无 tool call（可能是 input_items 格式问题）
3. **analysis-only run**：不修改代码，只让 Agens 在已有 snapshot 上运行，看是否直接通过验证
4. **如果 K=5 达到 90%**：重新评估 Gate 标准（当前 K=3 可能过于严格）

---

## 最终输出

```text
Behavior Test Provider =
agens（ResilientProvider / https://apihub.agnes-ai.cn/v1）

Behavior Test Model =
agnes-2.5-flash

Agens Provider =
AVAILABLE

Provider failure layer =
NONE

code_loop mutation truth =
PASS（结构化 Phase24-outcome 已添加；unchanged+pass=UNKNOWN 正确）

code_loop verification truth =
PASS（verification_result 独立于 mutation_effect）

Full Replay Fidelity =
PASS（M1/M4 hydration invariant check 通过）

M1 first semantic =
0/10

M1 semantic within-3 =
8/10

M1 evidence within-3 =
6/10

M4 first semantic =
1/10

M4 semantic within-3 =
5/10

M4 evidence within-3 =
4/10

Actual Latest Revision Coverage =
10/20（50%）

Agens Qualification Gate =
FAIL（50% < 90%）

Full E2E =
NOT ENTERED

M3 =
NOT ENTERED

Router/MCP =
PASS

run_tests =
EXPERIMENTAL

Current Behavior Baseline =
AGENS（部分建立，evidence_within_k=50%）

Historical Behavior Baseline =
QWEN（Phase 23/24 数据归档）

Primary remaining bottleneck =
Agens 在 K=3 步内仅 50% 产生 latest-revision verification evidence；主要失败模式 W3（验证后又有新 mutation）表明多步工作流超出窗口限制

Next component to modify =
扩大 K 到 5 重跑 window probe；或分析 W1 cases 的 no-tool-call 根因
```

---

## 本轮最终问题的回答

> "模型在 3 步内想到了验证" 和 "模型在 3 步内真的为最新 workspace revision 获得了足以满足 Completion 的验证证据" 是不是同一件事？

**仍然不是。Phase 26 进一步量化了这个差距。**

| 指标 | M1 | M4 | Combined |
|---|---|---|---|
| First Semantic（意图层） | 0/10（0%） | 1/10（10%） | 1/20（5%） |
| Semantic Within-3（意图层） | 8/10（80%） | 5/10（50%） | 13/20（65%） |
| **Evidence Within-3（事实层）** | **6/10（60%）** | **4/10（40%）** | **10/20（50%）** |

**关键洞察**：
- "想到验证"（semantic）和 "获得验证证据"（evidence）之间的差距：M1 80%→60%（-20%），M4 50%→40%（-10%）
- 主要流失点：W3（验证后又有新 mutation）占失败的大部分
- 这意味着模型**知道**要验证，但在验证后继续工作，导致最新 revision 未被覆盖

**与 Qwen 历史数据对比**（仅供 Historical Reference，不得合并）：
- Qwen M1 semantic_within-3：10/10（100%）
- Qwen M4 semantic_within-3：10/10（100%）
- Agens 明显低于 Qwen 的历史表现

---

## 附：本轮新增/修改文件

| 文件 | 改动 |
|---|---|
| `runtime/completion.py` | 新增 `codeloop_outcome_of()`；`mutation_outcome_of()` 对 code_loop 返回 UNKNOWN |
| `runtime/runner.py` | code_loop 使用 `codeloop_outcome_of()` 替代 `_mutation_result_ok()` |
| `benchmark/decision_qualification.py` | snapshot 新增 `snapshot_schema_version`、`evidence_epoch`、`verified_revision`、`verification_passed` |
| `benchmark/bounded_window.py` | 使用 `build_model_provider()` 替代 `local_model_provider()`；新增 hydration invariant |
| `tests/test_phase26_codeloop_dual.py` | **新增** 13 tests |
| `tests/test_phase24_evidence_truth.py` | 更新 code_loop 测试使用 `codeloop_outcome_of()` |
| `tests/test_phase25_codeloop_truth.py` | **已移除**（被 phase26 替代） |
| `phase26/snapshots_agens/` | Agens M1/M4 live snapshots |
| `phase26/window/` | Agens evidence window 实验结果 |
| `phase26/regression.log` | 781 tests manifest |
