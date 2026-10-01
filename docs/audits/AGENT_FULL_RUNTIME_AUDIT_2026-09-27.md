# Agent Runtime 全面检查（1–10）

日期：2026-09-27
范围：当前工作区源码、配置及 `var/state/agent.db` 中两条用户提供问题对应的只读 Run 记录。
方式：静态代码审阅与只读事件核对；未启动 Agent、未调用外部服务、未运行测试。

## 总结

已发现并定位两个真实运行故障：能力盘点问题重复调用 `extension_manager` 后失败；另一条能力盘点请求实际调用天气工具并回答天气。它们说明“能力清单意图识别”和“无业务工具执行”之间曾缺少可靠隔离。当前工作区代码已加入 capability context-only 路径，并有 TUI 去重修复，但本次没有动态验证这些改动。

最高优先级：将能力盘点修复做生产路径验收；把多阶段计划从提示词升级为可观察、可验证的执行约束；复现并验收 TUI 的流式/失败渲染。P2 项为 Router p95 指标误算。

## 逐项结果

| # | 检查面 | 结果 | 结论 |
|---|---|---|---|
| 1 | 用户意图与对话上下文 | **P1：真实故障；修复待验收** | `task_2ce76fc6` 对“你现在有哪些插件工具”重复调用 `extension_manager(action=list)` 3 次后，被 convergence 拦截并以 `no_progress` 失败。`task_fbdf0a46` 对“查询不了你有什么插件吗？”调用 `extension_manager` 后又调用 `get_current_datetime`、`get_weather(city=霸州)`，最终回答天气。当前代码以能力词和问句形态联合识别，并在 Runner 事实块路径禁用工具、限制历史到 2 条；尚未运行确认不再误调用。正则规则仍可能漏掉口语变体。 |
| 2 | Layer / Router 职责 | **P2：职责已可辨，但能力有限** | Laya 只在未命中强工具信号时参与高置信度 `direct_text` 短路；工具子集选择仍由 `select_tool_names` 负责。Laya route head 尚未训练，不能据此称为候选工具路由器。routing trace 记录 Layer 判定、路由模式、允许工具和 schema hash。 |
| 3 | 多能力、多阶段流程 | **P1：有计划记录，缺 Runtime 强制编排证据** | `infer_capability_plan` 覆盖外部检索、读文件、计算、写入、记忆和提醒等有限动作；Runner 将阶段依赖写入提示词和 `routing.decision` trace。未发现 Runtime 阶段状态机强制前置阶段成功、将结果绑定到下一阶段、或失败即停止。计划能提示模型，不能证明执行完所有阶段。 |
| 4 | 循环、重试、恢复 | **P1：重试边界、不确定结果和无进展答复已加固；真实入口仍待验收** | convergence/no-progress 会终止无进展循环。Broker 重试要求 ToolSpec 幂等；MCP 默认为单次调用，远端网络结果不确定时进入 unknown 并要求核对。降级答复按是否有执行证据区分“未完成”和“部分执行”，无证据时不再声称操作已执行。当前没有真实 MCP/provider 中断演练。 |
| 5 | 状态、执行证据、副作用可靠性 | **P1：关键状态与证据路径已加固；故障注入仍有限** | Run 状态事件随状态转移在同一事务写入；`run.terminal` 快照需匹配当前数据库状态。多阶段完成证据或 WAL 收口失败时暂停后续阶段。尚未对数据库磁盘满、损坏或进程硬杀进行故障注入。 |
| 6 | TUI / CLI 渲染 | **代码有防重；运行验收待做** | TUI 已将流式输出与最终 RunResult 设为互斥答复来源；失败事件只改变状态，最终错误由结果渲染一次。与用户贴出的重复答复和双 ERROR 对应，但本次没有启动 TUI 检查事件时序。 |
| 7 | 审批、安全与副作用 | **审批关闭；其他保护独立存在** | 当前 `.env` 的 `APPROVAL=off` 使 ApprovalGate 不再拦截其配置的审批工具。这符合此前关闭审批的要求。未知工具的 ToolSpec 默认有副作用、非幂等；Runner 的路径/执行边界、写前日志、预算等不是 ApprovalGate 本身，不能把审批关闭描述成关闭所有保护。需确认每个外部 MCP provider 的策略与工具级审批映射。 |
| 8 | Memory / Session / History 隔离 | **P1：主体有隔离，但存在清理与进程级 fallback 缺口** | Session 历史和产品消息分库/分层，项目记忆作用域在 RunContext 正常绑定时隔离；全局记忆跨会话是设计行为。`main.clear_session()` 只删 SDK 的 session 行，而 SQLiteSession 建表未启用外键级联；消息行可能残留并在同 session key 重建后重新读入。记忆作用域另有进程级 fallback，`recall_memory` 重复提示状态也为进程级共享。 |
| 9 | 工具契约与插件扩展 | **契约有保守默认与 MCP 幂等透传；召回覆盖仍有风险** | ToolSpec 描述风险、副作用、破坏性、幂等和来源；MCP 的幂等配置传入运行时目录。Router 动作规则仍以部分关键词/别名覆盖工具族，目录与语义规则漂移仍可能造成漏召回。能力事实块应再核验启用、本地可用、MCP 已配置未连接等状态。 |
| 10 | Trace、指标、成本与配置 | **P1/P2：token 可追踪，货币成本、延迟和隐私口径不完整** | 每次模型调用的 token 写入 `model_calls`；但 `cost_usd` 没有计算路径，`model_calls.latency_ms` 在 AuditCollector 中未传值。`FORGE_TRACE` opt-in 的本地 span 会写未脱敏工具 input/output preview。Prometheus 的 P95 是 query 长度而非延迟，字段名称 bytes 但实现按字符计数且 rank 偏一；JSONL 聚合每次全文件扫描，没有轮转策略。 |

## 证据位置

- 意图识别与能力事实：[`runtime/capability_introspection.py`](../../runtime/capability_introspection.py:146)
- Router 动作计划和 p95 指标：[`runtime/tool_router.py`](../../runtime/tool_router.py:65)
- Layer 选择与 trace：[`runtime/laya_router.py`](../../runtime/laya_router.py:19)、[`runtime/runner.py`](../../runtime/runner.py:1758)
- 完成证据门：[`runtime/completion.py`](../../runtime/completion.py:848)、[`runtime/terminalization.py`](../../runtime/terminalization.py:121)
- 审批和工具元数据：[`runtime/approval.py`](../../runtime/approval.py:150)、[`runtime/spec.py`](../../runtime/spec.py:216)
- 会话上下文与路径：[`runtime/context.py`](../../runtime/context.py:31)、[`runtime_paths.py`](../../runtime_paths.py:46)
- TUI 去重和失败显示：[`cli/tui/app.py`](../../cli/tui/app.py:1226)

## 建议验收顺序

1. 对插件/工具清单、工具能力解释、普通天气、天气加保存、多能力组合分别运行真实生产入口；确认路由事件、允许工具集合、工具调用和最终回答一致。
2. 为多阶段请求记录逐阶段的 started/succeeded/failed、输入输出摘要和依赖；确认阶段失败不会继续后续副作用阶段。
3. 在 TUI 验证流式成功、非流式成功、Run 失败、工具失败、审批暂停/恢复，逐项确认答案与错误只出现一次。
4. 修正 Prometheus `p95_query_len_bytes` 的 bytes/字符单位及 nearest-rank 索引；单独补采模型调用与 Run 端到端延迟，再与 benchmark 对齐。
5. 用并发多 session 和恢复场景检查历史、global Memory、WAL unknown、幂等重放及事件关联；验证 `--clear-session` 后同名 session 不会恢复旧消息。

## 限制

这是一份源码与两条真实 Run 记录相结合的审计，不构成当前未提交工作区的生产验收。所有“修复已存在”的结论都指代码路径，不代表已通过真实运行验证。未检查外部服务可用性、供应商 SLA、生产模型行为或完整 benchmark。

## 优先项修复跟进（2026-09-27）

根据后续授权，本轮已落实并验收前三项：

1. **能力查询隔离**：能力查询固定走 Runtime 事实块和零业务工具 Agent；fake 模型经完整 run_turn 的集成测试确认 routing.decision.allowed_tools 为空。
2. **多阶段 Runtime 编排**：动作计划按用户表达顺序排序；RunContext 只允许执行当前阶段工具，后续阶段抢跑会记录 capability.phase.blocked。成功、失败和不确定结果分别落事件；不确定阶段恢复时转为等待用户核实；未完成阶段使普通 answer/done 无法通过 Completion Gate。save_note 以落盘文件存在作为成功证据。
3. **p95 口径**：改用 nearest-rank percentile，公式为 ceil(0.95 × N) - 1，并保留最近 1000 个延迟样本。

验证命令：.venv/Scripts/python.exe -m pytest tests/test_tool_router.py tests/test_completion_gate.py tests/test_production_closure.py -q
结果：**137 passed, 2 subtests passed**。

这组验收不调用真实天气、搜索、记忆或文件写入服务；生产模型对多阶段提示的行为尚未做在线验收。Runtime 顺序门和 Completion Gate 已由 fake 模型及真实工具包装器集成测试验证。

## Runtime / Agent Systems 后续加固（2026-09-27）

本轮继续处理 2、4、5、9：

1. **Layer / Router 职责边界**：路由事件增加 `layer.authority=coarse_intent_only`、`router_authority` 和 `router_candidates`，明确 Layer 的直接回答快筛不代表具体工具选择，具体候选由 Router 产生。Laya route head 仍未训练，因此没有让它输出或裁剪具体工具；候选工具路由模型本身仍是后续工作。
2. **重试 / 恢复**：`ToolBroker.execute_resilient` 现在只接受 ToolSpec 标记为幂等的工具，每次 attempt 使用独立 invocation id。MCP 工具默认单次调用；只有服务器配置显式把远端工具列入 `idempotent_tools` 才启用网络重试。allow/approval 授权不再被误当成可安全重试。MCP 网络失败会返回 `MCP_OUTCOME_UNKNOWN`，Runner 将副作用 WAL 和多阶段结果收为 unknown 并要求核对，不再误记成普通失败或成功。无进展降级答复根据真实执行证据区分部分执行与零执行。
3. **状态与执行证据**：`run.terminal` 通过状态校验事务写入；如果取证期间 Run 状态变化，终态快照不会落入与当前状态冲突的记录。副作用 WAL 收口失败会让对应多阶段步骤转为 uncertain、阻止后续阶段并要求核实；阶段完成事件写入失败也会回滚内存阶段进度并 fail-closed。既有 `task.<state>` 状态变更事件仍在状态事务内提交。
4. **工具契约 / 扩展**：MCP 的幂等声明从配置传递到 ToolSpec；工具目录提供幂等字段。未显式声明的扩展仍采用 side_effect=true、idempotent=false 的保守默认值。

验证命令：`$env:APPROVAL='on'; .venv/Scripts/python.exe -m pytest tests/test_runtime.py tests/test_mcp_bridge.py tests/test_mcp_bridge_policy.py tests/test_resilience_token_gate.py tests/test_laya_router.py tests/test_tool_router.py tests/test_completion_gate.py tests/test_production_closure.py tests/test_api_layer_phase_a.py tests/test_api_layer_phase_b.py tests/test_readiness_closure.py tests/test_budget_convergence_guard.py -q`
结果：**316 passed，2 subtests passed**。

限制：未连接真实 MCP 服务，也未做生产模型/真实 Laya 权重验证。Layer 到具体候选工具的模型路由尚未实现；本轮落实的是可审计职责边界与保守回退，不能把它描述为 Laya 已承担工具路由。

## 第 8、10 项专项复核（2026-09-27）

### 8. Memory / Session / History 隔离

**已有保护**

- SDK 对话历史按 `session_id` 查询；产品消息以 `container_id` 保存，并用 `run_id` 关联单次执行。项目容器通过 `session_id` 复用，Run/工具调用/模型调用明细则以 Run id 归属。
- `RunContext` 是 `contextvars.ContextVar`；`tools._active_project()` 优先从当前 RunContext 读取 `container_id + memory_scope`。`project_only` 检索只读取当前项目记忆，不读 user/global 记忆；项目全局模式也只合并 global 和当前项目的记忆。
- Session compact/window 操作针对单一 SDK Session；摘要输入排除项目上下文与 Memory，并有同 Session 压缩锁及工具调用/结果成对裁剪测试。

**发现**

1. **[P1] 命令行 `--clear-session` 可能只删 session 元数据，未删除历史消息。** `main.clear_session()` 执行 `DELETE FROM agent_sessions`，但没有删除 `agent_messages`。SDK 表定义虽写了外键，但 SQLite 默认不启用外键，SDK 连接初始化代码也没有设置 `PRAGMA foreign_keys=ON`；`SQLiteSession.get_items()` 仍按 `session_id` 查询 message 表。重新打开同名 session 时，旧对话可能重新出现。CLI `/clear` 使用 SDK `clear_session()`，它会显式删除两张表，因此该问题集中在 `main.py --clear-session` 入口。
2. **[P1/P2] 记忆作用域仍有进程级回退路径。** `tools.py` 的 `_MEMORY_BINDING` 是可变全局；仅在当前 RunContext 取不到绑定时才回退使用。Runner 在设置 RunContext 前设置此全局绑定，RunContext 构建又包在宽泛异常捕获里。正常路径以 contextvar 隔离，但构建失败/非 Run 工具调用/并发 fallback 时可能读到另一个 Run 的作用域。
3. **[P2] `recall_memory` 的重复调用状态跨 Run/Session 共享。** `_last_recall_call` 按进程存最近关键词、时间和空结果；并发会话可能相互触发“已重复检索”的提示。它不会直接把另一会话记忆作为结果返回，但会污染当前模型决策。
4. **[P2] RunContext 在 Runner 中设置后未见常规收尾 reset。** `bind()` 只是对当前 asyncio Task 设置 contextvar；Runner 主执行路径绑定新对象，但除审批恢复局部逻辑外未发现 Run 完成时 reset。长期存活的调用 Task 在后续非 Run 代码或下一 Run 绑定失败时可能保留旧执行上下文。新 Run 正常绑定时会覆盖，因此属于异常/边缘路径隔离风险。
5. `TaskManager.delete_container()` 删除产品 DB 中项目历史和项目记忆，但不调用 `runtime.context.delete_session_history()`；如果该删除入口未来被生产 UI/API 使用，SDK history 会变成 orphan。当前全仓 Python 调用检索只发现测试调用该硬删除方法，尚未定位到产品入口。

### 10. Trace、指标、成本与配置

**已有保护**

- 路由耗时 P95 已在 `runtime/tool_router.py` 按最近最多 1000 个样本使用 nearest-rank 计算；模型调用 token 按 response 写入 `model_calls`，Run trace/event 以 `run_id` 关联。Trace 默认关闭，需 `FORGE_TRACE=1` 或 CLI `--trace` 开启。
- Runtime 输出路径集中在 `runtime_paths.py`，日志、trace、状态库和临时产物均有独立目录。

**发现**

1. **[P1，隐私] opt-in 本地 trace 会落未脱敏的工具输入/输出片段。** `runtime.observability._preview()` 只压缩空白并截断；Function span 的 `export.input/output` 被写入 `local.io.preview.*`。这和 AuditCollector 的 secrets redaction 不同。若工具参数或结果含邮箱、路径、个人资料或 token，`var/traces/traces.jsonl` 可能保存明文。Trace 文件还有 session group 标识。
2. **[P1，成本可见性] 系统没有真实美元成本计算。** `TaskUsage.cost_usd` 字段存在，但全仓未发现定价表/估算器；`model_calls` 只有 input/output token。Token budget 是停止阈值，不是成本金额。无法从当前数据可靠比较不同模型、Run 或项目的美元成本。
3. **[P2，性能观测] 单次模型调用延迟目前没有写入 `model_calls.latency_ms`。** `AuditCollector.ingest()` 插入模型调用时不传 latency；provider-attempt 有 HTTP 尝试延迟，但不是模型响应/端到端 Run 延迟。工具调用耗时另有记录，不能代替模型及 Run 的 P95。
4. **[P2，指标口径] Prometheus 暴露的 `forge_router_p95_query_len_bytes` 名称含 bytes，实际上使用 `len(query)` 字符数；`int(0.95*N)` 对应顺序统计的偏高一位（与 nearest-rank 约定不一致）。该指标是查询长度 P95，不是路由耗时 P95。`runtime/tool_router.py` 中的 router latency P95 已修正，两者应明确区分。
5. **[P2，存储/性能] `tool_router.jsonl` 和 `traces.jsonl` 是持续 append 文件，没有轮转/保留上限。** Prometheus server 每次抓取会从头扫描整个 Router JSONL，再筛最近 24 小时；运行时间越久，磁盘与每次 scrape 的 I/O 开销越大。Provider attempt 文件另有限行上限，但也依赖清理。
6. **[P2，审计耐久性] Provider attempt 文件在数据库回灌前被删除。** `take_attempts()` 读取 JSONL 后调用 `_persist_reset()` 删除源文件；Runner 随后逐条写 `provider_attempts`，异常被吞掉。数据库暂时不可写时，回灌失败的 attempt 可能同时失去文件与数据库副本。

复核基线：静态检查及已有测试源码审阅；后续修复记录如下。

### 修复进展（2026-09-27）

**第 8 项：**

- `main.clear_session()` 现在在同一事务中显式删除 `agent_messages` 与 `agent_sessions`，不再依赖 SQLite 外键开关。
- Memory fallback 与 `recall_memory` 连续调用状态改为 `ContextVar` task-local 状态；进程级 `_MEMORY_BINDING` 仅保留兼容镜像，运行时不再读取它。
- `RunContext.bind()` 返回 ContextVar token；Runner 在 `_close_run()` 恢复绑定前上下文，包括异常和等待审批的收尾路径。
- 硬删除容器及清空历史前，在 `agent.db.session_cleanup_queue` 写入清理意图；SDK Session 库失败时保留错误、次数与操作类型，启动时及重复操作时重试，并可通过 `TaskManager.list_pending_session_cleanups()` 盘点。

**第 10 项：**

- 本地 trace 不写工具输入/输出正文，只记录字符长度；Session/group id 不进入 trace。错误预览执行 secret 与常见 PII 脱敏，其他 detail span 只保留类型。
- 新增 Agnes 已识别模型的官方列表价快照估算（`agnes-list-price-2026-09-27`；来源：[Agnes AI 定价页](https://www.agnes-ai.com/zh-Hans/docs/pricing)），仍可由 `FORGE_MODEL_PRICING` 覆盖，逐次保存 `cost_usd`、`pricing_version`、`currency`；未知模型/提供方仍为 NULL，促销与账户折扣不计入快照估算。
- provider Gateway 每个成功模型响应的完整请求延迟与 `raw_responses` 按顺序关联后写入 `model_calls.latency_ms`。该值包含流式响应完成时间，不代表首 token 延迟；无法关联的行保持 NULL。
- `forge_router_p95_query_len_bytes` 和 JSON 报告改为 UTF-8 字节数及 nearest-rank P95，修正单位和样本秩口径。
- Router 与 trace JSONL 默认每个文件 10 MB、保留 3 个轮转副本；指标服务读取当前文件与副本，trace 导出也扫描轮转文件。配置项分别为 `FORGE_ROUTER_LOG_MAX_BYTES`、`FORGE_TRACE_LOG_MAX_BYTES`。
- `_close_run()` 记录 Run 首次 started_at 到本次收尾时刻的墙钟时长到有界 `run_metrics` 表（保留最近 20,000 个 Run）；等待审批/用户输入的挂起时间包含在墙钟口径中，恢复后的同一 Run 会更新该样本。Prometheus 提供端到端 Run P50/P95 与样本数，不依赖会轮转的 Router 原始日志。
- Provider attempt 先读后写 DB，所有事件与记录落库成功后才 ack 并删除 JSONL；记录增加 `attempt_id`，重复回灌具备幂等去重。数据库写入失败时保留源 JSONL 以供重试。

### 后续修复（2026-09-27）

- **第 2 项：** `FORGE_LAYA_TOOL_ROUTE_CHECKPOINT` 提供独立多类工具候选路由入口，置信度门控后只能从 Tool Router 已准入候选中收窄；多阶段或多个必需工具请求跳过收窄。现有仓库权重是 tool/text 二分类，不能冒充工具路由权重。未部署该独立训练 checkpoint 时继续走原 Tool Router；训练与独立验收仍是上线前置条件。
- **第 8 项：** Session 清理错误不再吞掉；删除/清空意图先进入持久队列，失败可观察、可重试。
- **第 9 项：** Router 对未登记的新增工具增加目录描述候选召回兜底；能力清单把当前真实注册、启用、MCP 连接及策略状态分开显示，Skill “启用”与工具注册数也不再合并表述。关键词语义召回仍应由持续独立 benchmark 校验。
- **第 10 项：** 已识别 Agnes 模型可以默认按带版本的列表价快照估算，完整 Run 时延及 nearest-rank P50/P95 落在有界数据库样本中。原始 JSONL 仍按保留策略删除；更早的详细查询记录不保证存在，但 Run latency 汇总样本不随 Router 日志轮转丢失。

### 2026-10-01 复核修订

- Agnes 列表价快照不再作为默认成本来源：当前没有在此部署核对账户费率与账单，故移除默认费率表；只有显式配置带版本的 `FORGE_MODEL_PRICING` 才保存估算金额。未配置金额为未知。
- Run P50/P95 仍来自最多保留 20,000 个生产 Run 样本；这是有界近期窗口，不代表长期基线。无样本时 Prometheus 输出 `NaN` 与样本数 0，避免误报 0ms。
- 其余六项修复及生产验收边界见[2026-10-01 修复记录](OPEN_AGENT_ISSUES_REMEDIATION_2026-10-01.md)和[验收记录](../acceptance/OPEN_AGENT_ISSUES_ACCEPTANCE_2026-10-01.md)。

静态语法检查通过（`py_compile`）；未运行测试、未启动 Agent，也未连接真实模型/MCP。成本金额只有在部署者配置了匹配模型的当前价格后才可用；模型调用延迟采用网关成功尝试耗时，不是完整用户 Run 延迟。

### Laya 权限边界更新（2026-09-27）

- 生产 `route_agent()` 不再因 Laya 的 `direct_text` 判断而返回零工具 Agent；Laya 二分类只作为 `text_likely` / `tool_likely` trace hint。纯文本请求是否暴露工具仍由 Tool Router 自己判定。
- 可选的 Laya 多类候选头只对已被 Tool Router 选中的单阶段候选调整顺序，不再收窄或扩展工具集合；多阶段计划不使用该排序提示。
- 当前未配置 `FORGE_LAYA_TOOL_ROUTE_CHECKPOINT`，所以多类候选排序提示仍未启用。Tool Router 仍是唯一工具候选集合裁决者，Runner 执行门仍是实际执行授权者。
- 该变更通过静态语法和 diff 检查；本轮未运行测试或真实模型验收。

### Task Plan / Tool Router 职责拆分（2026-09-27）

- 显式阶段规则与 `infer_task_plan()` 已移入 `runtime/task_plan.py`；Runner 从 Runtime 计划模块取得 TaskPlan，并继续交给 RunContext 跟踪进度。
- `select_tool_names()` 不再从用户文本推导阶段，只接收 Runtime 提供的 `plan_required_tools`，执行已注册性、当前意图策略和候选数量检查。
- `runtime.tool_router.infer_capability_plan()` 暂留为惰性兼容入口；生产 Runtime 不再调用该入口。
- 该拆分通过静态语法和 diff 检查；本轮未运行测试或动态 Agent 验收。
