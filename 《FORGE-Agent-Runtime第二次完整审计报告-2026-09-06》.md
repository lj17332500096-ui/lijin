# 《FORGE-Agent-Runtime 第二次完整审计报告 - 2026-09-06》

> 定位：**Post-Remediation Runtime Audit**（P0×2 + P1×5 整改后的当前真实状态复审）。
> 基线：首轮《FORGE-Agent-Runtime 完整审计报告.md》仅作为 Historical Findings；所有结论均基于本日重新读取的代码、数据库、离线测试与真网关 E2E 重新验证。
> 执行纪律：本审计**未修改任何项目源码**；仅清理了本审计产生的临时 DB 测试行与临时目录。
> 四层验证要求：代码证据 + 数据库证据 + 自动化测试 + 真实 E2E（除明确标注外全部满足）。

---

## 1. Executive Summary

经过 P0 两阶段与 P1 五阶段整改，FORGE 的 Agent Runtime 已经从中期“单 Run 可用但边界靠自觉”的状态，收敛为**具备运行时强制边界的主链架构**。以下事实全部经四层验证（证据见 §35-37 与验收矩阵）：

- **完成判定不再由模型单方面决定**：0-tool 假完成与“口头审批无 pending”两类旧 P0 在真网关对抗测试中被运行时拦截（run 终态 failed，而非 completed）；
- **上下文有界化进入真实 Web 主链**：compact/硬窗口在 `/api/projects/*/stream` 链路真实触发并持久化（重启不回弹）；
- **危险能力必须先过真实 Approval**：code_loop/sandbox_rollback 默认进门，副作用前拦截、批准后同一 run 执行（真网关全链）；
- **WorkLocation 已成为真实 Runtime 文件边界**：跨 Project、越界读写被工具执行前的 FileScope 判定拦截；
- **并发隔离**：Approval / Memory Scope / 执行账本 / 文件范围全部 run-scoped（contextvars + run-keyed 状态），并发测试零串扰；
- **失败可追踪**：Failed/被拒 run 的 model/tool 审计齐全（model 字段含请求模型与 attempted 状态），落库脱敏；
- **Web 仅一条 Agent Runtime 主链**：legacy /api/stream 为 compatibility adapter，内部统一走 `AgentRuntime.run_turn`。

**总体结论：CONDITIONALLY YES（有条件地适合普通用户长期使用）**——距离无条件 YES 还差本报告 §39 列出的 2 项 P1（Verification 结果语义、Web 并发下的同步摘要调用）与少量 P2（安全/状态一致性），详见 §14/§39。

## 2. 审计范围

- 静态：runtime/ main.py webapp.py tools.py code_exec.py project_edit.py office_docs.py rag.py multimodal.py compact.py agent.py 及 tests/ 全量；
- 数据库：agent.db(user_version=3, WAL) 16+ 业务表、sessions.sqlite(39 sessions/2913 msgs)；
- 运行：`tests/run_tests.py`（371 项）、真网关 E2E（本日新实例 + 此前同代码批次）、重启/恢复探针、离线并发矩阵；
- 环境：10 个 .env 键（无 MODEL_* 档位/无 MCP/无 APPROVAL 自定义——全部默认）。

## 3. 与上次报告的变化

| 维度 | 首审（Baseline） | 本次（Current） |
|---|---|---|
| 完成判定 | 模型文本决定 | Completion Gate（声明↔证据一致性）+ ≤1 次 repair |
| 历史 | Web 无限增长（单会话 1547 条全量读回） | prepare_session_context：SQL 快判→分块 compact→硬窗口（sessions 上限由阈值决定） |
| Approval | 3 工具；code_loop 旁路；all 语义错 | 5 类默认门（EXECUTION+DESTRUCTIVE）；all=ALL_SEMANTICS |
| 文件边界 | WORKSPACE_ROOT/BASE_DIR 三硬根；WorkLocation 仅文本 | RunContext.FileScope：wl+项目数据+共享产物+沙箱；legacy 保留原语义 |
| 并发 | 进程级单槽（gate/memory/ledger） | contextvars RunContext + run-keyed 状态 |
| Audit | 成功路径一次性 ingest；model 空 | 成功 ingest + 失败 backfill；model 有值/attempted；脱敏 |
| Web 执行链 | 两条（内联 + run_turn） | 一条（legacy=adapter） |
| 离线测试 | 320 | 371 |

## 4. 当前真实 Runtime 调用链（代码级）

```
User Message → Project/Task 容器
  → webapp.api_project_stream(或 task/run stream / legacy adapter / CLI chat / voice / scheduled)
  → AgentRuntime.run_turn（唯一生产执行门面；runtime/runner.py）
     ├─ 容器解析/建 Run(SUBMITTED)/登记 user Message(agent.db messages)
     ├─ ArtifactTracker.snapshot + audit collector(requested_model/profile)
     ├─ resolve_budget(更严格 max_turns) → route_profile → route_agent(模型档位克隆+ToolRouter≤16)
     ├─ bind RunContext(contextvars: run_id/container/channel/memory_scope/profile/requested_model/file_scope)
     ├─ Context Guard：prepare_session_context（SQL 快判 → compact_history 分块 → hard window_history）★P0-2
     ├─ Project Context 块 + 本次附件清单 → clone instructions（当前消息永不被本次 compact）
     └─ 执行循环（首次 + ≤1 repair）：
         gate.begin(run_id) → execute_turn（唯一 SDK Runner 包装；_run_attempt 3 模式）
             └─ Agent 工具包装层（runner._make_invoke，每个工具同一道包装）：
                ① ApprovalGate.check(run_id)  ② FileScope.authorize_tool(run_id)  ③ 执行并记 ledger(run_id)
         ├─ denied_for/pending_for(run_id) → failed / WAITING_APPROVAL（事实源=ApprovalStore）
         ├─ ReplyParser（只解析）→ Completion Gate（Evidence=ledger+产物diff+approvals）
         │    PASS → register artifacts → mark_success → completed
         │    非 PASS → ≤1 次 repair（运行时观察）→ 仍不通过 → failed
         └─ FinalResponseFailed（空输出/输出闸）→ 有证据则降级 completed+系统摘要；无证据→failed
  → 收口统一：_backfill_failure_audit（失败路径 model/tool 补写）+ gate.clear_run
  → SSE：run/task 事件 + reply + done → eventClient → UI（事件另有 context.*/completion.* 供审计）
```

## 5. 当前配置来源（现状速查，细节同首审总表 + 本阶段变更）

| 键 | 当前状态 | 读取点（真实） | 备注 |
|---|---|---|---|
| AGENT_MODEL / OPENAI_* / WORKSPACE_ROOT / TAVILY / ALLOW_* / SKILLS | 生效（.env 10 键实测） | agent.py/tools.py/code_exec/project_edit | 单模型 |
| APPROVAL / APPROVAL_GATED_TOOLS | 默认开；默认 5 工具；all=ALL_SEMANTICS | approval.py | 本阶段修正 |
| TOOL_ROUTER | 默认 on（≤16） | tool_router.py:70 | — |
| AUTO_SUMMARY_*（5 个） | 复用（CLI/Voice/Web 统一） | compact.py _refresh_thresholds | P0-2 接线 |
| FORGE_HISTORY_HARD_CHARS/MESSAGES、FORGE_CONTEXT_GUARD | 默认 260000/500/on | runtime/context.py | 新增（P0-2） |
| MODEL_CHEAP/REASONING/MAX_OUTPUT_TOKENS、MCP_SERVERS、GITHUB_TOKEN、VISION_MODEL、FORGE_REPLY_REPAIR | 未配置→功能未启用 | router/mcp_bridge/reply_parser | 如实：骨架存在未启用 |
| TASK_MAX_WALL_SECONDS | **死配置仍存**（无读取；RunBudget 需显式传入） | 无 | P2 |

## 6. Model Routing（复审）

- 真网关全部 run 实际模型 = `agnes-2.5-flash`（model_calls 现可证实，含 attempted 行）；
- `route_profile/agent_for` 骨架存在并被调用（runner.run_turn），但无 MODEL_CHEAP/REASONING 配置且无 deep 元数据 → **当前单模型设计**（不是“自动路由”）；
- Provider fallback：无（异常分类≠fallback）；失败无自动重试（guardrail 输出闸重试 1 次除外）。

## 7. Tool System（复审）

- 注册 36 工具=暴露层=执行层（agent.py:198-235+skills）；每轮 ToolRouter ≤16；
- 复合工具内部 impl 直调扫描结论：code_loop→run_python_impl 已被**入口门**覆盖（方案 A，A 阶段单测+真网关全链）；code_loop→snapshot_impl（快照工具本身无审批语义，低风险一致）；deep_research→web_search_impl/save_note_impl（网络读/产物写，无门工具）；其余 *_impl 均仅被各自注册工具调用。**未发现新的高风险 bypass**；
- 重复/重叠工具（read_workspace_file⊇read_note/read_code_file 等）原样存在（P3 维护项）。

## 8. Permission / FileScope（复审）

- 三层校验现状：① FileScope 包装层（run-scoped，白名单根+解析后绝对路径比较）② 工具层原硬边界（.env/敏感/越界/二进制/后缀）③ project_edit 运行期写根（wl 优先）。
- 读入口清单（包装层判定）：read_workspace_file/list_workspace_files/read_office_file/read_spreadsheet/ask_image；写入口：write_project_file/edit_project_file。**RAG(index/search)与 fetch_github_repo 未纳入**（P2 记录，见 §39）。

## 9. Approval（复审）

- 默认门=5 工具（EXECUTION∪DESTRUCTIVE）；`all/*`=再 ∪REAL_FILE_EDIT（语义修正）；
- 副作用前拦截、pending→WAITING_APPROVAL、approve→same-run resume、reject→不执行：真网关 code_loop 全链 + wrapper 单测三层工具验证；
- 口头 Approval 一致性：Gate APPROVAL_INCONSISTENT（P0-1 真网关 failed）；
- 事实源=ApprovalStore（runner pending_for/denied_for 按 run_id 读取）。

## 10. WorkLocation（复审）

- 真边界（不再只写 Prompt）：Project.work_location_id→local_path→RunContext.file_scope→包装层判定→工具运行期根（tools._active_read_root_for/project_edit._active_write_root）；
- 真网关：wl 内读执行成功（内容可见），wl 外绝对路径读被“运行时文件边界”拒绝且无泄漏；
- 无 wl 的显式 Project：read_only（写被拒并提示绑定），读限本人数据/共享产物/沙箱；
- Legacy personal/sess-* 语境保留旧语义（设计差异，文档化）。

## 11. Project Isolation（复审）

- A/B 离线隔离矩阵全过（读 B 的 wl/source/attachment/系统目录→deny；无 wl 不回落安装目录为读写根）；真网关越界拦截取证；
- FORGE_DATA 与 WorkLocation 分离保持：sources/attachments/artifacts/memories 仍在 `forge_data/projects/<cid>`（DB 行+目录实测），未因 FileScope 迁入 wl。

## 12. Context Builder（复审，真实顺序）

SDK 历史（compact 后=[摘要]+最近 KEEP 轮）→ 默认 instructions(~32,352 字符,静态) → 附件清单(≤8,名称+路径) → Project Context 块(≤6000 字符：标题/记忆范围/instructions/来源清单/最近 10 条项目记忆/工作位置) → 当前用户消息(SDK 本轮写入,永不进压缩)。来源文件内容需模型主动 read（≤20k 字符/次 trust 包裹）。真网关 0-tool 轮 input_tokens=13,200（≈首审 13,204，基本不变）。

## 13. History / Compact（复审）

- Web/Project 主链调用已证（真网关 context.compaction.* 事件 + 32→7 收敛；连续第二轮不再 compact）；
- Soft=复用 AUTO_SUMMARY_*；Hard=FORGE_HISTORY_HARD_*；Target=AUTO_SUMMARY_KEEP_TURNS；hysteresis 天然（摘要后落 KEEP 轮）；
- 分块摘要（2400 items 离线：多次 summarize+合并收敛 <100 条）；摘要单条 ≤1200 字符、多周期不累积（测试）；ToolPair 切割完整（窗口按用户轮起点切；函数级测试成对断言）；
- compact 失败→Hard Window（离线实证：摘要抛错+超硬→windowed，本轮不 failed、全量历史不回弹）；
- 重启持久：DB reopen 后仍 [摘要+尾轮]（9 items/head summary）。

## 14. Memory / Memory Scope（复审）

- 写=仅 remember 工具（无自动抽取）；全局 200 条上限；project_memories 无上限（P3 记录）；
- 三层隔离复测（当前代码）：工具绑定层（离线交错 10 轮零泄漏）、Context 层（ctx_block 不含全局标记）、**模型层（真网关：project_only→not_found；global→命中 TEST_GLOBAL_AUDIT_2026）**；
- summary 不吃 Project Context/Global Memory（transcript 捕获测试）；
- 遗留：tools._MEMORY_BINDING 进程级 fallback 仅为兼容保留；`_last_recall_call` 是跨 run 的进程级节流计数（只影响“重复提醒”，不构成隔离语义，P3 记录）。

## 15. Completion（复审）

- 终态决定链（当前代码逐行确认）：模型文本 → ReplyParser → **Completion Gate**（claim↔evidence）→ 通过才 mark_success；
- 声明类别确定性规则（write/verify/artifact/approval claim）+ 会话型放行（questions/plan/无声明 answer）；
- 真网关对抗（0 工具+假完成+口头审批）→ failed；QA/问答路径 0 工具正常 completed；完成证据含 ledger(tool 状态)+产物 diff+approvals；
- **残余语义缺口（本次新记 P1）**：verify-claim 的证据 = “run/verify 工具执行过”，未校验执行结果（如 exit≠0 的“已通过”）。即 Completion 校验“行为发生过”，未校验“结果通过”（§21 矩阵）。

## 16. Verification（复审）

- 仍无独立 Verification Engine/Hook 在完成前强制执行；code_loop 是可选工具（现需审批）；Completion Gate 的“验证声明需执行证据”是最低一致性校验（Coding 成功路径：真网关 write→approve→run→exit0→claims+evidence→completed）；
- Coding test-fail→repair→pass 的自动闭环只在模型自选 code_loop 时发生（工具语义），不是主链强制阶段；
- 文件生成：产物真实落盘由 ArtifactTracker diff 支撑（离线 test_success_registers_artifacts）；
- E5 语义（声称通过但无验证→不得可信完成）已由 Gate 覆盖；**声称通过且执行过但实际失败** 不在当前 Gate 覆盖内（与 §15 同一 P1）。

## 17. Run Policy / Retry（复审）

- max_turns：SSE 钳制 5–100（默认 20）→ run_turn → resolve_budget 取更小值（单层生效，无叠加）；
- guardrail 输出闸重试 1 次；Completion Repair ≤1；compact 摘要失败无自动重试（fallback 到硬窗口）；
- 工具超时：run_python 40s/120s+进程树强杀；子进程输出截断；环境白名单 16 键；
- 理论上限（有界）：模型调用 ≤ 2(首次+repair)×(guardrail 内至多 2)=≤4 常规 + 摘要模型 ≤分块数+合并 1（仅超阈值时）；无指数重试；
- max_wall_seconds：显式 RunBudget 传参才生效（离线测试证明超时→BudgetExceeded→failed）；env 未接（死配置，P2）。

## 18. Concurrent Runs（复审）

- 生产路径已无“当前活跃 Run”式全局唯一可变状态（runctx contextvar + run-keyed 表/账本）；保留属性（pending_this_run 等）为兼容读取；
- 离线：4 路 Approval 交错归属正确、A 批准不自动放行 B；Memory 10 轮交错零串扰；FileScope 纯函数并发安全（contextvar 隔离）；
- 真网关：双 run 并发独立完成（distinct runs）；
- 已知未覆盖：无真实“并发中触发同一 gated tool 并交错批准”的端到端（离线矩阵已覆盖同语义），标记为测试补强项而非功能缺口。

## 19. Audit / Logs（复审）

- 成功路径 ingest + 失败/拒绝/降级收口 backfill（model 带 requested/actual + attempted；tool 带 executed/blocked/error）；
- 脱敏：tool 参数/excerpt、approvals 参数入库前 redact（sk-/tvly/ghp_/AIza/api_key/authorization/…）——脱敏单测实证；
- 时间线可重建：completed/failed/waiting run 的事件/模型/工具/审批/终态齐全（本日 DB 抽样 task_8f10447a failed、task_828401ec waiting/approved 两段均可逐条回放）；
- 残余：断连真实客户端场景未做专门 E2E（收口代码路径与 cancelled 事件逻辑存在）；失败 run 的 pending approval 不自动关闭（P2）；成功路径 blocked 行审计误标 succeeded（P2）。

## 20. Provider / Secrets（复审）

- .env 不被读工具/写工具/子进程访问（工具层拒绝+子进程白名单）；guardrail 输出密钥拦截+日志打码；
- memory 写闸、输出闸保留；审计脱敏已上（§19）；memory.json.migrated.bak 仍可读（P2 未修）；
- 凭据不进入 LLM 上下文（路径全拒）。

## 21. Trust Boundary（复审）

- 已 tag：web(3 源)/文件/Office/表格/RAG/图片；**未 tag：run_python/code_loop stdout、read_code_file、notes、github 抓取返回**（同首审，P2）；
- 提示注入测试（含“忽略系统指令”stdout 场景）仅在已 tag 出口被显式边界声明覆盖；子进程输出出口仍裸奔（P2 未修；本轮不改）。

## 22. Network / Sandbox（复审）

- 出网：web_search 三源固定端点；github.com 白名单正则；模型网关；无通用 fetch；
- 子进程：cwd=sandbox 项目、环境白名单 16 键、40/120s 超时+进程树强杀、输出 12k 截断；网络不受限（事实记录，未擅改）。

## 23. Backup / Restore（复审）

- project_edit 覆盖前备份 logs/backups（单测 test_write_and_backup）；沙箱 snapshot/rollback（含 dry-run、5 份上限；rollback 现需审批）；
- checkpoint=运行摘要（非文件事务点）——本报告再次明确区分，不做恢复点误认；
- 无 Run 级事务回滚（设计如此，未实现）。

## 24. Sources / Attachments / Artifacts（复审）

- 三作用域分离保持（表/目录/语义）；附件默认仅本次消息（DB 绑定+下轮不注入测试）；promote 转 source（sha 去重）；artifacts 仅登记真实生成文件（失败不登记测试）；
- FileScope 改造未破坏：跨项目引用仍被 read 家族拦截（离线矩阵+真网关 wl 探针）；
- Sources 深度索引仍未实现（index_status=none，RAG 主链无 Sources pipeline）——如实保留“未实现”。

## 25. Session Persistence（复审）

- SDK sessions.sqlite=模型历史唯一源（39 会话/2913 条）；agent.db.messages=UI/审计源（81 条，本轮 E2E 所致）；双写无一致性协议（compact 只改 SDK 库、delete_container 不清 SDK 行）→ PARTIAL（P2）。

## 26. Crash Recovery（复审，重启探针）

- RUNNING 超龄→failed+task.recovered ✓；WAITING_APPROVAL/SUBMITTED 跨重启保持原态 ✓（approved 后 resume 语义可用）；waiting+approval 持久化 ✓（重开 manager 验证）；
- compact 持久化 ✓（reopen 9 items+head summary）；
- 缺口：stale SUBMITTED 永不回收（崩溃于 create↔transition 之间）；failed run 的 pending 不自动关闭（P2）。

## 27. Web Runtime 收敛（复审）

- Web 生产调用点：api_stream(273/353) 两条分支都只调 `runtime.run_turn`；SDK Runner 仅存在于 `main.execute_turn → _run_attempt`（唯一执行核心，被 run_turn 独占调用）；evaluate.py 的 Runner.run 是独立评测脚本（非产品入口）；
- legacy /api/stream 现为 adapter（建容器/Run→run_turn→SSE 契约映射）；真网关 legacy 轮带 context.metrics+completion.* 事件 → 同一 Runtime 证据；
- 渠道差异=channel policy（scheduled 自动拒绝审批等），非第二套 Runtime。

## 28. 假功能（第二次审计清单，2026-09-06）

沿用旧 16 项中仍成立者 + 新增：① ToolBroker/Registry/spec 不参与执行（盘点层）② WAITING_USER 死状态 ③ api_tasks_list/api_task_detail/touch_work_location/count_checkpoints(生产)/container_running_state_any 死代码 ④ TASK_MAX_WALL_SECONDS env 死配置 ⑤ Sources index_status 字段无 pipeline ⑥ 前端监听的 task.paused/cancelled、artifact.created、checkpoint.created 等 SSE 服务端从不发 ⑦ forge.mode localStorage 无后端消费 ⑧ approvals 文档行与 ALL_SEMANTICS 需同步（文档旧行仍写“默认3 件套”？）——本日核对 approval.py 模块 docstring 已更新（第 1 段写默认执行+破坏/恢复类）；README 36 工具未更新（写 32）⑨ `_MEMORY_BINDING` 兼容 fallback ⑩ e2e 报告“320”等旧数字残留于历史 md（非代码）。

## 29. 重复控制（第二次审计清单）

- 单 Runtime 主链已收敛；max_turns 三层参数最终 resolve 唯一；AUTO_SUMMARY_* 单一来源（CLI/Web 共用）；
- 仍重复：sessions.sqlite 与 agent.db.messages 双历史源；legacy adapter 保留事件名双发（task.*/run.* 兼容层，属设计）；`_last_recall_call` 跨 run 共享节流；approval 文档 vs ALL_SEMANTICS 文案（代码为准）；旧 md 报告遗留描述（文档性）。

## 30. 死代码 / 死配置（第二次审计清单）

见 §28；额外：webapp legacy 分支旧 import（Runner/ensure_mcp/retry_note）残留未清理（仅无害引用，报告不删）；web 前端旧页面函数残留（P3）。

## 31. 数据库检查（本日）

- agent.db：user_version=3(WAL)；tasks 36/runs 40/messages 81/task_events 271/approvals 6/artifacts 3/project_sources 1/message_attachments 4/memories 2/work_locations 3/model_calls 114/tool_calls 164/checkpoints 38；run states: completed 29/failed 10；
- 运行时事件存在：completion.check.* (24)、context.compaction.* (4)、context.metrics (10)、reply.degraded；
- model_calls.model 全部有值；approvals 5 approved/1 pending（该 pending 属已 failed run——P2 状态一致性）；
- orphan artifacts=1（materials 上传注册、task_id=None——设计路径非缺陷）；orphan approvals=0；
- sessions.sqlite 39/2913。无新增表、无 migration 遗留。

## 32. 自动化测试（本日）

`tests/run_tests.py`：**Ran 371 tests，OK（0 fail/0 skip）**，时长 ~94s。构成：基线 320 + P0 阶段 34 + P1 阶段 17。覆盖 Completion Gate、Context Guard/Compact、Hard Window、ToolPair、附件、Memory Scope（并发）、FileScope、Audit 脱敏/失败兜底、Approval 新名单/all 语义等。

## 33. E2E 测试（真网关，当前代码）

| 场景 | 结果 |
|---|---|
| E1 QA(0 tool) | completed，content=2，input_tokens=13,200 |
| E2 缺失信息（订机票） | kind=questions（出发地/目的地），completed(会话型) |
| E3 假完成对抗 | failed（approval_inconsistent→repair→claim_unsupported→failed） |
| E4 真 Coding（code_loop） | pending→approve→同 run exit0→（换参新 pending→approve）→completed |
| E7 Approval reject | 单测（wrapper 三工具：拒绝→不执行） |
| E8 WorkLocation 隔离 | wl 内读成功、wl 外读被运行时拒绝、无泄漏 |
| E10 Memory Scope | project_only→not_found；global→命中 marker |
| E13 并发双 run | 均 completed、run 独立 |
| E17 legacy Web | completed（带 context.metrics/completion.*） |
| E11 Compact(Web) | context.compaction.started/completed（32→7） |
| E12 Compact 失败 | 离线：摘要抛错→windowed，本轮不 failed |
| E14 Provider failure | 离线 fake 异常→failed 且审计兜底；真网关 MaxTurns 失败 run 取证 |
| E16 重启恢复 | 离线探针：RUNNING→failed+recovered；WAITING+approval 持久、approved+resume 可用；compact 不回弹 |
| E20 Artifact | 离线 test_success_registers_artifacts；真实 run 产物登记事件存在 |

E5(测试失败不完成)/E6(final 空输出降级)/E9(附件)/E15(断连)/E18(注入)/E19(密钥) 以离线/集成测试+代码证据覆盖为主（模型随机性无法稳定触发真实链路；E6 的降级路径为 fake FinalResponseFailed+真实账本集成测试，E5/E9/E19 为确定性单测）。

## 34. 并发测试（真实+离线）

- 离线：Approval 4 路交错 / Memory 10 轮交错 /（FileScope 为纯函数并发安全）；
- 真实：2 个 Project 并发流各自完成、run 独立（无状态串扰观测）；更重并发（5 run+交错 gated tool）建议下阶段补强（§40）。

## 35. 性能 / Token（测量，不优化）

| 场景 | input_tokens | 备注 |
|---|---|---|
| 0-tool QA（1+1） | 13,200 | ≈首审 13,204，基本不变 |
| compact 后 Web 轮 | 事件显示历史 555 chars→provider 输入由当轮工具决定（此前 run1 45,559 含 4 工具调用） | 历史贡献已收敛 |
| 60 轮离线曲线 | est_tokens ≤2,618（全程） | 锯齿有界 |
| 固定基线分解 | instructions≈32,352 字符、36 工具 schema≈9,058 字符 | 已记录（后续专项） |

## 36. 原 P0/P1 验收矩阵

见独立文件《FORGE-P0-P1整改验收矩阵》（25 行，含代码证据/E2E 证据/结论；新增 21-25 号本轮发现）。

## 37. 新 P0/P1/P2/P3（重新评级）

- **P0**：无新 P0；旧 P0 三项全部 FIXED（矩阵 #1-3）。
- **P1（残余 2 项）**：
  1. Verification 语义：Completion 校验“行为发生过”而非“结果通过”（exit≠0 的“测试通过”声明仍可过 Gate）——建议 Completion Gate 增加“run 输出退出码/错误标记”证据维度；
  2. compact 摘要为同步阻塞调用（Web 并发下占用事件循环秒级）——建议转线程执行（不改变语义）。
- **P2（安全/隐私/一致性，本日仍成立）**：memory.json.migrated.bak 可读；run_python/code_loop stdout 无 trust 标签；子进程网络不受限（设计记录）；runs.goal/messages 原文留存（工具参数已脱敏）；双历史源无一致性协议；Sources 无深度索引；RAG/github 未纳入 FileScope 清单；failed-run pending 不自动关闭；stale SUBMITTED 不回收；成功路径 blocked 审计行误标 succeeded；TASK_MAX_WALL_SECONDS env 死配置；README 数字过期。
- **P3（清理/文档/UX）**：死端点/死状态/死配置清单（§28）；工具重叠；`_last_recall_call` 跨 run 节流；legacy 分支残留 import；旧报告数字残留。

## 38. 下一阶段建议（按现状收敛）

- **Phase 1（无 P0；直接进 P1 收尾）**：P1-1 Verification 结果语义（run 输出 evidence 含退出码/错误）；P1-2 compact 异步化；随后做 5-run 真实并发 E2E（交错 gated tool）。
- **Phase 2（P2 Security/Privacy 优先序）**：① memory.bak 读保护与归档 ② stdout/notes/github 出口 trust 标签 ③ failed-run pending 自动关闭+stale SUBMITTED 回收 ④ audit blocked 状态修正 ⑤ RAG/github 并入 FileScope。
- **Phase 3（Runtime 精简，逐个验证后删）**：死端点/死状态/死配置收敛；双历史源一致性协议（compact/delete/reset 联动）；README 与 36 工具/新审批名单同步。
- **Phase 4（能力决策，暂不建议**）多模型 Router（先有 Provider/预算护栏再启用，当前架构已留 profile 通道）、MCP（缺服务器 allowlist+工具权限映射）、Sources RAG（先定义文件进上下文策略）、精确 Resume（依赖 Verification 语义稳定）、Subagent/新渠道（Runtime 未收敛完前不做）。
- **可以开始做**：Verification 结果语义（P1-1）与 compact 异步化（P1-2）；P2 隐私项。

## 39. 15 问回答

1. **0 tool 假称执行完成还能 completed？** —— 不能（真网关对抗→failed；Gate+证据）。残余：只声明“将要做/在分析”而实际未做且无完成声明的空转仍按会话完成收尾（设计边界，非“假称完成”）。
2. **完成判定最终由谁决定？** —— Runtime：ReplyParser(解析)→Completion Gate(声明↔Execution Evidence)→仅 PASS 才 mark_success；模型只“提出”。
3. **Verification 是否真正属于完成主链？** —— 部分是：声明-验证一致性校验在主链（验证工具执行存在性）；**未达到**“结果必须通过（exit0/文件可打开）”的完成前置——即上问 P1-1，因此标注 PARTIAL。
4. **Web/Project 长对话 Context 是否真正有界？** —— 是（快判+分块 compact+硬窗口；真网关+60 轮曲线）。
5. **Compact 是否真的进入真实 Web 主链？** —— 是（/api/projects stream 事件实证）。
6. **Compact 失败时有没有 Runtime Hard Guard？** —— 有（离线实证：失败→windowed→本轮继续，无全量回流）。
7. **code_loop 或其他复合工具还能不能绕过 Approval？** —— 不能经 code_loop 绕过（入口门+真网关全链）；impl 直调扫描未发现其他高风险 bypass。
8. **WorkLocation 是否真正成为 Project Runtime 文件边界？** —— 是（FileScope 执行前判定+真网关双向探针）。RAG/github 两出口除外（P2）。
9. **Project A 能不能读取 Project B 的私有文件？** —— 不能（Project 语境；离线矩阵+真网关越界拦截）；legacy personal 语境的整工作区读属设计语义（P3 建议显式 UI 提示）。
10. **并发 Run 的 Memory/Approval/FileScope 会不会串？** —— 不会（run-scoped 状态+离线交错矩阵+真实双 run）。
11. **Failed Run 是否有完整可追踪的 model/tool/audit 信息？** —— 是（model 有值+attempted；tool 含 executed/blocked/error；审批/事件齐全；本日 failed run 取证）。
12. **是否仍存在第二条 Web Agent 执行链？** —— 不存在（legacy=adapter，唯一核心 run_turn）。
13. **Memory Scope 是否仍然真实隔离？** —— 是（三层：工具/Context/模型，本日真网关复测）。
14. **当前最影响 FORGE 可靠性的三个问题？** —— ① Verification 结果语义缺口（声称通过但实际失败的完成漏洞，P1-1）；② Web 并发下 compact 同步阻塞事件循环（P1-2）；③ 双历史源与 delete/reset 一致性（P2，长期会造成 UI/模型历史分叉）。
15. **是否达到“适合普通用户长期使用”的基础标准？** —— **CONDITIONALLY YES**。主链的完成真实性、上下文有界、越权拦截、并发隔离、失败可追踪均已四层验证；距无条件 YES 还差：P1-1（Verification 结果语义）、P1-2（compact 异步化），以及建议在正式长期使用前完成的双历史源一致性协议（P2）。

## 40. 结语

首审回答“FORGE 有哪些东西是真的”；本次回答“整改后 FORGE 有多可靠”：**主链级可靠性已达标（有条件），边界由 Prompt 自觉升级为 Runtime 强制，且每条关键事实都有代码+DB+测试+真实运行四层证据**。剩余的 P1 是“语义深化与并发体验”，不再是“边界绕过或假完成”类基础缺陷。
