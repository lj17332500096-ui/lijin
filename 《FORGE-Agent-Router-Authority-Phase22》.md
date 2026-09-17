# FORGE Agent Router Authority — Phase 22 报告

> 日期：2026-09-12
> 主题：Benchmark Truth Recovery + Final Exposure Boundary Repair + MCP Routing Integration
> 基线：《FORGE-Agent-Final-Tool-Exposure-Phase21》 + 《...-Phase21-Addendum》 + 当前真实代码
> 模型：**Qwen3.6-35B-A3B**（`FORGE_MODEL_PREF=local`，`localhost:8080`）；禁止 gateway/agnes/外部模型。
> 本轮不继续：Structured Action Commitment / Prompt stacking / Planner / Workflow / Verification Planner / 新 Completion 系统。

---

## 1. Executive Summary

Phase 22 把 Phase 21 混在一起的三个问题**完全拆开**，分别给出确定性结论：

| 问题 | 结论 |
|---|---|
| **Benchmark checkpoint 问题** | **CONFIRMED（主因）**。Phase 20 的 M1 LIVE snapshot 在 `edit_project_file` **失败**后仍被标记 `verification_due=true` → 非法 post-mutation checkpoint。旧 M1 全部 0% 是 artifact。 |
| **Router exposure boundary 问题** | **CONFIRMED 并已修复**。`main._run_attempt` async 分支用全局 `assistant_agent`（120 tools）而非 Router 选择的 `active_agent`（14）。已最小修复。 |
| **模型问题** | **NOT THE CAUSE**。有效 checkpoint 下 Qwen M1 E0 = **10/10（100%）**，M1 早已可靠。 |

**有效 checkpoint 修正结果（Qwen，N=10）**：

| 变体 | 暴露 | M1 semantic | M4 semantic | Combined |
|---|---|---:|---:|---:|
| E0 Full | 120 | **10/10** | 7/10 | 17/20 (85%) |
| E1 Router-authoritative | 14 | **8/10** | 7/10 | 15/20 (75%) |
| E2 Verification-focused | 11 | 9/10 | 3/10 | 12/20 (60%) |
| E3 Verify+prep | 4 | 5/5 | 4/5 | 9/10 (90%) |

- **旧 M1 0% 被彻底推翻**：正确 checkpoint 下 M1 达 80–100%。
- **Async 暴露边界已修复**：真实 async run 的最终 `tools[]` 从 **120 → 14**，与 Router 选择**完全一致**（provider payload audit）。
- **MCP Routing 已接入 Router**：gitee 读任务正确暴露 `gitee_list_user_repos` 等，且不替换为 `fetch_github_repo`；普通 coding 暴露 14 个工具、**0 个 MCP**。
- **capability metadata 修复**：未知 MCP 工具不再一律 `MUTATION`（现为 READ/DISCOVERY/MUTATION 语义分离）。
- **Decision Qualification Gate**：E1 combined 15/20（75%）< 18/20 → **Full E2E = NOT ENTERED**；M3 = NOT ENTERED。
- **Structured Action Commitment = REJECTED FOR PRODUCTION**（维持）。
- **run_tests = EXPERIMENTAL**（有效 baseline 已大幅改善，但 M4 仍 70–80%，未达 Gate）。
- Regression：`tests/run_tests.py --exclude test_theme_cdp` → **723 passed / 0 failed / 0 skipped / exit 0**。

> 附带：用户新装的 `G:\llama.cpp-spark`（build 10514, commit 4a3635c32）**可运行 `Spark-X2.5-4B.gguf`**
> （:8081 已验证；该模型为 reasoning 模型，输出在 `reasoning_content`）。Phase 22 按规范仍用 Qwen。

---

## 2. Phase21 Addendum Recovery

Phase 21 主报告顶部已声明 `M1 恒 0%` 被 Addendum 推翻；本轮正式把它落为可审计事实：

- 读取 `phase20/`、`phase21/` 原始 artifacts；
- 用 `benchmark/decision_qualification.py::checkpoint_validity()` 对每个 snapshot 逐条校验 §二 Invariant；
- 结果见 `phase22/truth_errata.json`。

---

## 3. Phase21 Truth Errata

| Phase 21 报告 | Errata（当前唯一有效结论） |
|---|---|
| `M1 恒 0%`、`M1 choice prior`、`Case D` | **WITHDRAWN**（基于非法 checkpoint） |
| `M1 E0–E4 rates` | **WITHDRAWN** |
| 含旧 M1 的 combined exposure % | **WITHDRAWN** |
| `Full E2E NOT NEEDED`（由旧 M1 推导） | **WITHDRAWN** |
| `async 使用 global assistant_agent` | **STILL VALID**（已修） |
| `79 MCP global materialization` | **STILL VALID** |
| `Router 14 vs async final 120` | **STILL VALID**（pre-fix） |
| `M4 valid exposure ablation` | **STILL VALID** |
| `MCP control failure` | **STILL VALID**（pre-fix；已修） |
| `L1 production rejection` | **STILL VALID**（依据有效 M4 + latency） |

---

## 4. Invalidated M1 Artifacts

`phase20/snapshots/M1.json`：最后一次 mutation = `edit_project_file` → `错误：在 calc.py 里没有找到要替换的内容`，
但 runtime 仍 `mutation_seen=true`、`evidence_epoch=1`、`verification_due=true` → **INVALID_CHECKPOINT**。

继承该 snapshot 的全部结论标记 **INVALID / NOT EVALUABLE**：

```text
Phase20 LIVE M1 baseline = 0/10        INVALID
Phase20 L1 M1 self      = 0/5          INVALID
Phase20 L1 M1 oracle    = 0/5          INVALID
Phase21 E0 M1 = 0/10 / E1 M1 = 0/10    INVALID
Phase21 E2 M1 = 0/10 / E3 M1 = 0/5     INVALID
Phase21 E4 M1 = 0/5                    INVALID
```

`phase20/snapshots/M4.json` 的 mutation **成功** → M4 数据 **VALID，保留**。

---

## 5. Checkpoint Validity Invariant

`benchmark/decision_qualification.py::checkpoint_validity()`，全部成立才允许：

```text
VALID_POST_MUTATION_CHECKPOINT
```

条件：`mutation_tool_execution_success`、`mutation_evidence_exists`、
`mutation_result_explicit_success`、`evidence_epoch_incremented`、`mutation_seen`、
`current_revision_gt_previous`、`verification_required`、`verification_satisfied_false`、
`verification_due`、`snapshot_type_live`。

任一不成立 → `INVALID_CHECKPOINT`，**禁止进入 behavior statistics**。
`capture_live_snapshot` 现在用 `_mutation_result_ok()` 成功门槛抓取；`cmd_exposure` 对 LIVE snapshot
执行 `assert_checkpoint_valid()`（否则停止 exposure experiment）。

---

## 6. Mutation Success Semantics

**分类结论（benchmark vs runtime 明确区分）**：

- **Benchmark harness bug（主因，已修）**：抓取条件只看 `verification_due()+mutation_seen`，
  未要求 mutation 成功 → 抓到非法 checkpoint。
- **Runtime 语义观察（真实存在，本轮未改）**：`DiscoveryTracker.observe()` 对任何
  `_P9_MUTATION_TOOLS` 调用都置 `mutation_seen=True`；`RunContext.note_execution_identity()`
  对任何 mutation 工具调用都 `bump_epoch()`。即 **mutation attempt ≠ success** 仍会推进
  revision / obligation state。潜在风险：失败编辑可能虚假满足 mutation 义务。
  本轮按 §四十四“允许的生产改动清单”**未修改 runtime mutation 语义**，仅记录为 Next Direction。

---

## 7. Corrected M1 Snapshot

`phase22/snapshots/M1.json`（Qwen 真实 Full Agent prefix，mutation SUCCESS）：

```text
snapshot_type      = LIVE_SNAPSHOT_CHECKPOINT
validity           = VALID_POST_MUTATION_CHECKPOINT
mutation tool      = edit_project_file（已在 calc.py 替换 1 处匹配）
revision           = 1
verification_due   = true
tool_schemas       = 120（pre-async-fix）
mutation_success_proof = {result_ok: true, marker: SUCCESS_MARKER_MATCHED}
```

`phase22/snapshots/M4.json`：同样 VALID，revision=2。

## 8. Corrected M1 E0/E1

`phase22/e0e1`（Qwen，N=10，有效 checkpoint）：

| 变体 | 暴露 | run_tests | run_python(sem) | other | semantic |
|---|---:|---:|---:|---:|---:|
| M1 E0 | 120 | 10 | 0 | 0 | **10/10 (100%)** |
| M1 E1 | 14 | 8 | 0 | list 1 / NO_ACTION 1 | **8/10 (80%)** |

→ **M1 不再 0%**。旧 0% 完全由非法 checkpoint 造成。

## 9. Valid M4 E0/E1

| 变体 | 暴露 | semantic | 说明 |
|---|---:|---:|---|
| M4 E0 | 120 | 7/10 (70%) | run_tests 5 + run_python(sem) 2；1 PROVIDER_ERROR |
| M4 E1 | 14 | 7/10 (70%) | run_tests 4 + run_python(sem) 3；1 PROVIDER_ERROR |

Phase 21 有效 M4 证据保留：`M4 E0 = 5/10`、`M4 E1 = 9/10`（不同 snapshot / 不同采样，均 VALID）。

## 10. Corrected Exposure Causal Result

| 变体 | M1 | M4 | Combined |
|---|---:|---:|---:|
| E0 (120) | 10/10 | 7/10 | 17/20 (85%) |
| E1 (14) | 8/10 | 7/10 | 15/20 (75%) |
| E2 (11) | 9/10 | 3/10 | 12/20 (60%) |
| E3 (4) | 5/5 | 4/5 | 9/10 (90%) |

- 有效 checkpoint 下，**exposure 收缩不再是决定性因素**（E0 vs E1 差异在采样噪声内）；
- **E2 verification-focused 跨任务不稳定**（M4 30%）→ 不采用；
- **E3 上限 90%**。

---

## 11. Async Runtime Impact Matrix

| 入口 | mode | 实际使用 agent | 修复前影响 |
|---|---|---|---|
| `webapp.py /api/runs` | stream | active_agent | 无 |
| CLI 默认 | stream | active_agent | 无 |
| CLI `--mode async` | async | **global（BUG）** | Router 失效 |
| 语音会话（main.py） | async | **global（BUG）** | Router 失效 |
| 定时任务 `_run_task_prompt` | async | **global（BUG）** | Router 失效 |
| benchmark harness | async | **global（BUG）** | Router 失效 |
| `runtime/public_response.py` | stream | active_agent | 无 |

→ async bug 影响 CLI(async)/语音/定时/benchmark；**不影响 web（stream）**。不得描述为“所有生产路径 Router 都失效”。

## 12. Async active_agent Fix

`main.py::_run_attempt` async 分支：

```python
- return await Runner.run(assistant_agent, message, ...)
+ return await Runner.run(active_agent, message, ...)
```

与 sync/stream 一致；未重写 Runner。

## 13. Exposure Parity Tests

- 代码级：sync/stream/async 均用 `active_agent`。
- 单测：`tests/test_phase22_router_authority.py::ExposureParityTests`（3 mode 传同一 agent）→ PASS。
- **真实 provider payload audit**（`phase22/async_parity.json`）：修复后 async 真实 run 的
  `on_llm_start` agent.tools = **14 = Router 选择**；修复前 = 120。

---

## 14. MCP Tool Catalog

新增 `runtime/tool_router.py::build_tool_catalog(tools)`，每个候选提供：

```text
name / description / source(CORE|MCP|PLUGIN|SKILL|CONNECTOR) / server / domain /
capability / side_effect / risk
```

source 由真实注册路径属性决定（`_mcp_source` / `_tool_origin`），不靠名字猜。
`route_agent` 现在构建 catalog 并传入 `select_tool_names`。

## 15. MCP Capability Metadata

修复 `runtime/spec.py::capability_of`：未知外部工具不再因 `side_effect` 兜底一律 `MUTATION`；
新增 `semantic_capability_from_name()`（read/get→READ，list/search→DISCOVERY，create/update/delete→MUTATION）。

修复前后（真实 MCP 组成）：`MUTATION 79 → 28`、`READ 0 → 15`、`DISCOVERY 8 → 28`。
**semantic capability 与 side_effect/risk 分离**。

## 16. MCP Domain Approval

`_select_mcp_tools()`：只有 `domain/provider` 命中才批准 MCP 工具；mutation MCP 工具仅在
明确 mutation 意图或点名时暴露；相关性排序后最多补 6 个（`_MCP_MAX_ADD`）。
server→domain：gitee→source_control、chrome/playwright→browser、youtube→media、fetch→web、obsidian→notes、sqlite→database。

## 17. Provider Identity Routing

命中非 github 的 source_control provider（如 gitee）时，从结果中剔除 `fetch_github_repo`，
不得因同属 source_control 而互相替换。真实路由验证：

```text
"列出我的 gitee 仓库" ->
  base + read_spreadsheet + gitee_list_user_repos + gitee_list_repo_issues +
  gitee_list_repo_pulls + gitee_list_user_notifications +
  gitee_get_repo_issue_detail + gitee_get_user_info
（无 fetch_github_repo）
```

## 18. Gitee Control

- 路由级：`gitee_list_user_repos` 命中（相关性排序第一）。
- 决策级（`phase22/controls`，N=2）：first tool = `gitee_get_user_info`，`in_mcp=True`，`mcp_first_rate = 1.0`。
- **Gitee control = PASS**。

## 19. MCP Mutation/Approval Control

- 路由级：`"创建一个 gitee 仓库"` → 暴露 `gitee_create_repo`（MUTATION）。
- Approval 仍由既有 ApprovalGate 兜底（router 只决定 exposure，不放宽审批）。

## 20. Coding Control

- `"修复 calc.py 并运行测试"` → **14 个 coding 工具，0 个 MCP**。
- `M6/M7/M8`（N=2）无 MCP 漂移；`verification_due=false` 时无多余 verification。

## 21. Final Exposure Invariant

```text
Final model tools ⊆ Router-approved tools + strictly-proven system-required tools
```

- **严格证明的 all-task-required 工具 = ∅（0）**。`BASE_TOOLS`（get_current_datetime、
  calculate、read_workspace_file、list_workspace_files）是启发式默认，**未经证明**，不纳入 invariant。
- 因此生产候选为：`Final tools = Router-approved tools`。
- assertion：`capture_live_snapshot` 断言 live tools == route_agent tools（`phase22/async_parity.json`）。

## 22. Final Provider Payload Audit

- 方法：真实 async `run_turn` + `RunHooks.on_llm_start` 捕获实际交给模型的 agent；`agent.tools` 即 provider `tools[]`。
- 结果：修复后 M1/M4 live tools = **14 = Router**；修复前 = 120。
- **Post-router exposure pollution = FIXED**。

---

## 23. Corrected Decision Qualification

| case | E0 | E1 | E2 | E3 |
|---|---:|---:|---:|---:|
| M1 | 10/10 | 8/10 | 9/10 | 5/5 |
| M4 | 7/10 | 7/10 | 3/10 | 4/5 |
| Combined | 17/20 | 15/20 | 12/20 | 9/10 |

§三十三 Gate（Router-authoritative E1 combined ≥ 18/20）：**15/20 → NOT MET**。

## 24. Full E2E Decision

**NOT ENTERED**（E1 combined 75% < 90%）。不浪费长 Run。

## 25. M1/M4 Full E2E

NOT ENTERED。

## 26. M3

NOT ENTERED。

## 27. run_tests Qualification

| 条件 | 状态 |
|---|---|
| Corrected decision baseline | ⚠️ M1 可靠（80–100%），M4 70–80% |
| async exposure boundary | ✅ FIXED |
| MCP routing control | ✅ PASS |
| M1/M4 E2E | ❌ NOT ENTERED |
| M3 ≥2/3 | ❌ NOT ENTERED |

→ **run_tests = EXPERIMENTAL**。

## 28. Runtime Regression

- `tests/run_tests.py --exclude test_theme_cdp` → **723 passed / 0 failed / 0 skipped / 65.6s / exit 0**。
- 较 Phase 21（715）**+8** = 新增 `tests/test_phase22_router_authority.py`。Full suite（含 theme_cdp 8）= 731。
- 修改文件：`main.py`（async active_agent）、`runtime/tool_router.py`（catalog/MCP domain）、
  `runtime/spec.py`（capability metadata）、`runtime/runner.py`（传 catalog）、新增测试。
- **False Completion = 0**；**False Failure after satisfied obligations = 0（确定性）**。

## 29. Production Status

```text
Runtime Semantic Baseline            = YES
Benchmark Truth (post-recovery)      = YES（validity invariant enforced）
Async Exposure Boundary              = FIXED
sync/stream/async Exposure Parity    = PASS
Router = Final Exposure Authority    = YES（async 修复后全路径）
Post-router Exposure Pollution       = FIXED
MCP Domain Routing                   = PASS
MCP Capability Metadata              = FIXED
Structured Action Commitment         = REJECTED FOR PRODUCTION
Full Agent Verification Baseline     = M1 80–100% / M4 70–80%（valid checkpoint）
Full E2E                             = NOT ENTERED
M3                                   = NOT ENTERED
run_tests                            = EXPERIMENTAL
Local Microbenchmark Baseline        = NO
```

## 30. Next Direction

1. **M4 决策漂移**（有效 checkpoint）：M4 70–80%，E2 反而降到 30% → 属任务上下文特定漂移，
   需在 valid checkpoint 上继续研究（不是 exposure、不是模型、不是 checkpoint）。
2. **Runtime mutation-success gating**（§六）：明确 `mutation attempt ≠ success`，
   建议后续把 `mutation_seen`/`evidence_epoch` 与真实成功绑定（本轮未改，列为候选）。
3. **Always-required 工具最小化**：当前 BASE_TOOLS 4 个未经证明；后续可验证是否可降为 0。
4. Full E2E / M3：待 M4 corrected E1 ≥90% 后进入。

---

## 最终输出

```text
Phase21 M1 old checkpoint =
INVALID

Corrected M1 E0 semantic =
10/10

Corrected M1 E1 semantic =
8/10

M4 E1 semantic =
7/10

Corrected combined E1 =
15/20

Async exposure boundary =
FIXED

sync/stream/async exposure parity =
PASS

Router final exposure authority =
YES

MCP domain routing =
PASS

Gitee control =
PASS

Coding tool exposure =
14

MCP tools exposed in ordinary coding =
0

Post-router exposure pollution =
FIXED

Structured Action Commitment =
REJECTED FOR PRODUCTION

Full E2E =
NOT ENTERED

M3 =
NOT ENTERED

run_tests =
EXPERIMENTAL

Primary remaining bottleneck =
M4 post-mutation decision drift（valid checkpoint 下仍 70–80%；E2 收缩反而降至 30%）

Next component to modify =
M4 决策漂移研究（valid checkpoint）+ runtime mutation-success gating（mutation attempt ≠ success）
```

### 本轮最终问题的回答

> Phase 21 真正发现的是模型问题，还是 benchmark checkpoint 问题，还是 Router exposure boundary 问题？

**三者完全拆开：**

1. **Benchmark checkpoint 问题 = 主因（CONFIRMED）**：M1 的 “0%” 完全来自一个 mutation 失败却
   `verification_due=true` 的非法 checkpoint。修正后 M1 = 80–100%。**这不是模型问题。**
2. **Router exposure boundary 问题 = 真实且已修复（CONFIRMED）**：async 分支绕过 Router（120 vs 14），
   已修为 `active_agent`；但它在**有效 checkpoint 上的行为影响是次要的**（E0 vs E1 无稳定差异）。
3. **模型问题 = 不存在（REJECTED）**：Qwen 在正确 checkpoint 下 M1 100%。

→ Phase 21 的 “Agent 行为差” 是 **checkpoint 污染** 与 **exposure boundary bug** 的叠加，
**不是模型能力问题**。

---

## 附：本轮新增/修改文件

| 文件 | 改动 |
|---|---|
| `main.py` | async 分支 `Runner.run(active_agent)`（暴露边界修复） |
| `runtime/tool_router.py` | MCP/Plugin catalog + domain approval + provider identity + 相关性排序 |
| `runtime/spec.py` | `capability_of` 语义化，未知外部工具不再一律 MUTATION |
| `runtime/runner.py` | `route_agent` 构建并传入 catalog |
| `benchmark/decision_qualification.py` | `checkpoint_validity` / `assert_checkpoint_valid` / `mutation_success_proof` |
| `benchmark/exposure_phase21.py` | `--variants` / `--snapdir` / LIVE validity 断言 |
| `tests/test_phase22_router_authority.py` | **新增** MCP routing + capability + exposure parity 测试（8 项） |
| `phase22/snapshots/`、`phase22/snapshots_asyncfix/` | 有效 Qwen snapshots（修复前/后） |
| `phase22/e0e1/`、`phase22/e2e3/`、`phase22/controls/`、`phase22/audit/` | 实验结果 |
| `phase22/truth_errata.json`、`phase22/async_parity.json` | 真值/一致性 artifacts |
| `phase22/regression_full.log` | Regression Manifest 日志 |
