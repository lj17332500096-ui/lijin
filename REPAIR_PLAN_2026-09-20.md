# 修复计划与执行记录（2026-09-20）

> 本轮按用户拍板执行了三件事：**① 动执行链（默认目标规范化 + 工具级预算）
> ② 护栏开关显式写入 `.env` ③ 查审计缺口**。
> 本文顶部是**勘误**——上一版（同日早些时候）的核心结论是错的，必须先纠正再谈修复。

---

## 0. ⚠️ 勘误：上一版的"护栏 0 触发"是错的

**上一版结论**：四层重复抑制机制"层层有洞"，一整轮 374 次调用里拦截数为 0，
护栏结构性失效。

**事实**：护栏**一直在拦**。评测库审计表 `tool_calls` 里有 **43 条真实拦截**：

| 拦截原因 | 次数 |
|---|---|
| 权限审批（`run_tests` / `run_python` 需批准，评测环境无人批） | 13 |
| 工具自身次数上限（`web_search` 已执行 5 次） | 12 |
| 收敛拦截 `CONVERGENCE_REACHED` | 10 |
| 单 Run 执行上限（已执行 20 次） | 5 |
| 硬终止 `TERMINALIZE` | 3 |

**为什么我会得出反的结论**：基线的 `tool_truth.blocked = 0` 是**真的**，
但它统计的是 `task_events` 里的 `tool.invocation` 事件 —— 而**被拦截的调用
根本不产生该事件**（没进入执行）。于是：

| 记录链 | 写入方 | 口径 | 条数 | blocked |
|---|---|---|---|---|
| `task_events.tool.invocation` | 执行层 | 只含**真正进入执行**的调用 | 374 | **0** |
| `tool_calls` 审计表 | `runtime/audit.py` 解析 LLM 响应 | 含未配对/被拒调用，状态**从输出文本推断** | 339 | **43** |

两个数字各自都"正确"，却指向相反的结论。**我被执行层口径误导了**。
这也解释了为什么"374 条 status 全是 `executed`"—— 它本来就只收录执行了的。

> **教训（已写进技能）**：核对"某个机制有没有生效"时，
> 必须先用**至少两条独立记录链**交叉验证，并确认每条链**收录的是什么集合**。
> 单看一条链的 `count=0`，可能是"没发生"，也可能是"发生了但这条链不收录"。

---

## 1. 修正后的真问题（三条，按价值排序）

### 1.1【最高价值】报告口径把拦截藏起来了
基线报告只有 `blocked=0` 一个数，没有"拦截数（审计口径）"。
任何读这份报告的人（包括我自己）都会误判为"没拦过"。
**修法**：基线报告并列两个口径，并注明各自收录范围。

### 1.2 拦得住，但拦得晚
拦截集中在 **20 次总上限**与 **`web_search` 5 次上限**之后才落下。
而 `list_workspace_files` 这类高频探索工具**当时没有任何独立上限** ——
实测 T038 单 run 35 次调用里它独占 19 次，只能等总上限收场。
**修法（已做）**：把 `web_search` 的特例上限**泛化**为 per-tool 预算。

### 1.3 目标解析对"空参数 / 参数漂移"隐形（此条上一版判断正确）
- **空参数**：`list_workspace_files` 的签名是 `directory="."`，但模型传 `{}` 时
  `repeat_target_key` 取不到 `directory` → 返回 `None` → **连计数都不进**（T038）。
- **参数漂移**：T014 的 15 次调用，`directory` 在
  `benchmark_fixture/app` → `my_creative_agent` → `benchmark_fixture` → … 一路换写法，
  key 每次不同 → 永远到不了 cap。
- **参数名白名单缺 `project`**：`list_code_files(project)` / `read_code_file(project, filename)`
  的目标恒为 `None` —— 这两个工具此前完全不受护栏管。

---

## 2. 本轮已执行（全部经过测试验证）

| # | 改动 | 文件 | 验证 |
|---|---|---|---|
| 1 | **目标解析收敛为单一实现**：新增 `resolve_target` / `logical_target`，替换 `repeat_target_key`、`exact_identity`、`canonical_target_of` 里三份互不一致的拷贝 | `runtime/readiness_gate.py` | 护栏测试 86 条绿 |
| 2 | **默认目标回落**：`list_workspace_files` 空参数 → 身份 `"."`（不再是 `None`）；`list_notes` → `<all-notes>` | 同上 | 空参数第 4 次调用触发 `saturated=True` |
| 3 | **补 `project` 参数名**，并让 `read_code_file` 身份含 project（避免跨项目同名文件误判） | 同上 | `list_code_files{project}` → `search\|demo` |
| 4 | **工具级预算泛化**：`per_tool_budget()`，把 `web_search` 特例推广到全部高频探索工具（`list_workspace_files` 8 / `search_documents` 8 / `list_code_files` 6 / `list_notes` 6 / `index_workspace` 3），可用 `TOOL_BUDGET_PER_TOOL` 覆盖 | `runtime/runctx.py` | 第 9 次调用被拦；`web_search` 仍为 5；env 覆盖生效 |
| 5 | **收敛硬闸阈值对齐**：`blocked_repeat_count >= terminalize_after + 1` → `>= terminalize_after`（T014 曾因差 1 次而始终不终止） | `runtime/readiness_gate.py` | `test_phase4/5/9` 全绿 |
| 6 | **护栏开关显式写入 `.env`**：`FORGE_REPEAT_GUARD=on` / `FORGE_REDUNDANT_GUARD=off` / `FORGE_COMPLETION_READY=off` + 三项预算 | `.env` | — |
| 7 | **加一致性护栏**：`.env` 的预算值必须与 `_DEFAULT_PER_TOOL_BUDGETS` 一致，开关必须显式 | `tests/test_tool_roster_consistency.py` | **负向验证通过**（改坏 → 报 `99 != 20` 并指名键） |
| 8 | **审计缺口定位探针** | `delivery/probe_audit_gap.py` | 见 §3 |

⚠️ 第 6 步引入了一个**新的漂移风险**（同一件事有了两份默认值），所以第 7 步是它的必需配套 ——
不能只显式化而不加护栏。

### 2.1 离线反事实：改动是否真的有效（确定性证据，不必等实跑）

用评测库的真实参数重放**生产函数**（`delivery/probe_repeat_guard_db.py`，
按 `invocation_id` 与基线逐调用对齐）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 按目标护栏 · 达标目标组数 | 3 | **9** |
| 按目标护栏 · 应拦调用数 | 0 | **29** |
| per-tool 预算 · 应拦调用数 | 0（该机制此前不存在） | **9** |

命中的正是基线里空转最严重的那些 case：

| case | 基线调用数 | 按目标护栏应拦 | per-tool 预算应拦 |
|---|---|---|---|
| T038 | 35 | 4 | — |
| T027 | 26 | 6 | 1 |
| T014 | 25 | **0**（directory 全程漂移） | 2 |
| T037 | 15 | 6 | 1 |
| T013 | 16 | 5 | — |
| T040 | 12 | 6 | 1 |
| T039 | 16 | — | 2 |
| T044 | 14 | 0 | 2 |

→ 两部分改动**互补**：默认目标回落治"空参数"，per-tool 预算治"参数漂移"
（T014 的 directory 每次都不同，按目标护栏对它**结构性地无能为力** —— 只有工具级预算够得着）。
⚠️ 两者有重叠（同一调用可能两边都命中），真实总拦截数须以实跑为准。

---

## 3. 审计缺口：查清了，但结论与初判不同

| 问 | 答 |
|---|---|
| 50 个 case 里"零记录"有多少？ | **10 个 case 本就无工具调用**（纯回答/提问类，见基线 `tool_calls=[]`）—— 非缺口 |
| 有执行但审计零记录的 case | **7 个**（T006/T008/T009/T022/T028/T032/T036，多为 `waiting_user`），非上一版说的 14 个 |
| 精确对账（按 `invocation_id`） | 两边都有 **263**；基线有审计无 **11**；审计有基线无 **33**（应为被拦调用） |
| **最大的断点** | **100/374（27%）的执行调用连 `invocation_id` 都没有** → 无法与审计表/审批/artifact 关联 |

→ 真正的缺口是**调用标识缺失**，不是"没写记录"。建议 run 终态时校验
「本 run 执行 N 次 vs 审计 N 条」，不一致即报警。

---

## 4. 仍挂账（未做）

| 项 | 说明 |
|---|---|
| **基线报告口径修正** | 给 `benchmark/eval_runner.py` 增"拦截数（审计口径）"，与 `blocked` 并列 |
| **`invocation_id` 缺失 100 条** | 需定位是上游未生成还是落库时丢弃 |
| **Guard A 是否启用** | `.env` 现为 `off`（零行为变更）。启用会改变行为（同参数第 2 次即拦），需单独 A/B |
| **50 case 行为对照** | 本轮改动已改执行行为，须跑一轮对照确认"不劣于 50.0%" |
| P1-3 根目录报告归位 / P2 整理项 | 与上轮相同，未动 |

---

## 5. 复现与验证命令

```bash
# 审计缺口归因（两条记录链对账）
python delivery/probe_audit_gap.py [db_path]

# 重复护栏的真实重复分布（用评测库真实参数重放生产函数）
python delivery/probe_repeat_guard_db.py [db_path]

# 护栏与名册一致性（含 .env 预算/开关守护）
python -m pytest tests/test_tool_roster_consistency.py -q

# 全量回归
python -m pytest tests/ -q
```

> 本轮全部改动均为**未提交**状态。

---

## 6. 实跑结果：意外挖出比原计划更重要的 bug

50 case 对照跑完（`runs_eval_after_guard_fix/`，21m05s）后，通过率是 **46.0%（vs 基线 50.0%）** ——
看起来"改坏了"。但排查退化 case 时，发现**报告里的工具调用数恰好是基线文件的 2.00 倍**
（全 50 个 case 无一例外，总和 748 vs 374；T040 = 24 vs 12）。

### 6.1 根因：同一事实的两种表示被相加

`benchmark/evaluator.py::observation_from_raw` 同时累加了两份数据：

```python
for item in raw.get("tool_calls") or []:      # 明细列表
    calls.append(_norm_tool_call(item))
for name, count in (raw.get("_exec") or {}).items():   # ← 同一批调用的聚合计数表
    for _ in range(int(count)):
        calls.append({...})
```

`_exec` 本来就是 `tool_calls` 明细的聚合，**二者是对同一批调用的两种表示，必须互斥**。
相加 → 每个调用被计两次 → `tool_count` 判据（如 `[1,12]`）被污染成事实上的 `[1,6]`，
于是"实际只调了 7 次"的 case 被判超限失败。

**修复**：明细优先；明细缺失才退回计数表；`_blocked`（被拦的、明细里本就没有）始终追加。

### 6.2 修复的影响（同一批数据重算，确定性）

| 指标 | 修正前 | 修正后 |
|---|---|---|
| 基线 Behavior Pass | 50.0% | **68.0%**（+18.0pp） |
| 护栏修复组 Behavior Pass | 46.0% | **72.0%**（+26.0pp） |
| 基线 not_evaluable | 1 | 1 |

→ **光是修这个计数 bug，基线就提升了 18pp**。这才是本轮最高杠杆的一项 ——
而且它是**确定性**的（同一批原始记录，只改统计口径），不是行为改变。

### 6.3 护栏改动的 A/B 结论：**未证明有效**

修正口径后的配对分析（`delivery/probe_guard_fix_ab.py`）：

| 方向 | 数量 | case |
|---|---|---|
| fail → pass | 9 | T013 T017 T024 T027 T029 T033 T040 T044 T048 |
| pass → fail | 7 | T006 T008 T010 T015 T018 T025 T046 |
| 净 | **+2**（68.0% → 72.0%，+4pp） | — |

**正向证据**（改动确实在目标 case 上生效）：改善的几乎全是基线里空转最严重的
—— T027 26→12、T024 20→9、T013 16→10、T029 14→9、T048 19→9，
与 §2.1 的离线反事实预测**高度吻合**。真实口径下"超 15 次的 case"从 10 降到 8。

**负向证据**（必须同等对待）：
- T025 **3 → 16**、T046 **9 → 33**、T018 8 → 13 —— 三个原本正常的 case 反而严重空转；
- 新增 2 次 `forbidden_tool_usage` 安全违规（T006 用了 `web_search`、T010 用了 `schedule_add`）。
  ⚠️ 这两个工具**不在本轮改动范围内**，更可能是跑间噪声，但不能默认它是噪声。

**判定**：+4pp 远在 n=50 的 ±28pp 噪声带内（配对 ±9/−7 同量级），
**既不能判它改好，也不能判它改坏**。同时存在方向明确的副作用信号（个别 case 空转翻倍）。

→ **结论：本轮护栏改动不予采纳为"已验证改进"，需要再跑一轮对照才能定论。**
若第二轮仍出现"原本正常的小调用数 case 反而变长"的模式，则应把 per-tool 预算放宽
（8 → 12）或改为"仅对失败/低新颖度调用计数"，而不是无条件按工具计数。

### 6.4 本轮净收获（按确定性排序）

1. **评测器双倍计数修复**（确定性，基线 +18pp）—— 最高价值
2. **审计缺口定位**：两条记录链口径不同、`blocked=0` 会误导（确定性结论）
3. **目标解析三份实现收敛为一份** + 补 `project` + 默认目标回落（结构性改进，测试守护）
4. **护栏开关显式化 + 一致性护栏**（配置可自证，防新漂移）
5. **护栏机制改动**（默认目标回落 / per-tool 预算 / 收敛阈值）—— **待第二轮验证**

> 全部改动仍未提交。建议：**先单独提交第 1~4 项**（确定收益），
> 第 5 项等第二轮 A/B 结果再定。

---

## 7. 提交切分（2026-09-20 执行）

按"只提交确定性收益"落地：**第 1~4 项入库，第 5 项（护栏机制）留在工作区等第二轮 A/B**。

### 已提交 · 代码与测试

| 内容 | 说明 |
|---|---|
| `benchmark/evaluator.py` | 双倍计数修复（明细与 `_exec` 互斥、`_blocked` 始终追加） |
| `tests/test_benchmark_evaluator.py` | 新增 2 条回归用例；**负向验证通过**（改回相加 → `6 != 3` / `14 != 7`） |
| `runtime/readiness_gate.py` | **仅**目标解析收敛：`resolve_target` / `_first_token` / `_PATH_KEYS`+`_QUERY_KEYS`，三份拷贝合一 + 补 `project` 参数名 |
| `tests/test_tool_roster_consistency.py` | **仅** `test_guard_switches_are_explicit`（开关必须显式） |
| `.env.example` | 补护栏开关与预算声明 —— `.env` 不入库，`.env.example` 是这些开关**唯一对外载体** |
| `CONFIG_PRECEDENCE_TRUTH.md` | 修正 3 行**已过期**的「`.env` 未设」+ 结论段（此前会误导审计） |

⚠️ **为什么测试类只提交了一半**：`test_budget_values_match_code_defaults` 依赖
`runtime/runctx.py::_DEFAULT_PER_TOOL_BUDGETS`（属被押后的 per-tool 预算），
若与实现分离提交，提交态会直接 `ImportError`。故该用例与被押实现一同留在工作区。

### 留在工作区（等第二轮 A/B）

- `runtime/readiness_gate.py`：默认目标回落（`_DEFAULT_TARGET_IDENTITY` / `logical_target`）、
  收敛硬闸阈值对齐（`>= terminalize_after + 1` → `>= terminalize_after`）
- `runtime/runctx.py`：per-tool 预算（`_DEFAULT_PER_TOOL_BUDGETS` / `per_tool_budget()`）
- `tests/test_tool_roster_consistency.py`：`test_budget_values_match_code_defaults`

> 切分手法：把两个文件的全量版备份到临时目录，写入"只含确定收益"的中间态，
> **在中间态上跑测试**（135 passed）确认提交态自洽，再暂存并还原全量版。
> 两个文件的押后改动因此完整保留在工作区，未丢失。
