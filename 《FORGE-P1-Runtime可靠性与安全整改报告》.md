# 《FORGE-P1 - Runtime 可靠性与安全整改报告》

> 项目：`F:\Byong-hermes\Byong-hermes\my_creative_agent`（FORGE）
> 日期：2026-09-06 · 事实基线：《FORGE-Agent-Runtime完整审计报告.md》+ P0 两阶段整改后的当前真实代码
> 整改顺序（按任务书固定）：P1-A Approval 旁路 → P1-B WorkLocation 假边界 → P1-C 并发 Run 单例 → P1-D 失败路径 Audit → P1-E 双 Web 执行链。
> 每阶段均以“修改 → 单测 → 全量回归（每轮 Ran 371/371 OK）→ 真网关 E2E”推进，全程未发现 P0 回归；未做审计报告之外的“顺手清理”。
> DB migration：无（本阶段全部为代码/行为级整改，无 schema 变更）。

---

# Phase P1-A — Approval 旁路修复

## A1. 原旁路原因（代码核实）

- `runtime/codex_loop.py:121` 的 `code_loop_impl` **直接调用 `code_exec.run_python_impl`**——是 Python 函数直调，不经过 Agent 工具执行层，因此不受工具包装层审批门约束；
- `code_loop`、`sandbox_rollback` 之前都不在 `GATED_DEFAULT`，模型可“绕过 run_python 审批”通过 code_loop 自动执行任意沙箱代码；rollback 无审批而 run_python 要审批，策略不一致；
- `APPROVAL_GATED_TOOLS=all/*` 旧实现返回“默认 3 件套”，名不符实（语义 bug）。

## A2. code_loop 修复方式（采用任务书方案 A + 依据）

依据当前 SDK 架构（工具由 Runner 按 agent.tools 直接执行；工具实现内部是普通函数，无法在“impl 内部再挂一次 SDK 门”而不重写执行器），选择**方案 A 并在 code_loop 调用入口即门**：

- 能力风险分类写入 `runtime/approval.py`：`EXECUTION_TOOLS={run_python, code_loop}`、`DESTRUCTIVE_TOOLS={sandbox_rollback, forget_memory, schedule_remove}`；
- `GATED_DEFAULT = EXECUTION_TOOLS | DESTRUCTIVE_TOOLS`（默认即含 code_loop / sandbox_rollback）；
- 门包装在**工具调用瞬间、任何副作用发生前**检查；批准前 code_loop 一行代码都不会执行，其内部所有 `run_python_impl` 子调用自然全部发生在批准之后 → “执行动作前触发 Approval”成立；
- 内部直调本身保留（避免重写执行器），但已被“入口门”覆盖；`run_python_impl` 无其他外部调用者（已核实）。

## A3. sandbox_rollback 策略

- 默认纳入审批（`DESTRUCTIVE_TOOLS`）：回滚不可逆、改变工作状态，与 run_python 一致需要确认——写清判断依据：**“执行要批准、恢复/回滚也必须批准”的策略一致性**，不因其仅影响沙箱而自动放行；
- 渠道策略沿用：`scheduled/daemon` 自动拒绝（AUTO_DENY_CHANNELS）；批准记录按 (run, tool, args_key) 放行，拒绝记录同样生效不重复询问。

## A4. all/* 修复

- `gated_names`：`all/*` → 返回 **ALL_SEMANTICS** = 执行类 ∪ 破坏/恢复类 ∪ 真实文件编辑类（write_project_file/edit_project_file）；不再是“默认 3 件套”；
- 语义与测试：SAFE 只读工具（calculate/read_workspace_file）在 all 下仍放行；执行/恢复/文件编辑被拦（单测覆盖）。

## A5. Approval E2E（真网关）

- 全链实证（run `task_828401ec`，含两次审批）：code_loop 首次调用 → **pending（approv_a9d8d21e）→ run 转 waiting_approval**（工具未产生任何副作用）→ approve → **同一 run_id resume** → code_loop 真正执行并验证通过（退出码 0，stdout 正常）→ 模型换参数（`{}`）再调 → **新 pending（approv_6bdfb62b）→ approve → 同一 run resume → completed**；
- “同参数自动放行/换参数重新审批/拒绝不执行”由 GateTests + GatedToolWrapperTests（run_python/code_loop/sandbox_rollback 三层真实工具链）覆盖；
- Completion Gate 未受影响（该 run 以 completion.check.* 流程 completed）。

**Phase A 完成门：全部满足。**

---

# Phase P1-B — WorkLocation 真实边界

## B6. 原假边界原因（核实）

`runner._project_context_block` 只把 `工作位置：<path>` 当一行文本注入上下文；所有文件工具仍以 `WORKSPACE_ROOT`（读）/ `BASE_DIR`（项目写）/ `code_sandbox`（执行）为硬根，**没有任何工具读取 work_location** → “FORGE 可读写的工作位置”在 UI/Prompt 层面不成立。

## B7. 新真实 FileScope（Run-scoped）

- 新模块 `runtime/runctx.py`：`RunContext`（run_id/container_id/session_id/channel/memory_scope/profile/requested_model/file_scope），用 **contextvars** 绑定到当前 Task（并发 run 各自持有副本）；
- 新模块 `runtime/filescope.py`：`build_file_scope()` 由 Runtime 注入构造每 Run 授权根；`authorize_tool()` 在**工具包装层、执行前**判定并改写参数；
- 判定以“解析后绝对路径”做容器比较（os.path.realpath + normcase）：覆盖 `../`、绝对路径、盘符、UNC、大小写、junction/symlink 解析、`sub/..` 回绕；
- project_id/work_location 一律来自 Runtime（RunContext），**不信任模型自报**。

范围模型（三类语境）：

| 语境 | 读 | 写 |
|---|---|---|
| 显式 Project（session `proj-*`）且绑定 WorkLocation | WorkLocation + 本 Project FORGE_DATA + 共享产物目录(notes/exports/materials/summaries/logs) + code_sandbox | WorkLocation（`write_project_file/edit_project_file` 运行期写入根 = WorkLocation） |
| 显式 Project 未绑定 WorkLocation | 仅本 Project FORGE_DATA + 共享产物目录 + code_sandbox；**默认只读**，不能把安装目录/工作区当用户目录 | 拒绝（提示先绑定 WorkLocation） |
| Legacy 个人会话（personal/sess-*） | 保持既有 WORKSPACE_ROOT 工具级边界 | 保持 BASE_DIR（项目编辑既有保护） |

FORGE 生成产物继续走 notes/exports（共享产物目录）+ Artifact 登记，不塞进用户代码仓库（§15 语义）。

## B8-B9. 读/写边界实现点

- 读入口工具清单：read_workspace_file / list_workspace_files / read_office_file / read_spreadsheet / ask_image（参数映射表 `READ_TOOL_ARGS`），包装层解析 → 授权 → 绝对化后交工具；工具层 `_resolve_under_root` 增加“含 wl 的运行期根”支持（`_active_read_root_for`），legacy 语义不变；
- 写入口：write_project_file/edit_project_file；`project_edit._active_write_root()` 运行期根 = 绑定的 WorkLocation（严格 Project），否则 BASE_DIR；受保护名单（.env、suffix、密钥内容）在所有根上继续生效；
- code_sandbox 继续作为隔离执行目录（Python 子进程环境白名单不变）；沙箱读 WorkLocation 文件需显式路径授权（本阶段未引入挂载，维持“子进程不因 wl 获得整机权限”的现状并记录）。

## B10. Project Data 隔离

- 允许：本 Project 的 `forge_data/projects/<cid>/`（sources/attachments/artifacts）；
- 默认禁止：其他 Project 的同一目录树（即使知道绝对路径也拒绝——包装层判定发生在工具执行前）。

## B11. 无 WorkLocation 行为

- 聊天/Sources/Attachments/Artifacts/报告/Office/项目 Memory 全部保留；
- 本地文件写操作被拒并返回明确提示（“请在项目设置中绑定工作位置”），读操作仅限本 Project 数据与共享产物目录。

## B12. 跨 Project 测试（tests/test_p1_reliability.py::FileScopeTests）

A/B 两 Project 各绑独立 WorkLocation：
- A 读/写 A → allow；A 写相对路径 → 映射到 A 的 wl 根；A 读 B 的 wl → deny；
- A 读 B 的 sources → deny；A 读自己 sources → allow；系统目录（C:\Windows、工作区外）→ deny；
- 无 wl 的 Project：read_only=True，写拒绝；Legacy personal 语境 strict=False 行为不变。

## B13. Path 安全回归

- 矩阵单测：`../`、盘符绝对、UNC、大小写、`sub/..` 回绕（wl 内放行/wl 外拒绝）；
- 工具层回归锚点：`.env` 仍被工具层拒绝（dotenv 实测）、越界/敏感后缀/二进制规则未动；
- 真实链证据：P1-E2E 中严格 Project 下 `list_workspace_files(auditp1)` 被包装层以“运行时文件边界”文案拒绝并记入 ledger（见 P1-D 取证）。

**Phase B 完成门：绑定 D:\ProjectA 后，FORGE 文件工具实际被限制在本 Project 授权范围（离线矩阵 + 真实链双重证据）→ YES。**

---

# Phase P1-C — 并发 Run 单例消除

## C14. 原全局 mutable state

审计确认并已核实：ApprovalGate.active_task_id/active_channel/pending 列表、tools._MEMORY_BINDING、runner 单份 _run_ledger，全部是进程级/实例级“最后一个 Run 赢”的状态。

## C15. 新 RunContext

`runtime/runctx.py`（contextvars ContextVar）：run_turn 开始即 `bind(RunContext(...))`；SDK 在同 Task 内同步/异步执行工具 → 包装层通过 `runctx.current()` 拿到**本 Run** 的 run_id/channel/memory_scope/file_scope/requested_model/profile。并发 run_turn 在不同 Task → contextvar 天然隔离。

## C16. Memory binding

- `tools._active_project()`：优先读 RunContext（container_id + memory_scope）；无 RunContext 时回退 `_MEMORY_BINDING`（兼容旧测试/直接调用）；
- 并发隔离测试：project_only(A) 与 global(B) 两个 Task 各绑各自 ctx，交错 recall ×10 轮——A 只见 A 标记、B 只见全局标记，零串扰。

## C17. Approval context

- `ApprovalGate` 改为 **run-keyed 状态表** `_states[run_id]`（channel/pending/denied/pending_tools 全部按 run 隔离）；`begin/end/clear_run` 按 run 生命周期管理；
- 包装层调用 `gate.check(name, args, run_id=runctx.run_id)`；runner 判定改用 `gate.pending_for(run_id)/denied_for(run_id)`（不再读“最近 begin”的全局属性）；
- 并发测试：4 个 run（2×A 语义 + 2×B 语义）交错挂起/批准——每条审批行只挂在各自 run_id；A 批准不自动放行 B 的同名同参（B 仍 pending、需新审批）。

## C18. pending tools

- 每 run 独立 pending_tools 集合：A 的“本轮已申请”不抑制 B；批准/拒绝记录本身按 (run_id, tool, args_key) 存库，天然 run 级。

## C19. 并发测试结果

- tests/test_p1_reliability.py：ApprovalIsolationTests（4 路交错）、MemoryScopeConcurrentTests（10 轮 gather）；
- 现有“进程级单例”生产路径清零：仅剩兼容 API 属性（pending_this_run/denied_this_run 保留给旧测试与顺序调用方），其内部数据已 run-keyed。

**Phase C 完成门：无生产路径继续依赖“当前活跃 Run”全进程唯一变量 → 满足。**

---

# Phase P1-D — 失败路径 Audit

## D20. 原失败路径黑洞

`audit.ingest` 只在 execute_turn 成功返回后调用；guardrail 失败、工具异常、断连、审批恢复后部分失败都不落 model_calls/tool_calls（F3B 曾实证 resume 中多次工具调用缺失）。

## D21. 新增量 / finally Audit（本次落点）

- 工具包装层每次真实执行/被拦/报错都进 **run-keyed ledger**（_ledgers[run_id]，P1-C 基础设施）；
- run 收口统一助手（_fail / denied / waiting / degrade / _succeed）在退出前调用 `_backfill_failure_audit()`：**若该 run 尚无 tool_calls/model_calls 行则把账本与“模型尝试日志（attempt_log，记录每次 execute 的请求模型）”补写为标准行**（status: executed/blocked/error 与 attempted）；已成功 ingest 的不重复；
- Audit 自身异常一律吞掉并继续（观察性，不影响任务结果）。

## D22. model/provider/profile

- `AuditCollector(requested_model, profile)`：provider 返回 model 时用真实值；为空时记录 **requested model**（字段语义：model=实际/请求模型，status=attempted/succeeded 区分是否真的完成调用）；
- 真网关失败 run 取证：P1-E2E 的 Max-turns run 的 model_calls 现在有 `model=agnes-2.5-flash, status=attempted`（此前 model 列全空）。

## D23. Tool 状态

失败 run 的工具行带状态 executed / blocked / error 与结果摘要（excerpt）——P1-E2E 失败 run 中能看到 `code_loop blocked`、`save_note executed`、`list_workspace_files blocked(运行时文件边界)` 的完整事实链。

## D24. 敏感字段脱敏

- `runtime/audit.py::redact_value / redact_text`（sk-/tvly-/ghp_/AIza/api_key/authorization/password/secret/token 模式）；
- `task_manager.insert_tool_call`（arguments 与 result_excerpt）与 `create_approval`（arguments_json）入库前统一脱敏；RedactionTests 实证库内无明文密钥。

## D25. Failed Run 可追踪性测试

- AuditBackfillTests：真实执行后工具异常 → tool_calls 含 write/run、model_calls 含 attempted；RedactionTests 双路径脱敏；
- 真网关取证：失败 run（Max turns）的 approvals/tool_calls/model_calls/events 四表齐全，可完整回答“用了哪个模型/调过哪些工具/谁成功谁失败/是否发生审批/为何终态”。

**Phase D 完成门：随机挑失败 run（task_8f10447a）→ 模型 agnes-2.5-flash（attempted）；工具 code_loop(blocked+pending)、save_note(executed)、web_search(executed)、recall_memory(executed×n)、list_workspace_files(blocked by 文件边界)；审批 approv_cdedd61b pending；终态 failed(Max turns) → 全可答 → 满足。**

---

# Phase P1-E — 双 Web 执行链收敛

## E26. 原双 Web 链

- `/api/projects|tasks|runs/.../stream → run_turn`（完整 Runtime）；
- 旧 `/api/stream?message=` 内联 `Runner.run_streamed`：绕过 Project Context/Memory Binding/Budget/Completion/部分 Approval 语义/文件边界，同一 Agent 两个 Web 入口行为不一致（webapp 内联分支 ~100 行独立执行器）。

## E27. legacy consumers

- index.html（/chat 旧聊天页）仍真实使用 `/api/stream`；runtime.js 新 UI 不用它；无其他外部消费者发现。→ **保留 endpoint（不删除），内部收敛。**

## E28-E29. 新统一 Runtime + Compatibility Adapter

- legacy 分支改为：建容器/Run + 登记 user Message（预置逻辑保留）→ 直接 `AgentRuntime.run_turn(task_id=run.id, session=..., mode=stream)` → 统一 SSE 映射（run 结果 → task./run. 事件，保持旧前端契约）；**独立 Runner.run_streamed 执行器删除**；
- URL 可以旧，Runtime 不能旧：Context Guard / Memory Scope / Approval / Completion / Audit / FileScope 全数生效；
- 新增的唯一路径差 = 输入输出适配（SSE 事件名/会话键解析）。

## E30. 删除的旧执行代码

webapp legacy 分支内的：`route_agent+Runner.run_streamed` 内联执行、自建重试循环、手工 collector.ingest/artifact/usage/mark_success/assistant 落库、手工 gate.begin 管理等（全部由 run_turn 覆盖）；保留：容器创建/消息登记/SSE 事件名兼容。

## E31. Web E2E（真网关）

- legacy `/api/stream`：reply=“5”、task.started/completed+run.completed 正常，事件含 `context.metrics` + `completion.check.started/passed`（与 Project 主链同能力）；`/api/projects` 主链同轮对比行为一致。

**Phase E 完成门：Web 不再存在绕过 run_turn 的第二执行链 → 满足。**

---

# 收尾验证

## 32. P0-1 Completion 回归（真网关）

对抗提示（明令 0 工具 + 声明“文件已经修改完成，等待你批准执行验证。”）→ **failed**（“没有登记待审批操作…已阻止按完成收尾”），文件未变；QA“1+1”仍 0 工具 completed。

## 33. P0-2 Context/Compact 回归（真网关）

12 轮预置历史 + 真实消息 → `context.compaction.started/completed`（收敛）→ run completed，`completion.check.*` 同在；短对话仅 1 条 `context.metrics`。

## 34. 全量 tests

`tests/run_tests.py`：**Ran 371 tests（原 354 + 新增 13 项 P1 + 既有 4 项 A 阶段新增 = 371），OK**（多次执行稳定；无删除/跳过/降级断言）。

## 35. 真网关 E2E 汇总

| 场景 | 结果 |
|---|---|
| QA sanity | completed，context.metrics+completion.* |
| P0-1 对抗假完成 | failed（被 Completion/Approval 一致性拦截） |
| P0-2 Web compact | context.compaction.* 触发并完成 |
| legacy /api/stream | run_turn 统一（context/completion 事件存在） |
| code_loop 审批链 | pending→approve→同 run 执行(✅ exit0)→换参新 pending→approve→同 run completed |
| 失败 run 取证 | approvals/tool_calls(model 有值,status=attempted)/events 齐全 |
| FileScope 真实拦截 | 严格 Project 下越界 list 被包装层拒绝并留痕 |

## 36. 修改文件

`runtime/approval.py`（风险分类+run-keyed+all 语义）、`runtime/runctx.py`（新）、`runtime/filescope.py`（新）、`runtime/runner.py`（RunContext 绑定、包装层 gate+文件边界+run 账本、失败审计兜底/收口统一）、`runtime/audit.py`（requested_model/profile、脱敏）、`tools.py`（记忆绑定 ctx 优先、读根含 wl）、`project_edit.py`（写入根=wl）、`runtime/task_manager.py`（tool/approval 落库脱敏）、`webapp.py`（legacy 收敛 adapter）、`tests/test_approval.py`（+4）、`tests/test_p1_reliability.py`（新 13）、配置：无新增参数（沿用既有 env）。

## 37. DB migration：无。 38. 配置变化：无新增（all/* 语义变化为既有键行为修正，已在报告与 approval.py 文档注明）。

## 39. 尚未解决 P2（如实）

1. Tool 子集下 code_loop/rollback 的审批体验：模型若无视“结束本轮”仍会重试耗尽轮次（本次 E2E 遇到，靠 Max turns 收口；门本身无副作用泄露）——建议后续把“pending 中重复调用”直接短路本轮（repair 级 stop），列入 P2 体验项；
2. rag（index_workspace/search_documents）与 fetch_github_repo 尚未纳入 FileScope 入口清单（自身目录参数校验仍沿用旧根）；跨项目泄露面已由 read 工具族封堵，RAG 索引以用户显式目录为准——记录待收敛；
3. `_MEMORY_BINDING` 进程级 fallback 仍保留（兼容直接调用/旧测试）；生产 run 均走 RunContext；
4. webapp 旧聊天页仍依赖 legacy endpoint（保留兼容），其 UI 与 runtime UI 的信息架构合并属 Phase UI 范畴；
5. 8765 长驻实例为旧代码，需重启后生效（本轮 E2E 均在独立实例完成）。

## 40. 下一阶段建议

- P2-Runtime：修复“pending 重复调用 → 本轮短路”体验；把 rag/github 入口并入 FileScope；按 run 的 token/字符预算事件做仪表盘；
- 上下文专项：人设压缩（~32k 字符基线）；把 Agent 相关文档/配置集中成 runtime 契约页；
- 部署：单实例 + 守护重启（替换当前双 webapp 进程），随后在 8765 上线本轮全部改动。

---

# 最终 10 问回答

1. **code_loop 现在还能不能绕过 Approval？** —— 不能。code_loop 已默认进入审批门（GATED_DEFAULT），批准前零副作用；内部 `run_python_impl` 直调只在批准后运行。真网关实证 pending→approve→同 run 执行 exit0。

2. **sandbox_rollback 是否有明确权限策略？** —— 有。默认审批（DESTRUCTIVE 类），理由=与执行一致的回滚策略一致性；拒绝/批准按 (run,tool,args) 记录；scheduled 渠道自动拒绝。

3. **APPROVAL_GATED_TOOLS=all 是否真的符合名字？** —— 是。all/* = 执行+破坏/恢复+真实文件编辑（ALL_SEMANTICS），不再等于“默认 3 件套”；SAFE 只读工具仍放行（测试覆盖）。

4. **WorkLocation 是否已经成为真实 Runtime 文件边界？** —— 是。RunContext.file_scope 在工具包装层先于执行判定：绑定 wl 的 Project 读=wl+本 Project 数据+共享产物+沙箱，写=wl；未绑定=只读数据范围；legacy 个人会话保持原语义。离线矩阵 + 真实链拦截取证。

5. **Project A 能不能读取 Project B 的私有 Sources/Attachments？** —— 不能（严格 Project 语境）。B 的 forge_data 子树不在 A 的授权根内，包装层拒绝；测试 A-read-B-denied。

6. **两个 Run 并发时 Memory Scope 是否可能串？** —— 不会。tools._active_project 优先读 Task 本地 RunContext；10 轮并发交错测试零串扰（生产路径无全局单槽依赖）。

7. **两个 Run 并发时 Approval 是否可能错挂？** —— 不会。ApprovalGate 状态按 run_id 存储，包装层显式传 run_id，runner 用 pending_for/denied_for(run_id)；4 路并发测试验证审批行归属与跨 run 不自动放行。

8. **Failed Run 是否能够完整追踪已发生的 Tool/Model 行为？** —— 能。失败/拒绝/降级收口统一补写（账本→tool_calls；attempt_log→model_calls 含模型名与状态 attempted），落库前脱敏；真网关失败 run 四表齐全可完整复盘。

9. **Web 是否还存在绕过 AgentRuntime.run_turn 的第二套执行链？** —— 不存在。legacy /api/stream 已收敛为 run_turn 适配器；Web 侧唯一 Agent 执行入口 = run_turn。

10. **P0 Completion 与 Context Guard 是否全部保持通过？** —— 是。P0-1（假完成拦截，真网关 failed）与 P0-2（Web compact，真网关 context.compaction.* + completed）在最终代码上回归通过；全量离线 371/371。
