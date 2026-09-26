# 修复计划（基于 2026-09-19 实测基线）

> 依据：全量测试实跑、`git status` 全量分类、密钥泄漏扫描、目录/文件逐个核对。
> 所有数字均为本轮实测，非估计。

---

## 0. 体检结论：没有 P0

| 维度 | 实测结果 | 判定 |
|---|---|---|
| 全量测试 | **1177 passed / 6 skipped / 0 failed**（127.53s） | 健康 |
| 密钥泄漏 | `.env` **从未进入任何提交**；已跟踪文件中的 `sk-` 命中全是假值（`sk-probe-not…` / `sk-123456789…` / `sk-secretval…`） | 安全 |
| 主干源码入库 | 本次补录 173 项（`cli/` 整包、`runtime/episode*`、`resilience`、`ui_frozen`、`trace_export`、`benchmark/eval_runner`、11 个测试文件） | 已修 |
| 真实数据丢失 | **无**（见 §1 更正） | 虚惊 |
| TODO/FIXME 存量 | 源码中仅 2 处 | 干净 |

所以本计划**不含"救火"项**，全部是收敛与欠账。

---

## 1. 关键更正：那 1555 个删除里没有你的真实数据

上一轮我把其中 910 个称为"你的真实数据"，**这个说法是错的**。逐个查实后：

**forge_data 的 753 个被删源文件只有 6 个不同文件名**

| 文件名 | 副本数 |
|---|---|
| `输入.docx` | 189 |
| `P.xlsx` | 187 |
| `B.docx` | 187 |
| `来源.md` | 137 |
| `4Դ.md`（`来源.md` 的乱码名） | 52 |
| `银行流水2021.xlsx` | 1 |

git 记录 840 个项目沙箱，磁盘只余 99 —— 同一批文件被反复导入 759 个测试项目产生的堆积。

**notes 的 133 个**：抽读内容全是 agent 跑测试生成的 demo（"月度支出表 1550 元"、"北京出行推荐-户外公园"、"公众号欢迎语"），且同标题在一分钟粒度上重复出现（`20260904_2126_月度支出表.md` / `20260904_2127_月度支出表.md`）。

**真正要保护的东西全都在**

| 资产 | 状态 |
|---|---|
| `materials/关于对王庄子乡无手续涉气企业专项督查报告.docx` | git 跟踪中，磁盘完好（commit `667130b`） |
| `.env`（含 agnes / Tavily 密钥） | 未跟踪，从未入库 |
| `.workbuddy/memory/` 记忆库 | 受 `.gitignore` 例外保护，完好 |
| `data/`、`logs/`、`*.db` | 被忽略，完好 |

→ **结论：这 1555 个删除可以放心提交，仓库由此收敛。**

---

## 2. 修复项清单

### P1 — 仓库状态（唯一有实质风险的一类）

#### P1-1｜1555 个删除长期挂在工作区 = 定时炸弹
- **现状**：`git status` 永远脏。任何 `git add -A` / `git commit -a` / IDE 的"全部提交"都会把这 1555 个删除写进历史 —— 本轮若不是逐类核对，差点就这么做了。
- **动作**：提交以收敛（`git add -A` → commit）。
- **验收**：`git status --porcelain` 只剩运行时会话文件（2 个 sqlite 临时文件）。

#### P1-2｜`.gitignore` 缺口 → 运行产物持续污染 status
- **现状**：本轮刚把 `runs_eval*`（408 个）、`forge_data/`（81 个）纳入跟踪，但它们是**运行产物**，下次跑评测 / 建项目又会变脏，且会重演本轮"1555 个删除"的堆积。
- **建议补齐**：
  ```
  # 运行产物（不应跟踪）
  runs_eval*/
  forge_data/
  exports/
  report_*.json
  runs_*_rep*.log
  *.sqlite-shm
  *.sqlite-wal
  ```
- **配套**：`git rm --cached -r runs_eval* forge_data exports`（只摘跟踪，磁盘文件不动）。
- ⚠️ 这属于**改变仓库约定**，需你确认。

#### P1-3｜根目录 14 份报告散落
`ARCHITECTURE_REVIEW_2026-09-19.md`、`TOOL_INVENTORY_2026-09-19.md`、`EPISODE_RECALL_EXPERIMENT_REPORT.md`、`FORGE-RUNTIME-TRUTH-AUDIT*.md` 等平铺在根。
**建议**：归入 `docs/reports/`。低风险，但影响可读性。

---

### P2 — 代码层挂账

#### P2-1｜5 个引用不存在的工具名（护栏例外表里的"未决"项）
| 名字 | 出现位置 | 性质 | 建议 |
|---|---|---|---|
| `web_search_v2` | `runtime/completion.py:498`、`runtime/readiness_gate.py:81,405`、`benchmark/evaluator.py:55` | 实际注册名是 `web_search`，`_v2` 从未存在 | **删**（纯遗留） |
| `delete_task` | `benchmark/evaluator.py`×3、`benchmark/cases.py`×2、`benchmark/matrix.py:90`、`runtime/runctx.py:140` | 被当作"禁止删任务"的语义占位 | **二选一**（见下） |
| `sandbox_rollback`、`forget_memory`、`schedule_remove` | 同上名册 | 疑为同类占位 | 与 `delete_task` 一并定论 |
| `ask_user` | 仅见于 behavior **标签表** | 是期望行为名，**不是工具引用** | 不动，但要把护栏例外表的注释从"疑为误入"改正 |

**待你拍板**：`delete_task` / `sandbox_rollback` / `forget_memory` / `schedule_remove` 这一族是
① 删掉这些禁止项（工具本就不存在，禁它无意义），还是
② 真的补上对应工具再启用？

#### P2-2｜TUI 上下文提示是死代码
`cli/tui/app.py:785 _update_footer()` 被调用 **14 次**，但它依赖的 `self._hint_label` **从未被创建** → 整个函数是 no-op（有 `hasattr` 护栏，不会崩，只是不显示）。
- 动作 ①：接上 —— 在 `InputBar` 挂一个 `Static` 并赋给 `self._hint_label`（推荐，TUI 已对外宣称有上下文 Footer）
- 动作 ②：删掉函数 + 14 处调用

#### P2-3｜乱码残留
`forge_data` 历史中出现 `4Դ.md`（`来源.md` 的西里尔乱码）。磁盘现存**乱码名 0 个**（已全盘扫描），但 `delivery/repair_mojibake.py` 仍在。确认无用可一并归档。

---

### P3 — 未完成的验证（欠账，非缺陷）

#### P3-1｜50-case 基线从未跑过
`python -m benchmark.eval_runner --all`（默认路由开启）没跑过 → "真实配置下的表现"**没有数字**，之前所有 A/B 都是 `TOOL_ROUTER=off` 的固定工具集。
- 动作：跑一轮存 `runs_eval_baseline_router_on/`
- ⚠️ n=50 只能检出 ≈±28pp，别过度解读；引用时必须带样本量

#### P3-2｜`execute_resilient` 仍是 opt-in
`resilience.py` 的自动重试 + 幂等键没接进 MCP 网络类工具，而 `.env` 已配 6 个 MCP server（gitee / playwright / sqlite / chrome / fetch / youtube）——**网络抖动直接算失败**。
- 动作：给 MCP 工具路由接 `execute_resilient`

#### P3-3｜两条判据未定论
- T020 期望 `calculate`、T017 期望 `run_tests`：**只读任务能不能跑测试/计算**？需你拍板后写进判据。
- `logs/backups` 疑点（111 条记录声称已备份、但备份目录全盘不存在）——需要一次真实编辑才能定论。

---

### P4 — 收尾（可选）
- `sessions.sqlite-shm` / `-wal`：易失状态，已排除入库，建议直接加进 `.gitignore`
- `code_sandbox/`、`micro_fixture/`、`tools_phase1_*.py`：确认是否仍需保留

---

## 3. 建议执行顺序

| 步 | 动作 | 风险 | 收益 |
|---|---|---|---|
| 1 | **P1-1 提交 1555 删除** | 无（已证实无真实数据） | 工作区立刻干净，拆除"误提交"炸弹 |
| 2 | **P1-2 补 `.gitignore` + `git rm --cached`** | 低（不动磁盘文件） | status 长期干净，不再堆积 |
| 3 | **P2-1 死引用定论** | 低 | 护栏从"登记未决"变为**零例外** |
| 4 | **P2-2 TUI footer 接上** | 低（有 47 条测试兜底） | 补上宣称但缺失的功能 |
| 5 | **P3-1 跑 router-on 基线** | 无 | 拿到第一个真实配置数字 |
| 6 | P2-3 / P3-2 / P3-3 | — | 按你兴趣排 |

---

## 4. 需要你拍板的 3 件事

1. **P1-1**：1555 个删除 → 提交收敛（推荐），还是从 git 恢复？
2. **P1-2**：`forge_data/`、`exports/`、`runs_eval*` 要不要改为不跟踪（加 `.gitignore` + `git rm --cached`）？
3. **P2-1**：`delete_task` / `sandbox_rollback` / `forget_memory` / `schedule_remove` → 删禁止项，还是真补工具？
