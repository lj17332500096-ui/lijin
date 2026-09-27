# Dynamic Runtime Acceptance — 2026-09-27

## Scope and setup

验收使用真实 `AgentRuntime.run_turn` 与当前 Agnes Provider；三次 Run 的 `agent.db` / `sessions.sqlite` 写入隔离在 `var/acceptance/live-runtime-20260927/`。外部 MCP 只建立连接并核对工具映射，没有调用 Gitee 账户数据、浏览器交互、SQLite 写操作或其它外部副作用。故障注入只写入 `var/acceptance/` 临时数据库和日志。

生产模型配置存在且模型为 `agnes-3.0-flash`。本轮三次模型响应共记录 20,649 input tokens、117 output tokens。按代码里的 Agnes 列表价版本快照估算约 **$0.00105**；该数值是列表价估算，不等同账户账单。原始结果：`var/acceptance/live-runtime-20260927/results.json`。

## 逐项结果

| 项 | 场景与证据 | 结果 |
|---|---|---|
| 1. 口语变体与普通对话 | 真实 Run：口语天气请求、安慰闲聊。闲聊在 1 次模型调用内完成，未暴露工具，回答符合请求；天气 Run 路由允许 `get_weather`，但实际 `tool_calls=0`，模型直接生成天气回答。 | **失败（天气）/通过（闲聊）**。天气回答没有工具执行证据，不能视为真实查询。 |
| 3. 多能力流程 | 真实请求“查锦州天气、计算温差、给穿衣建议”。能力计划只要求 `get_weather`，模型没有调用天气或计算工具，最终回答“未找到锦州”。 | **失败**。未完成任何预期能力阶段。 |
| 4. Provider/MCP 中断恢复 | Provider 错误分类、有限重试、fallback 与 deadline 离线故障注入：59 项通过（需将仅对子进程生效的 `APPROVAL=on` 用于审批相关 fixture）。真实 MCP 启动连接 6/6 成功；本轮没有杀停 MCP 子进程或对真实 Provider 注入断网。Python 退出时观察到多个 MCP stdio generator 的 `cancel scope in a different task` 清理异常。 | **部分通过**。Provider 代码路径离线恢复通过；真实中断恢复未验收，MCP 关闭生命周期有实际异常。 |
| 5. 数据库/进程故障 | 隔离 DB 子进程写入 pending 副作用行后 `os._exit(73)`；重启恢复将 Run 标记失败，调用记为 `unknown`，无自动重放。隔离 SQLite 触发 `database or disk is full` 后 `quick_check=ok`。损坏 DB 打开时报 `file is not a database`，未被静默初始化为空库。 | **部分通过**。进程硬退出、磁盘满原语和损坏库 fail-closed 已演练；未在真实 Run 流程中注入磁盘满，也未验证备份恢复。 |
| 6. TUI 流程 | Headless Textual：`tests/test_tui.py` 59 项通过；与 MCP policy / stream pipe / public activity 合并的 suite 为 101 项通过。覆盖流式缓冲与单条回答、错误行、工具状态行、审批按键及 UI 暂停态。 | **组件级通过**。没有启动真实交互终端并连接 Provider，审批批准后自动续跑未做 TUI 端到端实测。 |
| 7. MCP 工具审批映射 | 真实 MCP 初始化成功：gitee、playwright、sqlite、chrome、fetch、youtube。实际挂载统计 `allow=52 / approval=34 / deny=3`；显式 deny 为 `playwright_browser_emulate_media`、`playwright_browser_run_code_unsafe`、`chrome_get_css_styles`。策略单测在审批启用 fixture 下通过。 | **映射已核对，但生产有效性不通过**：当前 `.env` 是 `APPROVAL=off`，因此 34 个 `approval` 映射不会触发审批门。配置标签表达“需审批”，运行时总开关却关闭了审批。 |
| 8. Session 隔离/清理 | Runtime、Session、产品化与上下文回归组 82 项通过。另在隔离 Session DB 注入损坏文件，确认清理任务带错误与 retry_count 留在队列；恢复 Session DB 后，clear 保留 session 行并清空消息，delete 删除 session 与消息，队列归零。 | **通过（本轮覆盖范围）**。未使用生产历史库。 |
| 10. 成本、延迟与轮转 | 真实 Run 每次写入 `model_calls.latency_ms` 和估算成本，完整 Run 延迟分别约 51s、12s、15s；最近 3 Run 的 P95 为 51s。首轮时长包含 MCP 冷启动。独立轮转演练生成当前 JSONL 与 `.1`–`.3` 三个副本。 | **记录路径通过**。样本太少且首轮含冷启动，不能用作性能基线；实际计费、长期 P95 与保留周期尚未建立生产基线。 |

## 测试命令与结果

- `tests/test_audit.py tests/test_task_runtime.py tests/test_runtime_cleanup.py tests/test_productization.py tests/test_tmr_model.py tests/test_context_guard.py`：82 passed。
- `tests/test_provider_errors.py tests/test_provider_deadline.py tests/test_aux_provider_retry.py tests/test_production_closure.py tests/test_approval_execution_parity.py`：临时启用审批 fixture 后 59 passed。按当前 `APPROVAL=off` 直接运行时，审批预期用例失败，这是当前配置造成的测试环境差异，不是测试被跳过。
- `tests/test_mcp_bridge_policy.py tests/test_mcp_bridge.py tests/test_tui.py tests/test_stream_pipe.py tests/test_public_activity.py`：临时启用审批 fixture、禁用外部 MCP 子进程后 101 passed。
- 测试统一使用 `.venv\Scripts\python.exe`。pytest 报一条 `Unknown config option: cache_dir` 警告。

## 验收结论与未关闭项

本轮不能给出“生产路径动态验收通过”的总判定。主要阻断点是天气和多能力请求虽然暴露了合适工具，但模型没有调用工具；MCP `approval` 映射受全局 `APPROVAL=off` 覆盖；真实 MCP 进程关闭产生异步清理异常。Provider 动态请求成功证明模型链路可达，但故障恢复只由离线模拟验证。

下一轮应先修复并复验天气/多能力请求的实际工具调用，再决定 MCP `approval` 策略是要生效还是要把配置标签改为 allow；随后增加 MCP 生命周期关闭/重连演练、真实 TUI 恢复闭环和生产流量延迟/成本样本。不要把本报告中没有执行的真实 Provider/MCP 中断测试标记为通过。
