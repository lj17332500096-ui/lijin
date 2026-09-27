# Agent Runtime 修复问题汇总

**整理依据：** [Runtime + Layer 全面审计](AGENT_RUNTIME_LAYER_AUDIT_2026-09-26.md) 与 [目标架构及工作流方案](../architecture/AGENT_RUNTIME_TARGET_ARCHITECTURE_AND_WORKFLOW_2026-09-26.md)。
**用途：** 将两份文档中的问题合并、去重，作为后续修复计划的入口。
**范围：** 问题汇总与优先级建议；本文件不代表问题已修复，也不修改 Runtime。

## 总览

两份报告收敛出一个主结论：当前生产 Runtime 的工具执行、安全检查、证据收集和终态收口已形成主闭环；主要缺口集中在 Layer/Tool Router 决策没有统一协议，实验性的七阶段 pipeline 未进入生产链，能力候选的遗漏无法通过现有观测与 benchmark 系统性发现。

本次审计**没有确认 P0 缺陷**。这不代表没有高影响问题，而是尚未复现已发生的错误副作用、状态损坏或无法结束案例。P1 项主要影响执行正确性和架构闭环；P2 项使问题难定位、收益无法量化；P3 项是后续整理优化。

## 需要修复的问题

| 汇总 ID | 级别 | 所属层 | 问题 | 证据/影响 | 主要依赖 |
|---|---|---|---|---|---|
| FIX-01 | P1 | Runtime 架构 | Layer 三判别/Orchestrator/LoopGate 七阶段 pipeline 未接入生产 `run_turn` | 主链实际为 `run_turn → route_agent → execute_turn/Runner → gates → terminalization`；`run_pipeline` 是独立注入回调路径且默认关闭。生产行为与设计中的 Layer→replan 流程不一致。 | 无；先定义生产事实链 |
| FIX-02 | P1 | Layer / Tool Routing | 没有统一的 Layer→Capability Policy→Tool Router 决策协议 | Laya 只对部分输入做“需工具/直接文本”二分类；工具能力实际由关键词 Router 选，Planner 再从子集选择，执行门又再次判断。决策来源分散、相互补救且无法区分漏判责任。 | FIX-01 |
| FIX-03 | P1 | Layer / Context | Laya 只看当前消息，且高置信 direct_text 可直接清空工具集 | 对依赖前文或隐式表达可能缺上下文；高置信分类并不保证语义正确。错误短路会让 Planner 看不到所需工具。 | FIX-02；之后才决定 Layer 是否能硬约束 |
| FIX-04 | P1 | Tool Router / Memory | 关键词能力路由存在漏召回与误放候选 | 实测“回忆我喜欢什么”暴露 0 工具；文件/代码请求会带入额外工具；部分多能力请求缺 FILE_WRITE。候选工具缺失时 Planner 无法补救。 | FIX-02；需要 capability/tool registry |
| FIX-05 | P1 | Multi-step Runtime | 多能力请求没有结构化依赖与阶段状态 | 搜索→整理→写文件→提醒只靠关键词并集，没有 required/optional capability 与 dependency frontier；后续阶段可能无工具可用，也没有统一的逐步扩展依据。 | FIX-02、FIX-04、FIX-08 |
| FIX-06 | P1 | Test / Evaluation | 缺独立 Layer Behavior Benchmark 与可复算真实基线 | 当前 Layer 测试 mock 分类器；没有 per-class P/R/F1、混淆矩阵和 FN 样本；`.env` 中的 500 条准确率声明缺可验证数据产物。无法安全设硬路由门槛。 | FIX-02；必须先定义 capability taxonomy 与标注集 |
| FIX-07 | P1 | Test Parity | 生产路径与部分 benchmark/evaluation 路径不等价 | 某些评测直接驱动 Runner，绕开 `run_turn` 生命周期与部分 gates；评测通过不能充分代表生产路径。 | 固定入口/运行契约后实施 |
| FIX-08 | P1 | State / Terminalization | 决策状态、工具执行事实与终态间缺少统一关联契约 | 主链已有真实完成门，但 Layer 结果、能力完成进度、工具调用与终态不能统一追踪；独立 pipeline 的空执行/partial 完成语义较弱。可能导致功能重复或将实验闭环误认为生产闭环。 | FIX-01、FIX-02 |
| FIX-09 | P1 | Tool Reliability | 副作用重试、幂等与 invocation 关联需形成统一契约 | 已有部分工具幂等与审批保护，但报告不能确认所有副作用工具统一覆盖；中断/重试/审批恢复时要确保不重复发生副作用。 | FIX-08；梳理工具 effect/risk registry |
| FIX-10 | P2 | Observability | 无法从一条 trace 解释为何工具被 Layer 允许或过滤 | 路由日志缺 task/run/step、Laya decision/confidence、policy allow/block、fallback、实际调用关联；Layer fallback/skip 也缺结构化记录。 | FIX-01、FIX-02；字段设计后接线 |
| FIX-11 | P2 | Metrics | 无法计算每任务成本与 Layer 收敛收益，p95 指标口径不正确 | 现有 metrics 是进程聚合，无 run 关联；`p95_latency_ms` 实际使用最近样本最大值。无法比较 Layer 与 Router-only 的模型/工具/搜索调用、重复率与终态表现。 | FIX-10；先建立统一 trace |
| FIX-12 | P2 | Fallback / Policy | 全量工具逃生路径没有纳入统一可解释 policy decision | `TOOL_ROUTER_FAST`、C2 escalation、能力盘点路径可能暴露全量工具；虽然执行 gate 仍存在，但候选面扩大、原因不可统一观察。 | FIX-02、FIX-10 |
| FIX-13 | P2 | Config / Model | Laya checkpoint/精度配置说明与源码实际选择存在漂移 | 配置注释描述的默认 checkpoint 与代码默认优先路径不同；性能数字缺来源，后续难以复现模型行为。 | FIX-06、FIX-10 |
| FIX-14 | P2 | Context / Memory | Layer 输入上下文与 task state 的选择机制未定义 | 不能把完整长历史交给分类器，也不能只看短句；需要紧凑 task summary、未决字段、已完成 capability 的输入边界及跨 Run 隔离规则。 | FIX-02、FIX-08 |
| FIX-15 | P2 | Error Recovery | Layer/model/tool 故障注入与完整恢复链尚未验收 | timeout、malformed output、unknown capability、工具异常、empty retrieval、状态损坏、中断等本轮未端到端注入；无法确认所有降级路径都不静默假成功或重复副作用。 | FIX-08、FIX-09、FIX-10 |
| FIX-16 | P2 | Tool Contract | Tool schema、参数验证、Observation/error 与 Provider 暴露集合需要统一验收 | 审计未能证实 Provider 实际收到的工具集合/schema 与 `route_agent` 输出一致，也未完整复现错误字符串是否会被模型误读为成功。 | FIX-02、FIX-10 |
| FIX-17 | P3 | Architecture Hygiene | 不要未经需要另建 `run_plan`、folder memory 或第二套 Orchestrator/Completion Judge | 现有 Task events、RunContext、ExecutionEvidence、CompletionGate 已承载相邻事实；新增真相源会增加双账本不一致与职责重复。 | FIX-01、FIX-08 |

## 优先级解释

### P0 — 当前未确认

现有证据没有确认正在发生错误副作用、状态损坏、无限循环或无法终止。实施时应将副作用重复执行、错误 terminalization 和任务无法收口加入故障注入验收；若复现，再升级对应问题级别。

### P1 — 先修正确性与架构边界

重点为 FIX-01 至 FIX-09：生产链定义、Layer 与 Router 分工、能力漏召回、多能力状态、评测真实性、终态/执行证据和副作用可靠性。这些问题决定 Agent 能否拿到正确工具并真实完成用户要求。

### P2 — 建立可调试与可证明性

重点为 FIX-10 至 FIX-16：没有 trace 和可信指标就无法确认 P1 修复是否有效，也无法判断 Layer 是否提高正确率或降低成本。配置漂移、fallback 可见性、上下文边界和错误注入同属此层。

### P3 — 后续清理

避免引入冗余执行/状态系统，整理名义实现与生产能力的文档差异。

## 推荐修复顺序与依赖

```text
FIX-01 生产执行链与入口边界
  ↓
FIX-02 Layer / Capability Policy / Tool Router 权责协议
  ├──→ FIX-06 Layer gold benchmark 与基线
  ├──→ FIX-10 trace correlation 与决策事件
  └──→ FIX-04 工具能力映射和 routing correctness
          ↓
       FIX-05 多能力依赖与 run-local state
          ↓
FIX-08 state transition / terminalization / 执行证据契约
  ├──→ FIX-09 副作用幂等、retry、approval/resume
  ├──→ FIX-15 错误恢复验证
  └──→ FIX-07 production benchmark parity
FIX-10 → FIX-11 指标、收益归因与 Layer A/B
FIX-02 + FIX-10 → FIX-12 全量 fallback 受控与可观测
FIX-06 + FIX-10 → FIX-13 checkpoint/config 真相
FIX-02 + FIX-08 → FIX-14 紧凑上下文与跨 Run 隔离
FIX-02 + FIX-10 → FIX-16 schema/provider parity 与 observation contract
```

建议执行阶段：

1. **定界：** 完成 FIX-01、FIX-02，先决定 Layer 的权限边界和生产主链契约，不接入第二条执行循环。
2. **测量：** 并行启动 FIX-06、FIX-10，建立 gold benchmark 与贯穿 run/step/invocation 的 trace。强制路由之前必须具备这两项证据。
3. **修正路由：** 依据 benchmark 修 FIX-04，再做 FIX-05 多能力流程；Layer 先 shadow/advisory，逐类别通过后再受限灰度。
4. **闭环可靠性：** 做 FIX-08、FIX-09；把 Planner 提议、工具执行、Observation、Evidence、CompletionGate 和唯一 Task terminalization 对齐。
5. **生产验收：** 完成 FIX-07、FIX-15、FIX-16，覆盖真实 `run_turn` 的 failure/retry/resume/cancel 与 Provider tool schema。
6. **收益与清理：** 有 trace 后完成 FIX-11、FIX-12、FIX-13、FIX-14、FIX-17；性能优化放在正确性基线之后。

## 关键验收条件

- 任一 Run 可还原输入、Layer 判断、policy 允许/过滤集合、发给 Provider 的工具 schema、模型选择、工具参数/状态、Observation、状态转移和终止原因。
- 用户明确要求的 Memory、Reminder、Code、File、Search、Ask User 请求分别有 FN 统计；讨论工具/过去行为等负例有 FP 统计。
- 组合任务按依赖阶段逐步开放 capability；每次扩展可解释，未完成步骤不会被标成成功。
- 模型/Laya/Tool 故障有界降级并保留 trace；副作用不会因 retry/resume 被静默重复。
- 生产质量验收通过 `run_turn`；低层 Runner 测试明确作为模型/planner 测试，不冒充生产验收。
- Layer 收窄带来的成本收益与 Router-only baseline 同数据对照，并且不能以 Action FN 或错误副作用上升换取。

## 当前建议先不要当作已确认事实的项目

- Layer 的类别 precision/recall/F1 和 `.env` 中的模型准确率声明。
- Layer 前后模型调用数、工具数、搜索数、完成率或 latency 收益。
- 所有 MCP/副作用工具均具备统一幂等与确认保护。
- 独立七阶段 pipeline 已代表生产行为。

以上项目应先由数据、trace 或生产路径测试证实，再进入修复验收结论。

## Runtime 状态、执行证据与副作用可靠性修复记录（2026-09-26）

本轮针对 FIX-08 / FIX-09 落实以下收敛：

- `TaskManager.transition` 在同一个 SQLite `BEGIN IMMEDIATE` 事务中提交 Run 状态、终态审批清理与 `task.<state>` 事件，避免状态已变但权威转换事件缺失。
- `ExecutionEvidence` 从 Run 内存账本、持久化 `tool.invocation` 事件和 Write-Ahead `tool_calls` 台账合并证据，并按 invocation ID 去重；Run terminal 事件记录已执行与结果未知的 invocation 摘要。审批在 claim 后崩溃、没有完成事件时会以 unknown 纳入证据。
- 副作用覆盖依据 `ToolSpec.side_effect`，未知工具遵循 `spec_for` 的保守副作用默认。副作用执行前必须有唯一 invocation ID 并成功原子写入 pending 台账；台账不可用、缺少 ID 或重复认领时 fail-closed。
- 成功状态统一使用 `executed`。超时、取消或副作用工具抛错时，持久状态为 `unknown`，不会把不确定外部结果伪装成失败后可安全重试；同一 Run 对 `idempotent=False` 的相同动作会先查执行历史并拒绝自动重放。
- 审批恢复由执行后标记改为执行前原子 claim。这个设计保证不会静默自动重放，但对无法与外部系统组成事务的工具只承诺 at-most-once；进程在 claim 后中断时必须按 unknown 对账，不宣称 exactly-once。
- 失败审计回填改为逐 invocation 补齐，不会因为数据库已有一条副作用记录就跳过其余内存账本记录。

本轮仅做 Python 语法编译和 `git diff --check` 静态检查；按当前工作约束未运行测试套件。仍需在生产 `run_turn` 集成路径注入 DB 写失败、重复 invocation、审批 claim 后崩溃、工具超时/取消等场景，确认状态与外部副作用的对账体验。
