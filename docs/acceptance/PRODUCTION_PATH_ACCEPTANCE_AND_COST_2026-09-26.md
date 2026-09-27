# 生产路径验收与成本优化

日期：2026-09-26
范围：Runtime 生命周期、审批恢复、副作用证据、CompletionGate、Provider 重试与 Run 成本账本。

## 验收结论

离线生产路径验收通过。验收覆盖 `AgentRuntime.run_turn` 的状态收口、审批恢复、文件范围、并发隔离、CompletionGate 和 Provider 错误/超时闭环。未调用真实付费 Provider，也未执行真实外部副作用。

发现并处理了三项验收问题：

1. 审批测试仍按“未写 executed 就可以重放”的旧语义编写。测试现已验证持久 claim 后崩溃不会自动重放；审批提交本身要求已 claim，且第二次提交返回 false。
2. 副作用 WAL 恢复代码将未决调用记录为 `unknown`，而测试仍期待 `interrupted`。验收统一为 `unknown`，保留 `tool.side_effect_unknown` 事件；此状态不代表成功，也不会自动重试。
3. 并发压力测试的模拟执行器在审批恢复时重复伪造了一次工具调用。Runtime 已先执行已批准调用，空消息续跑模拟器不应再造一次调用。修正模拟器后，8 Run 混合并发验收通过。

另修复 Provider 尝试记录写入时的文件句柄生命周期：原有行数统计直接迭代 `path.open()`，测试出现未关闭文件的 `ResourceWarning`；现在使用上下文管理器确定关闭。该修复减少每次尝试留下的临时文件描述符占用，不改变重试策略与账本内容。

## 成本账本现状

| 项目 | 当前口径 | 验收判断 |
| --- | --- | --- |
| 模型调用次数 | `model_calls` 按 SDK 返回的每个 raw response 逐条落库；失败路径另有 attempt backfill | 能追踪真实返回的响应数；Provider 尝试事件用于补充失败调用路径 |
| Token 用量 | AuditCollector 记录 input/output；每次响应同步进入 `TokenBudgetGate` | 预算按 Run 累计 token，默认环境预算 50,000，可由 `FORGE_TOKEN_BUDGET` 覆盖，0 关闭 |
| 美元成本 | `TaskUsage.cost_usd` 当前没有可靠的模型价格来源，通常为 0 | 不可作为实际账单或跨模型成本比较依据 |
| 重试 | 401/认证类不重试；暂时性网关错误受有限重试与 deadline 限制 | 相关策略测试通过；没有无界自动重试路径 |
| 历史压缩 | 低于阈值时走 `quick_stats` 快路径；接近/超过阈值时才精确读取与压缩 | 可减少常态下摘要模型调用；极长历史的全量统计仍有内存和 CPU 开销 |

## 定价核验补充（2026-09-27）

本机当前模型配置为 `agnes-3.0-flash`，Provider 主机为 `apihub.agnes-ai.cn`（未记录 API key 或完整 URL）。公开的 [Agnes API 参考](https://github.com/1038lab/Agnes-AI/blob/main/references/api.md)确认 `agnes-3.0-flash` 是有效模型 ID，并展示响应 token usage 字段；[Agnes 官网](https://agnes-ai.com/)当前宣传 Free API，但公开 API 参考没有给出可用于核算的每百万输入/输出 token 美元单价。公开参考使用 `.com` API 域名，与本机配置 `.cn` 主机也不相同，因此不能据此假设价格或计费规则相同。

因此本轮可完成 token 和调用量基线的设计及记录，但无法从可核验资料计算实际 USD 成本。任何账单金额必须来自该账户/区域适用的费率或账单导出；在此之前 `cost_usd=0` 仍只表示“未估算”，不能显示为免费或实际花费。禁止将其他模型或网关的价格套用到当前配置。

## 本轮优化与后续次序

已做的优化仅限能由本轮证据直接支持的文件句柄泄漏修正。没有在缺少真实成本基线时改模型档位、压低 token 预算、缩短上下文窗口或改变压缩触发条件，以免把质量/可靠性回归误当作节省。

建议下一步按以下顺序做可量化优化：

1. **建立真实成本基线**：增加 `provider/model/pricing_version/currency` 维度和 per-call 估算金额；价格必须配置化并带生效日期。把 retry、format repair、completion repair 分开统计，先观察至少一周的 P50/P95 每 Run token 与金额。
2. **削减失败重试支出**：按 Provider 错误类型统计失败前已消耗 token；认证/参数错误维持零重试，只有可恢复错误才重试。核实 fallback 不会令同一 Run 在多个渠道重复支付过多尝试。
3. **减少重复上下文**：为 compact/硬窗口过程采集读取条数、字符数和耗时；在保持 `quick_stats` 快路径的基础上，再评估 session 增量统计或分页读取。先以等价性回放证明工具调用配对、最近轮保留和压缩决策不变，再调整全量读取。
4. **减少修复轮调用**：按 completion repair 与 readiness/obligation repair 分桶，记录触发原因及修复前后是否收敛；只有能稳定避免额外模型轮的确定性修复才下沉为规则。

## 测试记录

使用项目 `.venv` Python 3.11/`agents 0.22.0` 执行，合计 148 个定向用例通过：

- `test_approval_execution_parity.py`：11
- `test_production_closure.py`：15
- `test_audit.py`：3
- `test_budget_router.py`：12
- `test_aux_provider_retry.py`：10
- `test_completion_gate.py`：26
- `test_task_runtime.py`：18
- `test_readiness_closure.py`：28
- `test_concurrency_stress.py`：4
- `test_provider_errors.py`：13
- `test_provider_deadline.py`：8

此外，修改文件通过 `py_compile`，`git diff --check` 通过。测试只覆盖离线/模拟 Provider，不构成真实 Provider 价格或生产流量表现的测量。
