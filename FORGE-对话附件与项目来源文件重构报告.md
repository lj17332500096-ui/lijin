# FORGE-对话附件与项目来源文件重构报告

> 项目：FORGE / my_creative_agent · 日期：2026-09-06 · 前端资源 v24
> 目标：严格区分 **Project Sources（长期来源）** 与 **Message Attachments（本次附件）**，并让数据模型、存储、Context Builder、Agent 执行、UI 全部生效。

---

## 1. 修改前上传文件真实流程

- Composer「＋ 添加文件」是占位（CSS 隐藏 + toast「即将上线」），**不存在 Composer 上传**；
- 文件只能进 Project Sources（`/api/projects/{id}/sources/upload`，落盘 `forge_data/projects/{pid}/sources/`）；
- messages 表无 attachment；Run 拿文件只能靠来源清单路径；Context 只注入 Sources 名+路径；
- 无 message 级作用域概念 → 所有上传即长期来源（默认污染项目长期上下文）。

## 2. Message Attachment 数据模型（新增 `message_attachments`）

```
id / task_id(project) / message_id / run_id / display_name / stored_path
mime_type / size_bytes / sha256 / attachment_scope('message_only'|'project_source')
promoted_to_source_id(可空) / created_at / updated_at
```
- 上传即持久化行（message_id NULL=待绑定）；发送时原子安全顺序：Upload→Attachment persisted→Message→Bind→Run（消息创建接口返回 bound_attachments 计数）；
- 删除未绑定附件：行+文件同删；绑定后的历史消息附件保留（供重开查看）；
- 项目删除级联清理行与 `forge_data/projects/{pid}` 目录。

## 3. Project Source 数据模型（既有 `project_sources`）

id/task_id/display_name/stored_path/mime/size/sha256/source_type/parse_status/index_status/metadata。删除 Source 时同步删除引用它的附件行（防悬空）+ 物理文件。

## 4. Artifact 区别

Artifacts（`/projects/{id}/artifacts`，JOIN runs）只含 FORGE 产物；Sources/Attachments 各自独立列表——三作用域互不混入（测试 6/10 实证）。

## 5. 本地文件存储结构

```
forge_data/projects/{project_id}/
  sources/{name}.pdf            # 项目长期来源
  attachments/{uuid}/{name}     # 当前消息临时附件（待绑定或已绑定）
```
同名冲突加 uuid 前缀，杜绝覆盖；均 BaseName + 白名单 + 大小上限。

## 6. Attachment → Source 升级逻辑（Promote）

`POST /projects/{pid}/attachments/{att}/promote`
- **按 sha256 去重**：项目已有同 hash Source → 引用该 Source（更新 stored_path/scope/promoted_to_source_id），并删除独立副本文件 → 不出现 `attachments/A.xlsx + sources/A.xlsx + sources/A-copy.xlsx`；
- 无同 hash：物理移/复制入 sources 目录一份 + 建 Source + 记关系（数据库知道来源，避免重复索引）；
- 前端 chip「加入项目来源」触发；升级后消息气泡显示「已加入项目来源」语义（scope 变 project_source）。

## 7. Context Builder 如何使用当前附件

`run_turn` 执行前：取本 Run 触发 user message 的附件（`attachments_for_run`），拼成**高优先级块**注入 agent instructions：

```
【本次消息附件——用户这次明确提供的文件，优先处理，务必不要与其他来源混淆】
- 2026预算.xlsx（仅本次使用）路径：forge_data/.../attachments/...
```
附在项目上下文之前；模型据此不会误选 Sources 中另一个同名/同类 Excel（离线测试用 captured instructions 断言）。

## 8. Project Source 如何参与长期上下文

项目上下文块持续注入 Sources 清单+路径；仅属于当前 Run 的临时附件不进入（下一条消息不带入——离线测试断言 captured[1] 无附件块），历史摘要/结论仍按会话正常保留。

## 9. 文件解析 / 索引范围

- Message Attachment：**不做长期索引**（message_only 只随 Run 临时解析；需求限定“当前 Run 临时解析/提取”），index 无副作用；
- Project Source：保持既有 parse/index 状态字段（现状 parse ok / index none，深度 RAG 仍属 P2——如实保留）；
- 解析失败不会令消息失败（多附件 A 可读 B 不可读时提示 B，可移除/重传/继续）。

## 10. Composer 修改

- 「＋ 添加文件」可见 → 轻菜单：**上传文件（仅本次使用）** / **从项目来源选择**；
- 上传 chip：📎 文件名 + 状态（正在上传… / 上传失败[重试] / 仅本次使用[加入项目来源][×]）；支持多附件、发送前移除；
- 从项目来源选择：多选 → 只建引用（`refs`，路径=Source 路径，零复制零重复索引），chip 显示「项目来源」；
- 发送：若有附件 → `POST messages/create{attachments:[ids]}` → 绑定计数 → 打开 run stream；无附件走原消息流；
- 发送后 chips 清空；历史消息重开气泡下显示 📎 附件 chips（仅本次使用 / 项目来源）。

## 11. Sources Drawer 修改

仍显示**长期来源**（含状态：可用/暂时无法使用，不出现 嵌入/索引/Vector 词）；附件不混入该列表。

## 12. 安全处理

- 上传共用白名单：文本/代码（.txt/.md/.py/.js/.ts/.csv/.json/.html…）、Office（.docx/.xlsx/…）、PDF、常见图片；
- **拒绝**：脚本/可执行（.exe/.bat/.cmd/.ps1/.sh/.msi…）、压缩包（.zip/.rar/.7z）、目录、svg；BaseName-only 防路径穿越；同名不覆盖（uuid 前缀）；50MB 上限；
- 附件≠工作权限：上传只落 `attachments/` 副本，绝不把父目录当 WorkLocation（无此逻辑路径）。

## 13. 自动化测试结果

新增 `tests/test_message_attachments.py` **11 用例**，全量 **319 tests OK**（skipped=1 属主题测试无 failed 容器）：
1 附件=message_only 非 Source ✅ · 2/3 Run 可读且下一条消息无附件 ✅（Context captured 断言） · 4 Promote→Source ✅ · 5 重启持久（文件+行）✅ · 6 引用不复制（文件计数不变）✅ · 7 三附件绑定 ✅ · 8 未绑定删除行+文件一致 ✅ · 10 Artifact 不入附件/Source ✅ · 11 重复 sha Promote 去重单副本 ✅ · 12 Promote 关系（scope/source_id/文件）✅

## 14. E2E 测试结果（真实网关 + HTTP）

T1 上传 scope=message_only、Sources=0 ✅ · T2 bound=1 ✅ · T3 run.completed ✅ · T5 消息详情携带附件（仅本次使用）✅ · T4 promote→project_source + sources=1 ✅ · T6 ref 路径==Source 路径 ✅ · T5b 原消息附件保留并显示项目来源 ✅ · T7 文件落盘 ✅；项目删除级联清理 ✅

## 15. 修改文件清单

- `runtime/task_manager.py`：`_ensure_v31_tables`、message_attachments CRUD/bind/promote/refs/by-run/删除、delete_container 级联、delete_project_source 关联清理
- `webapp.py`：附件 upload/list/delete/promote/refs、messages/create 附件绑定、两处详情富化 attachments、来源上传白名单化、公共 `_safe_upload_name/_store_upload_bytes`
- `runtime/runner.py`：run_turn 注入「本次消息附件」高优先级 Context 块
- `web/runtime.html`：Composer ＋添加文件 + 菜单 + chips 条 + 气泡附件 chips 样式（v24）
- `web/runtime/api/client.js`：附件/promote/refs/project-message 方法
- `web/runtime/workspace.js`：Composer 附件状态机（上传/失败重试/移除/加入来源/引用来源/发送绑定/清空）、user 消息附件 chips 渲染
- `tests/test_message_attachments.py`（新增 11 用例）

## 16. 尚未解决的问题

1. 上传并发同名不同内容：uuid 前缀保底，但同名旧 chip 仍显示原名（记录于 attach chip 文本，可接受）；
2. 绑定中途失败的孤儿附件行（message_id NULL）暂无自动清理任务（P2 临时文件清理）；
3. Sources 深度检索/索引（index_status 已预留）仍未接 RAG pipeline（P2）；
4. 前端 Composer 上传仅作用于项目任务页；首页大输入框的“添加文件”按钮仍为占位（其下即引导新建项目后在项目内使用附件）；
5. `.msg-att-list` chips 仅 user 消息渲染；assistant 消息若被注入路径文本按正文 Markdown 呈现。

## 验收结论

「临时附件」与「项目来源文件」已是**两个独立作用域**（独立表/存储目录/Context 规则/UI 措辞），并已通过：
- 离线测试（作用域、promote 去重、引用零复制、重启持久、Context 注入/隔离）；
- 真网关 E2E（上传→绑定→执行读取→promote→引用→消息展示→级联删除）。

普通用户路径：上传文件 → 📎 仅本次使用 →（可选）[加入项目来源] → 后续消息继续可用该来源 → Composer 里“从项目来源选择”只建引用不再重复上传。
