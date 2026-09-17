# 《FORGE-Runtime 当前完成情况与真实架构审计报告》

> 日期：2026-09-06（审计日）· 性质：前置事实基线审计（不重构、不按理想架构反推）
> 方法：真实代码调用关系 + 真实测试（离线全量 446 项）+ 当日早些时候同代码的真实网关记录 + DB 事件取证
> 范围：my_creative_agent（FORGE）完整代码树（web / runtime / sources / skills / tests / 根模块）

---

## 1. Executive Summary

FORGE 当前的 Runtime 是**单进程、单 Agent、单主链**架构：

```
webapp(Starlette/SSE) + main(CLI/voice/daemon) ──► AgentRuntime.run_turn（唯一生产门面）
   ├─ RunContext(contextvars) / Session Prep(compact+hard window) / Sources 自动检索
   ├─ execute_turn（唯一 SDK 调用点）→ openai-agents Runner 内置 Agent Loop
   ├─ 工具统一包装（Approval → FileScope → NetPolicy → 执行 → Ledger）
   ├─ ReplyParser → Completion Gate（声明↔证据）
   ├─ Audit（成功 ingest + 失败 backfill + Provider 尝试记录）
   └─ 终态收口（状态机 + pending 清理 + checkpoint）
```

- **已形成稳定主链**：用户消息→Frontend/CLI→`run_turn`→模型/工具→Completion→审计→落库→SSE 展示，链路单一、无第二条执行器（legacy `/api/stream` 已是 compatibility adapter，内部同样走 `run_turn`）。
- 模型执行实际由 SDK（openai-agents）的 Agent Loop 驱动；FORGE 在其外层做预算、审批、文件边界、完成门、审计，在“每次模型调用”粒度做 Provider 分类/有限重试/fallback。
- 无 Mission / Worker / Subagent / RunCoordinator / 第二 Context 系统等旧 Runtime 概念存在。

## 2. 当前真实 Production Call Chain（真实代码逐跳）

```
用户点击发送(web/runtime/workspace.js sendNow/commitHome)
 → GET /api/projects/{pid}/stream?message=…  (webapp.py api_project_stream → api_stream resume 分支)
    [兼容: legacy /api/stream?message=… 同样转 run_turn(webapp.py 334-…)]
 → AgentRuntime.run_turn(runtime/runner.py)
    ├─ 容器解析 / create Run(SUBMITTED)/ user Message 落库
    ├─ bind RunContext (contextvars: run/container/channel/memory_scope/file_scope/profile/model)
    ├─ Context Prep: prepare_session_context (SQL 快判→compact_history→hard window)
    ├─ Sources 自动检索（sources.service.build_reference_block→retriever hybrid）注入 instructions
    ├─ budget/route_agent（profile 恒 default；工具子集 ≤16）
    └─ 执行循环（首次+≤1 repair）：gate.begin→execute_turn
         execute_turn(main.py) → _run_attempt → SDK Runner.run_streamed/run/run_sync
           └─ SDK Agent Loop：Model.get_response
                provider = ResilientProvider→ResilientModel（每调用：分类→有限 retry→≤1 fallback）
                tool call → runner._make_invoke 包装层:
                    ① ApprovalGate.check(run_id)  ② FileScope.authorize_tool
                    ③ NetPolicy( run_python/code_loop )  ④ 执行并记 ledger(run_id)
                工具结果 → SDK 回传模型 → 继续 Loop（SDK 内部；max_turns 传入）
         → 结果 ingest audit / FinalResponseFailed 降级门控
    ├─ denied/pending_for(run_id) → failed / WAITING_APPROVAL（DB pending 为事实源）
    ├─ ReplyParser（容错）→ Completion Gate（Pass 才 mark_success）
    └─ 收口：_backfill_failure_audit + provider.model_attempts + gate.clear_run + checkpoint
 → SSE: run.started/tool/reply_delta/reply/run.completed|failed/waiting_approval/done
 → eventClient → eventReducer → UI
```

辅助直连模型（不经 ResilientProvider 包装、独立 OpenAI client）：`compact.summarize_transcript`（Context Prep）、`research.deep_research_impl` 子调用、`multimodal.ask_image`、`runtime.codex_loop` 修复、`reply_parser._repair_with_model`（默认关闭）。它们与主链“模型调用”语义分开（摘要/视觉/工具内修复），当前无统一 Provider 包装（见 §7/§17 记录，不改）。

## 3. Runtime Component Map（模块/文件/类/职责/调用者/下游）

| 模块 | 文件 | 类/函数 | 职责 | 调用者 | 下游 |
|---|---|---|---|---|---|
| 门面 | runtime/runner.py | AgentRuntime.run_turn | Run 生命周期+编排 | webapp/main | execute_turn/状态机/审计 |
| 执行器 | main.py | execute_turn/_run_attempt | SDK 调用与流事件 | runner | SDK Runner |
| RunContext | runtime/runctx.py | RunContext/bind/current | run-scoped 状态(contextvars) | runner/工具包装 | tools/filescope/gate |
| 状态机 | runtime/task.py+state_machine.py+task_manager | Task/TaskState/transition | Run 状态持久化 | runner/webapp/CLI | agent.db |
| Context Prep | runtime/context.py | prepare_session_context/compact | 历史有界化 | runner.run_turn | compact.py/store |
| Compact | compact.py | compact_history | 分块摘要/原子替换 | context | 本地 OpenAI client |
| Sources RAG | sources/* | service/retriever/indexer/parser/store | Source 解析索引混合检索 | runner（自动）/search_sources 工具 | agent.db 表 + trust.tag |
| 预算 | runtime/budget.py | resolve_budget/run_with_wall_limit | max_turns/墙钟 | runner | 任务异常 |
| 工具包装 | runner.py | _make_invoke/_record_tool | 门/边界/账本 | SDK Agent Loop | approval/filescope/netpolicy |
| Approval | runtime/approval.py | ApprovalGate(check/pending_for) | 审批门(run-keyed) | 包装层/runner | approvals 表 |
| FileScope | runtime/filescope.py | authorize_tool/build_file_scope | 运行期文件授权 | 包装层 | 工具执行 |
| NetPolicy | runtime/netpolicy.py | evaluate | run_python 网络策略 | 包装层 | 事件 |
| 信任 | runtime/trust.py | tag | 外部数据边界 | 读出口/检索/工具输出 | 模型上下文 |
| 完成 | runtime/completion.py | CompletionGate/verification_outcome | 声明↔证据终审 | runner | 终态 |
| 解析 | runtime/reply_parser.py | parse/coerce | AgentReply 容错 | runner/guardrails | — |
| Provider | runtime/provider_gateway.py | ResilientProvider/Model | retry/fallback/attempts | agent.py(构建) | SDK Model |
| 错误 | runtime/provider_errors.py | classify/retry_policy | 错误映射 | gateway/runner | 事件 |
| 审计 | runtime/audit.py + runner 收口 | AuditCollector/_backfill | model/tool 明细 | execute_turn/runner | agent.db |
| 快照 | runtime/snapshot.py | create/restore | 双库备份恢复 | CLI --snapshot/--restore | — |
| Web | webapp.py | api_* / api_stream | HTTP/SSE/上传/审批 | 浏览器 | runtime |
| CLI/voice/daemon | main.py | chat_async/chat_voice/daemon_loop | 本地入口 | 用户 | runtime |

## 4. Task / Message / Run 模型（真实关系）

- **Project（容器）＝用户级长期工作空间**（物理表 `tasks`，id `tk_*`，含 instructions/memory_scope/work_location_id/pinned）。
- **Message**（`messages`，project_id+run_id）：user/assistant 对话原始记录（UI 源）。
- **Run**（`runs`，id `task_*`，run_index）：**每一条需要执行的用户消息 = 一个新 Run**；容器内连续消息 = 多个 Run（离线与当日真机均验证：同一容器第 2 条消息新建 Run，绝不续跑旧 Run）。
- **Session（SDK sessions.sqlite）＝模型 Runtime Context 缓存键**（容器持有 session_id；不再是 Runtime 主体）。
- 无 Mission 概念；旧“Task=单轮”语义已迁移为 Run（兼容别名保留）。

## 5. AgentLoop（真实归属）

- 主循环 = **openai-agents SDK Runner 的内置 loop**（唯一入口 `main.execute_turn→_run_attempt`）。
- 谁控制 iteration：SDK run loop；FORGE 通过 `max_turns`（SSE 钳制 5–100→resolve_budget 取严格值）限制。
- 谁决定继续/结束：SDK（依据模型是否产出 final output）；工具结果回传由 SDK 完成。
- 谁产出 Final Answer：SDK final_output → FORGE ReplyParser。
- **辅助循环**（非主 AgentLoop）：Completion Repair（runner ≤1 次）、输出闸重试（execute_turn 内 1 次）、Provider 每调用 retry（gateway，有界）、no-progress recovery（runner ≤1 次）。旧 loop 无。

## 6. Context Architecture（真实来源合并）

没有独立 ContextBuilder 类；`run_turn` 按固定顺序把来源拼进克隆 Agent 的 instructions：

1. SDK session 历史（compact 后=[摘要]+最近 KEEP 轮；conversation）
2. 默认 system instructions（agent.py 人设，静态）
3. 本次附件清单（attachments_for_run）
4. Project Context 块（instructions/来源清单/最近 10 条项目记忆/工作位置，≤6000 字符）
5. Sources 自动检索段（hybrid→trust.tag 包裹；预算 FORGE_SOURCES_MAX_CONTEXT_CHARS=6000）
6. 当前用户消息（SDK 本轮写入，永不进 compact）

裁剪器：Context Prep(soft/hard/keep) + 检索预算；Memory 只以“项目记忆 ≤10 条注入指令 + remember/recall 工具”进入；无第二套合并逻辑。

## 7. Model / Provider / Routing

- 模型入口：单模型（AGENT_MODEL=agnes-2.5-flash）；route_profile 恒 default（骨架存在未启用）。
- Provider：agent.py build_model_provider → ResilientProvider（openai 兼容网关，chat/completions）。
- 每次模型调用：ResilientModel.get_response 内做 分类→有限 retry→≤1 fallback（fallback 需 FORGE_FALLBACK_* 配置；401 需独立凭据）；流式直接委托（防 token 重放）。
- 错误映射 400/401/403/404/429/5xx/No available channel/timeout → provider_*；runner 收口为友好文案+provider.failure 事件；UI/设置页显示 provider_text。
- 绕过统一入口的直连点（工具内子调用）：compact/research/multimodal/codex_loop/reply_parser(关) —— 记录在案（§17），当前不改。

## 8. ToolRuntime

- Registry：无独立运行时注册中心；**agent.py 静态列表 = 暴露层 = 执行层**（41 个：36 内置+技能工具 5）。registry/broker/spec 仅盘点/展示（PRESENT_BUT_UNUSED）。
- Schema/解析/结果回传：SDK（工具 schema→模型；call→execute→result→模型）。
- 授权链（每个工具调用同一道包装）：Approval(check run_id) → FileScope(authorize_tool) → NetPolicy → 执行（ledger 记录 executed/blocked/error + 输出）。
- 沙箱：code_exec（路径围栏/子进程白名单 env/超时强杀）；project_edit（BASE_DIR 或 wl 写根+备份）；Office 生成 exports。
- 观察归一化：trust.tag（文件/代码/笔记/GitHub/程序输出/Office/RAG/图片出口）。

## 9. Verification / Completion（真实结束流程）

- 无独立 Verification Engine；完成判定链：
  `模型 final_output → ReplyParser(容错) → Completion Gate(声明↔ExecutionEvidence: 写/验证结果/产物/审批) → PASS 才 mark_success`。
- “验证通过”声明要求 exit=0/✅ 通过（非仅“运行过”）；“确保测试通过”意图 + 最近验证失败 → 阻止 success 收尾；文件/产物声明要求真实文件（size>0）。
- Final Response 失败：有执行+验证证据→deterministic 降级 completed；意图验证类无通过记录→failed。
- Repair ≤1（完成/无进展），Provider 异常不进入 repair 语义。

## 10. RunPolicy（timeout/stalled/limits）

| 限制 | 负责人 | 值/来源 |
|---|---|---|
| max_turns | SDK(max_turns)+resolve_budget | 默认20(SSE 5-100) |
| 墙钟 | budget.run_with_wall_limit | RunBudget(显式参数/env TASK_MAX_WALL_SECONDS) |
| 输出闸重试 | execute_turn | 1 次 |
| Completion/No-progress repair | runner | 各 ≤1 次 |
| Provider retry | ResilientModel | 分类型有界(0/≤2) + fallback ≤1 |
| Tool timeout | code_exec | run_python 40/120s+强杀 |
| stalled | **无 stalled 语义**；等价=NO_PROGRESS(连续≥3 相同调用) 有界 | completion.py |
| Crash recovery | recover_stale_tasks | RUNNING+SUBMITTED 超龄→failed(重启钩子) |

## 11. Persistence / Event / SSE

- 落库：agent.db（tasks 容器/runs/messages/task_events/approvals/artifacts/checkpoints/model_calls/tool_calls/source_* + SDK 表外置 sessions.sqlite）；双库 Snapshot/Restore 协议已实现（runtime/snapshot.py）。
- 事件：task_events append-only（含 completion.*/context.*/provider.*/sources.retrieval/reply.degraded）；SSE 事件由 webapp 单生成器 `api_stream` 输出（run.*/task.*/tool/reply/…）；前端仅投影，**后端为状态唯一真相**（前端不推断 running/waiting 等终态语义）。
- 重复保存检查：agent.db messages 与 SDK session 是两套语义（产品记录 vs 模型缓存），非重复；assistant 消息只在 run_turn 各收口分支写一次；事件未发现重复双写。

## 12. Sources / RAG（现状）

上传→复制到 forge_data/projects/{pid}/sources → 后台异步 parse（md/txt/代码/json/yaml/toml/pdf/docx，扫描 PDF 明确 text_unavailable）→ chunk（结构优先）→ FTS5+本地 ONNX 向量（复用 rag 引擎）→ 原子替换索引；删除/容器删除级联清理。检索：RRF 合并+符号精确性 rerank，强制 project_id 过滤，结果带 provenance，进 Context 前 trust.tag；审计事件 sources.retrieval。RAG 不自行调用主模型（无 LLM rerank/改写）。

## 13. Memory（现状）

两层：全局 `memories`（工具 remember/recall；200 条上限，写闸密钥/空壳）与 `project_memories`（记忆范围 project_only/global 由 RunContext 绑定强制隔离，并发不串）。进 Context 方式=指令注入最近 10 条项目记忆 + 工具按需 recall；无自动抽取。Memory 与 Sources 在 Context 汇合点分离。

## 14. Legacy Runtime（分类清单）

- ACTIVE（兼容层，仍被生产调用）：`/api/stream` adapter；legacy 个人会话语境 FileScope（personal/sess-* 整工作区读）；voice/daemon 走 run_turn；`/chat` 旧页面。
- COMPATIBILITY（仅别名/兼容方法）：TaskManager v1 方法名（create_task/list_tasks… 语义=Run）、/api/tasks* run 级旧路径别名、approvals/artifacts 端点沿用 task_id 字段。
- PARTIAL：`runtime/broker.py`、`registry.py`、`spec.py`（盘点/展示；生产执行不经过）。
- TEST ONLY：`evaluate.py`（独立评测脚本，直连 Runner，非产品入口）、`count_checkpoints`、state 迁移测试等。
- DEAD：`WAITING_USER` 状态（仅状态机定义）、前端监听的 paused/cancelled/artifact.created/checkpoint.created 事件（服务端从不发）、`api/mock.js`（调试）、已删死端点（本轮清单外：api_tasks_list/api_task_detail/touch_work_location 等已在早前清理）。
- UNKNOWN/文档性：无新增发现（Mission/WorkPacket/Worker/Subagent/Coordinator 全仓零命中）。

## 15. Runtime Capability Matrix

（状态：COMPLETE=进生产链+测试；PARTIAL=部分；PRESENT_BUT_UNUSED=代码在但主链不调用；LEGACY=兼容；TEST ONLY=仅测试；MISSING=缺）

| 能力 | 状态 | 真实入口 | 真实实现 | 生产链 | 测试 | 重复 |
|---|---|---|---|---|---|---|
| Task/Project 容器 | COMPLETE | webapp/CLI | task_manager tasks 容器 | ✓ | ✓ | 旧表 projects 为 legacy 空表 |
| Message | COMPLETE | run_turn 收口+API | messages 表 | ✓ | ✓ | 与 SDK session 双语义(有意) |
| Run | COMPLETE | run_turn | runs 表/状态机 | ✓ | ✓ | 无 |
| Run lifecycle(start/cancel/pause/resume) | COMPLETE | runner+webapp actions | transition | ✓ | ✓ | 无 |
| Crash recovery | COMPLETE | 启动钩子 | recover_stale_tasks | ✓ | ✓ | 无 |
| Resume(审批/暂停同 Run) | COMPLETE | run_turn(task_id) | 状态机恢复 | ✓ | ✓ | 无 |
| 并发隔离 | COMPLETE | runctx | contextvars+run-keyed | ✓ | ✓ | 无 |
| Agent Loop | COMPLETE | SDK Runner | 唯一 | ✓ | ✓ | 无 |
| Context 组装 | COMPLETE | runner.run_turn | 顺序注入 | ✓ | ✓ | 无独立 Builder 类(职责漂移) |
| Compact/历史有界 | COMPLETE | runtime/context | SQL→分块摘要→hard window | ✓ | ✓ | 无 |
| Token 预算 | PARTIAL | context/env | 字符预算(6000/260k)；非 provider token 级 | ✓ | 部分 | 无 |
| Sources/RAG | COMPLETE | runner自动+工具 | sources/* | ✓ | ✓ | 复用 rag 引擎(共享非重复) |
| Memory | COMPLETE | remember/recall+注入 | agent.db 两表 | ✓ | ✓ | 无 |
| Model Routing | LEGACY/单模型 | router 骨架 | 恒 default | ✓(恒一) | ✓ | 无(骨架未启用) |
| Provider retry/fallback | COMPLETE | ResilientModel | provider_gateway | ✓(非流式) | ✓ | 工具内直连点见 §17 |
| Provider error mapping | COMPLETE | provider_errors/runner | 分类+事件 | ✓ | ✓ | 无 |
| Tool Registry | PARTIAL→PRESENT_BUT_UNUSED | agent.py 列表 | registry/broker 仅展示 | 部分 | ✓ | 展示层与执行层并存 |
| Tool Execution wrapper | COMPLETE | _make_invoke | 门+边界+账本 | ✓ | ✓ | 无 |
| Permission(FileScope) | COMPLETE | 包装层 | authorize_tool | ✓ | ✓ | 工具层旧根校验仍保留(纵深) |
| Approval | COMPLETE | 包装层 | ApprovalGate | ✓ | ✓ | 无 |
| NetPolicy | COMPLETE(Policy层) | 包装层 | netpolicy | ✓ | ✓ | OS enforcement 缺(记录) |
| Trust Boundary | COMPLETE | 各出口 | trust.tag | ✓ | ✓ | RAG/工具均过 |
| Verification 结果语义 | COMPLETE | Completion Gate | exit/通过解析 | ✓ | ✓ | 无独立引擎(门内实现) |
| Repair/No-progress | COMPLETE | runner | ≤1 有界 | ✓ | ✓ | 无 |
| Completion | COMPLETE | runner | Gate+证据 | ✓ | ✓ | 无 |
| Wall timeout | COMPLETE | budget | 显式/env | ✓ | ✓ | env 默认未开(记录) |
| Stalled 定义 | PARTIAL | completion | 无 stalled;NO_PROGRESS 等价 | ✓ | ✓ | — |
| Persistence 双库 | COMPLETE | agent.db+sessions.sqlite | 分离语义 | ✓ | ✓ | Snapshot 协议存在 |
| Audit | COMPLETE | audit+runner | 成功 ingest/失败 backfill/attempts | ✓ | ✓ | model_calls 双来源(成功/失败)非重复 |
| Event/SSE | COMPLETE | api_stream | 单生成器 | ✓ | ✓ | 前端若干死监听(§14) |
| Frontend projection | COMPLETE | runtime.js | reducer | ✓ | ✓ | 无 |

## 16. 重复能力（事实核查）

- **两套 Context Builder：未发现**（单点 runner 组装）。
- **两套 Retry：未发现主链重复**；辅助直连点各自独立重试语义（compact 无重试/fallback 走 hard window；工具内子调用无 retry）——与主 provider 不叠加。
- **两套 Completion：未发现**（唯一 Gate）。
- **两套 Approval/Permission：未发现**（唯一 Gate+FileScope；工具层旧校验为纵深而非第二策略）。
- 存在“双存储语义”：agent.db messages（产品）vs sessions.sqlite（模型缓存）——属设计差异，有 Snapshot/Reset/Delete 联动协议。
- 存在“工具内子调用直连模型×5”（§7）——事实记录，主链入口唯一但辅助入口未统一。

## 17. 职责漂移（只记录）

1. run_turn 承担“Context 组装+预算+检索+门+完成+审计+恢复”等多职责（单函数偏重，但链路内聚）；
2. Completion Gate 内部兼任“验证结果解析器”（无独立 Verification 引擎）；
3. Provider 错误分类在 runner 与 gateway 双处出现（标记统一，展示处冗余）；
4. 工具级旧路径校验(读根/敏感)与 FileScope 并行（纵深，非冲突）；
5. 前端 eventClient 订阅了服务端永不发送的若干事件（§14）。

## 18. Test Results（本节审计实测 + 当日较早真实记录；状态如实）

| 项 | 结果 |
|---|---|
| 全量离线回归 tests/run_tests.py | **PASS：446 OK（skipped=2 主题 CDP）**，109s |
| Test1 普通问答(真实网关,当日 16:17 记录 task_8fd10be2) | PASS(当日)；**本审计时刻网关超时→NOT VERIFIED（记录 provider 不可用）** |
| Test2 Workspace read | 离线工具层 PASS；真机 NOT VERIFIED（同上） |
| Test3 Workspace edit | 离线/历史真机证据 PASS(当日多轮 edit+diff)；本时刻 NOT VERIFIED |
| Test4 edit→test→verify→complete | 历史真机 PASS(finE2E E4)；本时刻 NOT VERIFIED |
| Test5 Source retrieval→answer | 历史真机 PASS(hybrid 事件+grounded 回答)；本时刻 NOT VERIFIED |
| Test6 Source→edit→Verify | 历史真机 PASS(provE2E4 写文件+run_python+审批+验证执行)；模型换参收尾 waiting 属模型行为 |
| Test7 Permission/Approval | PASS（离线全套 + 当日多轮真机审批链记录） |
| Test8 Provider unavailable/fallback | PASS（离线分类/fallback；真机 401/503 分类事件多次记录；fallback 成功路径离线） |
| Test9 真正 stalled | PASS（离线：重复相同调用→NO_PROGRESS→有界 failed；无 stalled 语义） |
| Test10 第二条 Message→新 Run | PASS（离线 test_tmr 多条；真机历史记录容器多 Run；本时刻由 DB 结构证据支持） |

DB 事件取证（本审计时刻前后）：provider.failure(kind=auth) 与 provider.model_attempts 事件真实落库（task_b66d5aa1/6522be10）；用户长会话（检验科 PPT）多次经历网关 401/超时，运行时均安全收口（failed/等待），一条取消源于客户端断连（cancelled 语义正确）。

## 19. Runtime Completion Assessment

- **核心主链完成度**：高（单链、终态语义真实、结果由证据决定；完成判定已升级为“声明↔证据”）
- **稳定性完成度**：中高（有界重试/恢复/隔离/审计齐全；剩余主要波动来自外部网关可用性与模型行为）
- **安全完成度**：高（FileScope/Approval/Trust/NetPolicy(策略层)/脱敏/隔离均已四层验证；OS 级网络 enforcement 属外部任务）
- **上下文完成度**：高（compact+hard window+预算+检索预算均生效）
- **模型层完成度**：中高（单模型、分类/重试/fallback 完成；多模型路由属未启用骨架；辅助工具直连点未统一）
- **工具层完成度**：高（注册=暴露=执行；包装层门齐全；Registry/Broker 展示层未用）
- **验证完成度**：中高（结果语义在 Gate 内实现，无独立引擎）
- **持久化完成度**：高（双库+快照/恢复+删除/Reset 联动）
- **恢复完成度**：中（重启 recover+审批持久化+compact 持久化；无精确断点续跑）
- **并发完成度**：中高（状态隔离已验证；真实多 run 压力与混合审批交错为补强项）
- **产品化完成度**：中高（UI 状态投影、技能体系、上传索引流程可用；仍有模型层体验与网关联动波动）

## 20. 下一阶段建议（仅分级，不改）

- **P0**：无主链级错误发现。
- **P1**：① 辅助工具直连模型点（compact/research/multimodal/codex_loop）缺少统一 Provider 分类/有限重试（网关故障时会直接抛原始错误进入对应工具路径）——建议后续统一接入 provider_errors；② 真实并发多 Run（混合审批/FileScope/紧凑窗口）端到端压力矩阵补测。
- **P2**：前端死事件订阅清理；展示层 Registry/Broker/spec 与执行层关系显式化或收敛；WAITING_USER 死状态清理评估；无独立 Verification 引擎的职责漂移整理；流式模式 attempt 记录为 0 的语义补强。
- **P3**：文档/README 与 41 工具数量同步；技能资产版本与更新策略说明。

## 审计事实底线

- 本审计未修改任何生产代码（仅清理审计自身产生的临时项目与 pre_restore 残留）。
- 446 项离线全绿为本审计主证据；模型相关真机结论以“当日较早同代码记录 + 本时刻网关不可用 NOT VERIFIED”如实标注，不据代码声称通过。
