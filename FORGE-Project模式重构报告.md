# FORGE-Project 模式重构报告

> 项目：FORGE / my_creative_agent · 日期：2026-09-06
> 目标：把「工作位置/Project → Task → Message → Run」重构为普通用户可理解的
> **Project（项目）= 用户长期做的一件事**，内部保留 Message→Run→Event 执行模型。
> 本次按 33 节规范实施 P0 全部 + P1 主体；P2（多 Thread/高级来源管理）后置并按真实状态记录。

---

## 1. 修改前 Project / Task / Session 真实定义（审计实证）

| 概念 | 修改前真实形态 |
|---|---|
| Project | `projects` 表（2 行：默认项目/学习平台，root_path 多空）＝“工作位置/文件夹占位”，非产品容器 |
| Task | `tasks` 表 tk_* = **用户长期容器**（session_id 内部上下文键、title、pinned），产品上叫“任务” |
| Session | `tasks.session_id`（会话内部键）→ SDK sessions.sqlite 历史；无产品级位置 |
| 一条消息 | create_task(run task_*) + user message(task_id=容器) → run_turn |
| 本地目录 | WORKSPACE_ROOT=仓库上级目录 |
| 上传文件 | `materials/` + artifacts(kind=material) 全局注册 |
| Memory | memories 表 scope user/personal（**仅全局**） |
| Artifacts/上传文件 | 同表同接口，靠 kind 区分 |
| 依赖旧语义 | task_manager 容器方法、webapp /api/tasks 容器端点、前端 rail/materials、多数容器相关测试 |

## 2. 修改后产品模型

```
Project（=用户长期做的一件事；=原长期容器，物理表 tasks(tk_*)）
├─ Conversation v1（=一条持续对话；messages 全部挂 project_id）
│  ├─ Message（user/assistant）→ Run（runs：Event/ToolCall/Approval/Verification/Artifact）
├─ Sources（来源文件，独立存储+表，检索/删除独立）
├─ Artifacts（FORGE 生成，独立于 Sources）
├─ ProjectMemory（项目记忆表）
├─ Instructions（项目说明，注入 Context）
└─ WorkLocation（可选本地目录，独立表；项目可无）
```

## 3. Project 定义

- `tasks` 物理表承载（tk_*），新增语义列：`instructions`、`memory_scope`、`work_location_id`
- 新 API `/api/projects*`：Project 列表/创建/详情/更新/删除
- 新建流程：仅需名称+记忆范围；**不要求 WorkLocation**（测试 1/10 通过）
- 追加消息=同 Project 新 Message+Run（测试 2/3、真实网关 E2E 通过，绝不新建 Project）
- 历史容器迁移为 Project；`tasks` 名保留为内部物理名（报告中注明，避免第三次破坏性 rename）

## 4. WorkLocation 定义

- 新表 `work_locations`(id/name/local_path/permission_profile/created_at/updated_at/last_used_at)
- 旧 `projects` 行自动迁移为 WorkLocation（默认项目/学习平台），旧表保留为兼容（legacy_project_id=旧行 id 原样）
- `PATCH project.work_location_id` 绑定/解绑；Context 注入工作位置路径行；当前版本单 WorkLocation

## 5. Sources 定义

- 表 `project_sources`（display_name/stored_path/original_name/mime_type/size_bytes/sha256/parse_status/index_status…）
- 文件持久化：`forge_data/projects/{project_id}/sources/`（复制入库，非浏览器临时对象）
- 上传 API `POST /api/projects/{id}/sources/upload?name=`；同项目同 sha 去重
- 删除保持一致性：DB 行 + 本地文件同步（测试 4/5）
- 失败态显示「这个文件暂时无法使用」不暴露嵌入/索引术语（前端状态词）

## 6. Artifacts 定义

- 与 Sources 严格分离：`/api/projects/{id}/artifacts` = 该项目 Run 产生的产物（JOIN runs）
- 测试 6：生成 artifact 后出现在项目产物，不出现在来源；上传来源不出现在产物

## 7. Memory Scope 定义

- `memory_scope`: `project_only`（默认，新项目）/ `global`
- 新表 `project_memories`（项目记忆：id/task_id/text/tags…）
- 工具层绑定（run 期间设置）：project_only → remember 写项目记忆、recall 只读本项目；global → 附加读全局；**绝不跨项目**
- UI：项目设置内「记忆范围」单选（仅此项目/使用全局记忆），不是装饰——真实影响 Context 与记忆工具（测试 7/8 实证）

## 8. Context Builder 新流程（实现）

```
User Message → Project(Context Builder 注入 instructions+sources+project memories+work_location 行)
→ 记忆工具受 memory_scope 门控（project_only 禁读全局/他人）
→ Agent Runtime（Message→Run→Model→Tool→Verification→Event）
```
- `runner.run_turn`：设置记忆绑定 + 对选中 agent clone 追加 Project 上下文块（≤6k 字符）
- 测试：上下文块含项目名/说明/来源名/项目记忆/工作位置路径；project_only 不含全局措辞

## 9. 数据库 migration（v2→v3）

- `user_version` 0→1（v1→v2）→**3**；`_migrate_v3`：
  1) tasks 加列 instructions/memory_scope/work_location_id
  2) work_locations 建表并从旧 projects 迁行（幂等 INSERT OR IGNORE，旧行原样保留）
  3) project_sources/project_memories 建表+索引
  4) 存量容器 memory_scope=global（保留旧行为）；新项目默认 project_only
- 全程无删表/清库/丢历史；runs/task_events/approvals 等原样保留

## 10. API 修改

新增：`/api/projects(列表/创建/详情/update/delete)`、`/projects/{id}/messages(+create)`、`/projects/{id}/stream`、`/projects/{id}/sources(+upload/删除)`、`/projects/{id}/artifacts`、`/projects/{id}/memories(+删除)`、`/api/worklocations(+create)`。
保留：全部旧端点（/api/tasks 容器、/api/stream、runs/*、/chat 等）作为兼容适配（阶段 9 旧入口零删除）。

## 11. 前端修改（P1 主体）

- 文案/导航：＋新任务→**＋新项目**；左栏「最近」→「项目」；「查看全部项目」
- 首页发送=创建 Project（memory_scope project_only），非旧“任务”
- 新项目：名称+记忆范围两问创建；空项目视图：大标题「开始和 FORGE 工作」+[添加来源文件][设置工作位置]
- 项目头部：新增「来源」「项目设置」按钮；meta 显示 记忆：仅此项目/使用全局记忆
- 项目设置 Drawer：名称/项目说明/记忆范围（真实 PATCH）/工作位置（输入路径→创建或复用 work_location→绑定/解绑）/删除项目
- 来源 Drawer：＋添加文件（上传）、列表（可用/暂时无法使用）、移除、下方独立“产物”区
- 资源版本 v23

## 12. 旧入口兼容

CLI/daemon/voice//chat/旧 /api/stream：物理保留未动；run_turn 语义不变，容器即项目 → 旧入口自动写进（默认）项目。旧「任务」前端标签已移除；`/api/tasks` 容器端点保留给兼容调用方。工作位置旧下拉不再作为项目入口（仍可从设置绑定）。

## 13. 自动化测试结果

- 新增 `tests/test_project_model.py` **9 用例**：
  1) 无 WorkLocation 建项目 ✅ 2/3) 同一项目 2 Message+2 Run、无新项目 ✅ 4) Sources 落盘+DB+重开(重启模拟)持久 ✅ 5) 删除 DB+文件一致 ✅ 6) Artifacts 与 Sources 互不混入 ✅
  7) project_only 读不到全局/他项目（remember/recall 门控实证）✅ 7b) project_only 写入不进全局 ✅ 8) global 可读全局 ✅
  Context Builder：project_only/global 措辞与说明/来源/记忆/工作位置注入 ✅
- 全量离线：**308 tests OK（skipped=1=主题测试库中无 failed 容器，正常）**
- 真实网关 E2E：创建「银行流水整理」→两轮消息 → 同项目 2 runs/4 messages → 无新项目 ✅；Sources 上传/产物分离 ✅（HTTP 实测）

## 14. E2E 测试结果

见 §13 真网关段落；另 HTTP 冒烟覆盖：work_locations 迁移=默认项目+学习平台、project 创建 scope 默认 project_only、source 上传去重/删除、artifacts 端点空（sources 不混入）、重启后 source 仍在。

## 15. 历史数据迁移结果

真实 agent.db：v3 已应用；旧 projects 2 行→work_locations；存量容器 memory_scope 置 global（旧行为），runs/tasks/messages 计数未变；`user_version=3`。

## 16. 未解决问题（如实）

1. **物理表名仍是 `tasks`（容器）**：为避免第三次破坏性 rename 与全线 SQL/兼容面重写，语义层=Project 且 API/UI 全称 Project，物理名保留并注释为内部细节；如未来要 100% 对齐物理名需一次全量改名迁移（风险列入 P2）。
2. **Sources 检索**：来源已本地持久化+列表+可被 Agent 读取（路径注入+read 工具工作区内），但未做按项目自动 RAG 索引/向量检索（index_status=none）；第一版以“明确路径+读取”满足，深度检索列入 P2（§“高级来源管理”）。
3. **多 Conversation/Thread**：v1 单对话（数据结构天然可按 project 分 threads，仅 UI 未暴露），P2。
4. 前端旧「资料/任务」全局页仍保留为兼容入口；版本迭代中逐步收敛。

## 17. 删除的旧代码

- webapp：旧语义 `api_projects_create`（写旧 projects 表的工作位置创建）删除（重复定义后者胜问题一并修复）；旧 `api_projects_list`（工作位置列表）替换为新 Project 列表（工作位置移至 /api/worklocations）
- 无整文件删除（遵守先确认引用原则）

## 18. 暂时保留的兼容代码

- 旧 `projects` 表与相关兼容查询（/api/worklocations 之外的旧调用方若仍用 /api/projects 旧形状→现已指向项目列表，前端同步适配）
- 旧 /api/tasks*、/api/stream、/chat、CLI/daemon/voice 全保留
- tools 全局记忆旧路径（无绑定=旧行为）保留
- v2 容器消息/run 表结构与旧任务视图保留（经 Project 面板查看）

## 最终实际架构

```
Project（tk_*：name/instructions/memory_scope/work_location_id/pinned）
├─ Conversation v1（messages → runs → task_events/approvals/checkpoints/artifacts）
│   ├─ Message(user) └─ Run(task_*) ─ Event/ToolCall/Approval/Verification
│   └─ Message(assistant 由 Run 产出写回)
├─ Sources（forge_data/projects/{pid}/sources + project_sources 表）
├─ Artifacts（notes|exports + artifacts 表，按 Run→Project 聚合，与 Sources 分离）
├─ ProjectMemory（project_memories 表；scope=project_only 时唯一记忆源）
├─ Instructions（注入 Context Builder）
└─ WorkLocation（work_locations 表；可空；Context 注入路径）
```

## 结论确认

- **Project-only Memory 与 Global Memory 边界已通过真实自动化测试证明有效**（tests/test_project_model.py::MemoryBoundaryTests，7/7b/8）：
  - project_only 项目：remember 只进项目记忆；recall 只返回本项目内容，明确不返回全局/其他项目（含关键词精确验证）；
  - global 项目：recall 附加全局记忆命中。
- 当前为“完成代码与测试的迭代 1”：P0 全项 ✅、P1 主体 ✅、P2 依规范后置并已列入 §16。
