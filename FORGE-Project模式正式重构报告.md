# FORGE-Project 模式正式重构报告

> 项目：FORGE / my_creative_agent · 日期：2026-09-06 · 前端资源 v26 · DB user_version=3（v3.1 表已入 v3 库）
> 本报告基于真实代码修改、真实数据库迁移、自动化测试与浏览器 E2E 生成，非设计方案。

---

## 1. 修改前真实产品模型

- 用户长期容器原称「Task（任务）」（`tasks` 表 tk_*），Session 藏在 `session_id`+SDK sessions.sqlite；
- 旧 `projects` 表=“工作位置/文件夹占位”（默认项目、学习平台）——产品语义混乱；
- 前端一级导航仍为 首页/任务/资料；页面显示“N 条消息 · N 次执行 · 未开始 · 记忆：…”，并渲染“第 N 次/Run 包装/巨大完成卡/顶部 工作位置：undefined”等内部信息；
- 文件上传仅进 Project Sources；无 Message Attachment；记忆仅全局。

## 2. 修改后的 Project 模型（产品/数据均真实生效）

```
Project（tk_*：name/instructions/memory_scope/work_location_id/pinned/…）
├─ Conversation（=项目对话，messages→runs→events/approvals/…）
├─ Sources（project_sources + forge_data/projects/{pid}/sources/）
├─ Message Attachments（message_attachments + attachments/{uuid}/）
├─ Artifacts（notes|exports 产物；JOIN runs 归属项目）
├─ Project Memory（project_memories；仅此项目模式下唯一记忆源）
└─ WorkLocation（work_locations；可选绑定）
```

## 3. 旧 Project → WorkLocation 迁移

`_migrate_v3`：旧 `projects` 每行原样迁入 `work_locations`（同 id 原样保留 = legacy id 兼容），旧表保留；新 API `/api/worklocations`。

## 4. Task 处理

- 用户级 Task 概念取消：原长期容器即 Project（物理表名保留为内部细节，已在前轮报告注明避免破坏性改名）；“＋新任务”改“＋新项目”；
- 旧 v1 单轮 Task(runs 旧行) 已在 v2 迁为 Run 并归入容器/项目，`legacy_task_id` 语义=原 id 原样保留。

## 5. Message / Run 关系

Message(user/assistant) → Run（runs）；`run_turn(task_container_id=项目)` 保证追加消息=同项目新 Run，绝不新建项目（离线+网关 E2E 证明）。用户 UI 不再显示 Run/执行次数/第 N 次。

## 6. Sources

长期来源：上传白名单、sha 去重、持久于 `forge_data/projects/{pid}/sources/`、项目内长期可检索（Context 注入清单+路径；深度 RAG index 字段预留 P2）。

## 7. Message Attachments

上传默认 `message_only`（“仅本次使用”），本地持久化；发送顺序 Upload→Attachment→Message→Bind→Run（返回 bound 计数）；删除未绑定附件行+文件一致；历史消息重开可见附件 chips；Context 以「本次消息附件」高优先级注入；下一条消息自动不带入。

## 8. Artifacts

只含 FORGE 生成（JOIN runs），与 Sources/Attachments 互斥（测试 6/10）。新轻量 Result Card 只读真实产物/修改/验证，杜绝空“已完成”重复。

## 9. Memory Scope

`project_only`/`global` 真实生效：run 期 tools 绑定；project_only 下 remember 写项目记忆、recall 只查本项目（关键词级验证不返回全局/其他项目）；global 才读全局；UI 单选非装饰。

## 10. Context Builder 顺序

当前消息+当前消息附件（优先）→ 项目说明 → 来源清单 → 项目记忆 → (memory_scope=global 才含全局记忆) → WorkLocation 路径 → Agent。runner 在每 run 组 instruction 注入块；记忆边界由工具绑定门强制。

## 11. API 变化（真实注册）

Project/Sources/Attachments/Artifacts/Memories/WorkLocation 全套（§前几轮），本报告版本新增/验证：附件 upload/list/refs/promote/delete、messages/create 附件绑定、两处详情富化（修复 TEXT id 键错配 bug）。

## 12. DB migration

v0→1（v1→v2）→3（v3：work_locations/project_sources/project_memories + tasks 语义列）＋v3.1 message_attachments（`_ensure_v31_tables` 无条件确保，兼容已到 v3 的库）；全程无删库/丢历史/重建。

## 13. 前端导航变化

- 删除一级导航「任务」「资料」（含移动条）；
- 左栏：首页 + 项目（最近列表）+ 查看全部项目（项目页分组：进行中 / 等你确认 / 最近）；设置入口在左下账户；
- 顶栏：移除“工作位置选择器”；新增 `#crumbProject` 显示当前项目名。

## 14. Conversation Flow（本轮核心）

- `renderTask` 重写为时间顺序统一流 `.flow`（max-width 1080px 居中）：
  你说 → FORGE 正文（Markdown）→（确实做过事时）轻量 Activity 行 →（有真实结果时）轻量 Result Card；
- 删除 Run 包装、`第 N 次` 头、run-time、整块过程卡嵌入；
- failed→“遇到一个问题”卡；waiting_approval→真实读取 pending 审批的“需要你确认”卡（看看会改什么/继续）；paused/cancelled/子状态→一行弱提示；
- 普通问答（无工具/无产物）无任何完成卡——正文即结果（E2E“你好”验证：2 条消息、无卡片）。

## 15. Composer 文件上传

真实两来源：上传文件（默认仅本次使用）/ 从项目来源选择（只建引用，零复制零重复索引）；chip 状态 正在上传/已准备/上传失败[重试]；可 加入项目来源 / 移除；发送绑定附件后执行。文案统一“告诉 FORGE 接下来要做什么……”。

## 16. undefined 问题根因

`#workspaceSelector` 旧逻辑把 listProjects（语义改为 Project 列表）当“工作位置”取名 → `projects.find(...)` 得 undefined 拼进文案。修复：删除该选择器及其绑定，顶栏改项目名 crumb；未绑工作位置不再出现在主界面（项目设置内可管理），全页 E2E 断言无 `undefined/null/[object Object]`。

## 17. 状态冲突根因

- 页面 meta 用「最新 run 状态」同时渲染 “未开始/已完成/正在处理” 且带 Run 执行统计 → 与对话流历史矛盾；
- 修复：删除 meta 统计与“未开始”；Project 不再有总状态（长期容器）；进行中状态只以 正在处理/等你确认 局部出现（rail 与实时行），终态不再作为项目状态词。

## 18. 删除的旧 UI

顶部工作位置选择器、`+ 新任务/任务/资料` 一级导航及对应绑定、页面 meta（N 消息/N 次执行/未开始/记忆：…）、`第 N 次`/Run 头包装、重复“已完成/FORGE 完成了/已完成”大卡、常驻 详情/归档/暂停/停止 按钮（详情归档并入项目设置，暂停/停止仅在运行时显示）。

## 19. 暂时保留的兼容代码

- 旧物理表名 tasks/runs 与旧 /api/tasks*、/api/stream、/chat、CLI/daemon/voice（入口层 adapter）；
- guardrails/approval/audit/checkpoint/skills 等 Runtime 全部保留；前端 pages.js 旧 section（未挂导航）与若干未用渲染函数保留（内部清理归 P2）。

## 20. 自动化测试

- 全量 **320 tests OK**（skipped=3 为主题结果卡可选项/无 failed 容器，非失败）；
- 附件/记忆/Project 定向 20+ 用例（test_project_model/test_message_attachments/主题回归已适配新选择器与入口）。

## 21. E2E 测试（真实网关 + 浏览器）

- A/B/D：创建「牙医预约E2E」→ 两轮真实消息 → 同 Project 2 runs 完成、无新建项目 ✅
- C（澄清能力）：既有修复验证（信息缺失→questions 追问不猜城市）沿用前轮真实运行结论；本轮不做模型行为承诺
- E：浏览器“你好”QA → `.flow` 2 条消息（user+assistant）、无 result/deliver 卡 ✅
- M/N：QA 页与历史项目页 innerText 均无 undefined/null、无 第N次/次执行/Task/session_name ✅
- 历史（做过工具的 completed 项目）同规则通过 ✅；主题 light/dark 回归适配新 DOM ✅

## 22. 未解决问题

1. 物理表名 tasks/runs 保留（避免第三次破坏性改名；语义层=Project/Run 已统一）——列入 P2 可选全量改名迁移；
2. Sources 深度检索/索引 pipeline 未接（index_status 预留）；
3. 附件孤儿清理（未绑定超时）与 composer 首页“添加文件”仍占位待补；
4. 完成卡/失败卡的轻量样式在个别 legacy 数据（无明细 run）下显示“查看过程”按钮而非内容（旧数据无 tool_calls）。

## 最终回答

**是**——当前已真实做到：

```
Project
├─ Conversation（统一流：消息/活动/审批/结果 按时间排序）
├─ Sources           （长期；独立）
├─ Message Attachments（临时；仅本次使用可升级）
├─ Project Memory    （scope 边界真实生效）
├─ Instructions      （注入 Context）
└─ WorkLocation      （可选；未绑定无 undefined）
```

并且浏览器验收证明：普通用户页面**不再需要理解 Task / Run / Session / Event / 执行次数 / 第 N 次 / session_name / workspace_id**（这些仅存在于 Runtime 与“查看完整过程”的高级详情）。
