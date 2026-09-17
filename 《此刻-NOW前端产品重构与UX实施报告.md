# 《此刻 · NOW 前端产品重构与 UX 实施报告》

> 日期：2026-09-07
> 范围：仅前端（`web/runtime.html` + `web/runtime/*.js`）。未改 Agent Runtime、Task/Run 数据模型、Completion/Verification/Approval/Sources/Tool/SSE 等后端核心；后端 class/database/API/内部 identifier 一律保留。

## 1. 原前端结构（审计结果）

当前前端不是框架工程，而是无依赖单页应用：一个 `runtime.html` 入口，多个 IIFE 模块按顺序加载，`workspace.js` 是主控。

```
App (RT.workspace.init)
├─ 左栏 brand：FORGE / PERSONAL AGENT + 「＋ 新项目」+ 最近任务(rail)
├─ Topbar：面包屑 + 右上「上下文 —」统计 chip
├─ Main #page-workspace
│   ├─ renderTaskList()  任务列表（进行中/等你确认/最近）
│   ├─ renderTask()      任务对话主体（历史轮次 + 内联过程 + 交付/失败卡）
│   ├─ renderMaterials() 资料页
│   ├─ renderSettings()  设置面（surface 模式）
│   ├─ renderTrash()     回收站
│   └─ Drawer #detailDrawer：文件 / 修改 / 测试 / 运行记录 + 高级信息折叠
├─ Composer：textarea + 添加文件 + 发送/停止
└─ MobileBar：旧「首页/项目」按钮（含指向“Phase 7 占位”的遗留 tasks 页）
```

模块：`types.js / api/* / eventClient.js / eventReducer.js / markdown.js / activity.js / components.js / pages.js / workspace.js`。

状态来源：组件内部 `state` 对象 + `TaskManager/API` 拉取；实时由 `eventClient`(SSE) → `onEvent` → 直接操作 `#stream` DOM；历史/重连通过「历史回放 + 实时队列」。

已有基础（审计确认，不是本轮新造）：
- `activity.js` 已有“内部工具名 → 普通用户动词/阶段”投影（检查/阅读/修改/验证/生成）。
- 状态词已映射：running→正在处理、waiting_approval→等你确认、failed→遇到问题、cancelled→已停止、completed→已完成。
- Drawer 已分四业务 Tab + 高级信息折叠；streaming 采用单 raw buffer 全量重渲染；完成态用 canonical 消息校准。

## 2. 主要 UX 问题

1. 品牌混用：页面标题 `Agnes Agent Runtime`、侧栏 `FORGE / PERSONAL AGENT`、面包屑 `FORGE`、账户页脚 `FORGE · 本地个人 Agent`、搜索项 `FORGE`、正文里大量 `告诉 FORGE / FORGE 完成了 / FORGE 正在处理`。
2. 首页（大输入+示例+最近）与任务列表/任务内输入框职责重叠，属于冗余入口（已按上一轮要求删除，本轮确认无回归）。
3. 新任务创建暴露内部概念：弹窗“新项目 + 记忆范围确认（全局记忆）”；空任务页出现“添加来源文件/设置工作位置”按钮；顶部“上下文 — / ≈Nk”token 统计 chip。
4. 文案大量“来源文件 / 产物 / 工作位置 / 运行记录”等偏工程词；左栏桌面端没有“资料/设置”直达。
5. 审批卡直接显示 `tool — arguments`；失败卡描述曾暴露“Run/证据/完成校验”语义。
6. 移动端底栏第二个按钮“项目”指向遗留占位页（Phase 7 未实现）。
7. 零任务时主区只有三组“暂无”，没有“现在想做什么”的引导与建任务 CTA。

## 3. 新信息架构

```
此刻 NOW
├─ 左栏（高频）
│   ＋ 新任务
│   最近任务（项目即对话，点击直接进入）
│   查看全部任务 →  资料   设置
├─ 主区
│   默认=任务列表（进行中 / 等你确认 / 最近）
│   零任务时=「此刻，现在想做什么？」+ 新任务 CTA
│   进入任务后=持续对话主体（你说 / 此刻处理中 / 需要确认 / 结果 / 继续追加）
├─ 输入区（任务内常驻）
└─ 移动底栏：任务 / 资料 / 设置（不再有占位按钮）
```

原则落地：**一个任务 = 一个持续对话**；过程内联在对话中；技术细节放进 Drawer（“查看完整过程/查看问题”后再展开）。

## 4. 修改组件清单

- `web/runtime.html`：页面标题、品牌（此刻 / NOW）、账户页脚、面包屑、composer placeholder；左栏“新任务/最近任务/查看全部任务/资料/设置”；移除顶部“上下文”chip；移动底栏改“任务/资料/设置”；清掉首页 CSS 与 `.home-hero` 拖拽样式；新增 `.tasklist-empty` 欢迎空态样式；`activity.js/workspace.js` 版本号 v=33。
- `web/runtime/workspace.js`：
  - 品牌文案替换：告诉 FORGE→告诉此刻、FORGE 完成了→已完成、FORGE 正在处理/继续处理→此刻正在处理/此刻继续处理、搜索结果显示“此刻”、来源面板“此刻/资料/成果”文案。
  - 任务语义：`新项目→新任务`（按钮/弹窗/提示），rail 空提示与任务列表 crumb 统一“任务”；`createNewTask` 不再弹“记忆范围”确认（默认仅此任务）。
  - 空任务页：去掉“添加来源文件/设置工作位置”工程按钮，只留一句话引导 + 常驻输入区；任务列表零容器时渲染欢迎块 + 新任务 CTA。
  - 审批卡（实时 + 历史 pending）：`tool — arguments` → `actionDesc()` 普通用户动作描述（创建/删除/修改/发送/查看…），详情仍可点“看看会改什么”。
  - 失败卡：描述改为“此刻没有完成全部内容。可以先‘查看问题’或再试一次。”；原始 Run/证据类文案仅在 Drawer“查看问题/高级信息”中保留。
  - 任务头“来源文件 N”→”资料 N”；资料面板与 Drawer 中“来源文件/产物”统一为“资料/成果”。
  - 新增 `RT.workspace.showMaterials/showSettings`，绑定桌面 `navMaterials/navSettings` 与移动底栏。
- `web/runtime/activity.js`：未登记工具（含 MCP）按动词归类为普通语言（创建/删除/修改/发送/安排/查看/确认/处理），不再直接吐英文工具名。

## 5. 删除组件清单

- 首页视图：`renderHome/commitHome/EXAMPLES/homeInput/.home*`（上轮完成，本轮复核：无残留调用、无导航入口、无 CSS 引用）。
- 顶栏“上下文 — / ≈Nk”统计入口（其 JS 逻辑保留且空值安全，未删除后端能力）。
- 旧移动端“项目”按钮（会切到 Phase 7 占位页）；占位 section 保留但已无用户入口。
- 新任务创建里的“记忆范围”两问流程。

保留（复用而非重写）：eventClient/eventReducer、markdown.js、activity.js 投影、API client、Drawer、approval 决定链、所有 Runtime/SSE。

## 6. 用户状态映射

| 内部 | 用户可见 |
|---|---|
| SUBMITTED / RUNNING | 正在处理 |
| WAITING_APPROVAL / WAITING_USER | 等你确认 |
| PAUSED | 已暂停 |
| COMPLETED | 已完成 |
| FAILED | 遇到问题 |
| CANCELLED | 已停止 |

（`activity.js USER_STATE` 即此映射，UI 一律用该投影，不直出内部枚举。）

## 7. Event → UI 映射（保持既有主链）

| 事件 | UI |
|---|---|
| run.started/task.started | 任务页进入处理态、显示“正在处理” |
| tool（live） | 顶部过程行更新“正在查看/读取/修改/验证…” |
| reply_delta | 稳定 assistant 气泡内 raw buffer += delta → 全量 Markdown 重渲染（60ms debounce） |
| reply / assistant.reply | 用 canonical 最终内容整段校准重渲染 |
| approval / approval.required | “需要你确认”+ 普通语言动作 + 查看影响 |
| task.completed/run.completed | 已完成 + 交付/结果卡 |
| task.failed/run.failed | 遇到问题卡（普通语言），详情进 Drawer |
| task.cancelled/run.cancelled | 已停止 |
| stream_reset | 清空上一轮失败增量（防重复视觉叠加） |

## 8. Responsive 方案

- 现有断点继续生效：窄屏隐藏品牌栏/顶栏，显示移动底栏。
- 移动底栏三入口：任务（默认列表/工作台）、资料、设置；不再链接到未实现占位页。
- 对话流与输入框在任务页始终占主面积；任务状态/过程采用**内联卡片**而非固定右栏，避免三栏挤压（这是相对建议稿的取舍：用内联替换独立右栏）。

## 9. Branding 修改

| 位置 | 旧 | 新 |
|---|---|---|
| 页面标题 | Agnes Agent Runtime | 此刻 · NOW |
| 左栏品牌 | FORGE / PERSONAL AGENT | 此刻 / NOW |
| 面包屑 | FORGE / 项目 | 此刻 / 任务 |
| 账户页脚 | FORGE · 本地个人 Agent | 此刻 · 本地个人助手 |
| 输入框/空态/处理文案 | 告诉 FORGE / FORGE 正在… | 告诉此刻 / 此刻正在… |
| 结果卡 | FORGE 完成了 | 已完成 |

普通用户可见面不再混用 FORGE/Agnes；内部代码与 .env（如 FORGE_LOCAL_MODEL_*）保持不变。

## 10. Streaming 兼容情况

未改动 streaming 主链；延续上一轮修复的约束：单 assistant 气泡 + 单一 raw text buffer + 全量 Markdown 重渲染 + final canonical 校准。Markdown 空项目符号根因修复（无序列表捕获组下标）与对应测试仍保留。

## 11. Approval 交互

- 普通用户看到：“需要你确认 / 此刻准备执行：修改 / 删除 / 发送…”。
- “看看会改什么”进入 Drawer 看具体影响；按钮为“暂不 / 继续”。
- 不再展示 Tool name / Approval ID / 原始 arguments 作为默认内容。

## 12. Error 交互

- 默认失败卡：“遇到问题 / 此刻没有完成全部内容。可以先‘查看问题’了解原因，或直接再试一次。”
- 原始“Run/证据/完成校验”类错误文案只在“查看问题”→Drawer（高级信息）出现，属开发者层。

## 13. 实际 E2E 测试（真实浏览器）

用真实 Chromium 打开 `http://127.0.0.1:8765/`（真实后端在跑）验证：

| 检查 | 结果 |
|---|---|
| 品牌“此刻 / NOW”与页面标题 | PASS |
| “＋ 新任务 / 最近任务 / 资料 / 设置”导航存在 | PASS |
| 默认进入任务列表（进行中/等你确认/最近） | PASS |
| 页面不出现 FORGE / 首页 / 上下文 — 等术语 | PASS |
| 点击“资料”进入资料页 | PASS |
| 控制台无新增 JS 错误（仅 favicon 404，改动前已存在） | PASS |

未在本轮重复的既有 LIVE 链路（此前已验证）：MCP 三梯队挂载+审批、真实 Provider 30 轮 Markdown 列表无空项、SSE 断连续订语义、approval resume；相关测试与脚本仍可复跑（`tests/live_markdown_list_check.py` 等）。

## 14. Build / Test 结果

- `node --check web/runtime/workspace.js`、`node --check web/runtime/activity.js`：通过。
- 离线回归 `.\regression.ps1`：**489/489 PASS**（确定性离线基线；未触碰后端）。

## 15. 尚未解决的问题（诚实清单）

1. 独立右侧“任务状态”栏未实现——采用内联过程/结果卡方案，若后续产品要求固定右栏需再评估。
2. “开发者模式”尚无显式开关，当前以 Drawer/高级信息作为 Level3；后续可加全局“开发者模式”开关把 Level3 独立出来。
3. 空状态欢迎块只在“零任务”时出现；有任务但都归档/无对话的展示仍是任务列表空分组。
4. 旧版静态 DOM（Phase 7 section/inspector）仍留在 HTML 中但已无用户入口；为避免误删兼容路径暂保留，后续清理需逐项确认无测试/路由引用。
5. 审批动作文案对未登记工具采用动词归类；个别 MCP 工具的精确可读描述（如“创建 Issue”）需要继续补充映射表。

## 最终验收

普通用户模式是否直接暴露以下概念（Drawer 高级信息/开发者层除外）：

| 概念 | 暴露 |
|---|---|
| Agent | NO |
| Run | NO |
| Runtime | NO |
| Tool | NO |
| MCP | NO |
| Context | NO（顶部 token 统计入口已移除） |
| Token | NO |
| Trace | NO |
| Evidence | NO |

结论：**FRONTEND REDESIGN = PASS**

（本次为产品面/信息架构/文案/交互收敛的完整落地；后端与协议零改动，回归 489/489，真实浏览器冒烟通过。）
