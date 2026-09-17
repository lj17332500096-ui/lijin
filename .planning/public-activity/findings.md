# 发现
当前工作目录不存在 agent-workbench/ 或 local_forge/；实际代码为 runtime/、web/、agent.py、webapp.py。无 Git 元数据。审计以真实调用关系为准。

## 审计结论（修改前）
1. main._run_attempt 消费 SDK Runner.run_streamed，raw type 包含 delta 就发送 reply_delta；没有 reasoning 白名单边界。
2. reasoning delta、function arguments delta 都可能直接转发浏览器；普通中间文本同样被转发。
3. SDK tool_called/tool_call_created 发送 legacy tool，前端映射 tool.started。工具真实执行在 AgentRuntime._patch_agent_tools 的 on_invoke_tool 包装中，不经过骨架 ToolBroker。
4. 包装层已有 executed/error/blocked 账本与副作用 pending durable 行，但 SSE 没有完整 started/completed/failed。
5. TaskManager.add_event 持久化 task_events；没有统一实时总线。Web _LiveRunSession 提供现有 run-scoped 广播、历史回放。可以复用两者，不新增数据库或 SSE。
6. CompletionGate 使用 ExecutionEvidence + verification_outcome_of；code_loop 内部执行/修复没有独立公开事件。
7. Final 和所有 delta 使用同一路 reply_delta；终态从 messages 回放 reply（当前截断至 4000 字）。
8. workspace.onEvent 将 reply_delta 全部渲染 Markdown；工具进度依赖前端名称推断；历史过程把调用次数当成果。无独立 thinking renderer，也没有隔离。
9. SSE 旧格式 event: name / data: payload；无 visibility/channel/event_id。没有 WebSocket 实现，沿用 SSE。
10. 可复用 RunContext contextvars、现有工具包装、ApprovalStore、CompletionGate、task_events、_LiveRunSession、折叠样式、Markdown renderer、现有高级抽屉。

真实链路：POST task runs → _start_run_session → AgentRuntime.run_turn → main.execute_turn → SDK Runner → Provider → raw/tool events → 包装工具执行与账本 → CompletionGate（最多一次 repair）→ messages + task_events → _synthesize_run_end / _LiveRunSession → SSE → eventClient → workspace.onEvent。

实现选择：增加 Runtime 公共投影服务，以现有 task_events 保存安全事件；工具入口产生事实，模型执行阶段所有原始文本内部化。通过 CompletionGate 后使用同一 Provider 的无工具最终表达调用流式生成结构化 content，避免把后续仍可能调用工具的中间文本误认作最终回答。普通问答不制造过程卡。额外 final call 的延迟/成本需在报告明示。
