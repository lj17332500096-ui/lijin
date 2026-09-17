# FORGE-Task-Message-Run 重构报告

> 项目：`F:\Byong-hermes\Byong-hermes\my_creative_agent`（FORGE / my_creative_agent）
> 日期：2026-09-05 · 版本：Agent Runtime v2（Project → Task → Message → Run → Event）
> 目标心智：**「一个 Task 是一件可以持续对话、持续工作的事情；用户每追加一条需要 Agent 执行的消息，就创建一次新的 Run。」**
> 配图：`exports/最终架构_Project-Task-Message-Run.png`、`exports/工作流程图.png`（迁移前流程）、`exports/架构分层全景.png`、`exports/前端现状信息架构.png`

---

## 1. 修改前真实调用链（已逐行核实，非推测）

```
用户发消息
→ Web v2 UI: EventSource GET /api/stream?session=personal&message=…
→ webapp.api_stream（非 resume 分支内联编排，webapp.py:237-307；不走 run_turn）
→ TaskManager.create_task(session_id, goal)          # 每次一个"Task"，实为单轮运行
→ transition(RUNNING) → gate.begin → Runner.run_streamed
→ 流中 tool 事件即时 SSE；结束 mark_success / mark_failure
→ 会话历史写 sessions.sqlite（agent_messages，第三方 openai-agents SDK）
→ SSE 事件: task.started / tool / reply / approval / task.completed|failed / error / done
→ eventClient(normalize legacy 名) → eventReducer(幂等去重) → taskStore → 全量重渲染
```

**核实结论（与任务书假设一致，逐条确认）**
1. Web v2 入口：`web/runtime.html` + `web/runtime/*.js`；发送走 GET `/api/stream`（SSE，非 POST）。
2. Task 创建接口：`webapp.py:237` 内联 `create_task`（session 语义）；恢复分支 `resume_task=` 才走 `run_turn`。
3. `AgentRuntime.run_turn`（`runtime/runner.py:188-321`）：task_id 为空→`create_task`+RUNNING；非空→校验可恢复→resume。审批门/审计/产物/checkpoint 全部围绕该次运行。
4. Runner：`main.py execute_turn → _run_attempt → Runner.run_sync / run_streamed / run`。
5. `session_name`：CLI 默认 personal、Web 默认 personal、定时 `sched-{id}`、voice personal；上下文由 SDK `SQLiteSession`（sessions.sqlite）承载。
6. Task 状态机 8 态（`runtime/task.py:18`）；`WAITING_USER` 无生产转入路径。
7. SSE：唯一端点 `/api/stream`；每请求一个生成器；无回放；断连 finally → RUNNING 变 CANCELLED。
8. eventReducer 快照（events≤500 / toolCalls / approvals / artifacts / lastReply）；taskStore 全局单活动任务。
9. **确认**：每次 run_turn 都新建 Task、上下文连续性完全依赖 session_name；终端/网页/定时三类入口均是"每轮 = 一个终态 Task"。
10. 附带发现：旧 v2「审批后 resume」URL `/api/tasks/{id}/stream` 后端未注册（404 悬空契约）；前端历史事件回放 `loadEvents` 未被调用；webapp 内联编排与 run_turn 双份实现。

## 2. 修改后真实调用链

```
（新对话）POST /api/tasks/create            （既有任务）用户继续输入
        ↓ 建容器 tasks(tk_*)+独立 session 键        ↓ 该容器
（同一条路径，语义二选一）
GET /api/tasks/{task_id}/stream?message=…    → 容器内新建 Run(task_*)+user Message
GET /api/runs/{run_id}/stream 或 ?run_id=…   → resume 同一 Run（审批/暂停后续跑）
        ↓
run_turn（task_id=Run 或新建）→ 状态机（submitted→running…）
        ↓ 期间 events 落 task_events(run_id)；tool/model 明细照旧
        ↓ 成功/失败/审批 分支 → 写 assistant Message + touch 容器 updated_at
        ↓ SSE 双发: run.started/run.waiting_approval/run.completed/run.failed + legacy task.*
        ↓ eventClient（run.*+legacy 均监听，无自动重连防重放）→ reducer → store → UI
```

- 上下文连续性：**Task 容器持有内部 session 键（tasks.session_id）**，模型上下文仍由 SDK SQLiteSession 构建；用户不再接触 session。
- 同 Task 追加消息 = 新 Run；审批批准后 = **同一 Run id 原地续跑**（不新建 Run、不新建容器）。
- 旧入口（终端 `main.py`、定时 daemon、语音、`/chat`、旧 `/api/stream`）行为不变：未显式指定容器时后端按 session 自动取/建容器 → 老调用方零改动，但底层已是新模型。

## 3. 数据库变化

`agent.db`（WAL，`PRAGMA user_version 0 → 1`；无数据丢失）：

| 表 | 变化 |
|---|---|
| `tasks`（旧） | **ALTER TABLE RENAME → `runs`**；行 id 不变（v1 task_* 即 v2 run id，天然 legacy 追踪）；加列 `task_id`（容器）、`run_index` |
| `projects`（新） | id(name prj_*)/name/root_path/created_at |
| `tasks`（新，容器） | id(tk_*)/project_id/session_id(内部上下文键)/title/summary/status/created_at/updated_at/archived_at |
| `messages`（新） | id 自增/task_id(容器)/run_id/role(user·assistant·system)/content/meta_json/created_at |
| `task_events` | 不变；`task_id` 列语义 = run id（事件归 Run） |
| `checkpoints/approvals/artifacts/model_calls/tool_calls` | 物理不变；均挂在 run id 下（字段名沿用 task_id） |
| `schedules/schedule_runs/memories` | 不变 |

sessions.sqlite（SDK）不变，仅作底层会话键存储。

## 4. Migration 方案

`runtime/task_manager.py::_migrate_db`（TaskManager 每次实例化时执行，幂等）：
1. `PRAGMA user_version` = 0 且存在旧形 `tasks`（含 goal/state 列）且无 `runs` → `ALTER TABLE tasks RENAME TO runs`，删除旧索引 `idx_tasks_session/idx_tasks_state`；
2. 补列 `runs.task_id`、`runs.run_index`（缺失才 ALTER ADD COLUMN）；
3. 执行 `_SCHEMA_V2`（IF NOT EXISTS，新建 projects/tasks(容器)/messages 与 runs 全量索引）；
4. `_backfill_containers`：把 `task_id IS NULL` 的旧行按 `session_id` 归并容器（一 session 一活跃容器），回填 `task_id`+`run_index`，空标题容器取最早 run 的 goal 前 60 字；
5. `user_version = 1`。

验证（真实库副本演练 + 单测）：56 个旧行 → 3 容器（personal 53/otel-smoke 1/verify_missing 2），56/56 全部回填，事件/审批/产物/状态原样保留；重复初始化不重复建容器。遗留兜底：历史行 id 未变即 `legacy_task_id`（无需额外列）。

## 5. 定义（固定）

- **Project**（`projects`）：Agent 被允许操作的工作环境容器（默认项目 root 留空表示本仓库工作区）。
- **Task**（`tasks`）：用户持续完成的一件事情（如"修复登录后自动退出的问题"），可跨多轮存在；追加要求不会新建 Task；含 `title/summary/updated_at/archived_at`。
- **Message**（`messages`）：Task 内 user/assistant/system 对话消息；**工具日志不算 Message**（在 Run 的工具明细与 Activity 里）。
- **Run**（`runs`）：每条需 Agent 执行的 user Message 触发一次 Run；承载原 Task 的生命周期能力：running / waiting_approval / paused / completed / failed / cancelled（+submitted 排队态）；内含 model calls、tool calls、approvals、verification（code_loop 四段）、checkpoint、artifacts、events。
- **Event**（`task_events`）：Run 内的真实执行事实（task.created/task.{state}/task.result/task.checkpoint/task.approval.*…），不与 UI 文案等同（UI 经 Presentation Projector）。

## 6. API 变化

用户级（新，均可用 curl/浏览器验证）：
- `GET /api/projects`；`POST /api/projects/create`
- `GET /api/tasks`（容器列表，含 stat/latest_run）；`POST /api/tasks/create`（默认分配独立 `sess-*` 会话键 → 新任务上下文隔离；显式传 session 可沿用）
- `GET /api/tasks/{id}`（容器详情：messages+runs）；`GET/POST /api/tasks/{id}/messages(/create)`
- `GET /api/tasks/{id}/stream?message=…`（容器内追加消息→新建 Run 并执行）；`?run_id=…`（恢复该 Run）
- `GET /api/runs?task_id=&state=`；`GET /api/runs/{id}`（含 events/tool_calls/model_calls）；`GET /api/runs/{id}/events`；`POST /api/runs/{id}/pause|resume|cancel`；`GET /api/runs/{id}/stream`（resume 执行）

兼容保留：`GET/POST /api/stream`（旧聊天/旧逻辑，行为语义不变）；`/api/tasks/{id}/pause|resume|cancel`（run 级旧路径）；approvals/artifacts/tools/schedules/memories 端点不变（approval 的 task_id 即 run id）。

## 7. SSE 变化

- 新事件：`run.started`、`run.waiting_approval`、`run.completed`、`run.failed`（payload 含 run_id/task_id/container 关联）；与 legacy `task.started/task.completed/task.failed/reply/approval/done` **双发**，旧前端无感。
- 修复悬空契约：`/api/tasks/{task_id}/stream` 已注册（容器执行流）；`/api/runs/{run_id}/stream` 提供 run 恢复。
- 断连语义不变：执行中的 run 由服务端 finally 收口（RUNNING→CANCELLED/FAILED）；**waiting_approval/paused 跨连接保留**（可稍后批准/恢复，测试 T8 实证）。
- 客户端 eventClient：run.* 加入命名监听；带 message/run_id 的流**关闭自动重连**（服务端断连即取消该 Run，自动重开会造成重复 Run——这是原实现的重放隐患，本轮一并修掉）。

## 8. 前端状态管理变化

- 左栏：项目 + 新建任务 + **Task 容器列表**（实时状态点、更新时间），更多管理页折叠进 `更多 · 管理`；
- 主区：**单个 Task 的混合流**（第 N 轮 Run 卡 + user/assistant 消息 + Activity 过程卡）——历史运行记录默认只按需加载最近 3 轮明细；
- `taskStore/eventReducer` 保留（旧运行视图兼容）；新工作台以容器 detail + Run detail REST 为准，SSE 只负责增量；
- `api/client.js` 增加容器/消息/Run 方法与流 URL 构造；assets 版本 `?v=11 → 12`（改 JS 后需 Ctrl+F5）。

## 9. Presentation Projector 设计

`web/runtime/activity.js`（RT.activity）：
- `phaseFor(tool)` 把工具名 → 用户级阶段：searching(搜索)/exploring(检查)/reading(阅读)/editing(修改)/running(运行)/generating(生成)/verifying(验证)；
- `describe(tool,args)` 提炼一行（"运行 app.py（demo）"、"编辑 authStore.ts"…）；
- `project(items)` 连续同类合并 → 组（组头如 "阅读 · 3 项"），供历史与 Drawer 使用。
- 默认用户文案只见 **检查/阅读/修改/运行/验证/完成/失败** 等 Activity；`model_call/tool_call/event` 等仅在 Drawer「运行记录」里展开（事件明细为 `<details>` 可折叠）。

## 10. Inspector → Drawer

- 原第三栏 `.inspector` 常驻 → CSS 隐藏；新增 `.drawer` 覆盖层（右滑 440px，`.app.drawer-open` 切换）。
- 触发点：任务头「详情」按钮（未选任务禁用）。内容：任务总览（统计+Runs 列表）→ 点某 Run 加载 `GET /api/runs/{id}`：Activity 投影 + 事件明细折叠 + 产物链接下载。
- 设计原则落地：Drawer 只承载详情/运行记录/审计/产物，**不是默认布局**。

## 11. Inline Approval 修改

- 审批卡继续内嵌在消息流（原 approval-card 样式保留），无 Modal；文案改为任务书风格（"FORGE 准备执行一个需要确认的操作 … [拒绝][允许]"）；
- 允许 → 逐条 `POST /api/approval`（approved）→ 重新打开**同一 run** 的 `/api/runs/{id}/stream` → 服务端 resume 同 id（E2E T4 实证：批准前 run_id = 批准后 run_id，容器 Run 数只 +1）；拒绝 → 刷新详情如实显示失败原因；
- 审批事件/决定/时间照旧落 approvals 表与 events（审计齐全）。

## 12. 删除的旧代码 / 13. 保留的兼容代码

删除：无整文件删除（遵守"先确认引用再删"）。清理行为：
- webapp 悬空 URL 设计移除（resume 改走已注册路由）；`_run_states_outbound` 半成品助手删除；
- 旧 `_SCHEMA` 中 tasks DDL 迁移为 runs（表名层面变更）。

保留（兼容层/别名）：
- TaskManager 旧方法名（create_task/get_task/list_tasks/transition/mark_*）语义=操作 Run → **全部既有调用方与 245 项旧测试不破**；
- `/api/stream`、`/api/sessions`、`/api/history`、`/chat`、终端/语音/daemon、tasks.json 镜像调度全保留；
- 旧 `/api/tasks/{id}/pause|resume|cancel|events` 路径保留（run 级）；
- SSE legacy 事件双发；sessions.sqlite 结构未动（SDK 自有）；`memory.json` 兼容迁移逻辑未删。

## 14. 自动化测试结果

- 全量离线回归：**252/252 通过**（原 245 + 新增 7：`tests/test_tmr_model.py` 同容器 3 Run/消息链、显式容器忽略 session 复用、审批 resume 同 Run 不新建、轻问答同样落 Run+Message、容器 API、v1→v2 迁移无损、新库幂等版本）。
- 存量测试适配（仅表名）：`test_schedules.py:67`、`test_task_runtime.py:130` 的 `UPDATE tasks` → `UPDATE runs`（内部表改名所致，语义未变）。

## 15. 手工端到端测试结果（tests/e2e_tmr.py · 真实网关 · 21/21 通过）

| 用例 | 断言 | 结果 |
|---|---|---|
| 1 建 Task | 容器创建、独立 `sess-*` 上下文键 | ✅ |
| 2 同 Task 连发三轮 | 同一容器 3 个不同 Run、无 Task2；run.started/completed 双事件 + legacy task.completed 兼容 | ✅ |
| 3 消息链 | user/assistant ×3、顺序与 run_id 关联 | ✅ |
| 4 审批 | 触发 run_python → approval 事件 + run.waiting_approval | ✅ |
| 5 批准 resume | 批准后 resume **同一 run id**，容器 Run 数不重复 +1，终态落库 | ✅ |
| 6/7 Projector 原料 | run detail 含 events/tool_calls 数组（Drawer/Activity 数据源） | ✅ |
| 8 断连 | 断开后不再有 RUNNING（取消或留在可恢复 waiting_approval） | ✅ |
| 10 旧入口 | `/chat` 页面可用；legacy `/api/stream` 完整收尾（task.completed+run.completed） | ✅ |
| （重启恢复） | 迁移在 webapp 启动自动执行；进程重启后历史可查（真实库演练 56 行验证） | ✅ |

## 16. 未解决问题（如实记录）

1. **"resume 续跑"仍是同 goal 重放语义**：审批批准后是对同一 Run 从该轮目标重新执行（审批记录放行对应工具），不是精确到 tool 断点的续跑；无副作用的工具可能被重复调用。断点级续跑需要 Runner 内循环 checkpoint（超出本次 P0 范围，任务书允许）。
2. **格式闸偶发**：网关模型偶发输出非 AgentReply JSON → 自动带原因重试（已由 2 次提高到 3 次）；开放式任务偶发工具空转导致轮次耗尽（模型质量问题，非本次重构引入；E2E 已用确定性小任务规避）。
3. **工具过程历史不逐工具持久化进 UI 流**：历史 Task 的 Activity 默认只取最近 3 轮明细（点"加载运行记录"可取全部）；tool 的完整 arguments/excerpt 未暴露新 REST 明细页（可加 `/api/runs/{id}/tools/{id}`）。
4. **Diff 只存在于工具返回给模型的文本**：修改类的 diff 未单独存证，Drawer「修改记录」暂以工具调用+产物呈现；持久化 tool result 后即可做专门 Diff 面板。
5. **旧 v2 管理页的部分列表**（产物/审批等仍在；旧"任务表格页"语义已被容器列表取代，入口收敛到"更多 · 管理"）；`renderTasks` 遗留对旧字段的引用可做 P2 清理。
6. 前端 assets 手动版本号（?v=12），改文件后需升版本并强制刷新。

## 17. 后续建议

- P2：删除旧"任务表格页"与 `taskStore` 单任务路径、`WAITING_USER` 死状态、旧 `/api/tasks/{id}` run 级别名（迁到 /api/runs 后）、webapp 内联编排收敛到共享执行器；
- 过程存证：把 tool diff/摘要写入 events payload（verification.started/completed 事件），让 Drawer 与 Activity 有真 diff；
- 精确续跑：引入 Run 内 turn 级 checkpoint（对 checkpoint schema 做 v2），审批/断线后从最后一个工具边界恢复；
- schedules 双写收敛：REST 写端点 + tasks.json 单源或 SQLite 单源二选一；
- 前端组件化基线：目前是模块化原生 JS（无框架）；若 UI 继续扩张可评估迁移轻量框架，但需保留 reducer+SSE 契约。

---

## 最终实际架构（同图 `exports/最终架构_Project-Task-Message-Run.png`）

```
projects (prj_*)
└─ tasks (tk_*)                      ← Task：持续存在的一件事
   ├─ id/project_id/session_id(内部上下文键)/title/summary/status/archived_at
   ├─ messages                        ← 对话消息（user/assistant/system）
   │   └─ runs (task_*)               ← 每轮执行；状态机 + 审批 + 产物…
   │      ├─ events (task_events)
   │      ├─ tool_calls / model_calls
   │      ├─ approvals（决定后 resume 同一 run）
   │      ├─ verification（code_loop 四段总结）
   │      ├─ checkpoint / artifacts
   │      └─ assistant message 写回
   └─ 追加消息 → 新 Run，Task 不变
```

完成状态：P0（数据模型/存储/Runner/API/SSE/迁移）✅ · P1（两栏化+Drawer+Projector+Inline 审批+消息流）✅（建议浏览器真机过一遍视觉）· P2 清理未做（不阻塞交付）。

---

# 附录 A：下一版 UIUX（普通用户向）执行记录 v16

> 方案：《下一版 UIUX 的正式修改方案.txt》（20 节，P0/P1/P2）
> 状态：**全部落地**（P0×8 · P1×2 · P2×4）；资源版本 `?v=16`；配图 `exports/最终审计架构_产品层与Runtime.png`

## A.1 执行对照

| 阶段 | 条目 | 实现要点 |
|---|---|---|
| 一 P0 | 左栏重构 首页/任务/资料 | 左栏=FORGE+新任务+首页/任务/资料/设置+最近+回收站入口；更多·管理折叠 |
| 一 P0 | 首页大输入框 | 问候语+「你想让 FORGE 做什么？」+5 示例 chip+最近任务；首页无仪表盘数字 |
| 一 P0 | Project→工作位置 | 顶栏「工作位置：默认项目▾」弹层（最近/新建）；项目不再一级导航 |
| 一 P0 | 状态语言普通化 | 内部状态→正在处理/等你确认/已暂停/遇到问题/已停止/已完成；全程无 Run/Approval 术语 |
| 一 P0 | 主任务执行过程简化 | 第 N 次 + 消息先/过程卡/回复；三层信息（过程→详情→高级信息） |
| 二 P0 | 完成交付卡 | ✔已完成卡：清单+修改N文件(查看)+生成(打开)+验证+接下来 |
| 二 P0 | Inline 确认 | 「需要你确认」内联卡（see 修改内容/暂不/继续）；批准 resume 同一 run |
| 二 P0 | 失败恢复界面 | 「遇到一个问题」卡：继续尝试(同Task新Run)/查看问题(展开技术) |
| 二 P1 | Inspector→Drawer | 四业务 Tab(文件/修改/测试/运行记录)+高级信息折叠+轮次选择+ESC |
| 三 P1 | 资料页简化 | 添加资料(上传端点)+连接资料(打开/让FORGE使用)+你的文件+最近使用；无索引术语 |
| 三 P1 | 设置基础/高级分层 | 使用方式(推荐/安全/自动)+基础(账户/外观三态/通知/数据)+高级(模型/额度/权限/审批/Sandbox/备份=真实只读值) |
| 三 P2 | 全局搜索 | ⌕/Ctrl+K 浮层：任务/运行/消息/资料四组，防抖+直接跳转 |
| 三 P2 | 通知中心 | 🔔 面板+未读徽章：等你确认/遇到问题/已完成(近48h) |
| 附 | 回收站 | 任务页入口：恢复/彻底清除(级联删除+二次确认) |
| 附 | 拖拽上传 | 首页 hero 拖文件自动上传为资料 |
| 附 | 工作位置切换 | 弹层切换按项目过滤任务列表 |

## A.2 后端新增（P2 阶段）

- `POST /api/artifacts/upload`（资料上传，白名单后缀 20MB，防穿越，自动登记 kind=material）
- `GET /api/search?q=`（containers/runs/messages/artifacts 四组 LIKE）
- `GET /api/notifications`（approval pending 全部 + runs failed/completed 近 48h）
- `POST /api/tasks/{id}/archive|restore|delete`（回收站：归档/恢复/级联清除）
- `GET /api/runtime/status.config`（只读：模型名/权限开关/沙箱路径/备份数/DB 大小——不含密钥）
- `TaskManager`：search_* / delete_container / restore_container / list_pending_approvals_all / container_running_state_any

## A.3 最终审计结论（2026-09）

| 审计项 | 结果 |
|---|---|
| 离线回归 | 252/252 通过（TMR 模型 7 项含迁移无损；webapp 路由清单已同步） |
| 端到端（真实网关） | e2e_tmr 21/21：同 Task 多 Run / resume 同 Run / 双发事件 / 断连不悬挂 / 旧入口可用 |
| DB 健康 | user_version=1 · WAL；runs 59/tasks 7/messages 6/task_events 246/approvals 2/artifacts 4/checkpoints 6/model_calls 224/tool_calls 215；未终态 1（waiting_approval 设计保留）；孤儿 run/message = 0 |
| 静态一致性 | webapp 路由 40 条唯一；前端资源统一 v16；JS `$()` id 引用与 HTML 全对表 |
| 审计修漏 | ① 默认项目未落库 → api_projects_list 兜底 ensure_default_project；② notifications 500（Row 缺 goal 列）→ 修正；③ 死函数 drawerRunDetail 移除 |
| 遗留 | P2 保持预留：OS 级推送、可视化 Diff 面板(tool 结果存证)、工作位置持久化项目唯一性约束、schedules 双源收敛（方案允许后置） |

## A.4 验收标准对照（方案 §二十）

未接触 Agent 的新用户流程：打开→大输入框「分析这个 Excel」→上传/选择文件→看到正在工作→遇到确认时「需要你确认」→完成→「再按月份分类」继续同任务——
过程中用户不会看到 Agent/Run/Workspace/Tool/模型/Project/Approval 等词（产品表面一字不出现）。✅ 层面完成。
