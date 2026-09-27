# Findings

## 当前事实

- 生产入口是 `AgentRuntime.run_turn`，生产模型调用经 `main.execute_turn` / `Runner`。
- 近期改动覆盖 TaskManager 状态事件事务化、副作用 WAL、Approval claim、ExecutionEvidence 合并、能力计划和 routing trace，必须优先回归这些交界。
- 用户本轮明确要求“生产路径验收”，因此可运行相关测试；无需触发真实模型 Provider 或外部副作用。
- Provider attempt 有统一预算但模型账本在 `runtime/audit.py`；工具调用/成本信息在 `task_events`、`tool_calls`、`runs.usage_json`。

## 已验证的判断

- AuditCollector 把每个 SDK raw response 写入 `model_calls` 并调用 token gate 一次；测试确认 input/output totals 与持久账本一致。
- Provider retry/fallback 有界；401 不重试，503/无渠道仅有限重试/fallback，deadline 用例全部通过。
- WAL 恢复把未决副作用持久化为 `unknown`，并事件化；它不表示已执行成功，且不应自动重放。
- 审批 claim 持久化后从待执行恢复集合排除，符合 at-most-once + uncertain reconciliation 语义。
- 性能问题可直接确认的一项是 provider attempt 行数检查遗留文件描述符；已改为上下文管理器关闭。

## 未完成测量

- USD 成本目前没有真实模型价格表支撑；`TaskUsage.cost_usd` 不能当作账单数据。
- 历史达到软阈值时仍需全量读取来精确计算和压缩；应先加读取指标与等价性回放，再做增量/分页变更。
