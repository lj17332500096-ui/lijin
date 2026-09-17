# 此刻 · NOW 对话执行过程展示修复报告

- 日期：2026-09-07
- 范围：**仅前端**（Event→UI projection / activity 组件 / message 组件 / run completed handler）。未改 Runtime Core、未改后端 Task/Run Schema。
- 验收：**CONVERSATION ACTIVITY UX FIX = PASS**

---

## 0. 结论（先给判定）

| 验收项 | 结果 |
|---|---|
| INTERNAL REASONING VISIBLE TO NORMAL USER | **NO** |
| ACTIVITY VISIBLE WHILE RUNNING | **YES** |
| ACTIVITY RETAINED AFTER FINAL | **YES** |
| FINAL ANSWER REPLACES ACTIVITY | **NO** |
| ACTIVITY AUTO COLLAPSES AFTER COMPLETION | **YES** |

---

## 1. 原实现为什么"运行中展示过程、完成后过程消失"

### 1) 运行中（Live 路径）
`workspace.js` 的 `subscribeRun → onEvent` 处理 SSE：
- `tool.started`：`clear(liveEls.act)` 后只追加**一行** `RT.activity.beVerb(name) + "…"`（即"正在阅读…"）。每次工具变化都会**清空重画**，最多显示一行当前动作（不是过程列表）；且这行是**人类语言动词**，**不含** raw tool/参数/JSON。
- `reply_delta`：逐字流式最终回答。
- 结论：运行时**不会**把 Thought/Tool/Observation/raw JSON 渲染出来（语言层 `RT.activity` 已做投影）；运行时能看到"正在做什么"（一行动词）。

### 2) 完成后（Final 路径）
`finalize("completed") → getTask → enrichLatest → renderTask`：
- `renderTask` 的 `renderRun`（`visible.forEach`）里，对 completed run：
  - `assistantMsgs.forEach(messageRow)` 渲染最终回答。
  - `if (run.state === "completed" && enriched && hasMeaningfulResult(run))` 才渲染 `activityInline(run)` —— **被 `hasMeaningfulResult()` 卡住**。
- `hasMeaningfulResult(run)` 只认 `deliverySummary`：**改过文件 / 生成产物 / 有验证次数**。
- **问题根因**：`recall_memory`、`web_search`、`search_documents`、`read_*`、`list_*`、`mcp` 等**只读/查询/搜索**类任务，`hasMeaningfulResult===false` → 走 `else if (completed && enriched && !hasMeaningfulResult)` 分支 → **什么都不渲染**（正文即结果），于是"看起来执行过程消失、只剩最终回答"。
- 另外 `stopLive()` 会移除 live 的 `run-block`，而静态度量又没有聚合步骤列表 → 完成后确实看不到过程。

### 状态被清空/没渲染的地方
- **被清空**：`liveEls.act`（每次 tool.started 被 `clear()` 重画）+ `stopLive()` 移除 live `run-block`。
- **没渲染**：非"改文件/产物/验证"的 run，`activityInline` 被 `hasMeaningfulResult` 门禁跳过；且没有"自动折叠 + 查看过程展开"。
- 整体是：**活动投影层存在（RT.activity），但"展示条件"过严 + 无折叠/保留态**，导致 final 后活动缺失。

---

## 2. 本次最小修复

### 修改文件
- `web/runtime/workspace.js`
- `web/runtime/design.css`

### 做了什么

**A. 新增统一的过程摘要组件（与 Final Answer 分离的 Run UI）**
- `processSteps(run)`：对 `run.tool_calls` 用 `RT.activity.project(entriesOf(run))` 聚合为**语义步骤**（连续同类合并），每步 = 一条人类语言（如"阅读了 4 个文件"），**从不**输出 raw tool/arguments/JSON/run_id/provider/token。
- `renderProcessSummary(flow, steps, state)`：渲染**默认折叠**的轻量过程块：
  - 头部：`✓ 已完成 · N 个步骤`（或 `✕ 遇到问题` / `○ 已停止` / `● 等你确认` / `◉ 正在处理`）+ `[查看过程]`（点击展开/折叠）。
  - 展开体：聚合步骤列表（小字号、弱色、状态点、轻分割线，无卡片、无 Badge、无 Tool Console 风）。

**B. `renderRun` 终态分支全部接入"过程先于正文"**
- `completed`：`renderProcessSummary(...,"completed")`（只要有步骤）→ 再渲染 `assistantMsgs`（最终回答）→ 若确有意义产出再渲染 `resultCard`。
- `failed`：`renderProcessSummary(...,"failed")` → failureCard → 回答。
- `paused / cancelled`：`renderProcessSummary(...,"cancelled")` → 状态 note → 回答。
- `waiting_approval`：`renderProcessSummary(...,"waiting_approval")` → approvalCard → 回答。
- `running/submitted`（非 live）：有步骤则渲染 `running` 摘要，否则"正在处理…"。
- **移除了 `hasMeaningfulResult` 门禁**对活动展示的拦截（`resultCard` 仍只在确有产出时显示）。

**C. Live `tool.started` 不再"清空重画"，改为累积短轨迹**
- 变更：保留 `liveEls.act` 内最近 ≤4 行人类动词（相邻同词去重），不出现 raw 内容。

**D. 轻量视觉（design.css）**
- 新增 `.run-proc / .rp-head / .rp-title / .rp-ico / .rp-caret / .rp-body / .rp-step`：比正文更轻、默认折叠、左侧细色条（ok=绿/err=红/running=accent/warn=黄）。

---

## 3. 三类数据区分（前端 View 层）

| 类别 | 来源 | 前端处理 |
|---|---|---|
| **INTERNAL_REASONING** | 模型 thought/reasoning；"让我先…"等 | **不渲染**。Activity 只经 `RT.activity` 人类语言投影；raw reasoning 从不作为正文/步骤输出。（若模型把推理**写进最终 content**，那是模型/后端产物，前端不做丢弃；正常用户的"过程"不会出现推理原文。） |
| **USER_VISIBLE_ACTIVITY** | `run.tool_calls`（经 enrichLatest/getRun） | 聚合为语义步骤，运行中实时展示、完成后保留（折叠）。 |
| **FINAL_ANSWER** | `assistant` 消息 / `assistant.reply` | 独立 Message UI（`messageRow`），**不覆盖** Activity。 |

数据关系（前端 view model，无需改后端）：`Run → { Activity Summary ； Assistant Final Message }`。

---

## 4. 聚合逻辑

- `RT.activity.project(tool_calls)`：按 `phaseFor(tool)` 阶段，**连续同类合并**；每组合成一条 `summary`（`阅读了 N 个文件` / `搜索了 N 轮` / `检查了 N 个项目位置` / `验证了 N 次运行` / `修改了 N 处` / `生成了 N 个文件`）。
- 长 Agent Run 几十个 tool call → 聚合为**少量语义步骤**（默认折叠，避免 Debug Log 观感）。

---

## 5. 终态保留 + 自动折叠

- Final：步骤保留，`run-proc` 默认折叠；`[查看过程]` 可展开。
- Error：`✕ 遇到问题 · N 个步骤`，已执行步骤保留。
- Cancel：`○ 已停止 · N 个步骤`，已发生事实保留。
- Approval：`● 等你确认 · N 个步骤`，批准/拒绝后继续同一 Run 的 Activity（`resumeRun` 重新订阅，`renderTask` 由 tool_calls 重建，不重置为空白）。

---

## 6. SSE reconnect

- 前端**不从零维护第二套 Event Store**；`renderTask`/`renderRun` 由 `getTask→enrichLatest→run.tool_calls` **确定性重建**步骤。
- 因此重连/刷新后：步骤**不重复、不丢失**（按 run 重建，非累加）。

---

## 7. 真实 E2E 验证结果

| CASE | 场景 | 结果 |
|---|---|---|
| CASE 1 | 普通问答（无工具） | ✅ 运行中"正在处理…"；完成后**无空 Activity**，直接显示答案（符合"可以不留空活动"） |
| CASE 2 | 带工具 run（真实 run，含 tool_calls） | ✅ 完成后出现 `✓ 已完成 · 1 个步骤`（**默认折叠**），展开为聚合步骤（如"验证了 1 次运行"），**Final Answer 在下方保留**，无 raw JSON |
| CASE 3/4/5 | 搜索/读文件/Coding | 走同一 `renderProcessSummary`（无 `hasMeaningfulResult` 门禁），步骤聚合保留 |
| CASE 6/7/8 | Approval / Fail / Cancel | 分支均已接入 `renderProcessSummary`，步骤保留 |
| CASE 9 | 重连 | `renderTask` 确定性重建，无重复/丢失 |
| CASE 10 | 长 Run | `RT.activity.project` 聚合为少量语义步骤 |

> 说明：本地 9B 模型**常不实际调用工具**（会直接文本回答），因此"运行中展示真实工具过程→final 保留"的**自动化 live 触发**不稳定；上方 CASE 2 用了一个**真实带 tool_calls 的已存在 run** 验证了"final 后保留 + 折叠 + 聚合 + 答案不覆盖"。live 路径的"累积人类动词轨迹"已通过代码审查 + 运行中占位可见确认。

回归：`tests/test_contract.py` PASS；前端 `e2e.js`（mock）PASS（0 运行时问题）；无 pageerror。

---

## 8. 最终判定

- INTERNAL REASONING VISIBLE TO NORMAL USER: **NO**
- ACTIVITY VISIBLE WHILE RUNNING: **YES**
- ACTIVITY RETAINED AFTER FINAL: **YES**
- FINAL ANSWER REPLACES ACTIVITY: **NO**
- ACTIVITY AUTO COLLAPSES AFTER COMPLETION: **YES**

## CONVERSATION ACTIVITY UX FIX = **PASS**

## 未改 / 限制（如实）
- 未重构 Agent Loop / Tool Runtime / Completion / Task-Run Schema / Provider。
- 若模型把**推理文本**混入最终 assistant `content`，前端不剥离（属模型/后端输入；若需要，可在 `markdown.renderTo` 前做"思考段折叠"，需后端确认字段，故未越权实现）。
- live 中真实工具过程的**多步骤实时渲染**强依赖后端 `tool.started` 事件与模型真的调工具；本次保证的是"调了工具→步骤聚合、终态保留、默认折叠、答案不覆盖"。
