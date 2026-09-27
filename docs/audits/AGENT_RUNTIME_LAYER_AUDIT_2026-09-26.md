# Agent Runtime + Layer 全面审计报告

**审计日期：** 2026-09-26（Asia/Shanghai）
**范围：** 当前工作区源码、`.env`、Runtime、Layer/Laya、Tool Router、日志、测试、既有执行记录。
**方式：** 只读审计与本地路由观测；没有修改程序代码，没有触发外部模型对话或工具副作用。
**证据原则：** 当前源码及实际观测优先；既有设计和审计文档只用于定位，不直接作为实现证据。

## 执行摘要

1. 生产主 Runtime 是 `AgentRuntime.run_turn → route_agent → main.execute_turn → Agents SDK Runner → 工具包装器/门 → CompletionGate → Task terminalization`。`runtime.pipeline.run_pipeline` 并未接入此主链。
2. 主链里确实有 Laya：`route_agent` 对部分请求调用 Laya 二分类 `screen()`。高置信 `direct_text` 会清空该轮 Agent 工具；其它结果都回退静态关键词 Tool Router。Laya 三判别 `classify()`、Orchestrator 和 LoopGate 只在显式 pipeline 路径可调用，`FORGE_ORCHESTRATOR` 当前未开启。
3. Tool Router 将选中的工具子集注入克隆 Agent，因而能实际缩小模型候选工具；但 Laya 本身没有决定 capability/tool 集合。Router 仍保留全部基础工具，并在会话连续“零目标命中”时可能升级为全量工具。
4. 当前样本显示 Laya 高置信结果有明显语义错判，但主链的关键词护栏和 Tool Router 对部分请求进行了补救。例如“记住我喜欢喝茶”被 Laya 判为 greeting，却因命中 `laya_screen_skip` 而交给 Router；“搜索最新 AI 新闻”被 Laya 判 greeting（置信度不足）但 Router 仍放行搜索工具。
5. 当前主架构有确定性执行/完成门闭环，但多处策略重叠：Laya、Tool Router、Action intent/readiness、ApprovalGate、CompletionGate、收敛控制均可能限制或推动执行。独立七阶段 pipeline 则只是部分接线的实验/评测路径，不能据其模块存在断言闭环已投入生产。
6. 路由 JSONL 记录 query 和 selected 工具，但没有 Laya 决策、confidence、过滤原因、task/run/step 关联字段；故无法从日志追溯某一工具“为什么被 Layer 允许”。

## 1. 当前真实 Runtime 架构与调用链

```text
CLI / ChatApp / scheduled / 部分 benchmark
  → AgentRuntime.run_turn(message, session/task metadata)
  → 建立/恢复 Task 与 Run、记录 User Message、解析预算/模型 profile
  → route_agent(message)
      → 模型档位 Agent 克隆
      → 能力盘点特例可返回全量工具
      → 若 TOOL_ROUTER 关闭：返回全量 Agent
      → 否则：部分输入调用 Laya screen 二分类
          → 高置信 direct_text：clone(tools=[])
          → 其它/异常/低置信：静态 select_tool_names 关键词筛选
      → 按所选名称 clone(tools=subset)，C2 连续零目标命中可回退全量
  → main.execute_turn
      → ensure_mcp / 构造 RunConfig
      → _run_attempt → Runner.run_sync / run_streamed / run
      → SDK 根据当前 Agent.tools 向模型提供工具，并在同一 Runner loop 消费工具结果、继续模型调用
      → 工具由 _patch_agent_tools 包装，依次经过用户输入/约束/必需字段/intent/readiness/预算/收敛/审批等 gate
      → 工具执行与观测写入 ledger / task events / tool_calls
  → CompletionGate 与 obligations / verification 证据检查
  → _succeed / _succeed_waiting_user / _fail / approval/cancel 收口
  → Task 状态、assistant message、terminal event、checkpoint/persistence
```

关键源码：`runtime/runner.py::AgentRuntime.run_turn`、`route_agent`、`_patch_agent_tools`、`_succeed`、`_fail`；`main.py::execute_turn`、`_run_attempt`。不同的 sync/async/stream 形态都汇入 `execute_turn` 的 Runner 三模式；stream 是同一个 Agent/Run 主体，不是单独的 Layer 链。Approval resume 通过 `run_turn(task_id=...)` 恢复既有 Run，并执行已批准未执行 invocation。

### 名义管线与主链的差别

`runtime/pipeline.py::run_pipeline` 提供 Laya classify → `orchestrator.decide` → 可注入 completer/executor/comparer → LoopGate 的独立路径。但 `route_agent` 不调用它，`run_turn` 不调用它；`.env` 未配置 `FORGE_ORCHESTRATOR=on`，`orchestrator_enabled()` 默认 false。该 pipeline 的工具执行器是调用方注入回调，并非生产 Tool Registry/`_patch_agent_tools`；其默认没有 executor 时，仍可能将无结果步骤判成 completed。它不能视为生产主 Runtime。

## 2. Layer/Laya 真实职责、输入输出与位置

### 主链里实际运行的 Laya

- **输入：** `route_agent(message)` 收到的本轮 `message` 字符串；未传完整 conversation、history、memory、RunContext、previous tool results 或 task state。
- **位置：** Task/Run 已建立且预算/模型 profile 已解析之后，Runner/Planner 调用之前；执行一次路由入口决策，不在 Runner 每个 tool step 后重新分类。
- **协议：** `LayaRouter.screen(query)` 调判别器 `predict`，问题为 `needs_tool: {text, tool}`，读取 choice 与置信度。env 阈值默认 0.85，`.env` 实际值为 0.85。
- **允许的输出效果：** 仅当没有命中 `laya_screen_skip(message)` 且 Laya 高置信输出 `direct_text` 时，返回 `chosen.clone(tools=[])`。`tool_needed`、低置信、异常、缺字段均不直接路由具体能力；交给关键词 Router。
- **真实消费方：** `route_agent` 消费 `screen` 的字符串结果。Planner 不接收 `LayaDecision`；SDK 只看到克隆 Agent 的工具集。
- **置信度：** 二分类阈值实质只影响 direct_text/tool_needed 返回，但 `tool_needed` 结果没有能力/工具映射，也不限制候选集；主链只有 direct_text 短路是硬效果。

### `classify()` 三判别与 Orchestrator

`LayaRouter.classify()` 输出 `intent + intent_conf + completeness + completeness_conf + route + route_conf`；route 目前恒为 `None`（代码注释说明 route head 尚未训练）。`orchestrator.decide()` 接受 intent/confidence 等字段，但实现对 greeting/arith 不检查 confidence，对多数 tool-needed 情形也不将 intent_conf/completeness_conf 用作硬阈值。`classify()` 的异常/模型缺失给全 None；`decide()` 对 None 回 `llm_fallback`。但该协议当前不是主链的 Layer→Planner 协议。

### Layer 性质结论

**Laya 不是生产中的 Runtime Control Layer。** 它是部分请求上的二分类硬短路器，加上不改变正常路由的软分类结果；主链可称作“Laya 快筛 + 静态 capability/tool 候选筛选”。真正改变模型候选空间的是 `select_tool_names`，不是三判别 Layer。对 direct_text，Laya 的效果是硬约束；对其余判断，它基本是提示/旁路结果或被跳过。

## 3. Layer / Tool Router 行为样本

下表来自本地当前权重真实推理及 `AgentRuntime.route_agent()` 工具集合观测。中文因当前 PowerShell/Python stdin 编码配置被控制台显示为乱码，但模型和路由收到的是对应中文输入；报告在表中以语义转述。该次运行加载了本地 checkpoint，日志显示权重加载完成；未发起对话模型或工具调用。实际能力集合受 `laya_screen_skip` 影响。

| 用户输入（语义） | Laya intent / confidence | 屏蔽快筛 | route_agent 实际工具暴露 | 说明 |
|---|---|---:|---|---|
| 普通问候“你好，最近怎么样” | ambiguous / 0.9999（三判别观测） | 否 | 空工具 | 静态 Router 的 direct text 规则裁空；不是依靠该三判别输出 |
| 北京现在天气 | ambiguous / 1.0 | 是 | datetime、文件/列表/计算基础工具 + web_search、get_weather、search_documents | Layer 被强工具信号护栏跳过，Router 多放了通用搜索/文档工具 |
| 搜索最新 AI 新闻 | greeting / 0.8123 | 是 | 同上含 web_search | Layer 分类错误，置信度低；Router 补救 |
| 读取 README 并总结 | greeting / 0.3271 | 是 | 基础（含 `read_workspace_file`）+ search_documents、read_code_file、list_code_files | Laya 不决策；Router 暴露了文件读取工具，也额外开放代码读取/列表候选 |
| 修改 README 第一段 | greeting / 0.9949 | 否（强信号正则未命中） | 基础 + edit/index/search/code/run/test/write 等一组工具 | 高置信 direct_text 被跳过，Router 放行；多个同族候选增加 Planner 自由度 |
| 运行 Python `print(1+1)` | arith / 1.0 | 是 | 基础 + run_python、index/search、写/读代码、测试/表格等 | Layer 语义错判但被跳过；静态 Router 把算术和代码执行混在一起暴露大量候选 |
| 记住我喜欢喝茶 | greeting / 0.4627 | 否 | 基础 + remember、save_note | Layer 被跳过，Router 补到记忆写入能力 |
| 回忆我喜欢什么 | ambiguous / 0.9045 | 否 | 0 工具 | Layer 不影响，静态关键词语义没有为这个表达放入 recall 工具；是 Memory retrieval FN 风险样本 |
| 明天上午9点提醒我开会 | greeting / 0.9985 | 是 | 基础 + schedule_add | Layer 错判/高置信但强信号保护使工具可见 |
| 搜索资料、整理、写文件、设提醒 | greeting / 0.6871 | 是 | 基础 + web_search、schedule_add、get_weather、search_documents | 多能力表达丢失文件写入工具；整体是 tool-family 关键词补齐不足实例 |
| “文件内容是什么？”（缺具体文件） | ambiguous / 1.0 | 否 | 0 工具 | 静态路径直接回答无候选；存在应追问文件目标却无法进入读取/澄清分支的风险 |
| 删除旧提醒（未说明具体对象） | ambiguous / 0.4935 | 是 | 仅基础工具，无 schedule_remove | Router 未暴露删除工具；后续可能只文字回应，具体结果依赖工具 intent/readiness gate，未在本审计中触发模型执行 |

**解释限制：** classify() 的这组实际权重探针仅 12 条，且不构成带 gold label 的 benchmark。因此不能据此推算 Precision/Recall/F1，也不能把 Laya 二分类准确率等同于最终 routing accuracy。现有 `.env` 注释声称 500 条样本、98.7% accuracy、tool F1=0.985、FP=0，但本仓未找到对应 eval manifest/`test_final.jsonl`/输出报告；这些数字尚不可由当前仓库证实。当前可证实的是上述错判和主链护栏后的实际工具集合。

## 4. Layer 能力矩阵与责任边界

| 决策 | 当前主要责任者 | 观察到的边界/重叠 |
|---|---|---|
| 是否需要工具 | Laya screen（仅高置信 direct_text 短路）；Tool Router 的 direct-text/关键词规则 | 双重判断；命中强信号时绕过 Laya。三判别 classify 不参与生产。 |
| 需要什么能力 | 静态 Tool Router 关键词族 | 不是 Laya 的类别输出；multi-capability 仅集合并关键词规则，不表达依赖 DAG。 |
| 哪个具体 Tool | `select_tool_names` 提供候选集；Planner/模型选择具体工具 | Laya route head 恒 None；Planner 在候选集内再决策。 |
| 工具可否执行 | 包装器中的 intent/readiness/预算/约束、ApprovalGate、工具自身校验 | 多层重复拦截，安全性强但策略真相分散。 |
| 是否继续执行/重新规划 | Agents SDK Runner 的模型循环；RunContext 预算/重复动作/收敛门中止 | SDK 模型决策与 Runtime 预算、收敛门共同控制；并非 orchestrator 控制。 |
| 是否任务完成 | CompletionGate、ExecutionEvidence/obligations、`_succeed` | 不等同单次 tool success；有 final-response retry、degraded/failure 分支。 |
| 是否需要用户补充 | readiness/required_questions、工具参数 gate、SDK 返回 questions、terminalization | 入口文本预检和工具执行时再次验证；答案表现和 WAITING_USER 状态有专门收口。 |
| 故障降级 | Laya 返回 None→Router；Provider retry/Runner 外层 retry；Run terminalization | Layer 错误大多静默回落，缺少关联的 fallback 事件。 |

## 5. Tool / Context / Memory / Search / Terminalization 证据

### Tool selection 与 execution

- `route_agent` 使用克隆 Agent.tools 子集；因此普通路径不是全工具注入。但能力盘点特例和 TOOL_ROUTER 关闭路径返回全量，C2 连续零目标工具命中可熔断升级全量。
- `select_tool_names` 是静态关键词、常驻基础工具、显式高门槛、intent family、MCP domain 规则组合；Router 日志里的 `selected` 是筛选后工具集合，不是实际模型选中或实际执行结果。
- 工具包装器记录真实调用、blocked/error/status、参数摘要、result fingerprint、latency、turn number 等，并串联审批、readiness、intent、mutation、重复动作和预算控制。`_record_tool` 事件对未知 run_id/写事件失败存在容错吞异常路径；部分写审计失败可能造成可观测性缺口。
- 已有代码对 save_note/remember 做同 Run 幂等，副作用工具有 WAL/审批/重放保护清单。不能由此断言所有外部 MCP 副作用都具备统一 idempotency key。

### Context 与跨轮状态

- Laya 只看当前消息，不读历史 task state；“继续”一类依赖前文表达对它缺上下文，但当前 Laya 通常只有 direct_text 短路权，正常其他情况由 Runner Session history 承担。
- 主链调用 Session Preparation 做 compact/window，并将检索资料前缀合入本次用户消息；RunContext 保存本 Run 证据、约束与收敛状态。Layer 决策没有作为 run state 持久化，避免已知跨 Run 决策继承，但也意味着无法在 trace 中重建 Layer。
- Task container/session 复用与 RunContext 隔离分别承载对话和本轮执行状态；真实状态恢复主要有 approval resume 路径，任意执行中断的完全执行状态恢复能力不能仅据当前证据确认。

### Memory 与 Search

- 主链有 `remember` / `recall_memory` 等真实工具，由关键词路由提供候选，包装器检查用户 intent，实际写入工具负责持久化。`“回忆我喜欢什么”` 的路由实测 0 工具，说明表达变体会出现 retrieval FN；不应归因于 Laya。
- Pipeline 中 `memory_layers.record_step` 是过程记忆写入，但主链没有调用这条 pipeline；不等于主 Memory Runtime 的任务规划记忆。
- Search 由关键词 Router 暴露 `web_search`/`search_documents` 等，RunContext 有 search 硬预算和重复/无进展控制。已有日志含路由结果，不记录 query 实际执行、搜索结果利用率或 Layer→tool 理由；无法从 router log 得到每任务 model/search call 全指标。

### Terminalization 与循环

- 主生产主链终态通过 TaskManager 状态转换实现，代码可见 `COMPLETED / WAITING_USER / WAITING_APPROVAL / FAILED / CANCELLED` 的收口路径和 `run.terminal` 一致性事件。
- Agents SDK 的 `max_turns` 来自 Runtime budget；Run 有墙钟/token/工具调用/搜索预算、收敛 gate 和取消处理。CompletionGate 要求执行/验证证据，tool success 不自动意味着任务完成。
- 新 `LoopGate` 只用于未接入主链的 `run_pipeline`。此外该 pipeline 的 `while True` 逻辑目前只在 `redispatched` 后增加 loop_count；第一次 Laya 判决及工具派发、未注入 comparer 时可直接返回 completed，故它不是可替代生产 loop 的成熟闭环。
- 用户要求的错误注入（Layer timeout、坏 schema、工具异常、状态损坏等）及 20 组完整模型端到端 case 本轮没有执行，因为它们会运行外部/本地模型循环及部分副作用工具；本地审计只读完成。不能伪造 E2E 结果。

## 6. 日志与历史数据

- `.env`：`FORGE_LAYA=on`、`FORGE_LAYA_BACKEND=torch`、`FORGE_LAYA_CONF=0.85`、`FORGE_LAYA_PRELOAD=1`；Tool Router 默认开启（若无覆盖，`router_enabled()` 默认开）。`FORGE_ORCHESTRATOR` 未设置，默认为 off。
- `logs/tool_router.jsonl` 当前约 8.55 MB，最后写入 2026-09-26 21:09；尾部记录包含 timestamp、query（PII regex 脱敏并截断）、selected、selected_count、total_available、hit_zero、intent_class、reasons。无 task_id/run_id/step、Laya outcome/confidence、blocked capabilities、模型版本、fallback、latency（router latency 仅在聚合 metrics）。
- `logs/router_metrics.jsonl` 约 100 KB，聚合 `total_calls/hit_zero/escalated/fast_mode/empty/p95_latency_ms/avg_latency_ms`，其所谓 p95 当前实现取最近样本最大值，并非统计学 p95。指标没有 Layer 分类准确性或每任务调用链。
- `.env` 注释称模型 route head/route accuracy，但源码里 `classify.route` 恒 None；配置注释还写 `FORGE_LAYA_CHECKPOINT` 的默认目录为旧目录，当前源码优先选择 `forge_finetuned_5000_xpu_fixed_arith`。这是配置文档真相漂移。
- `agent.db`、`sessions.sqlite`、benchmark/production 历史分析文档存在，但本轮没有完整 SQL 导出每任务模型/工具/Layer 关联统计；数据库表没有可验证 Layer event 字段，现有历史不能归因 Layer calls。Layer transitions per task、Layer 前后成本差异、precision/recall/F1 均不可计算。

## 7. 测试真实性与覆盖

- Layer 单测 `tests/test_laya_router.py` mock 了底层 `predict`，验证开关、置信度边界、fallback 和单例延迟；它不验证训练权重的真实分类质量、下游 Agent 工具集合、模型真实调用或 session/task context。
- `tests/test_tool_router.py` 包含 route_agent/tool names 和关键词回归，可验证静态筛选器，但多数属于纯函数/伪造可用工具测试；Layer 和 tool router 联合作用/真实权重不等于完整 E2E。
- `tests/test_behavior_layer_rework.py` 名称中的 behavior layer 指收敛、clarification、trace metrics 等行为控制改造，不是 Laya Behavior Benchmark。
- `config/runtime_acceptance_suite.json` 有 runtime/terminalization/router/tool 集成测试清单，但没有独立 Layer 多类 benchmark、gold labels、类别混淆矩阵或 Memory/Reminder/Code/File/Ask User FN 统计。
- 没有跑完整测试套件；按本轮要求建立基线而非测试实现，且不添加/执行额外测试。测试结论依据源码测试内容与本地 inference/route probe。

## 8. 问题清单（严重级别）

### RT-LAYER-01 — P1 — Layer 输入只含单轮文本，且高置信直接短路

- **现象/复现：** `route_agent("读取 README 并总结")` 的 Laya classify 实测为 greeting；对“继续”这类语境依赖输入也无历史状态。高置信 `direct_text` 会清空工具集。
- **实际/预期：** Layer 目前可能以无上下文输出改变工具空间；意图/工具动作判断应结合必要的 task state，或只作可回退建议。
- **Root cause：** `route_agent` 只传 message；screen 的 `direct_text` 是有后果的短路。
- **文件/函数：** `runtime/runner.py::route_agent`；`runtime/laya_router.py::LayaRouter.screen`。
- **证据：** 当前权重 probe；`laya_screen_skip` 只保护若干关键词族。
- **影响：** 文本中无强信号但依赖历史的 Action 可能被裁空工具。
- **建议方向/测试：** 明确低成本 task summary 注入协议；覆盖多轮“继续”、同句历史消歧、direct_text 错判与 fallback。
- **Expected/Actual Layer：** 普通文件请求应 FILE_READ；当前 classify 为 greeting/低置信或主链不使用三分类。confidence 依样本列于 §3；工具集合依 §3。

### RT-LAYER-02 — P1 — Tool Router 关键词漏召回能力

- **现象/复现：** “回忆我喜欢什么” route_agent 实测空工具；复杂多能力请求未暴露 file write。
- **实际/预期：** 用户明确要求记忆检索/组合动作时，相关工具应进入候选；当前语义变体可导致 Planner 根本无工具可选。
- **Root cause：** 静态 `TOOL_TERMS` / `_MEMORY_NOTE_INTENT` / `_intent_family_tools` 词典覆盖不足，且 Layer route 恒 None。
- **文件/函数：** `runtime/tool_router.py::select_tool_names`、`_memory_tool_allowed`、`_intent_family_tools`；`runtime/laya_router.py::classify`。
- **影响：** Memory/File/Reminder 等 Action FN；模型可能改为文字回答。
- **建议测试：** 每 capability 正例/讨论语句负例、同义表达、组合任务的必需集合与额外集合。
- **Layer 证据：** memory retrieval 样本 classify ambiguous/0.9045，但 Layer 不提供工具名；allowed capabilities 为空，blocked set 即所有不在基础集的 capability；最终 0 工具。

### RT-LAYER-03 — P1 — 多能力无依赖表达，候选能力集合不完整

- **现象/复现：** 搜索→整理→写文件→提醒组合输入，Router 暴露搜索、提醒等却缺文件写入候选。
- **Root cause：** 工具族 regex 以词条/intent family 补齐，单纯拼接不表达 primary/optional capability、依赖或阶段状态。
- **文件：** `runtime/tool_router.py::_intent_family_tools`、`TOOL_TERMS`、`route_agent`。
- **影响：** 多步请求执行中途缺工具或被 Planner 以文本代替动作；无 task-level Layer reclassification/状态计划。
- **建议测试：** §3 组合 case 与依赖顺序 SEARCH→SYNTHESIZE→FILE_WRITE→REMINDER。
- **Layer 字段：** Expected capabilities SEARCH, FILE_WRITE, REMINDER；Actual selected set缺少 FILE_WRITE；Laya greeting/0.6871（但因护栏被跳过，不是实际过滤决定）。

### RT-LAYER-04 — P2 — Layer 可观测性缺失且仅部分请求调用

- **现象：** router log 只有 selected set 与规则分类，没有 Laya decision/confidence/fallback/task id；screen 被 `laya_screen_skip` 时也没有 Laya trace。
- **Root cause：** Laya 日志只在 direct_text 时写一条 slog；正常回退不保留结构化事件，tool router 与 Run audit 不共用 correlation id。
- **文件：** `runtime/runner.py::route_agent`、`runtime/laya_router.py`、`runtime/tool_router.py::_write_tool_router_log`。
- **影响：** 无法还原为什么某工具被允许/过滤，也无法算 transitions、混淆矩阵、成本归因。
- **建议测试：** 每次 Layer eval 持久化 task/run/step/query hash、决策、置信度、allow/block、fallback、延时、checkpoint/backend，并在实际 tool invocation 关联来源。

### RT-PIPE-01 — P1 — 声称的七阶段编排不在生产执行链

- **现象：** `FORGE_ORCHESTRATOR` 默认 off；主链只直接调用 `screen` 与 `select_tool_names`。显式 pipeline 使用 injected executor/comparer，不是 Registry。
- **Root cause：** 设计文档与生产 runtime 分离，pipeline 未从 `run_turn` 接入。
- **文件：** `runtime/pipeline.py::run_pipeline`、`runtime/runner.py::route_agent/run_turn`、`.env`。
- **影响：** 对“Layer→Planner→Tool execution→replan”所作 pipeline 测试不能证明生产行为；LoopGate 和过程记忆不保护主链。
- **建议测试：** production-path parity contract，确保接受同一工具门、执行账本、终态语义后再逐步接入。

### RT-CTRL-01 — P1 — 选工具决策与执行控制多层重叠

- **现象：** Layer 二分类短路、静态 Router 再分类；Planner 从候选集中再选；intent/readiness/budget/approval gate 再次裁决；CompletionGate 最后判是否完成。
- **Root cause：** 没有单一权威 routing decision contract，关键词 Router、RunContext 和 ExecutionEvidence 各有事实/策略。
- **文件：** `runtime/runner.py::route_agent/_patch_agent_tools/run_turn`、`runtime/tool_router.py`、`runtime/readiness_gate.py`、`runtime/completion.py`、`runtime/approval.py`。
- **影响：** 同一请求的“可见/可选/可执行/已完成”边界不同；有安全冗余，也有“工具不可见导致未执行”与“执行被 gate 阻止后模型继续”的复杂状态。
- **建议测试：** 从候选 capability 到 invocation 再到终态的跨层决策一致性表。

### RT-OBS-01 — P2 — Router 指标不能回答完整执行成本

- **现象：** 聚合指标无 task/run 关联；历史 model_calls/tool_calls 无 Layer trace join key；p95 实际取最大值。
- **Root cause：** Router metric 与 task observability 独立。
- **文件：** `runtime/tool_router.py::_maybe_flush_metrics`、`logs/router_metrics.jsonl`。
- **影响：** 无法得出用户要求的每任务 model/layer/planner/tool/search/duplicate/finalization 成本或 Layer 收敛收益。
- **建议测试/修复：** 同一 run ID 指标回放，并对 p95 使用分位数计算。

### RT-TEST-01 — P1 — 缺少独立 Layer Behavior Benchmark

- **现象：** 当前 Laya test 是 mock 行为测试；缺少分类 gold set 和分类别 P/R/F1；`.env` 声称的 500 样本结果缺可验证产物。
- **影响：** 当前无法确认 Layer FN/FP 分布，特别是 Memory/Reminder/Code/File/Ask User；总体 accuracy 也无法复算。
- **建议测试：** 用户要求的十类及讨论/过去时/组合/模糊/高副作用样本；记录 expected/actual/capability/tool/final behavior，并按类别计算 P/R/F1、置信度区间和混淆矩阵。将分类器单测与路由/E2E 分层。

### RT-PIPE-02 — P2 — 独立 Pipeline 的完成/预算语义弱于描述

- **现象：** executor/comparer 可不传；无 executor 也可将空 result 判 completed；`LoopGate.can_continue` 在此处只传 loop_count，token/时间默认 0；比较为 partial 也返回 completed。
- **Root cause：** 独立 demo/pipeline 回调协议没有强制执行证据和 terminalization contract。
- **文件：** `runtime/pipeline.py::run_pipeline`、`runtime/loop_gate.py::LoopGate.can_continue`。
- **建议测试：** executor/comparer 缺失、工具异常、部分完成、budget/timeout 真实累计和 terminalization 结果。

## 9. 已实现 / 名义实现 / 无法确认

**已由当前代码/观察确认：** 单一主 Run facade；Runner sync/async/stream；Tool Router 候选集克隆；Laya 二分类 direct_text 短路；工具包装与多级执行 gate；审批/取消/预算/收敛/CompletionGate/terminal event；Session compact/window；基础工具/路由日志。

**名义存在但不属于生产主链：** Laya 三判别 route head（route 恒 None）；Orchestrator 的 Laya Step 决策；LoopGate 与 `run_plan` 过程记忆 pipeline；七阶段“工具执行→LLM 比对→replan”。

**本轮无法证实：** 当前权重准确率与 per-class F1；Layer 前后成本改善；历史任务的 Layer transition 次数；真实 provider 收到的工具 schema 与 route_agent 工具集合完全一致性；完整 20 case 端到端表现；全部副作用工具统一幂等/确认语义。

## 10. 四个指定问题

1. **主要问题是局部 bug 还是 Runtime 控制架构未闭环？** 两者都有，但对本次 Layer/Runtime 目标而言是架构未闭环：生产链有工具 gate 与 completion/terminalization 闭环；新增七阶段 Layer-Orchestrator 方案没有接入。分类与关键词漏召回是其上的局部正确性问题。
2. **新增 Layer 是否真正降低 Planner/Tool Selection 决策空间？** Laya 仅对未触发跳过护栏且高置信判 direct_text 的请求将工具空间降至 0；其它场景不由它收窄。实际候选空间由静态 Tool Router 降低，仍常驻基础工具，部分失败场景 C2 会回全量。三判别 Layer 没有降低生产 Planner 空间。
3. **Layer 是 Runtime Control Layer 还是分类提示器？** 主链中的 Laya 是带一个 direct_text 硬短路效果的二分类快筛器；在“能力路由/工具选择/步骤控制”意义上主要是分类提示器。生产真正控制 tool set 的是静态关键词 Tool Router，真正控制执行/完成的是 Runtime gates。
4. **Layer、Planner、Tool Router、Terminalization 是否职责重叠？** 是。是否需工具由 Laya screen 与静态 direct-text/intent 规则重判；候选 capability/tool 由 Router 规则裁剪，Planner 在子集再选具体 tool；执行 gates 再判合法性；CompletionGate/terminalization 再判任务完成。该分层可形成纵深控制，但决策来源和 trace contract 分散，存在重复判断和错误被下游补救掩盖的情况。

## 11. Runtime Repair Roadmap（依赖顺序）

1. **确定生产主链与事实协议：** 锁定唯一 Run/Runner/Tool/terminalization 路径，区分生产 Layer 与实验 pipeline；定义 allowed capability、具体 tool、执行证据、task state 的权威 schema。
2. **厘清 Layer 职责/边界：** 选择 Laya 是仅建议、硬短路或输出多能力；明确类别集合、置信度阈值、skip/fallback/失败策略；保留 tool-level safety gate。
3. **修 routing correctness：** 建 gold Layer Behavior Benchmark；先测分类别 FN/FP，再校正表达变体、讨论/过去时、多轮与多标签组合；确保工具必须可见时不会因 Router 漏召回。
4. **定义 task state 与 step transition：** 明确任务级 intent/能力计划、每 step reclassify 条件、依赖关系与防抖，决定真实 pipeline 是否逐步接入。
5. **统一 terminalization 语义：** 绑定完成状态到真实执行/验证证据；定义 continue/retry/replan/wait/ask/complete/failed/cancelled 及预算耗尽行为。
6. **收敛 tool correctness：** 统一 schema/args validation、result/error 类型、retry/idempotency、read/mutation 风险与确认；确保所有工具注册路径走包装门。
7. **校准 Context/Memory/Search：** Layer 使用紧凑必要任务摘要；防止历史状态污染；补 Memory/Reminder/File/Search 同义请求及多阶段能力传递。
8. **补齐 error recovery：** 注入 Laya/model/tool timeout、malformed result、empty retrieval、state corruption、中断，验证无静默成功、重复副作用和无 trace。
9. **建立行为收敛基线：** 每 case 追踪 model/tool/search/duplicate/step/finalization，并对比接入 Layer 前后；防止 classifier call 抵消节省。
10. **Observability 与性能：** 按 run/step 记录 Layer 决策、候选/屏蔽工具、模型决策、调用结果、转移、延时、版本；正确计算分位数后再做吞吐优化。

**审计结束。** 本报告没有包含大规模代码修改或重构。
