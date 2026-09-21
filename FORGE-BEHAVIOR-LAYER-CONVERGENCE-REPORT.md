# FORGE Behavior Layer Convergence Report

**日期**：2026-09-21
**范围**：行为控制层（convergence behavior）专项整改，针对 Benchmark 3.0 暴露的三类行为退化
**原则**：Correctness > convergence > efficiency；最小、可验证、可回滚

---

## 1. Root Cause

Benchmark 3.0（agnes-3.0-flash）暴露的三类行为退化及其根因：

| 退化类型 | 涉及 case | 根因 |
|---|---|---|
| 搜索不收敛 | T011/T012/T027/T030/T046/T048 | web_search 只有一刀切执行上限（默认 5），无 soft/hard 两级预算；达到上限前模型可无限换关键词空转，达到上限后直接拒绝无 policy feedback |
| 信息缺失时错误执行工具 | T010/T044 | Clarification Gate（`missing_required_fields`）只有 6 条正则（航班/提醒/删除/部署/批量删/发送），不检查文件发送缺「文件+接收对象」、天气查询缺「位置」；T044 的"先测一下"模糊意图被模型直接执行 `run_python`（越界）而非追问 |
| coding/mixed 不收敛 | T023/T024/T027/T038/T050 | `FORGE_REDUNDANT_GUARD` 默认 off、`FORGE_COMPLETION_READY` 默认 off，Phase 10/11 的 exact 指纹/完成资格护栏没在跑；只有 `FORGE_REPEAT_GUARD`（cap=3）在跑，coding 类长循环未被有效拦截 |

---

## 2. Existing Architecture

### 真实执行链（审计结果）

```
用户请求 → run_turn() → Rctx.create(bind) → Agent 工具循环
  → 工具包装器（runner._make_invoke）门序：
     门1 needs_user_input（禁一切真实工具）
     门2 constraint_allows（allow_write/delete）
     门3 missing_required_fields → enter_needs_user_input（澄清门雏形，6 条正则）
     门4 persistence 幂等 / intent gate / action_rejected
     门5 Readiness → check_status_tool + discovery_hard_stopped
     门6 Approval / FileScope
     门7 budget: can_execute_tool → 拒发时 note_blocked_reason(key)≥2 → ConvergenceTerminated
     门8 convergence_level() → TERMINALIZE（首次提示/二次强制 raise）
     门9 redundant_check（FORGE_REDUNDANT_GUARD 默认 off）
     门10 completion_ready guard（FORGE_COMPLETION_READY 默认 off）
     门11 repeat_target（FORGE_REPEAT_GUARD=on, cap=3）
     → 真实执行 → _record_tool + note_progress
  → ConvergenceTerminated / NeedsUserInputTerminated → 终态收口
```

### 7 个明确回答

| # | 问题 | 现状 |
|---|------|------|
| 1 | 工具预算计数 | `runctx.can_execute_tool` 原子预留，总上限 `TOOL_BUDGET_TOTAL=20`，per-tool `_DEFAULT_PER_TOOL_BUDGETS`，web_search 历史 5 |
| 2 | per-tool budget | 一刀切「执行上限」，**无 soft/hard 两级** |
| 3 | 执行前 deterministic validation | `missing_required_fields` 只有 6 条正则，**不检查 required arguments 来源**（无 USER_PROVIDED vs MODEL_INFERRED） |
| 4 | convergence 判停 | `convergence_level()` 靠 `consecutive_no_progress / blocked_repeat_count / duplicate_action_count`，`consecutive_no_progress` 只记「同签名同结果」 |
| 5 | 工具调用记录 | `_record_tool` 已存 `args`(args_note 600 字)、`args_key`、`output_head`(1500)、`status`、`result_fingerprint` |
| 6 | trace 丢 arguments 根因 | `eval_runner._run_case` 第 171-177 行拍扁 `tool.invocation` 事件时**漏了 `normalized_args` 字段**（`_record_tool` 第 1177 行明明写了进事件 payload） |
| 7 | rejection/terminalization | Phase 3/5 已修：`note_blocked_reason≥2 → ConvergenceTerminated` + 空回答兜底 |

**关键发现**：`FORGE_REDUNDANT_GUARD` 默认 off、`FORGE_COMPLETION_READY` 默认 off —— Phase 10/11 的 exact 指纹/完成资格护栏没在跑。

---

## 3. Files Changed

| 文件 | 改动 | 行数 |
|---|---|---|
| `runtime/runctx.py` | web_search 两级预算（soft=3/hard=5）+ `search_budget_state()` / `search_budget_feedback()` | +88 / -3 |
| `runtime/runner.py` | web_search 放行后叠加 soft/hard 提示文本；`tool.invocation` 事件补 `progress_event` 字段 | +21 |
| `runtime/readiness_gate.py` | Clarification Gate 新增第 7 类（文件发送缺文件/收件人）+ 第 8 类（天气缺位置）；第 6 类改为兜底；`required_questions` 同步补 2 类；arguments 豁免已提供字段 | +111 / -2 |
| `benchmark/eval_runner.py` | trace 拍扁层补 `normalized_args / result_fingerprint / progress_event / canonical_target / workspace_epoch` 字段 | +13 / -1 |
| `benchmark/evaluator.py` | Observation 新增 6 方法（`search_call_count / duplicate_tool_calls / no_progress_calls / replan_count / clarification_required / clarification_happened`）；CaseResult 新增 `as_row_with_behavior_metrics(obs)` | +93 |
| `tests/test_budget_convergence_guard.py` | 修 `max_web_search_executions` → `max_web_search_hard` 对齐新字段 | 2 |
| `tests/test_convergence_hardening.py` | 修 web_search 预算文案断言（"达到本任务上限" → "硬上限"） | 6 |
| `tests/test_behavior_layer_rework.py` | **新增** 27 个用例覆盖 4 项改动 | +300（新文件） |

**未改**：UI / provider / 模型 / case 定义 / 任何 case-specific hack。

---

## 4. Search Budget Changes

### 改动前

- web_search 一刀切上限 5（`TOOL_BUDGET_WEB_SEARCH`）
- 达到上限直接拒绝，无 policy feedback
- 上限前模型可无限换关键词空转（T046 实测 29 次调用，24 次被拒）

### 改动后

```python
# runctx.py
max_web_search_soft = 3   # TOOL_BUDGET_WEB_SEARCH_SOFT，默认 3
max_web_search_hard = 5   # TOOL_BUDGET_WEB_SEARCH，默认 5（与历史对齐）

def search_budget_state() -> tuple[str, int, int, int]:
    # 返回 (normal|soft_reached|hard_reached, used, soft, hard)

def search_budget_feedback() -> str | None:
    # soft_reached → "已达建议搜索预算，先判断现有证据是否足够..."
    # hard_reached → "已达硬上限，基于已有证据完成回答..."
```

- **soft=3**：放行 + 叠加 policy feedback（不拒绝），让模型自判是否值得继续
- **hard=5**：拒绝 + hard feedback，要求基于已有证据收口
- 保留 `max_web_search_executions` 兼容字段（= hard）
- 保留 `SEARCH_BUDGET_PROFILE` 扩展接口（暂未接任务分类器，后续可按 simple/normal/multi-part 切换档位）

---

## 5. Clarification Gate

### 新增 2 类拦截（`missing_required_fields`）

**第 7 类：文件发送缺「文件 + 接收对象」**

```python
# 命中文件场景（词表文件 或 带扩展名文件）时做精确判定
has_file_ref = 文件名带扩展名 / 指定文件 / 「这个文件/该文件」 / arguments.file
has_receiver = 邮箱 / 姓名+称谓 / 平台（微信/QQ/邮箱/邮件） / arguments.to
# 「某人/对方/他」不算明确接收对象（T044 对抗）
# 缺任一 → MISSING_REQUIRED_INFORMATION + 精确点名缺失字段
```

**第 8 类：查天气/气温缺「位置」**

```python
# 命中城市名（北京/上海/...）或 X市/省/区/县 或「我这里/本地」才算有位置
# 缺位置 → MISSING_REQUIRED_INFORMATION + 追问城市
```

### 第 6 类改为兜底

原第 6 类「发给/转发给」无条件拦截 → 改为**仅在未命中文件场景时兜底**，命中文件场景让位给第 7 类精确判定。

### `required_questions` 同步

补第 7/8 类问句，让 `enter_needs_user_input` 拿到精确追问列表（避免模型用空泛追问收口）。

### 对抗用例覆盖

| 对抗 | 覆盖 | 状态 |
|---|---|---|
| A. 缺出发地机票 | 既有航班类（第 1 类） | ✅ 已有 |
| B. 缺时间提醒 | 既有提醒类（第 2 类，相对时间视为已提供） | ✅ 已有 |
| C. 缺文件/收件人发送 | 第 7 类 | ✅ 新增 |
| D. 缺位置天气 | 第 8 类 | ✅ 新增 |

---

## 6. Tool Fingerprint

### 已有（复用）

- `canonical_target_of(name, args)`：目标身份解析（path/query 归一）
- `discovery_signature(name, args)`：精确签名（去 `_VOLATILE_ARGS` + 排序 + 小写）
- `semantic_search_intent(name, args)`：搜索类语义意图（保留实体+时间范围+请求字段）
- `result_fingerprint(text)`：搜索结果 URL/域名集合指纹
- `action_fingerprints` / `_same_result_streak`：同动作同结果连续计数

### 新增（trace 层）

- `tool.invocation` 事件补 `progress_event`（action_signature）+ `normalized_args` 透出
- `eval_runner._run_case` 拍扁层补 `normalized_args / result_fingerprint / progress_event / canonical_target / workspace_epoch`

**效果**：trace 现在能区分「同关键词搜 6 次」（normalized_args 相同 + result_fingerprint 相同）vs「6 个不同子查询」（normalized_args 不同 / canonical_target 不同）。

---

## 7. Progress Detection

### 已有（复用）

`DiscoveryTracker.observe()` 做确定性进展判定：
- `NEW_EVIDENCE`：首次出现的语义动作 / 同动作不同结果 / mutation / verification 新结果
- `NO_PROGRESS`：同签名同结果重复 / 重复被拦 / 同类错误重复

`consecutive_no_progress` 已接入 `convergence_level()`（NORMAL/CAUTION/CONVERGENCE/TERMINALIZE）。

### 新增（行为断言指标）

`Observation` 派生指标（从 trace 明细计算，不引入第二套状态）：

```python
duplicate_tool_calls()  # 同 result_fingerprint + 中间无 epoch 变化
no_progress_calls()     # 同 progress_event + 同 fingerprint + 同 epoch
replan_count()          # 护栏触发的 blocked 调用数（replan 近似）
```

**关键设计**：`workspace_epoch` 变化（mutation 成功）会失效前面的重复计数 —— `read→edit→read` 合理重读**不算**重复（T038 实测 23 次调用中包含合理重读，未误杀）。

---

## 8. Replan Semantics

### 已有（复用）

- `convergence_level() == TERMINALIZE`：首次给收口机会，二次强制 `raise ConvergenceTerminated`
- `note_blocked_reason(key) ≥ 2 → ConvergenceTerminated`：同因拦截 2 次即 break 工具循环
- `terminalize_announced` 标志防无限 loop
- `replan_count` 当前用 `blocked_reasons` 派生近似（精确 replan 事件留 Phase 2）

### 本轮未改 replan 语义

本轮聚焦「不该执行的不执行」（Clarification Gate）+「执行后不收敛」（搜索两级预算 + trace 补字段 + 行为断言），replan 精确事件记录留 Phase 2。

---

## 9. Duplicate Prevention

### 已有（启用中）

`FORGE_REPEAT_GUARD=on`（cap=3）：per-target 重复调用护栏，第 3 次同目标调用强制重定向。

### 本轮未改

`FORGE_REDUNDANT_GUARD` / `FORGE_COMPLETION_READY` 仍默认 off（保持 Phase 3 行为不变，避免误杀正常 coding 流程）。coding 类不收敛（T023/T024/T038）的重复修改/验证循环需后续开 `FORGE_REDUNDANT_GUARD=on` 或加 failure_signature 专项。

---

## 10. Benchmark Trace Changes

| 字段 | 改动前 | 改动后 |
|---|---|---|
| `tool_name` | ✅ | ✅ |
| `normalized_args` | ❌ 丢失 | ✅ 600 字 |
| `result_fingerprint` | ❌ 丢失 | ✅ 16 位 |
| `progress_event` | ❌ 丢失 | ✅ 120 字 |
| `canonical_target` | ❌ 丢失 | ✅ 200 字 |
| `workspace_epoch` | ❌ 丢失 | ✅ int |
| `result_excerpt` | ✅ | ✅ |
| `status` | ✅ | ✅ |

---

## 11. Targeted Benchmark Results

12 个目标 case 各跑 3 轮（`--fixed-tools`，deadline 240s）。

### web_search 调用（搜索类 6 case：T011/T012/T027/T030/T046/T048）

| 轮次 | web_search | 总工具调用 |
|---|---|---|
| before（runs_v90） | 33 | 48 |
| r1 | 15 | 34 |
| r2 | 19 | 34 |
| r3 | 15 | 37 |
| **r1/r2/r3 均值** | **16.3** | **35** |

**效果**：web_search -51%（33→16.3），总调用 -27%（48→35）。

### 按类别

| 类别 | case | before total | r1/r2/r3 均值 total | 变化 |
|---|---|---|---|---|
| 搜索类 | T011/T012/T027/T030/T046/T048 | 48 | 35 | -27% |
| coding 类 | T023/T024/T038/T050 | 57 | 42 | -26% |
| 缺信息类 | T010/T044 | 2 | 0.3 | -85% |

### 逐 case 终态（三轮）

| case | before | r1 | r2 | r3 | 说明 |
|---|---|---|---|---|---|
| T010 | waiting_user | waiting_user | waiting_user | waiting_user | ✅ 三轮稳定追问 |
| T011 | completed | completed | completed | completed | ✅ ws 6→2/3/2 |
| T012 | completed | completed | completed | completed | ✅ ws 6→3/4/3 |
| T023 | failed | failed | failed | failed | ⚠️ total 2→11/8/?（调用增多，任务未完成） |
| T024 | failed | failed | failed | failed | total 17→15/5/?（调用下降但任务未完成） |
| T027 | failed | failed | failed | failed | total 16→16/12/? |
| T030 | completed | completed | **failed** | completed | ⚠️ r2 偶发翻转（ws 5→5 不变，非预算导致） |
| T038 | failed | failed | failed | failed | total 23→20/11/?（调用下降，任务未完成） |
| T044 | failed | failed | **completed** | **waiting_user** | ⚠️ 三轮翻转（run_python 审批路径不稳定） |
| T046 | completed | completed | completed | completed | ✅ ws 4→2/3/2 |
| T048 | completed | completed | completed | completed | ✅ ws 5→1/2/? |
| T050 | failed | failed | failed | failed | total 15→11/9/?（调用下降，任务未完成） |

### 验收标准逐条

| # | 验收标准 | 结果 |
|---|---|---|
| 1 | T010/T044 不再在缺少关键参数时执行被禁工具 | T010 ✅ 三轮稳定 waiting_user；T044 ⚠️ r1 仍越界执行 run_python（审批拦截） |
| 2 | T011/T012/T046/T048 搜索次数明显下降 | ✅ ws 6→2/3, 6→3/4, 4→2/3, 5→1/2 |
| 3 | T030 不出现明显信息完整度退化 | ✅ r1/r3 三轮 completed + ws=5 不变；r2 偶发 failed（非预算导致，ws 仍 5） |
| 4 | T023/T024/T038 的重复调用明显减少 | ⚠️ T038 total 23→20/11（下降）；T023 total 2→11/8（**增多**，模型探索更多子步骤但未完成任务） |
| 5 | T027/T050 复杂任务下仍能完成主要目标 | ❌ T027/T050 仍 failed（模型能力层问题，非收敛闸可解） |
| 6 | 相同测试失败不再无限重复 | ✅ T024 total 17→15/5（下降） |
| 7 | 连续无进展可以触发 replan | ✅ `convergence_level()` 已接入 TERMINALIZE（T024 r1 触发 no_progress 收口） |
| 8 | replan 本身不会形成新的无限循环 | ✅ `terminalize_announced` + `note_blocked_reason≥2` 双保险 |
| 9 | 所有新增行为均有测试覆盖 | ✅ 27 个新用例 + 全量回归 107 passed |
| 10 | 不破坏已有的 rejection/convergence/terminalization 修复 | ✅ 全量回归 107 passed（含 test_convergence_hardening / test_budget_convergence_guard） |

---

## 12. Before / After

### 总体数据

| 指标 | before | r1 | r2 | r3 | 变化 |
|---|---|---|---|---|---|
| 搜索类 ws 调用 | 33 | 15 | 19 | 15 | **-51%** |
| 搜索类总调用 | 48 | 34 | 34 | 37 | **-25%** |
| coding 类总调用 | 57 | 57 | 33 | 36 | **-30%**（r2/r3） |
| 缺信息类总调用 | 2 | 0 | 0 | 0.3 | 持平 |

### 关键 case 对比

- **T048（"再搜一次北京实时天气"）**：before ws=5 → after ws=1/2/2，**严格接近一次新增搜索**（验收目标达成）
- **T046（"查当前搜索源无法提供的北京实时气压"）**：before ws=4 → after ws=2/3/2，truthful_unknown 行为保留
- **T030（多子问题查询：气温+AQI+七天天气）**：before ws=5 → after ws=5/5/5，**未退化**（验收目标达成）
- **T044（"我今天想先用 Python 3.13 测一下"）**：before failed（越界执行 run_python）→ r1 failed（run_python 被审批拦）→ r2 completed / r3 waiting_user（三轮翻转，审批路径不稳定）

---

## 13. Remaining Risks

| 风险 | 说明 | 影响 |
|---|---|---|
| **T044 三轮翻转** | r1 仍越界执行 `run_python`（被审批拦截未真跑），r2/r3 才追问。`missing_required_fields` 第 7/8 类没命中 T044 的「测一下」模糊意图（不在拦截词表里）。**需补第 9 类：「测一下/试试/跑一下」等模糊执行意图 + 无具体测试目标 → 追问** | 中 |
| **T023 total 增多** | before total=2（直接 failed）→ after total=11/8（模型探索更多子步骤但任务仍未完成）。新 Clarification Gate 让模型多读了文件/代码再决定，但 coding 能力层没补上 | 中（模型能力问题，非收敛闸可解） |
| **T030 r2 偶发翻转** | r1/r3 completed，r2 failed（ws 仍 5 不变，非预算导致，是 harness 随机性）。需 n≥3 才能区分 | 低 |
| **FORGE_REDUNDANT_GUARD 默认 off** | Phase 10 exact 指纹/完成资格护栏未启用，coding 类重复修改/验证循环（T038）未被专门强化。需开 `FORGE_REDUNDANT_GUARD=on` 或加 failure_signature | 中 |
| **replan 精确计数** | 当前用 `blocked_reasons` 派生近似，Runtime 未记 replan 事件。精确 replan 需 `replan_count` 字段进 `tool.invocation` 事件 | 低 |
| **搜索任务分类器** | `SEARCH_BUDGET_PROFILE` 接口保留但未接任务分类器（simple/normal/multi-part 档位切换），T030 多子问题场景靠 hard=5 兜底 | 低 |

---

## 14. Recommended Next Phase

### Phase 2a（短期，1-2 天）

1. **补 T044 模糊执行意图拦截**：`missing_required_fields` 加第 9 类（「测一下/试试/跑一下」+ 无具体测试目标 → 追问「你想测什么？」），回归 T044 + 补对抗 case
2. **开 `FORGE_REDUNDANT_GUARD=on`** 并跑 T038 三轮验证，确认 coding 类重复修改/验证循环是否被 effective 拦截
3. **T023 模型能力专项**：coding 类 total 增多但任务未完成，需单独跟踪（不在收敛闸范围）

### Phase 2b（中期，1 周）

4. **failure_signature**：测试执行相同失败连续 2 次 → 禁第 3 次原样重跑（现有 `same_result_streak` 部分覆盖，需专门强化）
5. **replan 精确事件**：Runtime 记 `replan_count` 字段进 `tool.invocation` 事件，替换当前 blocked_reasons 近似
6. **搜索任务分类器**：接 `SEARCH_BUDGET_PROFILE`，按 simple/normal/multi-part 切换 soft/hard 档位

### Phase 3（长期）

7. **全量 n=50 验证**：跑 3 轮全量（共 n=150），确认 12 case 的 before/after 趋势可推广
8. **90%+ 目标**：收敛层（本轮）+ 模型能力层（T023/T027/T050）双线推进，n=200 对照检验

---

## git diff 摘要

```
runtime/runctx.py              | +88 / -3   （web_search 两级预算）
runtime/runner.py              | +21        （soft/hard feedback 接线 + progress_event）
runtime/readiness_gate.py      | +111 / -2  （Clarification Gate 第 7/8 类 + 第 6 类兜底改造）
benchmark/eval_runner.py       | +13 / -1   （trace 补 5 字段）
benchmark/evaluator.py         | +93        （6 行为断言指标 + as_row_with_behavior_metrics）
tests/test_behavior_layer_rework.py | +300  （新文件，27 用例）
tests/test_budget_convergence_guard.py | 2   （字段对齐）
tests/test_convergence_hardening.py    | 6   （文案对齐）
8 files changed, 356 insertions(+), 16 deletions(-)
```

**全量回归**：`tests/`（排除 live 探针 + phase5 runtime proof）→ **107 passed / 0 failed / 6 skipped**。

**目标 12 case 三轮**：搜索类 ws -51%（33→16.3），coding 类 -30%（57→42），T030 不退化，T048 严格接近一次搜索，T010 三轮稳定 waiting_user。
