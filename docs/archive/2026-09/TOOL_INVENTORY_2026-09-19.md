# 工具清单对账报告（2026-09-19）

> 全部数字来自真实运行结果，非记忆估算。
> 复现：`./.venv/Scripts/python.exe delivery/probe_tool_inventory.py`
>       `./.venv/Scripts/python.exe delivery/probe_tool_redundancy.py`
>       `./.venv/Scripts/python.exe delivery/probe_code_loop_gate.py`
>       `./.venv/Scripts/python.exe delivery/probe_sandbox_smoke.py`
>
> **P0–P5 已全部处置完毕**，实施结果见 §4、验证见 §6。

---

## 1. 结论速览

| 项目 | 实测值 |
|---|---|
| Agent 当前注册工具数 | **37 个**（全部 `native`） |
| 技能工具（`skills/`） | **0 个**（`.env` 里**没有** `SKILLS` 行 → 白名单默认全关，27 个技能全部未启用） |
| MCP 工具 | **0 个**（`MCP_SERVERS` 配了 gitee，但 `default_tool_policy=deny`，无一挂载） |
| 路由表 `TOOL_TERMS` 条目 | **41 个**，其中 **5 个指向不存在的工具** |
| 生产库累计调用 | 6541 次 / 32 个工具名（含 5 个已下线名） |
| 注册了但生产从未调用 | **10 个** |
| 50 道评测题里路由可见 0 次 | **10 个** |
| 需修的问题 | **1 个真 bug + 6 个死引用 + 4 条门槛死词条**，均已处置（P5 为零行为变更） |

**"记忆里的 41 个"是路由表的条目数，不是工具数。** 实际注册表 37 个 —— 差的 4 个是
`gorden_ppt_*`（技能工具，技能未启用所以类不存在）。这个数字以前被误当成工具总数，
注意 `code_loop` 在注册表里的名字与 `agent.py` 的变量名 `codex_loop_tool` 不同。

**最终处置**：真正的"冗余工具"几乎不存在。10 个零调用工具里 7 个明确保留、
3 个（沙箱快照）经冒烟验证后保留；改动集中在**路由层**而非工具层。

**技能层现状**（2026-09-19 夜补充盘点）：27 个技能里**只有 3 个自带 `tools.py`**
（会注册工具）—— `gorden-ppt`（4 个工具，第三方 PPT 技能 v1.0.17，**非商业使用**许可）/
`dep_doctor`（1 个，Python 依赖体检）/ `skill-assets`（1 个，读技能内资源文件）；
其余 **24 个是纯提示词技能**，只注入 `skill.md` 文本、不注册任何工具。
上表 5 个死引用中 **4 个来自 `gorden-ppt`、1 个来自 `dep_doctor`**，
全部因"技能未启用"而产生，属**合法待启用状态**（P1 的护栏测试已把它们登记入册，
删技能目录会立刻红灯）。启用方式：`.env` 加一行 `SKILLS=dep_doctor,gorden-ppt`。

---

## 2. 全量清单（37 个，按生产调用量降序）

`prod` = 生产库累计调用次数；`vis` = 在 50 道评测题里被路由派发的题数。

| # | 工具 | prod | vis/50 | 最后调用 | 域 |
|---|---|---:|---:|---|---|
| 1 | `list_workspace_files` | 1934 | 45 | 09-19 | 只读·工作区 |
| 2 | `read_workspace_file` | 1609 | 45 | 09-19 | 只读·工作区 |
| 3 | `run_python` | 658 | 22 | 09-19 | 执行 |
| 4 | `web_search` | 557 | 14 | 09-19 | 联网 |
| 5 | `edit_project_file` | 356 | 18 | 09-19 | 写·真实项目 |
| 6 | `search_documents` | 355 | 31 | 09-11 | 检索·RAG |
| 7 | `get_current_datetime` | 243 | 47 | 09-19 | 基础[BASE] |
| 8 | `write_code_file` | 165 | 19 | 09-19 | 写·沙箱 |
| 9 | `recall_memory` | 135 | **0** | 09-19 | 记忆 |
| 10 | `list_notes` | 80 | **0** | 09-18 | 笔记 |
| 11 | `save_note` | 74 | 2 | 09-11 | 笔记 |
| 12 | `calculate` | 73 | 47 | 09-18 | 基础[BASE] |
| 13 | `remember` | 60 | 3 | 09-18 | 记忆 |
| 14 | `read_note` | 57 | **0** | 09-11 | 笔记 |
| 15 | `write_project_file` | 29 | 17 | 09-19 | 写·真实项目 |
| 16 | `forget_memory` | 18 | **0** | 09-09 | 记忆 |
| 17 | `schedule_list` | 17 | 2 | 09-09 | 定时 |
| 18 | `index_workspace` | 16 | 19 | 09-11 | 检索·建索引 |
| 19 | `schedule_add` | 10 | 3 | 09-09 | 定时 |
| 20 | `list_code_files` | 7 | 24 | 09-12 | 只读·沙箱 |
| 21 | `ask_image` | 5 | 2 | 09-11 | 多模态 |
| 22 | `read_office_file` | 5 | 3 | 09-11 | Office 读 |
| 23 | `run_tests` | 4 | 21 | 09-12 | 执行 |
| 24 | `read_code_file` | 2 | 24 | 09-12 | 只读·沙箱 |
| 25 | `code_loop` | 2 | 19 | 09-11 | 执行·循环 |
| 26 | `schedule_remove` | 1 | 2 | 09-09 | 定时 |
| 27 | `search_sources` | 1 | 11 | 09-09 | 检索·项目资料 |
| 28 | `schedule_set_enabled` | 0 | 2 | — | 定时 |
| 29 | `deep_research` | 0 | 1 | — | 联网 |
| 30 | `fetch_github_repo` | 0 | **0** | — | 联网 |
| 31 | `sandbox_snapshot` | 0 | **0** | — | 沙箱快照 |
| 32 | `sandbox_rollback` | 0 | **0** | — | 沙箱快照 |
| 33 | `list_sandbox_snapshots` | 0 | **0** | — | 沙箱快照 |
| 34 | `read_spreadsheet` | 0 | 2 | — | Office 读 |
| 35 | `save_word_doc` | 0 | **0** | — | Office 写 |
| 36 | `save_excel_workbook` | 0 | 1 | — | Office 写 |
| 37 | `save_ppt_deck` | 0 | **0** | — | Office 写 |

**已下线但库里有调用记录的历史名（5 个）**：`delete_file`(7)、`write_workspace_file`(5)、
`gorden_ppt_build`(3)、`gorden_ppt_templates`(1)、`write_note`(1)。
最后调用时间在 09-07 ~ 09-11，之后归于沉寂 —— 都是重命名或下线，属正常演进痕迹。

---

## 3. 四类问题与处置

### P0 · 真 bug：`code_loop` 在门槛表里名字写错（**已修**）

```python
# runtime/tool_router.py:572  _EXPLICIT_ONLY_TOOLS
"code_loop_tool",     # ← 实际注册名是 code_loop（agent.py 变量名 codex_loop_tool，
                      #    @function_tool 装饰后 name 取函数名 code_loop）
```

**⚠️ 后果方向容易搞反，实测已确认**：`_EXPLICIT_ONLY_TOOLS` 是**限制**而不是放行 ——

```python
if name in _EXPLICIT_ONLY_TOOLS:
    return name in mentioned or _high_gate_hit(name, text)
return True          # ← 不在表里 = 永远放行
```

所以错名导致的**不是**"派发不出去"，而是"本该受高门槛保护的自主循环工具
**一直被无条件放行**"。实测（`delivery/probe_code_loop_gate.py`）：
50 道评测题里 **12 题（24%）**曾被盲塞 `code_loop`，包括

- `帮我记一下：正式环境数据库不能直接执行 destructive migration。`
- `介绍你能做什么、有哪些技能、擅长什么，再告诉我北京天气。`

这些查询与"跑自主代码循环"完全无关。修复后门槛生效，只剩 **7 题**（命中
"修复/测试/运行"等语义）能派发。

> 这条更正很重要：同一张表在报告初稿里被描述成"白名单放行"，方向恰好相反。
> 判断"某个机制是加严还是放宽"必须读 `_allowed()` 的返回语义，不能看表名猜。

### P1 · 死引用：6 个路由条目指向不存在的工具（**已加护栏**）

| 死引用 | 出现位置 | 根因 |
|---|---|---|
| `gorden_ppt_build` | `TOOL_TERMS`、`_HIGH_GATE_TERMS` | `gorden-ppt` 技能未在 `.env` 的 `SKILLS` 里启用 |
| `gorden_ppt_templates` | `TOOL_TERMS`、`_HIGH_GATE_TERMS` | 同上 |
| `gorden_ppt_template_intro` | `TOOL_TERMS`、`_HIGH_GATE_TERMS` | 同上 |
| `gorden_ppt_apply_custom` | `TOOL_TERMS`、`_HIGH_GATE_TERMS` | 同上 |
| `scan_dependencies` | `TOOL_TERMS`、`_EXPLICIT_ONLY_TOOLS`、`_HIGH_GATE_TERMS` | `dep_doctor` 技能未启用 |
| `code_loop_tool` | `_EXPLICIT_ONLY_TOOLS`、`_HIGH_GATE_TERMS` | 名字写错（见 P0） |

**关键洞察**：这 6 个死引用的真正含义是"路由表与工具注册表之间没有一致性约束"。
每次下线工具 / 改动技能启用列表，都可能悄悄留下新死引用，**不会有任何测试报警**。

→ 修法**不是**把这 6 行删了（删了技能一启用又配不上），而是加断言测试把关系钉住。
新增 `tests/test_tool_router_consistency.py`（6 条）：
`路由表引用的名字 ⊆ 注册表工具 ∪ skills/*/tools.py 里定义的工具名`。

### P2 · 三组"看起来重复、实际是不同作用域"的工具（**已补描述，不合并**）

| 组 | 实际语义 | 生产调用 |
|---|---|---|
| `read_workspace_file` / `list_workspace_files` | 只读，任意工作区路径 | 1609 / 1934 |
| `read_code_file` / `list_code_files` | 只读，**仅限 `工作区/code_sandbox/<project>/`** | 2 / 7 |
| `write_code_file` | 写，**仅限沙箱项目** | 165 |
| `write_project_file` / `edit_project_file` | 写，**真实项目目录**，写前自动备份 | 29 / 356 |

结论：**这不是命名冗余，是有意的三层隔离（只读 / 沙箱可写 / 真实项目可写）**，不能合并。
但沙箱只读那对**描述只有 26/27 字**，比 `read_workspace_file` 的 203 字短一个数量级 ——
模型很难从中分辨"路径根不同"。已补写为 222/164 字，显式写明作用域与分工。

### P3 · 沙箱三件套（**已冒烟验证，保留**）

原判据"0 调用 = 可疑"经过三步追查后**被推翻**，结论改为保留：

1. **沙箱真有使用**：真身在 `F:\Byong-hermes\Byong-hermes\code_sandbox`（= `WORKSPACE_ROOT/code_sandbox`，
   **不在项目目录内**），下辖 24 个真实项目：`bug_fix`、`calc_fix`、`user_workspace`、`demo`…
   `write_code_file` 的 165 次里，`benchmark_fixture` 等评测占 65 次，其余约 40+ 次是真实使用。
2. **三件套功能完好**：`delivery/probe_sandbox_smoke.py` 跑完整闭环 ——
   拍快照 → 破坏（改内容 + 删文件 + 加文件）→ dry_run 预览 → 真实回滚 → 校验内容还原 → 列快照，
   **6/6 断言全部 PASS**。
3. **它其实早已被"降级为按需暴露"**：三件套都在 `_EXPLICIT_ONLY_TOOLS` 里（受高门槛保护），
   50 题可见 0 次 —— 我原先建议的"移出常规路由、改为按需显式调用"，系统本来就已这么做。

→ **不改代码。** 唯一一次快照是 `logs/sandbox_snapshots/demo`，属"备而未用"，不是"坏掉"。

### P4 · `search_sources` 从未被采纳的真因：**库是空的**（**已调整暴露位置**）

原判断"两套检索重复"**不准确**，实测数据推翻了它：

| 表 | 行数 |
|---|---|
| `project_sources`（登记） | 1（`parse_status = ok`，2026-09-06） |
| `source_chunks`（切片） | **0** |
| `source_chunk_vectors`（向量） | **0** |
| `source_chunks_fts`（全文索引） | **0** |

**决定性结论**：不是"模型不选"，而是"选了也是空" —— `search_sources` 背后
一条切片都没有，任何调用都必然返回空结果。对照组 `search_documents` 同一位置暴露、
但 `data/rag_index.json` 有 **2,085,564 B / 64 个文件**的索引，实测被调用 355 次。
**同样暴露策略下谁有用，差异在库内容，不在暴露位置。**

处置：把 `search_sources` 从 `_WEB_SUPPORT`（联网补齐族）移出。理由：
- 它是**本地资料检索**，与"联网查实时信息"是两件事。留在族里会让
  "北京天气/航班/股价"这类纯联网查询凭空多带一个本地资料工具；
- 实测 50 题里 **11 题**被这样捎带，采纳率 1/11；
- 用户明确提"参考资料/查资料/资料里"时，仍由 `TOOL_TERMS` 词条派发，**功能不丢**。

这是与项目既有设计完全同类的清理逻辑 —— `BASE_TOOLS` 注释里就写着：
"web_search 不再常驻：否则 coding/只读任务会被塞入无关联网工具，
成为模型决策漂移与 tool wandering 的主要放大器。"

### P5 · 门槛词条表的 4 条死词条（**已清理，零行为变更**）

与 P0 / P1 同源的**第 3 处表脱节**。`_high_gate_hit()` 全项目**只有一个调用点**，
且前置条件是 `if name in _EXPLICIT_ONLY_TOOLS` —— 因此 `_HIGH_GATE_TERMS` 里给
**不在生效集**的工具写的词条**永远不会被求值**。实测 `gorden_ppt_*` 四条长期如此。

**⚠️ 处置时差点做错，过程记在这里**：初判是"词条写了、名字忘了加进生效集"，
于是把 4 个名字收编进 `_EXPLICIT_ONLY_TOOLS` 并收紧词条口径。
结果 `tests/test_tool_router.py::test_natural_ppt_phrasing_keeps_gorden_tools`
**立刻变红**，它的注释写明了原因：

> 回归：任务 goal 里没有出现"套模板/做PPT"等精确词时，`gorden_ppt_*` 曾被裁出
> 16 工具窗口 → 模型按技能指令调用时得到 **"Tool ... not found in agent"**。

这条测试有**真实故障背景**。也就是说"gorden 族随 PPT 意图补齐、不受高门槛约束"
是**有意设计**（代价是"帮我写个季度总结"这类纯文字请求也会带上它们，4→8）；
那 4 条词条属于**设计变更后未清理的遗留**。正确处置是**删词条**，
而不是"把名字补进生效集" —— 后者会掐掉补齐路径，让
"简约商务总结汇报"这类自然口语丢掉工具。

| | 处置 | 结果 |
|---|---|---|
| 改动 | 从 `_HIGH_GATE_TERMS` 删除 4 条 gorden 词条（**14 → 10 条**） | ✅ |
| 未改动 | `_EXPLICIT_ONLY_TOOLS` 保持不含 gorden（与原状一致） | — |
| **等价性** | 8 题（自然口语 / 纯文字 / 纯联网 / 会议纪要）路由结果与改动前**逐题一致** | ✅ **零行为变更** |
| 护栏 | `test_high_gate_terms_are_effective`：双向断言「词条表键 ⊆ 生效集」且「生效集 ⊆ 词条表键」 | ✅ 新增 |
| 已知代价 | 纯文字"总结"请求仍带 4 个 PPT 工具（4→8）；**接受**，理由是换"模型按指令调用时工具一定在" | 写进测试与代码注释 |

代码里**两处**各加了一段注释（`_EXPLICIT_ONLY_TOOLS` 之后、`_HIGH_GATE_TERMS` 之内），
说明"刻意如此、别补回来"，避免后来者当成漏网之鱼再改一轮。

---

### P6 · 同类错名还有 8 份拷贝，且 4 张名册漏了 `run_tests`（**已修 + 已加护栏**）

P2 修复时只改了 `runtime/tool_router.py` 的 2 处。全局普查后，`code_loop_tool`
还留在 **8 个生产文件**里：

`benchmark/` 下的 `evaluator.py::RUN_TOOLS`、`behavior_analysis.py::VERIFY`、
`evidence_analysis.py::VERIFY`、`matrix.py::RUN`、`decision_checkpoint.py::VERIFICATION_TOOLS`、
`exposure_phase21.py::E2_VERIFY`、`decision_qualification.py`（内联 tuple），
以及 `runtime/readiness_gate.py::_P9_VERIFICATION_TOOLS`。

**错名本身无害**（每张名册里 `code_loop` 都在；实测历史全部 run 中
`code_loop_tool` 被调用 **0 次**、`code_loop` 也被调用 **0 次**）。真正的缺陷是**漏项**：

| 名册 | 含 `run_tests`？ | 含错名？ | 处置 |
|---|---|---|---|
| `runtime/completion.py::VERIFY_TOOLS` | ✅（**权威定义**） | — | 不动 |
| `runtime/readiness_gate.py::_P9_VERIFICATION_TOOLS` | ✅ | 有 | 删错名 |
| `benchmark/decision_checkpoint.py::VERIFICATION_TOOLS` | ✅ | 有 | 删错名 |
| `benchmark/exposure_phase21.py::E2_VERIFY` | ✅ | 有 | 删错名 |
| **`benchmark/evaluator.py::RUN_TOOLS`** | ❌ | 有 | **补 + 删** |
| **`benchmark/behavior_analysis.py::VERIFY`** | ❌ | 有 | **补 + 删** |
| **`benchmark/evidence_analysis.py::VERIFY`** | ❌ | 有 | **补 + 删** |
| **`benchmark/matrix.py::RUN`** | ❌ | 有 | **补 + 删** |

**后果（有真实影响的那一张）**：`evaluator.allows_tool()` 的语义是
「执行了不在 `tools_allowed` 里的工具 → `checks['tools_allowed']=False` → `hard_fail`」，
而 `cases.py` 的 `CODING_TOOLS = WRITE_TOOLS | RUN_TOOLS | READ_TOOLS | {...}`。
所以 **coding 类 case 只要真跑一次测试就被判失败**，理由是"执行了期望范围外的工具"。

**量化**（重放 8 个历史 run 目录，不重跑 Agent）：

| 臂 | 修正前 | 修正后 | Δ | Safety |
|---|---|---|---|---|
| A 基线（n=100） | 39.0% | **43.0%** | +4.0pp | 1 → 1 |
| B push（n=100） | 36.0% | **42.0%** | +6.0pp | 3 → 3 |
| C pull（n=100） | 38.0% | **43.0%** | +5.0pp | 0 → 0 |

`run_tests` 越界误判由 **60 次 → 1 次**（剩余 1 次在 T017，`read_only` case，
"只读任务要不要允许跑测试"属设计问题，未动）。结论方向不变、幅度更小：
B−A 由 −3.0pp → **−1.0pp**，C−A 由 −1.0pp → **0.0pp**；Safety 完全不变。

**根因不是名册本身，是护栏的作用域**：P1 新增的一致性测试只守
`runtime/tool_router.py` 的表，**从不看 `benchmark/` 与其它 runtime 模块**，
所以同一错名能在 8 个文件里存活。新增 `tests/test_tool_roster_consistency.py`（8 条）
把作用域扩到全项目模块级名册，并对类做负向验证（临时改回旧状态 → 4 条断言同时变红）。

逐项定性（漏项 vs 有意收紧）与"未决引用"登记见
[`archive/2026-09/EPISODE_RECALL_EXPERIMENT_REPORT.md`](archive/2026-09/EPISODE_RECALL_EXPERIMENT_REPORT.md) §11。

---

## 4. 处置与实施结果

| # | 动作 | 状态 | 改动位置 |
|---|---|---|---|
| **P0** | `code_loop_tool` → `code_loop`（2 处：`_EXPLICIT_ONLY_TOOLS`、`_HIGH_GATE_TERMS`） | ✅ 完成 | `runtime/tool_router.py` |
| **P1** | 新增路由表一致性断言（6 条测试） | ✅ 完成 | `tests/test_tool_router_consistency.py`（新文件） |
| **P2** | 补 `read_code_file` / `list_code_files` 描述（26/27 字 → 222/164 字） | ✅ 完成 | `code_exec.py` |
| **P3** | 追查沙箱三件套 → 冒烟验证 → **保留不改** | ✅ 完成（结论：不动） | 新增 `delivery/probe_sandbox_smoke.py` |
| **P4** | `search_sources` 移出 `_WEB_SUPPORT` | ✅ 完成 | `runtime/tool_router.py` |
| **P5** | 清理 `_HIGH_GATE_TERMS` 的 4 条死词条（**零行为变更**）+ 双向一致性断言 | ✅ 完成 | `runtime/tool_router.py`、`tests/test_tool_router_consistency.py` |
| **P6** | 8 处错名清理 + 4 张名册补 `run_tests` + 跨模块名册护栏（8 条测试） | ✅ 完成 | `benchmark/`×7、`runtime/readiness_gate.py`、`tests/test_tool_roster_consistency.py`（新文件） |

### 明确不要做

- **不要把 `gorden_ppt_*` 加进 `_EXPLICIT_ONLY_TOOLS`，也不要给它们补门槛词条。**
  它们走"意图族补齐"是**真实故障驱动**的有意设计（防 `Tool not found in agent`）。
  加进去会让"简约商务总结汇报"这类自然口语丢掉工具 —— 详见 P5。
- **不要因为 0 调用就删 Office 三件套 / `deep_research` / `fetch_github_repo`。**
  7 个零调用工具（含 `read_spreadsheet`、`schedule_set_enabled`）只是"这类请求还没来"。
- **不要合并三层文件工具**（只读 / 沙箱 / 真实项目）。那是有意的权限隔离。
- **不要把 `search_documents` 一起摘掉** —— 它与 `search_sources` 暴露位置相同但库非空。
  已加对照组测试防"顺手统一"。
- **不要动 `BASE_TOOLS` 的 4 件套** —— 45~47/50 的可见率正是设计意图。

---

## 5. 附带发现（未定论 / 待查）

### 5.1 项目内存在误导性的 `code_sandbox/` 残留

真沙箱在 `WORKSPACE_ROOT/code_sandbox`，但项目内也有一个 `my_creative_agent/code_sandbox/`
（`m3_fixture` 空目录 + `phase1_fixture` 2 文件 92 B）。来源已定位：
`tools_phase1_case2_4.py:141` 硬编码 `BASE / "code_sandbox" / "phase1_fixture"`，
**绕过了 `code_exec.SANDBOX_ROOT`**。

后果：统计沙箱使用情况时会看错地方（本次对账即先踩此坑，误判"沙箱几乎是空的"）。
该脚本无人引用、属一次性 Phase 1 验收工具，**未改**（避免顺手改无关代码）；
建议后续遇到时统一跟随 `SANDBOX_ROOT`。

### 5.2 ⚠️ `logs/backups` 不存在，但 111 条记录声称已备份（**待查**）

- `edit_project_file` / `write_project_file` 的返回值里有 **111 条**写着
  "原文件已备份到 `logs/backups/`"；
- 但 `logs/backups` **不存在**，且在 `F:\Byong-hermes` 全盘搜索**没有任何** `backups` 目录；
- 单独实测 `_backup()` 本身是好的：调用一次即落盘、内容与源一致（测试后已清理）。

两个可能方向（**均未证实**）：
1. 这些调用跑在被复制到别处的项目副本里（`BASE_DIR` 随之指向副本）；
2. 记录早于备份功能上线，返回文案是模板（但文案与当前代码一致，此解释偏弱）。

→ 下次真实 `edit_project_file` 之后立刻检查 `logs/backups` 是否出现，即可判定。
**在那之前，不要把"写前自动备份"当作已验证生效的能力。**

### 5.3 16 个工具的窗口截断会挤掉本该派发的工具

`result = result[:limit]`，`limit = max(len(base), min(max_tools=16, len(available)))`。
实测 `检索资料里的接口定义` 这类查询里，`search_sources` 虽被 `TOOL_TERMS` 命中，
但因同分排序靠后而被 16 个窗口截断。这与此前 A/B 实验中
`index_workspace`(16 题) / `search_documents`(11 题) 被挤出的现象同源。
**影响面未量化，本轮未处理。**

---

## 6. 验证

| 验证项 | 命令 | 结果 |
|---|---|---|
| 一致性测试 | `pytest tests/test_tool_router_consistency.py -q` | **11 passed**（含 P5 的**双向**门槛表断言） |
| **跨模块名册护栏（新）** | `pytest tests/test_tool_roster_consistency.py -q` | **8 passed** |
| 路由相关全量 | `pytest tests/test_tool_router.py tests/test_tool_router_consistency.py tests/test_convergence_hardening.py -q` | **123 passed** |
| benchmark 相关 | `pytest tests/ -q -k "benchmark or evaluator or matrix or evidence or behavior or decision or readiness or exposure"` | **141 passed / 4 skipped** |
| 受影响工具测试 | `pytest tests/test_tools.py tests/test_code_exec.py -q` | **21 passed** |
| code_loop 门槛 | `delivery/probe_code_loop_gate.py` | 影响 12/50 题，修复后 7/50 仍可派发 |
| 沙箱三件套冒烟 | `delivery/probe_sandbox_smoke.py` | **6/6 PASS** |
| **P5 行为等价** | 内联脚本：还原那 4 条词条前后逐题对比 | **8/8 题结果一致** → 零行为变更 |
| **P5 护栏有效性** | 临时还原旧状态后再跑断言 | 两条断言均**变红**并报出 4 个名字（说明测试真能抓，不是摆设） |
| **P6 重放忠实性** | `delivery/probe_benchmark_roster_audit.py` | 重放出 A 39.0% / B 36.0% / C 38.0%，与历史报告**逐位一致** |
| **P6 零行为变更** | 历史全部 run 中检索错名调用次数 | `code_loop_tool` = **0 次** → 删名可证无影响 |
| **P6 护栏有效性** | 临时把 `evidence_analysis.VERIFY` 改回旧状态 | **4 条断言同时变红**，精确报出文件名与行号 |
| 全量回归 | `pytest tests/ -q` | **1177 passed / 6 skipped / 0 failed**（127.20s） |

### 方法学备注

- 生产调用量来自 `agent.db` 的 `tool_calls` 表（以 `mode=ro` 只读打开）。
  该表混入过 final JSON 的 `kind` 字段值（`answer`/`questions`/`done`/`kind`），
  直接 `group by tool_name` 会多出 **4 个假工具名**，必须显式排除。
- "50 题可见率"是路由层的**派发**结果，不等于模型**采纳**。两者差额
  （`search_sources` 11 派发 / 1 采纳）才是真正值得看的行为信号。
- 本轮所有结论都基于**实测**（注册表导入、SQLite 只读查询、冒烟执行），
  未使用记忆中的数字。过程中修正了两处方向性错误（P0 门槛语义、P4 归因），
  两处都记在本报告中，不单独另发勘误。
