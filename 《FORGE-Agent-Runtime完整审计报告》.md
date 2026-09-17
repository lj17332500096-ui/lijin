# 《FORGE-Agent-Runtime 完整审计报告》

> 审计对象：`F:\Byong-hermes\Byong-hermes\my_creative_agent`（FORGE）
> 审计日期：2026-09-06（UTC+8 17:35 – 18:20）
> 审计方式：静态代码核实（每项附 文件:行号）+ 数据库检查（agent.db / sessions.sqlite）+ 配置检查 + 离线回归测试（320 项）+ 真实网关 E2E（独立 8799 实例，测试后已关闭）+ 确定性工具边界测试。
> 审计范围声明：**未修改任何项目源代码**。全部动态测试在临时脚本与自建审计项目/沙箱中进行，测试数据已清理（临时记忆标记已删除、auditfib 沙箱已删除、8799 实例已关闭）。原 8765 实例未受打扰。
> 代码库事实基线：agent.db `user_version=3`（v3.1 结构，WAL）；注册工具 36 个；`sessions.sqlite` 21 会话 / 2410 条 SDK 历史；离线测试 `tests/run_tests.py` 实测 **320 项 OK（skipped=2）**，87.3s。

---

## 1. 当前真实 Agent 调用链（代码级，非推测）

### 1.1 全景（Web v2 主链路 = Project 容器流）

```
用户消息(Composer)
  └─ web/runtime/workspace.js commitHome/sendNow → createProject / taskStreamUrl
  └─ GET /api/projects/{pid}/stream?message=…&max_turns=20   [webapp.py:1036 api_project_stream]
      └─ create_task(SUBMITTED) + add_message(user)          [webapp.py:1056-1062 → task_manager.py:635/592]
      └─ 改写 query → api_stream(resume_task=run.id)          [webapp.py:1071 → api_stream 253 分支]
          └─ SSE 事件生成器(Queue 排水)                        [webapp.py:265-301]
          └─ AgentRuntime.run_turn(task_id=run.id,…)          [webapp.py:273 → runtime/runner.py:250]
              ├─ is_resumable? transition(RUNNING)             [runner.py:281-288, task_manager.py:733]
              ├─ gate.begin(run.id, channel=web)              [runner.py:327, runtime/approval.py:68]
              ├─ ArtifactTracker.snapshot / AuditCollector     [runner.py:328-330]
              ├─ resolve_budget(取更严格 max_turns)            [runner.py:333, runtime/budget.py:17]
              ├─ route_profile(task) → "default"              [runner.py:334, runtime/router.py:36]
              ├─ route_agent = 模型档位克隆 + ToolRouter≤16 子集 [runner.py:335 → 168-197, runtime/tool_router.py:73]
              ├─ 记忆绑定 set_active_memory_binding            [runner.py:344-349 → tools.py:40]
              ├─ Project Context 块(ctx_block)                 [runner.py:350 → _project_context_block 199-233]
              ├─ 本次消息附件清单(前置)                         [runner.py:352-365]
              ├─ agent.clone(instructions=基础+ctx_block)      [runner.py:366-371]
              ├─ run_with_wall_limit(execute_turn)             [runner.py:378 → main.py:424 execute_turn]
              │    └─ Runner.run_streamed(SDK agent loop)      [main.py:365-371]
              │         └─ 工具调用 → 审批门包装检查            [runner.py:136-147 gate.check]
              │              ├─ 放行 → 原工具执行
              │              ├─ 拦截 → 模型收到【需要审批】文本  [approval.py:23-31]
              │         └─ 会话历史读写 = SDK sessions.sqlite   [SDK sqlite_session / session_persistence]
              │         └─ 成功后 audit.ingest(model/tool 明细) [main.py:454-458 → runtime/audit.py:59]
              ├─ reply_parser 宽容解析(格式错误不判失败)        [runner.py:480-489 → runtime/reply_parser.py]
              ├─ mark_success → COMPLETED（无验证闸）           [runner.py:493 → task_manager.py:763]
              ├─ 产物 diff 登记 ArtifactStore                   [runner.py:494 → runtime/artifacts.py:58]
              ├─ assistant Message 落库(agent.db messages)     [runner.py:495 → _note_assistant 311-321]
              └─ save_success_checkpoint(仅摘要)               [runner.py:502 → runtime/checkpoint.py]
  └─ SSE: run.started / tool / reply_delta / reply / run.completed  [webapp.py:262/290/294/316/318]
```

### 1.2 关键事实

| 环节 | 真实归属 | 说明 |
|---|---|---|
| 用户消息→Project 对话 | `webapp.py api_project_stream`(1036) / `api_task_stream`(1526) | 容器即 Project；`tasks` 物理表承载 Project 语义 |
| Run 创建 | `task_manager.create_task`(635) | id `task_*`；状态机 8 态(runtime/task.py:18) |
| 上下文历史 | **SDK sessions.sqlite**（每 run 全量读回） | agent.db `messages` 仅供 UI/审计，模型从不读它 |
| Context 注入 | `runner._project_context_block`(199) + 附件块(352) | 注入到 clone 的 instructions，截断 6000 字符 |
| 模型选择 | `router.route_profile`(36) + `agent_for`(61) | 当前无任何档位 env → 一切走 default |
| 工具执行 | SDK Runner 直接执行 `agent.tools` | ToolBroker/Registry 不参与执行 |
| 审批 | 工具包装(runner 110-160)在调用瞬间 check | 3 个默认工具被门 |
| 验证 | **无**（见 §17） | code_loop 仅是可选工具 |
| 完成判定 | 模型循环结束 + parse 宽容 + `mark_success` | 无"任务真的完成"检查 |
| UI | web/runtime/*.js (v29) | EventSource + reducer |

### 1.3 并行存在的第二条执行链（Legacy /chat）

`GET /api/stream?message=`（webapp.py:322-413）**不经过 run_turn**：无 Project Context 注入、无记忆绑定、无 budget 墙钟、直接 `Runner.run_streamed`。仅旧聊天页 index.html 使用，但它是"同一 Agent 两套行为"的源头（见 §23/§24）。

### 1.4 终端/语音/定时入口

全部收敛到 `AgentRuntime.run_turn`（main.py:528 chat、723 voice、776 scheduled）；`execute_turn`（main.py:424-480）是唯一共享执行器，web L3 也走它。

---

## 2. 当前配置来源总表

> 配置层结论先行：**"配置文件里有"≠"运行时真的用"** 的例子在 FORGE 大量存在（详 §22）。

| 配置项 | 默认值 | 真实读取点 | 是否真生效 | 备注 |
|---|---|---|---|---|
| `OPENAI_API_KEY` | — | agent.py:55 dotenv → SDK | ✅ | .env 有值（值未审计输出） |
| `OPENAI_BASE_URL` | 直连 | agent.py:70-74 | ✅ 网关模式 | |
| `AGENT_MODEL` | SDK 默认 | agent.py:79；当前 `agnes-2.5-flash` | ✅ | 单模型现状 |
| `OPENAI_USE_RESPONSES` | true | agent.py:71 | ✅ false→chat/completions | |
| `WORKSPACE_ROOT` | — | tools.py:23 等 | ✅ `F:\Byong-hermes\Byong-hermes` | **真正文件边界** |
| `TAVILY_API_KEY` | 无 | tools.py web_search_impl:521-555 | ✅ 有则优先 | |
| `ALLOW_CODE_EXEC` | false | code_exec.py:40-41 | ✅ true | |
| `ALLOW_PROJECT_EDIT` | false | project_edit.py:52-53 | ✅ true | |
| `SKILLS` | 空 | skills_loader.py | ✅ 4 个已启用 | 仅 dep_doctor 带工具 |
| `VISION_MODEL` | =AGENT_MODEL | multimodal.py:52 | ⚠️ 未配 | 不填走 AGENT_MODEL |
| `MODEL_CHEAP/MODEL_REASONING` | 空 | router.py:28-33 | ⚠️ **未配→档位不生效** | 路由=假路由（见 §3） |
| `MODEL_MAX_OUTPUT_TOKENS` | 4096(agent.py:80 固定) | router.py:50 | ⚠️ 未配 | 克隆才覆盖 |
| `TOOL_ROUTER` | on | tool_router.py:69-70 | ✅ 默认开 | ≤16 工具/轮 |
| `APPROVAL` | on | approval.py:50-52 | ✅ | 未配=开 |
| `APPROVAL_GATED_TOOLS` | 3 件套 | approval.py:55-61 | ✅ 默认名单 | `all/*` 不扩展（bug 级语义） |
| `TASK_MAX_WALL_SECONDS` | 无 | budget.py:30-32 | ⚠️ **未接线**：run_turn 从不读 env，须传 RunBudget | 死配置 |
| `AUTO_SUMMARY_*` | 60轮/180k字/20轮 | compact.py:30-46 | ⚠️ 仅终端/语音链路 | Web 链路不调用（见 §10） |
| `FORGE_MEMORY_ENABLED` | 1 | tools.py:49；webapp.py:723(api/settings/memory) | ✅ | UI 开关真实写 os.environ |
| `FORGE_REPLY_REPAIR=model` | off | reply_parser.py:115-144 | ⚠️ 模型修复默认关 | 本地修复路径工作 |
| `MCP_SERVERS` | — | mcp_bridge.py | ⚠️ 未配 | 无 MCP |
| `GITHUB_TOKEN` | — | github_fetch.py:208 | ⚠️ 未配 | 仅私有仓库需要 |
| 前端 localStorage | `rt.backend`/`forge.memoryEnabled`/`forge.mode`/`forge.notify`/`forge.theme.main` | web/runtime/*.js | ⚠️ 仅有 `rt.backend`、主题、记忆开关有后端消费 | `forge.mode` 未发现后端读取（死 UI 键） |
| 前端"模型"设置 | — | workspace.js:689 只读展示 | ⚠️ **仅展示**，无模型选择器 | UI 与后端一致（都只有一个模型） |

**重复配置**：`AGENT_MODEL` vs `MODEL_DEFAULT`（router.py:24-25 双读，当前只配前者，语义重复）；`sessions.sqlite` 与 `agent.db.messages` 双写对话（各自为政，无一致性协议）；进程级 env `FORGE_MEMORY_ENABLED` 由 UI 直接写 `os.environ`（webapp.py:723），与 .env 不同源（进程内覆盖仅本进程）。

---

## 3. Model Routing 审计

### 现状（代码 + 实测）

1. **模型入口只有一个**：`assistant_agent`（agent.py:77，`AGENT_MODEL=agnes-2.5-flash`）。
2. 谁决定模型：`runner.route_agent`（runner.py:168-197）→ `agent_for(base, profile)`（router.py:61）；profile 由 `route_profile`（router.py:36-47）从 metadata.channel/deep 推断。
3. **route_agent 真实参与运行**（run_turn 335 与 webapp legacy 342 都调用）——但它当前只做 **ToolRouter 裁剪**，模型档位分支永不触发，因为：
   - `MODEL_CHEAP`/`MODEL_REASONING` 未配置（router.py:65-67 无模型 → 原样返回 base）；
   - 无任何调用方传 `model_profile=cheap/reasoning` 或 `deep=True`；
   - 定时渠道即使命中 cheap 分支，也因无 `MODEL_CHEAP` 落回 default。
4. **E2E 实测（F1/F2/F3/F4，真实网关）**：8 个 run 全部同一模型 `agnes-2.5-flash`，`model_calls` 表 model 列全部为空（`audit.py` 不记录 provider 返回的 model 名 → 审计不可查模型）。
5. fallback：**无模型级 fallback**。SDK/网关失败只报错分类（main.py:238-260），无第二模型切换。
6. 失败重试：guardrail 输出闸失败自动带原因重试 **1 次**（main.py:446-480：attempt==1 即 raise）；Provider 网络错误无项目侧自动重试。
7. **指数重试风险：不存在**——只有 execute_turn 一层自动重试（见 §16）。
8. 最大上下文：无项目侧上限；SDK SQLiteSession 每轮全量读回历史（web 链路），无 token 上限 → 溢出时网关 400，项目侧只有错误提示（main.py:238-260）。最大输出：`max_tokens=4096`（agent.py:80），`MODEL_MAX_OUTPUT_TOKENS` 未生效。
9. timeout：SDK 无显式配置；工具超时 run_python 40s(默认)/120s(max)。
10. thinking/reasoning effort：**无任何配置**（代码中无 reasoning_effort 字段）。
11. Structured Output：**未启用**（README:136-139 自述网关兼容原因放弃 output_type）；输出结构靠 prompt 约束 + ReplyParser 宽容解析 + guardrail 兜底。**注意**：模型"0 工具调用直接回答"时，没有任何层强制要求结构（F3 假完成即此路）。
12. UI 模型选择：**无选择器**，设置页仅展示运行时 config.model（workspace.js:689）→ "UI 选择"不存在，不存在不一致；但展示的 "FORGE 自动选择"文案有误导（实际从不自动选择）。

### 结论（15 问的浓缩回答）

- 几个模型入口？**1 个真实**。
- route_agent 是否参与？**参与，但只裁工具，不换模型**。
- 不同任务是否路由不同模型？**不。所有任务同一模型**（实测 8/8 相同）。
- fallback 工作？**否（不存在）**。
- 失败重试几次？guardrail 1 次；provider 0 次。
- 档位功能 = **已实现的骨架 + 0 配置**：属"功能未启用"，不是 bug，但"多档模型"宣传要降级为"待配置"。

---

## 4. Tool Registry（真实清单）

### 注册 36 个（agent.py:198-235 + skills/dep_doctor/tools.py）

| 分类 | 工具 | 写/执行范围 | 需要审批 | 网络 | 备注 |
|---|---|---|---|---|---|
| 文件产出 | save_note/read_note/list_notes | 项目 `notes/` | 否 | 否 | 文件名时间戳生成，无穿越面 |
| 工作区读 | read_workspace_file/list_workspace_files | **WORKSPACE_ROOT 内只读**（拒绝 .env、>2MB、二进制） | 否 | 否 | 读侧保护见 §19 |
| 计算/时间 | calculate/get_current_datetime | — | 否 | 否 | AST 白名单 |
| 记忆 | remember/recall_memory/forget_memory | memories/project_memories 表 | forget 需 | 否 | 写闸密钥/空壳 |
| RAG | index_workspace/search_documents | WORKSPACE_ROOT 内 | 否 | 否 | 跳过敏感目录 |
| 搜索/调研 | web_search/deep_research | — | 否 | **是**（3 源固定端点） | trust 包裹 |
| GitHub | fetch_github_repo | WORKSPACE_ROOT/github_repos | 否 | 是（仅 github.com） | 解压防穿越 |
| 图片 | ask_image | WORKSPACE_ROOT 内只读 | 否 | 是（网关多模态） | ≤10MB |
| 代码沙箱 | write_code_file/read_code_file/list_code_files/**run_python** | **WORKSPACE_ROOT/code_sandbox** | **run_python 需** | 子进程网络可通 | 子进程环境白名单 16 键（code_exec.py:36-37） |
| 代码闭环 | **code_loop**（自动运行-修复-验证） | 沙箱内 | **否（旁路审批！）** | 子进程网络可通 | 内含 run_python_impl（codex_loop.py:121） |
| 沙箱快照 | sandbox_snapshot/**sandbox_rollback**/list_sandbox_snapshots | logs/sandbox_snapshots | **rollback 否（旁路面）** | 否 | 保留 5 份/50MB |
| Office | read_office_file/read_spreadsheet | WORKSPACE_ROOT 内只读 ≤4MB | 否 | 否 | xlsx 与 read_spreadsheet 重叠 |
| Office 生成 | save_word_doc/save_excel_workbook/save_ppt_deck | 项目 `exports/` | 否 | 否 | 上限表级 |
| 项目文件 | write_project_file/edit_project_file | **项目目录**（BASE_DIR）内，白名单后缀 | 否 | 否 | 覆盖前自动备份 logs/backups |
| 计划 | schedule_add/list/remove/set_enabled | tasks.json | remove 需 | 否 | |
| 技能 | scan_dependencies | WORKSPACE_ROOT 只读 | 否 | 否 | |

**不存在**的工具：delete_file / shell / git 写操作 / 通用 fetch_url —— 全部没有（实测工具全集 36 个无 delete/shell/git）。系统无"删除文件"能力，这是好事，但用户审计清单中的"删除文件"项**无对应工具**。

### 工具系统事实

- 暴露给模型：36 中按轮裁剪 ≤16（tool_router 默认开；基础集 8 永不裁）。**模型看不到 ≠ 不可达**：模型不可见的工具不会被调用（无 schema），符合"模型能看到就能执行、看不到则不能"。
- 执行层注册 = 暴露层 = 模型可见层：三者是同一份 `agent.tools` 清单（SKILL_TOOLS 启动时收集一次，agent.py:58/234）。
- **重复/重叠**：read_workspace_file ⊇ read_note ⊇ read_code_file；save_note 与 write_project_file(可写 notes/)能力重叠；read_office_file 与 read_spreadsheet 对 xlsx 重叠；run_python 与 code_loop（**后者绕过审批**）重叠。
- 测试覆盖：tests/test_tools / test_code_exec / test_project_edit / test_office_docs / test_trust 等离线用例齐全（工具层 320 项中占大头），但 **E2E 级此前无自动化**（tests/e2e_tmr.py 是流程级，手动跑）。

---

## 5. Permission 审计（实测结果）

### 5.1 Permission 是谁

没有独立 Permission 策略层。实际安全边界 = **工具函数内的路径/类型校验（Runtime 硬限制）**：

| 能力 | allow/deny 判定点 | 实测 |
|---|---|---|
| 读 WORKSPACE_ROOT 内文本 | `_resolve_under_root` + `_is_protected`（tools.py:102-123） | ✅ 越界/穿越/.env 全拒（见下） |
| 读 .env | _SKIP_FILES={.env,apikey.txt} + `.env.*` 前缀 | ✅ 拒绝"出于安全考虑" |
| 读 agent.db/sessions.sqlite | 二进制/\x00 检测 + 2MB 上限 | ✅ 拒绝（库/会话不泄漏） |
| 读旧记忆备份 `memory.json.migrated.bak` | **不在拒绝名单** | ❌ **可读**（P2 泄漏面，见 §19） |
| 跨项目读（另一 Project 的 sources 文本） | **无项目归属校验** | ❌ **可读**（P2，见 §19） |
| 写项目文件 | BASE_DIR + 后缀白名单 + PROTECTED（project_edit.py:27-49） | ✅ .env/越界全拒 |
| 写沙箱 | code_sandbox 正则 + resolve（code_exec.py:44-58） | ✅ 绝对/越界拒；`sub/..\` 被折叠为沙箱内路径（不出界） |
| 执行 | 仅 run_python（沙箱内子进程，白名单环境） | ✅ 需审批 |
| 删/改系统文件 | 工具不存在 | ✅ 不可能 |

### 5.2 实测记录（audit_boundary，确定性离线）

```
read C:\Windows\win.ini            → 错误：只能读取工作区 F:\Byong-hermes\Byong-hermes 内的文件。
read ../../../../Windows/win.ini   → 同上（穿越拒绝）
read .env                          → 错误：出于安全考虑，这个文件不允许读取。
read agent.db                      → 看起来是二进制文件…无法直接阅读
read sessions.sqlite               → 错误：文件超过 2MB，暂不支持整读
read memory.json.migrated.bak      → 成功读取（带【外部数据】边界）⚠
read 其他项目 sources/来源.md       → 成功读取（带【外部数据】边界）⚠
pe write .env / ../../escape       → 路径不合法或属于受保护文件
ce write ../../escape、C:\…\pwn    → 文件名必须是沙箱内的相对路径
ce write sub/..\evil.txt           → 已写入（折叠为沙箱内 evil.txt，不出界，可接受）
```

### 5.3 结论

- 安全边界**确实在 Runtime 层**，不是口头 prompt。读/写/执行三档都真实存在代码级校验。✅
- 但边界是**"工作区 = WORKSPACE_ROOT"这一整个目录**（当前含 awesome-llm-apps-main/code_sandbox/my_creative_agent 全部）——不是 Project、不是 WorkLocation。**授权一个 Project 不会给全盘**（盘外不可达），但给的是"整个工作区兄弟目录"的只读面。

---

## 6. Approval 审计（代码 + E2E 实测）

### 6.1 机制

- 名单：默认 3 个 `run_python/forget_memory/schedule_remove`（approval.py:21）；`APPROVAL_GATED_TOOLS` 可自定义；`APPROVAL=off` 关闭。**"all/*" 语义 bug：返回默认 3 件套而非全部**（approval.py:57-58）。
- 拦截点：**工具调用瞬间**，由 runner 包装克隆执行 `gate.check`（runner.py:136-147），**先于工具本体执行** → 满足"approval 在操作执行前"。
- 挂起：pending → 落 approvals 行 + 事件 → run 转 `WAITING_APPROVAL`（runner.py:456-463）。拒绝：`denied_this_run` → `mark_failure`（runner.py:428-454）。定时渠道自动拒绝（approval.py:101-107）。
- 恢复：`POST /api/approval`（webapp.py:457）→ `decide_approval`（task_manager.py:994）→ 前端 resume → `run_turn(task_id=同 run)`（runner.py:281-288）**同一 runs 行续跑**；已批准记录按 (run_id, tool, args_key) 命中放行（approval.py:89-92）。
- 本轮防重复：同工具再次调用 → REPEAT_TEXT / 已挂起同参数 → BLOCK_TEXT（approval.py:96-99），防死循环。

### 6.2 E2E 实测（F3B，真实网关，run `task_47a5521d`）

```
1. 模型调用 write_code_file(成功,写修复) → run_python
2. run_python 首次调用被门拦：“【需要审批】工具 run_python 参数 {…}” —— 未执行 ✅
3. 模型重试一次 → 纯 BLOCK_TEXT（同参数挂起不重复建单）✅
4. run 转 waiting_approval；approvals_pending=1；approv_3c3c0e9a（run_python）
5. POST /api/approval {approved} → 09:46:16 决定
6. GET /api/runs/{同一run_id}/stream → 同一 run 续跑（runs 行不变）
7. 恢复后 write+run_python：门查 approved 记录 → 放行执行 ✅
8. 但模型最终输出为空 ×2（guardrail tripwire）→ run 终态 failed（见 §18）
```

- **审批先于执行：实证成立**（第 1 次调用被拦时沙箱里没有运行副作用，工具审计显示 run_python 状态为 blocked 文本）。
- **批准后同 run_id 续跑：实证成立**。
- **拒绝路径**（denied）：代码链完整（decide_approval → denied → resume 后 DENY_TEXT；或直接拒绝时 mark_failure），本次未重复实测（tests/test_approval.py 覆盖门逻辑）。
- **UX 漏洞（F3 实证）**：模型可以不调用工具、"口头请求批准"并结束回合 → run 直接 `completed`，**没有任何 pending approval**（approvals_pending=0），用户永远不会看到确认卡。**Approval 只能由 Runtime 在工具调用时触发——但"模型决定不触发"时，没有任何一层要求它把话说成 questions/等待，run 会被判完成**（P0，见 §17/§29）。

---

## 7. WorkLocation / 文件系统边界

### 7.1 真实关系

- Project = `tasks` 容器（DB）；WorkLocation = `work_locations` 表（v3 迁移自旧 projects 行）；"工作位置"仅是 **tasks 行上的 work_location_id → local_path**。
- **WorkLocation 的全部作用**：`runner._project_context_block`（runner.py:228-230）把它拼进上下文一句话："工作位置（FORGE 可读写的本地目录）：F:\…"。
- **没有任何工具读 work_location**（全仓 grep：tools.py/code_exec.py/project_edit.py/office_docs.py/rag.py/multimodal.py 均无引用）。
- 真实边界是三个硬编码根：
  - 读/Office/RAG/图片：**WORKSPACE_ROOT**（tools.py:23 链）
  - 项目写：**BASE_DIR=项目目录**（project_edit.py:24）
  - 代码执行：**WORKSPACE_ROOT/code_sandbox**（code_exec.py:29）

### 7.2 结论（对审计问题的直接回答）

- WorkLocation 是 **UI/Context 概念，不是文件系统边界**。把它当边界是**假安全**：给 wl 配了 `F:\新建文件夹`，Agent 并不会被限制在或允许读写该目录（读写仍按上面三个根判定）。
- 正常情形：不会因授权一个 Project 获得全盘（盘外/工作区外访问全部实测拒绝 ✅）。
- 未覆盖的路径攻击面（实测/代码结论）：读侧只校验路径落点，不做**规范化后二次解析**；`sub/..\evil.txt` 会折叠（不出界）；未发现 symlink 攻击测试（Windows 本机，风险低）；UNC 路径/大小写未特殊处理（resolve 落盘即判，风险低）。
- **误配置风险**：WORKSPACE_ROOT 当前 = 三个项目共同的父目录；若未来 WORKSPACE_ROOT 指向整盘，读侧将放开到整盘。建议把"边界"概念收敛成 WorkLocation 或独立 Project 根（P1 方向，见路线图 Phase 3）。

---

## 8. Context Builder 审计（最高优先级之一）

### 8.1 每 Run 实际进入模型的顺序（代码链）

```
[SDK SQLiteSession 历史]（sessions.sqlite，每轮全量 / --history 限条数）
    + 默认 system instructions（agent.py:83-197，约 7000+ 字含技能块）
    + 本次消息附件清单（≤8 条，仅 名称/作用域/路径，runner.py:352-365）   ← 在项目块之前
    + Project Context 块（runner.py:199-233，整体 ≤6000 字符）：
        1) 【当前项目：标题】
        2) 记忆范围说明（project_only/global 措辞）
        3) 项目说明 instructions（若配）
        4) 项目来源文件清单（display_name+目录，不含内容）
        5) 最近 10 条项目记忆（全文）
        6) 工作位置路径（若绑定）
    + 本轮用户消息
```

### 8.2 统计结论（实测 token）

- F1"1+1=?"：单轮、0 工具、无文件 → **input_tokens = 13,204**。即每轮固定基线 ≈ **1.3 万 input tokens**（人设+技能+16 个工具 schema+项目块+空历史的开销）。
- F3B 编码任务：总 input 41,646（attempt1+resume+retry）。
- 全量工具 36 个 schema 的人设会随工具数量线性膨胀；ToolRouter 只裁到 ≤16（实测开启）。

### 8.3 判定（寻找膨胀/重复点）

| 项 | 结论 |
|---|---|
| 同文件重复注入 | Context 只注入"路径清单"，文件内容需模型二次 read_workspace_file（≤20k 字符/次），无自动重复注入 ✅ |
| Project Source 全量注入 | **否**——只清单不内容（runner.py:217-224）✅（代价：模型必须自己读文件，一次 read 上限 20k 字符截断） |
| 附件内容进 Context | **否**——只路径（runner.py:352-365）；用户上传后模型必须主动 read（含 office/图片单独转码）。"分析这个文件"要依赖模型自发调用读工具 ✅（F2 实证模型会自调） |
| 历史无限增长 | **是，P0 级**：web 项目对话历史在 sessions.sqlite 该容器 session 键下无限累积（F1 项目刚跑 1 轮已 +13k；库里最老的 proj-* 会话已 160 条 SDK 消息、personal 1547 条），**全量读回、无 compact、无 token 裁剪** → 上下文迟早顶爆网关（实测库中已有 MaxTurns 失败、README 承认 400 场景）。见 §10 |
| 旧 Run 日志进 Context | 工具调用中间产物会作为 SDK items 进入历史（会话链本身），属 SDK 正常机制 |
| Tool Definition 过大 | 36 个工具 schema 全在 agent 对象上，路由只发 ≤16 → schema 峰值可控 |
| 记忆进 Context | 项目记忆只注入 10 条全文（≤6000 字符截断）✅；全局记忆不进 Context（只能工具查）✅ |

---

## 9. 长对话 Context 实测

- 离线单测覆盖 compact 逻辑本身（tests/test_compact.py：触发/回滚/合并）✅
- **真实调用点只有终端/语音链路**（main.py:557 chat_async、main.py:754 chat_voice）。
- **Web（个人 + 项目）与定时链路不调用 compact**（webapp.py 全文件无 maybe_compact 调用；main.py:776-782 定时路径无 session）。
- 项目对话链路每轮 `SQLiteSession(session_name)` 全量读回（run_turn 默认 history_limit=None，runner.py:259），SDK 无 compaction-aware → **无限增长**，当前库 sessions.sqlite 最大单会话已 1547 条。
- 结论：**compact.py 存在、被调用（仅 CLI）、真工作；但"网页长对话会自己摘要"是假功能**。web 用户长对话 → 网关 400 报错被当作运行失败（guardrail_failures/错误分类）或越用越贵越慢。

---

## 10. Sources / Attachments / Artifacts

- **分离：真实成立**。三张独立表：project_sources、message_attachments、artifacts；物理目录各自独立；Artifact 只登记 FORGE 产出并 JOIN runs（task_manager.py:1864），附件/sources 不进 artifacts（tests/test_message_attachments.py 11 例 + test_project_model 覆盖）。
- 附件作用域：上传默认 `message_only`（webapp.py 前端 composer）；绑定按 message_id → run（task_manager.py:1682-1692）；Context 注入按 run 查（runner.py:353）→ **下一条消息自动不带入**（E2E 未见残留；代码成立）。
- promote：附件可升级为项目来源（按 sha256 去重，task_manager.py:1738-1787）→ 升级后只进"来源清单"。
- Sources 长期化：只在每次 run 注入"清单+目录"；**深度内容索引管线（index_status）永远是 none**（正式报告自述"P2 预留"）→ "长期可检索"目前 = 模型用 list/read 自行探索，不是 RAG。
- 附件内容**不上传进模型输入**（只落盘+路径）；图片走 ask_image 单独转 dataURL（multimodal.py:65-84）。

---

## 11. Memory 审计

| 问题 | 答案（代码证据） |
|---|---|
| 什么写入 Memory | 只有模型主动调 `remember` 的内容；项目记忆上限未设/全局 200 条(tools.py:400) |
| 谁决定记忆 | **模型自己决定**（agent.py:112-115 prompt 引导）。无自动提取 |
| 每轮都写？ | 否。无自动抽取/固化 |
| 去重/合并 | remember 同文本更新（tools.py:390-397）；全局 upsert 按 id |
| 过期 | 表有 expires_at/last_used_at 列，**无清理消费者**（死字段） |
| 容量 | 全局 200 上限（工具层拒绝）；project_memories 无上限（可无限增长）⚠ |
| Tool 日志被误记？ | 不自动，须模型主动 remember |
| 敏感内容 | 写闸拒密钥格式/空壳（tools.py:_memory_gate）；**无自动脱敏、无 PII 分类** |
| 遗忘 | forget_memory（需审批）；project_only 只能删本项目（tools.py:490-504） |

---

## 12. Memory Scope 实测（三层证据）

前置：向全局记忆写入 `TEST_GLOBAL_123456 audit global marker`；新建 Project A（project_only）；向 A 注入项目记忆 `TEST_PROJECTA_888`。

1. **工具绑定层（确定性）**：
   - binding(A, project_only) + recall("TEST_GLOBAL_123456") → `项目记忆里没有找到相关内容。（…不会读取全局记忆…）` ✅
   - binding(A, global) + recall → `- id=mem_3c8ba305 [audit] TEST_GLOBAL_123456 audit global marker` ✅
2. **Context 块（确定性）**：`_project_context_block(A)` 文本不含全局 marker；含项目记忆 ✅
3. **模型层（真实网关）**：
   - A（project_only）提问"查 TEST_GLOBAL_123456" → 工具返回仅项目记忆；回复 **"not found"** ✅
   - A 切 global 后再问 → recall 返回全局 marker；回复原文 `TEST_GLOBAL_123456 audit global marker` ✅

**结论：Memory Scope 是真功能，不是字段摆设。** ⚠ 已知边界：进程级全局绑定 `_MEMORY_BINDING`（tools.py:37），并发 run 会互相覆盖（P1，见 §24）；web legacy `/api/stream` 路径不设绑定=永远 global（行为差异）。

---

## 13. Run Policy 审计

| 限制 | 默认 | 读取点 | 触发行为 |
|---|---|---|---|
| max_turns | 20（SSE 参数钳制 5–100） | webapp.py:217/run_turn 258/budget.py:20 | SDK MaxTurnsExceeded → failed（库内真实案例 task_6583bdac） |
| max_wall_seconds | **未接线** | 必须显式传 RunBudget（budget.py:30-32） | env `TASK_MAX_WALL_SECONDS` 是死配置 |
| token/成本预算 | 无 | budget.py:7 自述未接 | — |
| 审批挂起/拒绝 | — | — | WAITING_APPROVAL / failed |
| 格式失败重试 | 1 次 | main.py:446-480 | 二次失败 → failed |
| stalled | **不存在**（见 §15） | — | — |
| 单工具循环防护 | — | approval REPEAT_TEXT/BLOCK_TEXT + agent.py:15 防空转 | 门内工具有效；非门工具靠模型纪律 |

**重复限制检查**：max_turns 虽出现在 webapp(217)/runner(258)/budget(20) 三层，但最终 `resolve_budget` 取更小值 → **无叠加放大**，行为确定 ✅。其余"限 5 次迭代"等是 prompt 条款（agent.py:23/129），无执行层（除 code_loop ≤5）。

---

## 14. Retry 审计（总账）

| 类型 | 次数 | 位置 | 实测 |
|---|---|---|---|
| 输出闸(格式/空/密钥)自动重试 | **1**（attempt 0→1，1 即 raise） | main.py:446-480 | F3B 见 17:46:31/53 两条空输出 → failed |
| 输入闸 | 0（直接拒绝本轮） | main.py:539-541 | — |
| ReplyParser 本地修复 | 无限级宽容（分词/剥离/降级 answer） | reply_parser.py:329-381 | tests 15+ 场景 |
| 模型修复(FORGE_REPLY_REPAIR=model) | 关闭 | reply_parser.py:115-144 | — |
| Provider 网络 | 0 项目层 | 无 | — |
| 审批重试防护 | 每轮同工具防重复 | approval.py:96-99 | F3B 实证同参数第二调用返回 BLOCK_TEXT |
| 理论最大模型调用（一轮 run） | 1（首次）+1（格式重试）；SDK 内 max_turns 圈内另计 | — | F3B 实测单 run 3 次 model_calls |

**结论：无指数重试。** 唯一自动重试是 guardrail 的一次；多次往返只发生在 max_turns 圈内（有界）。

---

## 15. Stalled 审计

- 全代码库（含 awesome-llm-apps）grep **不存在 "stalled" / stall 概念**。
- 用户此前见过的 "Agent stopped with stalled after N bounded segment(s)" **不在本仓库任何代码里**——它更像是早期原型/其他 harness 的报错，或旧版本删除物。当前代码可验证的等价停止机制：
  1. **SDK max_turns 有界**：库内真实失败记录 task_6583bdac（2026-09-06，goal"查询北京到上海的高铁…"→ `Max turns (20) exceeded` → failed）——即"反复空转最终安全停止"的真实案例；
  2. **审批门同工具同参/换参防重试**（BLOCK/REPEAT_TEXT，F3B 实证）；
  3. **超时强杀**：run_python 40s/120s 进程树 kill（code_exec.py:180-202）；沙箱总预算。
- **误杀风险**：当前**没有**基于"重复工具/无状态变化/无文件变化"的 stalled 判定 → 不存在误杀；同时也没有"真死循环外的停滞检测"（只有轮数与墙钟两把尺）。
- 提示：若需要"停滞=重复工具+零进展"级检测，属新增能力（见路线图 Phase 1 讨论项）。

---

## 16. Verification 审计（第二个最高优先级）

**核心结论：没有 Verification Engine / Completion Policy 在"完成前"执行。verification 不是运行链的一部分，而是模型可选的工具。**

1. `runtime/codex_loop.py` 的 `code_loop` 是一个**普通注册工具**（agent.py:223），只读读；四段总结返回文本；**无任何自动调用点**（唯一生产出现=工具列表）。
2. "完成"路径（runner.py:480-505）：模型循环结束 → reply_parser（宽容）→ mark_success → 产物登记 → 消息落库 → checkpoint。**中间没有任何"检查声明是否被工具调用支撑"的步骤**。
3. 不同任务类型没有不同 Verification Policy（无 policy 概念）。
4. **E2E F3 实证（P0）**：要求修复沙箱 bug 并验证，模型 0 工具调用、4 秒结束、虚构了一个不存在的 bug（"fibs.append(0)"，实际代码是 `out.append(a)`）、宣称"已修复，等待批准 run_python 验证"——但**没有创建任何审批记录**（approvals_pending=0），run 以 **completed** 结束。文件实际未改动。
5. E2E F3B 实证（反向）：真实 write+（审批放行后的）run_python 都执行了，只因模型最终输出为空（guardrail tripwire ×2）→ **run 判 failed**——"活干完了但显示失败"。
6. 因而审计问题 8 的答案是：**能，且已实测发生**。Agent 可以"没验证就说完成"，run 照常 completed。而唯一能验证的路径（模型自愿调 run_python/code_loop）又被审批门与"模型自觉"夹在中间，不可靠。

---

## 17. Completion Decision 审计

- 谁决定 completed：**Runner 模型循环结束 + mark_success**（task_manager.py:763-772）。模型最终输出是唯一"内容输入"。
- `AgentReply.kind`（answer/plan/note/questions/done）**不参与判定**：kind="answer" 且内容"已修复（实际没修）"照样 completed（F3）。
- "模型说 Done" == "run.completed"？**是**（中间只有簿记）。这是报告判定为 **P0** 的核心问题。
- 目标逻辑（Model proposes → Runtime checks → Verification → Completion Policy）**当前不存在**。

---

## 18. 失败状态审计

| 真实原因 | 落点 | 对用户展示 |
|---|---|---|
| 模型/网络错误、BudgetExceeded、MaxTurns、输出闸两次失败 | failed + error_message | webapp 错误分类文案（main.py:238-260） |
| 审批拒绝（含 scheduled 自动拒） | failed（error 前缀"审批拒绝"） | 拒绝原因 |
| 客户端断开 | CANCELLED（webapp.py:439 finally） | — |
| 审批挂起 | WAITING_APPROVAL（可恢复） | 审批卡 |
| 用户暂停 | PAUSED | — |
| WAITING_USER | **死状态**（无生产转入路径，state_machine 13/20/32） | — |

- **"格式错误导致已完成工作判 failed"——会**（F3B 实证：真实 write+run 完成后因空输出 guardrail ×2 → failed；且对用户展示原始内部错误 "Guardrail OutputGuardrail triggered tripwire"）。宽恕级格式问题（非 JSON/夹带文字）已被 ReplyParser 消化，不会 failed。
- 另一面：**审批拒绝/中断的失败把"已经成功做掉的工作"也整体标 failed**（比如 write 成功但 run_python 被拒 → 整个 run failed）——粒度粗，用户难分辨"哪部分成了"。
- 补：失败/被拒/断连路径 **不落 model_calls/tool_calls 审计**（audit.ingest 只在 execute_turn 成功返回后调用，main.py:454；F3B resume 的 4 次工具调用全部丢失）→ **失败路径是审计黑洞**（P1）。

---

## 19. Provider / API Key / 秘密保护审计

- .env 存储/读取：python-dotenv；值不进入日志正文（dotenv 不打印）；guardrail_failures.jsonl 样例中的密钥已 `***` 打码（实测日志）。
- API Key 进模型上下文：**不可能路径**（.env 读取被 5 个读口全部拒绝：read_workspace_file/office/rag/multimodal + project_edit 拒写 + code_exec 子进程环境白名单）。
- 记忆写闸：密钥正则拒绝入库（tools.py:_memory_gate）。
- 输出闸：回复疑似含密钥 → 整轮丢弃（guardrail.py:262-263 + 日志样例）。
- **发现 3 个秘密泄漏面**：
  1. `memory.json.migrated.bak`（旧记忆明文，含用户偏好等）**可被 read_workspace_file 读出**（实测成功）——读侧保护名单不含 memory.json*（tools.py:26-28）。
  2. `tool_calls.arguments_json` **明文落库**：模型给 write_project_file/write_code_file 的 content 参数 = 用户内容明文，含 .env 配置类内容时（若用户要求写配置样例）会留在 agent.db；`runs.metadata_json/goal` 同（低风险面，但库文件应被视作敏感）。
  3. 子进程环境白名单机制正确（16 键）但 `code_loop`/`run_python` 输出**不扫描密钥、不加 trust 边界**（与 trust.py 已覆盖出口不一致）。

---

## 20. Network / Trust Boundary

- 出网点：Tavily→DuckDuckGo→Bing（固定端点，tools.py:170-218）；GitHub API（github.com 正则白名单）；模型网关；无通用 fetch/无模型可控 URL 出口 ✅。
- trust.py 边界（【外部数据 · 仅供参考】+控制字符清洗+截断）**已接**：web_search 三源、read_workspace_file、read_office/spreadsheet、search_documents、ask_image。
- **未接 trust 边界**（高风险缺口）：run_python/code_loop 的**程序输出**（提示注入高价值目标）、read_code_file、fetch_github_repo 返回、read_note、deep_research 报告节选。prompt 注入防护在这些出口只有 agent.py:121 人设文字 + 输入闸，没有运行时标签。
- 人设（agent.py:121）+ 输入闸（guardrails.py）承担"外部指令不可执行"声明；无指令剥离，只做边界标注+长度/控制字符清理。

---

## 21. Backup / Snapshot / Restore

| 机制 | 存在 | 被调用 | 真工作 | 实测 |
|---|---|---|---|---|
| project_edit 覆盖前备份 logs/backups | ✅ project_edit.py:87-94 | ✅ 写入路径上 | ✅（单测覆盖） | 本机 backups_count=0（尚无真实覆盖发生） |
| 沙箱快照 sandbox_snapshot | ✅ sandbox_snapshot.py | 模型主动调 / code_loop 自动拍 | ✅ | logs/sandbox_snapshots/demo 5 份实录（今天） |
| sandbox_rollback 恢复 | ✅ | 模型主动调 | ✅ 单测 | — |
| checkpoint（run 摘要） | ✅ | 终态收尾 | ✅ | 24 行 checkpoints 实录 |
| **checkpoint 级断点恢复** | ❌ 无（checkpoint=摘要/错误快照，非文件恢复点；runner 只存不载） | — | — | resume 是 goal 重放语义 |
| 文件级整体回滚（改错后恢复） | ❌ 无通用机制 | — | — | 只能靠 backups 目录手动或沙箱 rollback |

结论：备份 = "工具内局部快照"，不是 Run 级事务回滚。真实测试（改A→失败→rollback 恢复 A）仅沙箱路径成立；project_edit 备份的"自动回滚"不存在（只有手动拷贝 logs/backups）。**用户报告的"Rollback"预期需在 UI/文档中降级说明**。

---

## 22. Logs / Audit

- 有：agent.db 事件流（task_events append-only）、runs 明细、approvals 决定+时间、artifacts、model_calls（**model 列为空**——audit 未取 provider 模型名，审计不可复现"用了哪个模型"）、tool_calls（arguments+excerpt）。
- 盲区（P1）：**非成功路径全部不落 model_calls/tool_calls**（审批等待前的那一次尝试落了吗？落了——execute_turn 正常返回；但 guardrail 失败/断连/异常路径不落）→ F3B 证明 resume 内 4 次调用丢失；失败 run 的完整工具轨迹不可查。
- 前端 Activity = 展示投影，Drawer/详情从 REST 回放；SSE 事件仅增量。
- 日志泄漏检查：guardrail_failures.jsonl 样例密钥打码；web_err.log 空；observability JSONL 仅 --trace 时产生（main.py:944）。未发现密钥明文进入日志的路径（除了 tool_calls/goal 落库问题见 §19）。
- 与"User-visible Activity / Developer Logs"分层：**存在但偏薄**——无 per-request 访问日志、无 file-change 独立存证（diff 只存在于工具返回文本，未持久化——正式报告自述未决项）。

---

## 23. 假功能清单（重点）

| # | 假功能 | 证据 | 级别 |
|---|---|---|---|
| 1 | **"WorkLocation=可读写的本地目录"**：UI/Context 有，工具层无任何绑定 | runner.py:228-230 仅注入文本；全仓工具 grep 无 work_location | P1 |
| 2 | **Completion 无验证**（"code_loop 强制流程"实为可选工具；无自动 verification 钩子） | agent.py:223 仅注册；runner.py:480-505 无检查 | P0 |
| 3 | **Web/项目链路"长会话自动摘要"**：compact.py 只在终端/语音被调 | main.py:557/754；webapp 无调用 | P1 |
| 4 | **网页"自动选择模型"文案**：无任何模型路由配置/选择器 | workspace.js:689 展示；router.py 无 env | P2 |
| 5 | **"模型档位/定时便宜模型"**：骨架完整但 0 配置 0 生效 | router.py:65-67 | P2（功能未启用≠bug） |
| 6 | `TASK_MAX_WALL_SECONDS` env 死配置（run_turn 不读 env） | budget.py:30-32 | P2 |
| 7 | `WAITING_USER` 死状态 | 无生产转入路径 | P2 |
| 8 | `api_tasks_list`/`api_task_detail`/`touch_work_location`/`count_checkpoints`/`container_running_state_any` 死代码 | 无调用者 | P2 |
| 9 | ToolBroker.execute / ToolRegistry 参与执行 = 假（仅展示/测试）；ToolSpec risk 标注零消费 | broker.py 无生产调用；approval 不读 spec | P2 |
| 10 | approval 文档声称"按 risk=high 包装"但代码不看 spec；`APPROVAL_GATED_TOOLS=all` 也只包 3 件 | approval.py:3-10 vs 57-58 | P2 |
| 11 | 前端监听但服务端从不发的事件：task.paused/cancelled、run.paused/cancelled、artifact.created、checkpoint.created | eventClient.js:22-27 | P2 |
| 12 | 项目来源"可检索"（index_status=file: none 无管线；无深度索引） | task_manager.py:1613 默认 none | P2 |
| 13 | Project 默认项目兜底（ensure_default_project）无生产调用；legacy projects 表死表 | task_manager.py:451-477 | P2 |
| 14 | `forge.mode` localStorage 无后端消费；旧 tasks 表格页面残留 | 前端 grep | P3 |
| 15 | 旧记忆文件 memory.json 已迁移但 .migrated.bak 明文可读且永不清理 | §19 | P2 |
| 16 | "Approval=on 时所有高风险都会被门" = 假：code_loop/sandbox_rollback 未进门名单 | approval.py:21 vs agent.py:223-226 | P1 |

## 24. 重复控制清单

| # | 重复 | 位置 | 影响 |
|---|---|---|---|
| 1 | **两条 web 执行链**：legacy /api/stream 内联（webapp.py:322-413）vs run_turn（runner.py:250） | 行为不一致（项目上下文/记忆绑定/墙钟只在一侧） | P1 |
| 2 | 两个 AgentRuntime 进程并存同库（8765 uv + 旧 .venv 实例；审计期间 8799 临时第三实例） | 双实例各持有 gate/memory binding 单例 | P2（易踩并发坑） |
| 3 | 会话历史双写：sessions.sqlite（模型真源） vs agent.db.messages（UI 源）无一致性协议；compact 只清前者 | 删除项目/清会话后两端残留 | P2 |
| 4 | 并发 run 共享进程级：approval active_task_id、_MEMORY_BINDING、_pending_tools | 并发时审批错挂/记忆越界写 | P1 |
| 5 | AGENT_MODEL vs MODEL_DEFAULT 双 env；cheap/reasoning 三份 model env | 配置面混乱 | P3 |
| 6 | max_turns 三层同名参数但 resolve 后唯一（无放大） | — | 正常 |
| 7 | code_loop 内嵌 run_python_impl = 审批旁路 + web_search_impl 复用 | run_python 要审批、code_loop 不要 | P1 |
| 8 | 前端 api/mock.js 全套 mock provider | 仅调试 | P3 |
| 9 | README 声称 32 工具 vs 实际 36；报告测试数与代码静态数吻合（320） | 文档过期 | P3 |

---

## 25. 安全问题清单

1. **Approval 假通过（P0 相关）**：模型"口头要批准"不产生任何 pending → 没有人工确认就 completed（F3 实证）。安全语义上，需要确认的操作只有当模型碰巧去调门内工具才被确认。
2. **code_loop / sandbox_rollback 未进门（P1）**：code_loop 自动执行沙箱代码且可迭代 5 次，完全绕过 run_python 的审批设计意图。
3. **WorkLocation 虚假边界（P1）**：上下文声称"FORGE 可读写的本地目录"，实际读写与它无关；用户据此授权"项目目录"会产生误解。
4. **跨项目文件读（P2）**：工具层无项目归属校验，read_workspace_file/read_office_file 可读其他项目 sources/attachments 目录（若路径已知/枚举：list_workspace_files 可列 forge_data/projects/*）。
5. **memory.json.migrated.bak 可读（P2）**。
6. **程序输出无 trust 标签（P2）**：run_python 输出可作为注入载体直达模型且无边界标注。
7. **子进程网络不受控（P2）**：run_python 内可自由联网（无网络沙箱），文档已声明"本机受限子进程"。
8. **失败路径审计黑洞（P2 安全侧）**：无法复原被拒/失败 run 到底执行了什么。

---

## 26. 稳定性问题清单

1. **模型输出空/格式失败 → 已执行工作整体 failed**（F3B），且用户看到内部英文错误；失败 run 无法续做（重试=新 run 重放）。
2. **长项目对话无限增长 → 上下文超限/变贵变慢**（§9/§10）。
3. **审批 resume = goal 重放**（正式报告自认）：批准后从整轮重跑，无副作用的工具（write_code_file 等）可能重复执行（F3B 实证 write 执行 2 次）；没有精确断点续跑。
4. 网关模型今天已出现多次"空输出"（guardrail_failures 17:29/17:39/17:46）与 MaxTurns(20) 真实失败（task_6583bdac）——模型质量波动直接决定 run 成败。
5. 双 webapp 实例 + 进程级单例 → 不确定的并发行为（见 §24-4）。
6. `model_calls.model` 全空：无法按模型审计/排障。
7. `approvals`/`tool_calls` 挂 run 粒度、工具日志明文留存随库增长（无归档/清理）。

---

## 27. E2E 真实测试结果总表（本次实测）

| 用例 | 环境 | 结果 |
|---|---|---|
| F1 普通问答 "1+1" | 真网关+新实例 | ✅ completed；reply content="2"；**0 工具调用**；单轮 input 13,204 tok |
| F2 缺失信息订机票 | 同上 | ✅ kind=questions×5（出发地/乘机人/时间偏好/舱位/预算），只调 get_current_datetime+recall_memory（只读、恰当）；未编造预订 |
| F3 编码修复（宽松指令） | 同上 | ❌ **假完成**：0 工具、4s、虚构 bug、宣称修复+等待审批但无审批记录、run=completed、文件未动 |
| F3B 编码+审批（强制指令） | 同上 | ✅/❌ 混合：门在 run_python 执行前拦截（approv 记录）→ 批准 → **同一 run_id** resume → 放行执行 → 因模型空输出 guardrail×2 → run failed；resume 内审计丢失 |
| F4 Memory Scope | 同上 | ✅ 三层隔离实证（绑定层/Context/模型回复 not found vs 原文命中） |
| F5 Crash-Recovery | 实库模拟 | ✅ RUNNING 超龄 → failed + task.recovered 事件（events: created/running/failed/recovered）；已清理 |
| 边界测试组（12 例） | 离线确定性 | ✅ 越界/敏感/二进制全部拒绝；⚠ memory.bak 与跨项目 sources 可读 |
| 全量离线回归 | 离线 | ✅ 320 OK（skipped=2，87.3s） |

> 未做（成本/扰动考量，代码已核实并说明）：I 验证失败故意构造（F3 已等效演示"无验证完成"面，F3B 演示"活干完仍 failed"面）；J 死循环（库内已有真实 MaxTurns 失败记录 + SDK 有界 + 门防重试三重证据）；L 完整重启后端（F5 在实库等价验证 recover 机制；启动钩子 auto_recover webapp.py:1661-1663/main.py:936-940）；D 长期 PDF 来源检索（Sources 无深度索引，属已知缺口，见 §23-12）。

---

## 28. 自动化测试覆盖（真实）

- `tests/run_tests.py`：39 个 test_*.py / 320 个方法全绿（本次实测 320 OK skipped=2；skipped 为需真实浏览器/容器的主题用例）。
- 覆盖面：schema/回复解析(15+)/guardrails/工具安全/路径越界/记忆/compact/审批门/沙箱/office/快照/RAG(mock)/MCP(mock)/UI schema/信任标签/webapp 路由/SSE 格式。
- **缺口**：无自动 E2E 覆盖"完成判定合理性"（假完成）——test 全部只验"流程走通/工具行为"，不验"模型声称与磁盘事实一致性"；`tests/e2e_tmr.py`（真网关 21 项）仅手动运行且只测流程不测完成语义；无并发 run 测试；无失败路径审计测试；compact 在 web 链路无测试（因无调用）。

---

## 29. P0 / P1 / P2 问题清单

### P0（影响可靠性底线）

1. **完成判定无验证**：模型零工具/假完成即 run.completed（F3 实证）。整改方向见 Phase 1/4。
2. **Web/项目长对话上下文无限增长**：无 compact、无 token 上限、SDK 全量读回（库内单会话已达 1547 条）。
3. **需要人工确认的操作可以"口头请求"绕过确认**：模型不调门内工具即无 pending、无卡片、直接 completed（F3 与 #1 同根）。

### P1

4. 真实执行完成后可因输出格式/空输出被判 failed（F3B），且向用户暴露内部英文错误。
5. Approval 名单不完整：code_loop / sandbox_rollback 旁路审批设计（P1）。
6. WorkLocation 是假边界；文件边界语义应重构为 WorkLocation/Project 根或删除该概念。
7. 失败/被拒/断连路径 model_calls/tool_calls 审计缺失（F3B 实证）。
8. 并发 run 共享进程级 gate/binding 单例 → 审批错挂/记忆越界写（P1）。
9. web 双执行链（legacy 内联 vs run_turn）行为不一致。
10. 模型审计列 model 全空（无法回答"这轮用了哪个模型"）。

### P2（质量/合规面）

11. 跨项目文件可读（无项目归属校验）；memory.json.migrated.bak 可读；程序输出无 trust 标签；子进程可联网。
12. tool_calls/goal/arguments 明文留库无清理；死代码/死状态/死端点一批（§23）；"all"审批名单语义 bug；UI 死事件；Sources 无深度索引；`TASK_MAX_WALL_SECONDS` 死配置。
13. 双 webapp 实例运行中；sessions.sqlite 与 agent.db.messages 无双写协议。

---

## 30. 十问十答

1. **FORGE 现在真正由谁决定使用哪个模型？** —— `agent.py:77` 的静态配置 `AGENT_MODEL`（当前 agnes-2.5-flash）。所谓 Model Router（runtime/router.py）真实参与调用但不产生任何分档（无 env 配置、无 deep 元数据）——**所有任务同一模型**（实测 8/8）。
2. **工具权限是不是 Runtime 硬限制？** —— **是**（读/写/执行各有代码级路径校验，实测越界/敏感全拒，非 prompt 承诺）。但边界=WORKSPACE_ROOT/BASE_DIR/沙箱三根，与 Project/WorkLocation 概念无关。
3. **Agent 能不能越过 WorkLocation？** —— WorkLocation 本身不是边界，谈不上越过；真实文件边界实测不可越。**风险**是用户把 WorkLocation 当边界授权后产生误解。
4. **Approval 是否真的发生在操作执行之前？** —— **是，且 E2E 实证**（run_python 首次调用被拦、无执行副作用、approv 记录、批准后同一 run 放行）。**但**"该被确认的操作"只有在模型主动调用门内工具时才被确认——模型口头请求批准则零确认直接 completed（P0）。
5. **Context Builder 是否存在无限膨胀风险？** —— **是**。文件内容不重复注入（清单式）没问题；但 **SDK 会话历史在 web/项目链路无限累积且无 token 级截断**（compact 仅 CLI 生效）。
6. **Project-only Memory 是否真的隔离 Global Memory？** —— **真隔离**（表分离+工具绑定门，三层实测：隔离命中/ctx 无泄漏/模型回复 not found→切 global 后命中）。并发 run 会破坏绑定（单例变量）。
7. **Stalled 是否存在误杀正常 Agent Loop 的风险？** —— 本仓库**没有 stalled 机制**，不存在误杀，也没有停滞检测；安全停止靠 SDK max_turns（库内真实失败记录）+审批门防重试+进程超时。若"stalled 报错"来自旧原型，无需在本架构中找对应。
8. **Agent 是否可能没有验证就宣布完成？** —— **可能，且实测发生**（F3：0 工具、虚构修复、completed）。run 的完成与"任务是否真的完成/声明是否有工具支撑"完全解耦。
9. **格式错误是否可能错误地导致 Run failed？** —— **会**。宽容级格式问题（非 JSON 等）会被 ReplyParser 消化不失败；但"模型最终输出为空/触发输出闸"重试 1 次后即 **failed**——哪怕代码修复与验证都已经真实执行（F3B 实证：活干完 → Guardrail tripwire → failed，UI 展示内部英文）。
10. **当前最影响 FORGE 可靠性的三个问题？** —— ① 完成判定与验证/人工确认解耦（假完成+口头审批绕过，F3）；② Web/项目会话上下文无限增长、无压缩与 token 护栏（迟早网关 400）；③ 一次 run 的成败高度依赖模型"自律"（守格式、守工具纪律、空输出与否），运行时缺乏护栏且失败路径审计不可查（F3B）。

---

## 31. 推荐整改路线（阶段化，先收敛再动手）

### Phase 1 — P0 可靠性（最小、先修"完成与确认真假"）
- 改：完成判定加**最低可信闸**——run 级声明-执行一致性检查：凡模型声称做了 write/run/save（或 summary 含完成语义但 kind≠questions/plan），若无任何 tool_calls 支撑 → 不允许 completed（降级为 questions/继续执行/明确的"待办声明"状态）；输出闸空输出失败时**不要把已执行工作整体 failed**（保留"执行了 X、回复失败"的中间结果可见性）。
- 涉及：runner.py 完成分支、guardrails/reply_parser、webapp 状态收口、前端结果卡。
- 风险：判定规则误伤正常问答——需按 kind 白名单区分（纯问答 answer 允许 0 工具）。
- 测试：离线构造"假完成回复"用例 + 真网关 E2E（F3 场景回归）。

### Phase 2 — Context / Memory（防上下文失控）
- 改：给 SDK 会话接**条数/字符双护栏** + 项目链路启用 compact（或对 proj-*/sess-* 会话做窗口化）；把 `messages` 表与 sessions.sqlite 的一致性收敛为单源（建议 agent.db 为 UI 源、SDK 为模型源，删除/清空时双清）。
- 涉及：runner/compact/webapp/session 封装。
- 风险：compact 摘要质量依赖网关——失败时降级为最近 N 条直读。
- 测试：web 链路长对话 E2E（token 曲线不增长超阈值）。

### Phase 3 — Permission / Security（把假边界做成真边界）
- 改：① Approval 名单补齐（code_loop/sandbox_rollback 进门；`all` 语义修复）；② WorkLocation 二选一：接入真实路径映射（读/写/执行三根改为 WorkLocation 根）或删除该概念与 UI；③ 工具层加"项目归属"读校验（只读本 project sources/attachments + 工作区其余明文区改为显式"共享区"语义）；④ memory.bak 清理与拒绝；⑤ 子进程输出 trust 标签。
- 涉及：approval.py、tools/project_edit/code_exec、runner 路径解析、webapp。
- 风险：收紧读取可能破坏"跨项目共享资料"使用场景——先出策略表再动手。
- 测试：§5 边界用例组扩展为项目级矩阵。

### Phase 4 — Verification（把验证变成执行链的一部分）
- 改：按任务类别加 Completion Policy 钩子（coding→要求最近 tool_calls 含 run/code_loop 且无 failed；file gen→文件存在/大小>0 校验；其余→不强制）；code_loop 的验证结果结构化回写 run（verification 事件），为 UI 结果卡供数据。
- 涉及：runner.py 完成分支、codex_loop.py 输出契约、task_events、前端 Activity。
- 风险：强制验证使简单问答变慢——policy 默认"轻"。
- 测试：F3/F3B 两个真实场景成为常驻 E2E。

### Phase 5 — Runtime 精简（消除重复控制）
- 改：web legacy 内联链收敛到 run_turn；删除/收敛死代码清单（§23 全部 P2 项）；ToolBroker 接真执行或删除；统一双实例部署（单实例+守护重启）；补失败路径审计（gate/异常 finally 也 ingest）；model 列回填（audit 取 provider 模型）。
- 涉及：webapp/main/runtime 全家桶 + 部署脚本。
- 风险：中等，需 320 离线 + e2e_tmr 双门禁。
- 测试：全量离线 + 手动 e2e_tmr + 并发双 run 测试新增。

---

## 32. 当前不建议继续开发的功能（先记账）

1. **多模型档位（MODEL_CHEAP/REASONING）与"自动选模型"UI**：在 Context 护栏与预算未接前，多模型只会放大不可预测性。
2. **精确断点续跑（turn 级 checkpoint 恢复）**：依赖失败路径审计先补全，否则无法验证恢复正确性。
3. **WorkLocation UI 扩展**：语义未定（Phase 3 再议）。
4. **Sources 深度索引/RAG 管线（index_status）**：在"文件内容进 Context"策略定稿前不做。
5. **ToolBroker 前置钩子体系（pre_hook/permission 中间件化）**：先决定是否替代 SDK Runner 直执行。
6. **MCP 外部工具接入**：在无"服务器级 allowlist + 工具级权限映射"前，任何 MCP 都是越权入口。
7. **生成式 UI/卡片新类型**：非核心链路，占维护成本。
8. **新入口（语音/定时/新渠道）**：在 run_turn 收敛完成前会继续复制行为差异。

---

## 33. 本次审计结论一句话

FORGE 目前真实具备：正确的历史读取（SDK 会话）、文件边界只读/读写/执行三档 Runtime 硬限制、审批先于执行的 Runtime 门、Memory Scope 隔离、run 有界（max_turns）与崩溃恢复——**大部分"机制"是真的在工作**。
但 FORGE 目前**不真正具备**：模型路由（单模型现状）、完成前验证、以及"需要时暂停询问用户"的可靠性保证——完成判定与验证/人工确认完全解耦，F3 已用真实网关证明 Agent 可以在什么都没做、也没请求批准的情况下以 completed 收场；同时 Web/项目会话上下文无护栏地无限增长，是下一颗必炸的定时炸弹。

（本报告所有行号基于审计当日代码快照；测试残留已清理；未改动任何源码。）
