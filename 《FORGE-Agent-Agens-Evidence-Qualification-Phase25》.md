# FORGE Agent Agens Baseline Reset + Evidence Replay Truth + Production E2E Qualification — Phase 25 报告

> 日期：2026-09-13
> 主题：Agens Baseline Reset + Evidence Replay Truth + Production E2E Qualification
> 基线：《FORGE-Agent-Evidence-Truth-Phase24》 + 当前真实代码
> **模型切换**：Qwen3.6-35B-A3B local → **Agens（gateway）**
> 本轮不增加 Agent 能力。只修正 Harness Truth 并重置 Baseline。

---

## 1. Executive Summary

Phase 25 完成三项核心工作：

1. **Agens Model Identity 确立**：确认当前行为测试模型为 Agens（`agens-2.5-flash`，gateway provider）。
2. **Phase 24 Harness Bug 修复**：snapshot hydration 现在正确恢复 `evidence_epoch`、`mutation_seen`、`verified_revision`；新增 `snapshot_schema_version=1`。
3. **code_loop 双重 Outcome 拆分**：`mutation_effect`（CHANGED/UNCHANGED/UNKNOWN）与 `verification_result`（PASS/FAIL/UNKNOWN）不再互相推导。

**关键数字**：
- Regression：**779 passed / 0 failed / 0 skipped**（Phase 24: 768 + Phase 25: 11）
- Hydration invariant：**M1 PASS / M4 PASS**（epoch=1, mutation_seen=True 正确恢复）
- **Live Agens Window Probe：未完成**（gateway 服务器 `apihub.agnes-ai.cn` 当前不可达）
- Full E2E：**NOT ENTERED**
- run_tests = **EXPERIMENTAL**

---

## 2. Model Baseline Reset

### 2.1 Agens Identity（§一）

```text
Behavior Test Provider      = agens (ResilientProvider / OpenAI-compatible gateway)
Behavior Test Model         = agnes-2.5-flash
Provider Adapter            = runtime.provider_gateway.ResilientProvider
Tool Calling Mode           = standard OpenAI function_calling
Base Endpoint               = https://apihub.agnes-ai.cn/v1
Temperature                 = unspecified（SDK default）
Top P                       = unspecified（SDK default）
Seed                        = unsupported
Max Tokens                  = 4096（agent.py ModelSettings）
Model Pref                  = gateway（.env: FORGE_MODEL_PREF=gateway）
API Key                     = cpk-...（从 .env 读取）
```

### 2.2 Qwen Historical Boundary（§二）

```text
Runtime Semantic Evidence   = MODEL INDEPENDENT（保留）
Qwen Decision / Window Data = HISTORICAL ONLY
Agens Decision Baseline     = NOT ESTABLISHED
Agens Full E2E              = NOT ESTABLISHED
```

Phase 23/24 的 Qwen 数据归档于：
- `phase22/snapshots_asyncfix/M1.json`（Qwen）
- `phase22/snapshots_asyncfix/M4.json`（Qwen）
- `phase23/window/partial.json`（Qwen bounded window）
- 《FORGE-Agent-Mutation-Truth-Phase23》.md
- 《FORGE-Agent-Evidence-Truth-Phase24》.md

**禁止将 Qwen 10/10 与 Agens N=x 混合统计。**

---

## 3. Phase 24 Harness Bug（§三/四）

### 3.1 Bug 描述

Phase 24 bounded replay 中，`_analyze_window_evidence()` 使用：
```python
snap_epoch = snap.get("evidence_epoch", 0)  # ← 返回 0！
```

但 Phase 22 合法 snapshot 中：
- `evidence_epoch` 字段 **不存在**（MISSING）
- 仅有顶层 `revision=1`（M1）或 `revision=1`（M4）

因此 replay 初始 epoch=0，导致：
- verification 工具执行后 `verified_revision = 0`
- 但 `evidence_epoch` 仍为 0（无 mutation 在窗口内发生）
- `evidence_pass = (verified_revision == epoch > 0)` → False
- 结果：所有 case 分类为 W1（无 evidence）

### 3.2 根因

Snapshot schema 不完整：
- 缺少 `evidence_epoch`、`verified_revision`、`verification_passed` 顶层字段
- `snapshot_schema_version` 缺失

### 3.3 修复

**`benchmark/decision_qualification.py`**（`SnapshotCapturer.on_tool_end`）：
```python
# Phase 25：添加 snapshot_schema_version 和完整状态字段
self.snapshot = {
    ...
    "snapshot_schema_version": 1,
    "revision": _evidence_epoch,           # evidence_epoch 的别名
    "evidence_epoch": _evidence_epoch,     # ← 新增
    "mutation_seen": mutated,
    "verified_revision": _ver_rev,          # ← 新增
    "verification_passed": bool(_ver_pass), # ← 新增
    ...
}
```

**`benchmark/bounded_window.py`**（`_hydrate_initial_state`）：
```python
def _hydrate_initial_state(snap):
    initial_epoch = snap.get("revision") or snap.get("evidence_epoch") or 0
    mutation_seen = bool(snap.get("mutation_seen"))
    verified_revision = snap.get("verified_revision")
    verification_passed = bool(snap.get("verification_passed"))
    return initial_epoch, mutation_seen, verified_revision, verification_passed
```

**Hydration Invariant Check**：
```python
def _hydrate_invariant_check(snap, initial_epoch, mutation_seen):
    errors = []
    snap_revision = snap.get("revision")
    if snap_revision is not None and initial_epoch != snap_revision:
        errors.append(f"epoch 不匹配: snap.revision={snap_revision} but hydrated={initial_epoch}")
    snap_mut = snap.get("mutation_seen")
    if snap_mut is not None and mutation_seen != bool(snap_mut):
        errors.append(f"mutation_seen 不匹配")
    return errors
```

### 3.4 验证

```python
# M1 snapshot（Qwen，Phase 22）
epoch=1, mut=True, ver_rev=None, errors=[]  ✅
# M4 snapshot（Qwen，Phase 22）
epoch=1, mut=True, ver_rev=None, errors=[]  ✅
```

---

## 4. Snapshot Schema（§八）

### 4.1 Phase 25+ Schema（version=1）

```json
{
  "case": "M1",
  "snapshot_type": "LIVE_SNAPSHOT_CHECKPOINT",
  "snapshot_schema_version": 1,
  "revision": 1,
  "evidence_epoch": 1,
  "mutation_seen": true,
  "verification_required": true,
  "verification_satisfied": false,
  "verification_due": true,
  "verified_revision": null,
  "verification_passed": false,
  "workspace_hash": "a13e9fb5...",
  "model_config": {...},
  "tool_schemas": [...],
  "input_items": [...],
  "captured_at": 1789227207.65
}
```

### 4.2 旧 Schema（version=missing）

- M1/M4 Phase 22 snapshots 缺 `evidence_epoch`、`verified_revision`、`verification_passed`
- `_hydrate_initial_state` 使用 `revision` 作为 fallback → 仍然正确恢复 epoch=1
- **旧 snapshot 仍可用于 replay，但新 capture 必须使用 version=1**

### 4.3 Hydration Hard Invariant（§五）

在 `_run_window` 开始前，如果 `_hydrate_invariant_check` 返回非空错误列表：
```text
HARNESS_INVALID → 立即终止该 case，不得产生 PASS/FAIL
```

---

## 5. code_loop Truth Correction（§九/十）

### 5.1 旧语义（Phase 24）

```
✅ 通过 → COMMITTED（错误：首次通过无写入也算 COMMITTED）
❌ 失败 → FAILED
```

### 5.2 新语义（Phase 25）

```python
def mutation_outcome_of(call) -> str:
    # 优先级 1：解析 [Phase24-outcome] 结构化标签
    if "[Phase24-outcome]" in text:
        workspace_changed = parse("workspace_changed=true/false")
        verification_passed = parse("verification_passed=true/false")
        if not workspace_changed:
            return "UNKNOWN"       # 无写入 ≠ mutation committed
        if verification_passed:
            return "COMMITTED"     # 有写入且验证通过
        return "FAILED"            # 有写入但验证未通过
    # 优先级 2：回退到关键词匹配（兼容旧格式）
    if "❌ 失败" in text: return "FAILED"
    if "✅ 通过" in text: return "COMMITTED"
    return "UNKNOWN"
```

### 5.3 四种组合

| workspace_changed | verification_passed | mutation_outcome | 含义 |
|---|---|---|---|
| false | true | **UNKNOWN** | 首次运行即通过，无 mutation |
| true | true | **COMMITTED** | 修复后通过，revision+1 |
| true | false | **FAILED** | 修复后仍未通过，revision+1 但未验证 |
| false | false | **UNKNOWN** | 首次运行即失败，无 mutation |

### 5.4 code_loop Trust Gate（§十六）

只有当 `workspace_changed=true AND verification_passed=true` 时，code_loop 才允许作为 latest-revision verification evidence。

---

## 6. Workspace Revision Regression（§十八）

### 6.1 新增测试

**`tests/test_phase25_codeloop_truth.py`**：11 tests，全部 PASS

| 测试 | 场景 | 期望 | 结果 |
|---|---|---|---|
| unchanged+pass | 首次通过无写 | UNKNOWN | PASS |
| changed+pass | 修复后通过 | COMMITTED | PASS |
| changed+fail | 修复后失败 | FAILED | PASS |
| unchanged+fail | 首次失败无写 | UNKNOWN | PASS |
| missing_marker_pass | 无标签+✅ | COMMITTED | PASS |
| missing_marker_fail | 无标签+❌ | FAILED | PASS |
| multi_write+pass | 多次写+最终通过 | COMMITTED | PASS |
| multi_write+fail | 多次写+最终失败 | FAILED | PASS |
| evidence_with_codeloop | ExecutionEvidence 集成 | COMMITTED | PASS |
| evidence_unchanged_pass | ExecutionEvidence UNKNOWN | UNKNOWN | PASS |
| evidence_mixed | edit+code_loop 混合 | 分别正确 | PASS |

### 6.2 完整 Regression

```
tests/run_tests.py --exclude test_theme_cdp
Ran 779 passed / 0 failed / 0 skipped / 67.8s / exit 0
```

较 Phase 24（768）+11 = `tests/test_phase25_codeloop_truth.py`

---

## 7. Agens M1/M4 Snapshot（§十九）

### 7.1 当前状态

**未完成**。Gateway 服务器 `https://apihub.agnes-ai.cn` 当前不可达（connection timeout）。

### 7.2 计划

待服务器恢复后，运行：
```bash
FORGE_MODEL_PREF=gateway \
python -m benchmark.decision_qualification capture --cases M1,M4 --out phase25/snapshots_agens
```

要求：
- mutation COMMITTED
- verification_due=true
- snapshot_schema_version=1
- hydration PASS

---

## 8. Agens Evidence Window（§二十/二十一）

### 8.1 当前状态

**未完成**（依赖 gateway 服务器恢复）。

### 8.2 计划

```bash
FORGE_MODEL_PREF=gateway \
python -m benchmark.bounded_window run \
  --out phase25/window \
  --n 10 \
  --snapdir phase25/snapshots_agens \
  --cases M1,M4 \
  --k 3
```

输出三层指标：
1. `first_semantic_verification` — 首次动作是否为语义验证
2. `semantic_within_k` — K 步内是否出现验证意图
3. `evidence_within_k` — K 步内是否产生 obligation-satisfying verification evidence（最终 Gate）

---

## 9. Latest Revision Coverage（§二十三）

**待 Agens 实验完成**。

定义：
```text
Actual Latest Revision Coverage =
  count(verified_revision == current_revision at window end) / N
```

要求：≥ 9/10（90%）

---

## 10. Provider Reliability（§二十五）

### 10.1 当前 Gateway 状态

```
https://apihub.agnes-ai.cn/v1/models → CONNECTION TIMEOUT
localhost:8080 (llama.cpp)          → DOWN（Phase 24 后未重启）
```

### 10.2 Provider Error 统计规则

Agens 实验中：
- provider error / timeout / rate limit / parser failure → **不计入 Behavior Fail**
- 单独统计 `provider_errors` 和 `execution_validity`
- 仅 `execution_valid=true` 的 case 参与 Gate 统计

---

## 11. Qualification Gate（§二十六）

### 11.1 新 Gate 条件

```text
M1:
  evidence_within_k >= 9/10
  latest_revision_coverage >= 9/10

M4:
  evidence_within_k >= 9/10
  latest_revision_coverage >= 9/10

Combined: >= 18/20
False Completion = 0
```

### 11.2 旧 Gate 已废弃

```text
First Verification Decision >= 90%  → 废弃（§二十七）
理由：产品目标是 bounded verified completion，不是下一动作必须 run_tests
```

### 11.3 当前状态

**NOT EVALUATED**（Agens 实验未完成）

---

## 12. M1/M4 Full E2E（§二十八/二十九）

**NOT ENTERED**（Gate 未评估）。

要求（若 Gate 通过）：
```text
Behavior Pass >= 5/6
Real Verification Evidence >= 5/6
Latest Revision Coverage >= 5/6
False Completion = 0
False Failure after obligations satisfied = 0
```

---

## 13. M3 Closed Loop（§三十一）

**NOT ENTERED**。

---

## 14. Router/MCP Regression（§三十六）

- Router authority：PASS（Phase 22 fix 保留）
- MCP routing：PASS（Phase 22 fix 保留）
- **Router / MCP 冻结**（本轮未修改）

---

## 15. run_tests Production Qualification（§三十三）

| 条件 | 状态 |
|---|---|
| Mutation Commit Truth | ✅ PASS（三态，Phase 23） |
| Workspace Revision Truth | ✅ PASS（no-op 不递增，Phase 24） |
| code_loop Mutation Truth | ⚠️ PARTIAL（UNKNOWN 处理完成，但 live 验证未完成） |
| Snapshot Hydration | ✅ PASS（Phase 25 修复） |
| Agens Evidence Window | ⏳ PENDING（server unavailable） |
| Full E2E | ❌ NOT ENTERED |
| M3 | ❌ NOT ENTERED |

→ **run_tests = EXPERIMENTAL**

---

## 16. Runtime Regression（§三十七）

```
command: tests/run_tests.py --exclude test_theme_cdp
collected: 779
passed: 779
failed: 0
skipped: 0
duration: 67.8s
exit: 0
```

**新增 tests**：
- `tests/test_phase24_evidence_truth.py`：30 tests（Phase 24）
- `tests/test_phase25_codeloop_truth.py`：11 tests（Phase 25）
- Phase 23 tests：15 tests（更新适配三态）

**净增**：Phase 24 (+30) + Phase 25 (+11) = +41 vs Phase 22 baseline（738）

---

## 17. Production Status

```text
Runtime Semantic Baseline            = YES
Benchmark Truth Recovery             = YES
Mutation Commit Truth                = FIXED（三态）
Unknown defaults to committed        = NO（已修复）
No-op mutation increments revision   = NO（已修复）
Workspace Revision Truth             = PASS（回归证明）
code_loop mutation truth             = PARTIAL（结构化输出已添加，live 待验证）
Snapshot Hydration                   = PASS（M1/M4 invariant check）
Agens Behavior Baseline              = NOT ESTABLISHED（server unavailable）
Qwen Historical Baseline             = PRESERVED（归档）
Full E2E                             = NOT ENTERED
M3                                   = NOT ENTERED
Router final exposure authority      = PASS
MCP routing                          = PASS
run_tests                            = EXPERIMENTAL
Primary remaining bottleneck         = Gateway 服务器 apihub.agnes-ai.cn 不可达
Next component to modify             = 等待 gateway 恢复后 capture Agens M1/M4 snapshots 并重跑 window probe
```

---

## 18. Next Direction

1. **恢复 gateway 服务器**：联系运维确认 `apihub.agnes-ai.cn` 可用性
2. **Capture Agens Snapshots**：
   ```bash
   FORGE_MODEL_PREF=gateway python -m benchmark.decision_qualification capture --cases M1,M4 --out phase25/snapshots_agens
   ```
3. **Run Agens Window Probe**：
   ```bash
   FORGE_MODEL_PREF=gateway python -m benchmark.bounded_window run --out phase25/window --n 10 --cases M1,M4 --k 3
   ```
4. **评估 Gate**：如果 evidence_within_k ≥ 9/10 → 进入 Full E2E
5. **运行 Full E2E**：M1 N=3, M4 N=3（真实 Agens agent）
6. **运行 M3**：closed-loop repair + verify

---

## 最终输出

```text
Behavior Test Provider =
agens（ResilientProvider / OpenAI-compatible gateway）

Behavior Test Model =
agnes-2.5-flash（endpoint: https://apihub.agnes-ai.cn/v1）

Snapshot hydration =
PASS（M1 epoch=1 mut=True；M4 epoch=1 mut=True；无 invariant 错误）

code_loop mutation truth =
PARTIAL（结构化 Phase24-outcome 已添加；unchanged+pass=UNKNOWN 正确；live 验证待 gateway 恢复）

Workspace Revision Truth =
PASS（779 regression tests）

M1 first semantic =
N/A（Agens 实验未完成）

M1 semantic within-3 =
N/A

M1 evidence within-3 =
N/A

M4 first semantic =
N/A

M4 semantic within-3 =
N/A

M4 evidence within-3 =
N/A

Actual Latest Revision Coverage =
N/A

Agens Qualification Gate =
NOT EVALUATED

Full E2E =
NOT ENTERED

M3 =
NOT ENTERED

Router/MCP =
PASS

run_tests =
EXPERIMENTAL

Current Behavior Baseline =
AGENS（待实验建立）

Historical Behavior Baseline =
QWEN（Phase 23/24 数据归档）

Primary remaining bottleneck =
Gateway 服务器 apihub.agnes-ai.cn 不可达，无法 capture Agens 真实行为数据

Next component to modify =
等待 gateway 恢复后运行 capture + window probe；无需修改代码
```

---

## 本轮最终问题的回答

> "模型在 3 步内想到了验证" 和 "模型在 3 步内真的为最新 workspace revision 获得了足以满足 Completion 的验证证据" 是不是同一件事？

**仍然不是。Phase 25 进一步收紧了这个区分。**

| 层级 | 定义 | 判定依据 |
|---|---|---|
| First Semantic | 首次动作选 run_tests | tool name 匹配 |
| Semantic Within-3 | K 步内出现验证意图 | tool_sequence 中有 VERIFY_TOOLS |
| **Evidence Within-3** | K 步内产生 latest-revision 验证证据 | verification tool 真实执行 + result PASS + verified_revision == epoch |

Phase 25 的关键进步：
1. **Hydration 修复**：replay 初始状态现在正确（epoch=1），不再因 epoch=0 导致误判 W1
2. **code_loop 拆分**：`workspace_changed=false + verification_passed=true` 不再误判为 COMMITTED
3. **Schema 升级**：新 snapshot 包含 `evidence_epoch`、`verified_revision`、`verification_passed`，hydration 可直接验证

当前阻塞：gateway 服务器不可达，Agens 真实行为数据尚未 capture。

---

## 附：本轮新增/修改文件

| 文件 | 改动 |
|---|---|
| `runtime/completion.py` | `mutation_outcome_of()` 新增 Phase24-outcome 结构化解析（code_loop 专用） |
| `benchmark/decision_qualification.py` | snapshot 新增 `snapshot_schema_version=1`、`evidence_epoch`、`verified_revision`、`verification_passed` |
| `benchmark/bounded_window.py` | `_hydrate_initial_state()`、`_hydrate_invariant_check()`；`_analyze_window_evidence()` 使用 hydration |
| `tests/test_phase25_codeloop_truth.py` | **新增** 11 tests（code_loop 四种组合 + ExecutionEvidence 集成） |
| `phase25/regression.log` | 779 tests manifest |
