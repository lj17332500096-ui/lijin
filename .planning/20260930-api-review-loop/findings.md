# Findings

## 初始范围

- 用户要求落实四项：API 预分析；Runtime 保持唯一安全执行边界；API 结果复核；最多一次有界补充循环。
- 本轮工作区已有未提交改动：`cli/tui/app.py`、`cli/tui/panels.py`、未跟踪 `cli/tui/artifacts.py`、`tests/test_tui_artifacts.py`。这些来自前一项 TUI 产物交互工作，必须保留且不混改。
- 仓库遵循 `.planning/` 任务计划归档习惯；本计划独立存入 `.planning/20260930-api-review-loop/`。

## 待确认代码发现

- `AgentRuntime.run_turn` 是生产 Run 主路径，已有统一 `_succeed` / `_fail` / `_succeed_waiting_user` 收尾及 `run.terminal`、`ExecutionEvidence` 契约。
- 当前 `route_agent()` 在 Runtime 调模型前用 `infer_task_plan()` 做本地规则规划，并由 Tool Router 选工具子集；Laya 明确为建议来源。要增加 API 预分析，应区别于此本地安全/能力路由计划，不能取代或绕过它。
- Agent SDK 目前经 `execute_turn()` 跑一个可自动多轮调用工具的 Agent loop；收尾时 ReplyParser + Completion Gate + Obligation Gate 复核状态/证据。目标 API 内容复核尚无独立阶段。
- `RunContext` 持有按 Run 绑定的 capability plan 与阶段状态；多阶段副作用具有阶段 claim、WAL 和未知结果阻止重放等机制。
- 预分析/复核应接在 `run_turn()` 的正式模型执行外围，并共用选定 provider/预算/trace；SDK 自动工具循环仍需服从 Runtime 包装器。
- `runtime.execution.execute_turn()` 可接受一个临时 Agent、同一 provider 和 AuditCollector，因此可承载无工具的结构化 Planner/Reviewer 请求，调用计入模型审计/token 预算；内部调用必须不绑定用户 session，避免计划对象污染对话历史。
- 生产收尾已有 `questions` → `WAITING_USER`，而 Completion/Obligation Gate 处理证据真实性；API Planner 的 `ask_user` 应复用问题终态，Reviewer 不得覆盖安全/审批门结论。
- 结构化 Planner 的工具提示只用于上下文/候选建议，实际可执行工具集合仍由 `route_agent` 形成；执行器仍只能通过 Runtime wrappers 调用。
- 当前 `_repair` 循环最多 6 次，包含确定性 Completion/Obligation Recovery；新增 API 内容补充要有独立计数，最多 1 次，不能复用较大循环额度。
- 当前工作区另有用户此前要求的 TUI 文件产物功能改动，保持隔离。

## 实施结果

- 新增 `runtime/api_workflow.py`：定义 RequestAnalysis / AnswerReview 结构、无工具 Agent 提示、JSON 容错解析和脱敏执行证据视图。
- Runner 在主执行前调用一次 API 分析，记录 `conversation.analysis.*` 与 `conversation.plan`。`answer` 分流为无工具 Agent；`ask_user` 进入已有 `WAITING_USER`；`execute` 保持原工具子集。
- 工具执行仍通过原 `execute_turn` / Runtime 包装器；Planner/Reviewer 没有工具和会话访问。模型调用继续通过相同 Provider 和 AuditCollector，计入模型调用/Token 审计。
- Completion Gate / Obligation Gate 通过后执行 API Reviewer。`supplement` 最多产生一轮补充执行/答复，然后再复核一次；复核仍不足或 Reviewer 不可用时不标记成功。
- 复核阶段及补充计数写入 task events 和终态 Trace。TUI 流式场景在进入补充循环时发送 `stream_reset`，避免初稿与补充稿重复显示。
- 使用普通 JSON 文本约束与本地 Pydantic 校验，而非 SDK JSON Schema 输出模式，以适配当前 OpenAI-compatible Agnes 网关。
- 已运行 `git diff --check`；没有运行任何测试/真实模型或生产链验收。

## 后续收口（2026-09-30）

- `route_agent()` 的 routing Trace 表明 Laya 已是 advisory-only 且可跳过；API 分析上下文现在读取同一 Run 的 Laya hint（或 skipped/unavailable），并明确由 API 给出正式 answer/execute/ask_user disposition。Laya 指引已写进分析器说明。
- Runtime 在 API 分析前识别用户直接提供的 Windows/UNC 绝对文件路径。若路径不在 Run 允许读取的 roots 且没有同名附件，Run 转为 `WAITING_USER`，提示 TUI「+ 文件」或选择项目文件夹；事件只保留文件名，不记录完整用户路径。
- 缺少工具调用 `call_id` 时新增 `model.protocol_error`，错误说明调用未进入工具执行，并返回中文可操作提示；该错误不再作为普通 Provider 成功结果处理。
- 阶段 API 事件补充模型阶段耗时、token 增量；终态 Trace 汇总已落库的模型调用数、token、延迟统计和逐工具状态/调用 ID。
- 本轮未运行测试/真实 API，需通过后续用户要求的验收确认 Windows 路径匹配、终态列兼容性和网关协议报错展示。
