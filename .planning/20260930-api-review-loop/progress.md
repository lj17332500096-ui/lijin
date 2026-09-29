# Progress

## 2026-09-30

- 开始实施 API 预分析 / 执行后复核 / 最多一次补充循环。
- 明确保留用户现有 TUI 产物相关的未提交改动，不运行测试（除非用户明确要求）。
- 已创建本任务计划与 findings 记录。
- 确认 `AgentRuntime.run_turn()` 有统一终态与证据门；当前本地 `infer_task_plan()`/Router 不是 API 预分析，应继续作为安全执行约束。
- 已确认 SDK Agent 当前以一个自动多轮工具 loop 执行；内容复核需增加独立结构化调用，不得替代 Completion/Obligation Gate。
- 确认 `runtime.execution.execute_turn()` 支持传入临时 Agent/provider/audit；结构化调用不带 SQLiteSession，可避免计划/复核结果进入用户会话历史。
- 现有问题收尾支持 `questions` → `WAITING_USER`；新复核补充计数必须独立于现有最多 6 次 Completion/Obligation 修复预算，并严格限制为 1。
- 增加 `runtime/api_workflow.py`，使用无工具、无 Session 的结构化分析器/复核器；普通 JSON 提示加本地 Pydantic 校验，以避免依赖网关 JSON Schema 能力。
- 接入 `AgentRuntime.run_turn()`：预分析 answer/execute/ask_user 三路、主 Agent 仍走 Runtime、Completion/Obligation Gate 后复核，最多一轮 supplement 并二次复核。
- 增加 `conversation.analysis.*`、`conversation.plan`、`conversation.review.*`、`conversation.supplement.started` trace；Run 终态含 analysis disposition / review 次数 / supplement 标记。
- 流式补充前发 `stream_reset`；失败、等待用户和审批状态仍走既有终态 helpers。
- 完成 `git diff --check`。未添加或运行测试，未连接真实 Agnes API，未做 TUI 物理终端验收。
- 后续收口：将 Laya 的 hint/skipped 状态从同一 Run routing Trace 传入 API 分析上下文；分析器说明明确 Laya 仅是可选建议、API 决定粗粒度处置、Runtime 保留授权和执行权。
- 后续收口：在主模型调用前检查明确的 Windows/UNC 绝对文件路径是否越过当前读取根；越界时进入 `WAITING_USER` 并告知通过「+ 文件」附加或选择项目文件夹，不再让模型重复列目录。
- 后续收口：缺少 `call_id` 错误写入 `model.protocol_error`，区分模型/网关协议故障与工具执行，并返回可操作的中文说明。
- 后续收口：分析/复核 API 阶段事件增加耗时和 token 增量；Run 终态增加模型 token/延迟汇总及逐工具状态/调用 ID。
- 仅做源码与 diff 静态检查；未运行测试、真实模型或生产验收。
