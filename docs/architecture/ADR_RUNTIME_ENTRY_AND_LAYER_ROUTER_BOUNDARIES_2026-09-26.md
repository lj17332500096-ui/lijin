# ADR: 生产执行链与 Layer / Tool Router 职责边界

- **日期：** 2026-09-26
- **状态：** Proposed — 作为后续实现的边界决策；代码尚未按本 ADR 改动。
- **依据：** 当前源码调用点、[Runtime + Layer 审计](../audits/AGENT_RUNTIME_LAYER_AUDIT_2026-09-26.md)、[目标架构方案](AGENT_RUNTIME_TARGET_ARCHITECTURE_AND_WORKFLOW_2026-09-26.md)。

## 决策摘要

1. 唯一生产 Run lifecycle 是 `AgentRuntime.run_turn`；唯一生产模型/工具循环是 `main.execute_turn → _run_attempt → Agents SDK Runner`。
2. `runtime.pipeline.run_pipeline`、`runtime.orchestrator.Orchestrator` 与 `LoopGate` 暂定为实验/评测组件，不属于生产执行链，不得被 benchmark 结果表述为生产闭环证据。
3. Laya 是分类评估器，只输出请求意图/所需能力/完备性建议与置信度。现阶段默认 shadow/advisory；不得单独清空 Agent.tools、执行工具、决定审批或判定任务完成。
4. Capability Policy 是唯一负责把用户请求、Layer evidence、紧凑 task state 和安全规则转换为 `allow/blocked/ask/fallback` 能力集合的确定性责任点。
5. Tool Router 只把已确定的 capability 集合映射成 Registry 中的具体工具子集并形成 Agent clone。不得再次用隐式语义判断覆盖 policy 决策。
6. Planner 是现有 Agents SDK Runner 中的模型：在被提供的候选工具里选择具体 tool/args，读取 observation 后继续或提出最终答复；不是第二个独立 Orchestrator。
7. 工具包装器、Readiness/Intent/预算门、ApprovalGate、ExecutionEvidence、CompletionGate 和 TaskManager 继续拥有执行授权、人工审批、事实记录、完成判定与唯一状态转换。工具可见不等于获准执行，工具成功不等于 Run 完成。

## 当前入口分层

| 入口类型 | 当前代码路径 | 规范归类 | 边界 |
|---|---|---|---|
| 交互式 REPL/CLI | `main.py` / `cli/app.py` → `AgentRuntime.run_turn` | 生产 Runtime | 必须保留 Task/Run、包装器、证据和 terminalization |
| Chat UI | `cli/ui/app.py` → runtime/chat handler → `run_turn` | 生产 Runtime | 与 CLI 共用同一 Run owner；渲染/stream callback 不另造执行链 |
| Scheduler | `scheduler.py` 创建任务并调用 Runtime | 生产 Runtime | channel policy 可以不同，Run owner 与执行门相同 |
| `benchmark/microbenchmark.py`、`eval_runner.py` 部分路径 | 直接 `run_turn` | Runtime 集成评估 | 可代表相应配置下的 Run 主链；仍需核对模型、feature flags、外部依赖与生产 parity |
| decision/checkpoint/exposure 等低层 benchmark | 可能调用 `route_agent` / `Runner.run` | Planner/Router 组件评估 | 不代表完整生产 Runtime；不得据此宣称审批、CompletionGate 或 terminalization 已通过 |
| `runtime.pipeline.run_pipeline` | 独立函数，注入 executor/completer/comparer | 实验/原型 | 默认不启用，未接 `run_turn`，没有生产 Registry/Tool wrapper 所有权 |

## 责任矩阵

| 决策/动作 | 唯一 owner | 输入 | 输出/效果 | 明确禁止 |
|---|---|---|---|---|
| Run 创建、恢复、预算和最终状态 | `AgentRuntime` + TaskManager | request/session/task/channel | Run identity、预算、Task state transition | 第二个组件自行创建/完成 production Run |
| 请求/上下文归一化 | entry adapter + Context assembler | message、attachment、有限历史/task facts | 当前请求与最小 task summary | 将整段无限历史默认送分类器 |
| 语义分类 | Layer evaluator（Laya 或替代） | 当前请求 + 受控 task summary | intent/capability/completeness 候选、置信度、reason | 执行授权、具体 args、完成判断 |
| capability allow/block/ask | Capability Policy（需作为明确单一责任新增/收敛） | Layer evidence、显式措辞、task state、risk rules | required/optional/allowed/blocked、clarification、fallback | 隐式新增所有工具或将 confidence 当权限 |
| capability→tool mapping | Tool Router + Registry metadata | Policy allowed capabilities、可用工具、channel | 精确 allowed tool subset + policy decision ID | 第二次独立意图分类或静默全量回退 |
| 当前具体动作和参数 | Planner（Runner 中的模型） | prompt/context、allowed tools、observations | final proposal 或 tool call | 调用 Agent.tools 之外工具、绕过执行 wrapper |
| 工具能否真实执行 | Tool wrapper + readiness/intent/budget/constraint gates | 原始请求、RunContext、tool/args | execute / blocked / ask / timeout / error | 依据 Laya confidence 代替用户授权/参数验证 |
| 副作用审批 | ApprovalGate | tool risk、invocation、run/channel | pending/approved/denied | 由 Router/Planner 自行批准 |
| 执行事实 | Registry binding + invocation ledger/ExecutionEvidence | validated invocation/result | status、evidence、mutation/verification facts | 把模型文字或工具可见当作执行证据 |
| 任务是否完成 | CompletionGate | final candidate、obligations、ExecutionEvidence | pass/reject/repair reason | Layer/Tool Router 宣告 COMPLETE |
| Task terminalization | TaskManager + terminalization | completion/approval/ask/failure/cancel outcome | 唯一合法状态变更与 terminal event | 并行状态机或成功后继续派发工具 |

## 主链契约

```text
Entry
→ AgentRuntime.run_turn (create/resume Run; bind budgets/state)
→ Context assembly
→ Layer evaluation (advisory evidence)
→ Capability Policy (one allow/block/ask/fallback decision)
→ Tool Router maps approved capabilities to current Agent.tools
→ execute_turn → Agents SDK Runner (Planner/model loop)
→ Tool wrapper gates → Registry execution
→ Observation returned to the same Runner loop
→ CompletionGate / obligation / evidence validation
→ TaskManager terminalization
```

关键不变量：

- Policy 的 `allowed_tools` 是本次模型请求可见工具的上限；Provider payload 不得包含 Router 之外的工具。
- 每次 tool invocation 必须带 `run_id`、`decision_id`、`invocation_id`；工具结果必须可回连到对应 observation 和终态。
- Policy 后续扩展工具集合必须产生新 decision event 与 reason（例如上一阶段 search 得到目标路径后允许 file write）；不复用旧 Run 的许可作为新 Run 授权。
- 正常 Layer fallback 只回到可解释的 policy fallback。全量工具逃生必须显式 policy mode、有事件与安全门；不能是任意异常的默认动作。
- tool result/error 的状态结构化进入 Runner；错误文案不能被误当成功事实。
- 任何 route/policy 判断都不能跳过现有 wrapper、ApprovalGate、ExecutionEvidence、CompletionGate 或 TaskManager state transition。

## 逐步收敛方式

### 现在开始约束的行为

- Laya 不再拥有“直接回答就裁空所有工具”的生产最终权；在 policy 接线完成前，将 screen 结果视为 shadow evidence，当前 Tool Router 结果保持行为基线。
- 把 Laya classify 与 `route_agent` 实际工具集合的观测分开记录，避免将 Laya 的类别误称为真实 capability routing。
- 标记 `run_pipeline` 为实验性路径，其测试只验证原型函数本身。

### 后续实现顺序

1. 给 Layer、Router、Provider tool schema、tool invocation 和 terminal event 建立 decision/run correlation。
2. 建 capability taxonomy 与 Registry 映射，并定义 Capability Policy 输入/输出结构。
3. 将现有关键词规则降为 Policy evidence/rule，不再让 Router 内部另有无法解释的意图裁决。
4. 以 shadow benchmark 校准分类与候选集合；按类别逐步灰度，不先做全量改写。
5. 只有生产 `run_turn` path 能复用同一 tool wrappers/approval/evidence/completion/state 时，才讨论是否将 pipeline 原型功能折叠/迁入主链；不直接把 `run_pipeline` 并列接入。

## 明确不做

- 不新建第二个 production Agent loop。
- 不让 Layer 直接调用工具或替用户确认副作用。
- 不让 Tool Router、Orchestrator 和 CompletionGate 各自维护一份“任务是否完成”的事实。
- 不先新增 `run_plan` 或新状态数据库作为另一份执行真相。
- 不在没有 gold benchmark 和 trace 的情况下，用 `.env` 声明的准确率数字打开硬约束路由。

## 验收条件

后续实现只有同时满足以下条件，才能认为“生产执行链与 Layer/Router 边界已落地”：

1. 所有生产交互与定时入口都进入 `run_turn`；低层 Runner 与 pipeline 路径明确标记为非 production parity。
2. 任一请求有且仅有一个 policy decision 定义 allowed/blocked/ask/fallback。
3. 实际发给 Provider 的 tools 与 policy 生成的 allowed tools 完全一致；C2/fast/full-tool 路径也可追踪且不绕过执行门。
4. Layer 输出无法单独授权副作用、清空必需工具或完成 Run。
5. 测试覆盖 direct/search/file/code/memory/reminder/ask/multi-capability、Layer 故障 fallback、工具执行与终态，并使用生产 `run_turn` 集成路径验证关键链条。

## 代码入口索引

- Run lifecycle：`runtime/runner.py::AgentRuntime.run_turn`
- 当前工具选择：`runtime/runner.py::AgentRuntime.route_agent`、`runtime/tool_router.py::select_tool_names`
- 生产模型循环：`main.py::execute_turn`、`main.py::_run_attempt`
- 工具执行拦截：`runtime/runner.py::AgentRuntime._patch_agent_tools`
- 任务完成与状态：`runtime/completion.py::CompletionGate`、`runtime/runner.py::_succeed/_fail`、TaskManager/state machine
- 非生产原型：`runtime/pipeline.py::run_pipeline`、`runtime/orchestrator.py::decide`、`runtime/loop_gate.py::LoopGate`
