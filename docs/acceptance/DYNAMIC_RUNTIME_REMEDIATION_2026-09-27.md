# 动态验收未覆盖项：修复与复验补充 — 2026-09-27

本报告补充上一轮 [DYNAMIC_RUNTIME_ACCEPTANCE_2026-09-27](DYNAMIC_RUNTIME_ACCEPTANCE_2026-09-27.md)。所有故障注入均在 `var/acceptance/` 独立目录执行；没有对真实账户执行写操作，没有修改持久 `.env`，没有提交代码。

## 修复和动态复验

| 场景 | 执行方式 / 结果 | 结论 |
|---|---|---|
| MCP stdio 进程中断和重连 | 使用真实 Agents SDK MCP stdio 与隔离 Python MCP 子进程。`ping` 成功；工具让隔离子进程 `os._exit(71)` 后得到 `McpError: Connection closed`，没有重放；在相同事件循环清理后状态归零，重连后 `ping` 再次成功，前后清理错误均为 0。 | **通过**。此前观察的 cancel-scope 清理错误本次未复现。 |
| 主 CLI / TUI / 语音生命周期 | 新增 `mcp_bridge.close_servers()`，在 CLI、TUI、CLI、每轮语音 Runtime、单次定时任务及常驻调度器退出时在对应 asyncio 生命周期内关闭 Server；移除经典聊天跨两个事件循环复用 MCP 的预连接。 | **修复并由 MCP 子进程演练覆盖**；没有启动麦克风/真实 TTY。 |
| Provider 503 / 恢复 | 使用实际 `ResilientProvider`、OpenAI SDK 和 loopback HTTP 服务；前两次 HTTP 503，第三次返回成功。共 3 次 HTTP；attempt 账本为 2 次 unavailable + 1 次 success。未触碰生产 Provider。 | **SDK 传输层动态通过**；不代表 Agnes 远端故障切换已在生产网关上演练。 |
| Agnes 真实上游首块后断流 canary | 使用 `.env` 中配置的 Agnes HTTPS 上游，经本机临时代理转发一次无工具、12-token 上限的短请求。上游返回 HTTP 200，代理收到并转发首个真实文本事件后立即关闭 Agent 侧连接。上游 HTTP 请求 1 次；Provider 收到 `RemoteProtocolError`，attempt=`interrupted`、`tokens_output=true`、`interrupted_reason=mid_stream_error`；没有第二次请求或 fallback。 | **真实上游 + Agent 侧断流通过**；只中断本次客户端连接，没有关闭/扰动 Agnes 网关本身。 |
| Provider 流中断 | loopback HTTP 服务已发送首个文本块后停顿，触发 stream idle timeout。观察到 `ReadTimeout`、attempt=`interrupted`、`tokens_output=true`，HTTP 请求数为 1。 | **通过**：部分输出后不从头重放。 |
| 损坏数据库恢复 | 新增损坏当前 `agent.db` 后从 hash 校验通过的快照恢复：原实现会因当前 DB 不可读而拒绝恢复。现在保留损坏库原始副本、替换两库并检查一致性；新增回归测试通过。replace 中途失败仍保留回滚路径。 | **修复并通过**。 |
| Runtime 磁盘满 | 隔离 TaskManager DB 的每个连接注入 SQLite `max_page_count`，完整 TaskManager 事件写入返回 `database or disk is full`，没有半条事件，`quick_check=ok`；解除故障后 stale Run 回收为 failed。 | **Runtime 持久化层通过**；这不是操作系统真实磁盘耗尽。 |
| TUI 审批续跑 | 新增 Textual `App.run_test()` 交互闭环：输入 → `waiting_approval` → 按 Y → 记录批准 → 对同一 Run 续跑 → 显示完成答复。 | **headless TUI 端到端通过**；真实交互终端未启动。 |
| 真实 TTY 对话 | 通过 PTY 启动 `main.py --tui`，使用独立临时 Runtime DB、关闭 MCP/fallback，在真实 Agnes 模型上输入 `What is 2 + 2? Reply with only the number 4.`；消息往返为 `4`，Run=`completed`，completion check 通过、无工具调用，Provider attempt 两次均 success。之前的“只回复 OK”canary 未按精确字面返回，因此未作为验收样本。 | **物理 PTY 的真实模型对话通过**。原 `.env` 和既有会话数据库没有改动。 |
| Session 隔离/清理与策略 | MCP policy、Runtime cleanup、产品化、生产闭环和 TUI 回归纳入本轮 249 项总测试。MCP 工具配置仍按上一报告核对的 6 个服务与 allow/approval/deny 映射。 | **覆盖通过**。当前持久 `APPROVAL=off` 仍会关闭全局审批门；本轮没有更改用户此前明确设定的审批开关。 |
| 成本、Run 延迟和 trace 关联 | 隔离 Run 写入 cost event、latency metric、provider attempt 和 OTel span；trace 导出按同一 Run ID 聚合。attempt 日志模拟进程内存丢失后回灌数据库，确认 ack 后删除持久 JSONL。 | **通过**。 |
| Run P95 低于 1 秒样本 | 动态回灌演练发现墙钟时间戳精度会把 Runner 提供的 3210ms 单调时钟样本覆盖为 0。已改为取单调时钟耗时与墙钟耗时的较大值；新增回归测试，复验 P95=3210ms。 | **缺陷已修复并通过**。 |
| JSONL 轮转 | 32 条隔离记录触发 `router.jsonl` 与 `.1`–`.3` 三份备份；最新 4 条留在当前文件，旧文件按顺序轮转。 | **通过**。 |

## 回归结果

- 组合回归：**249 passed**，覆盖 MCP bridge/policy、TUI、Snapshot/Runtime cleanup、生产闭环、API Phase B、Task Runtime、Provider errors/deadline/retry、Observability、cleanup resilience。
- 新增快照损坏恢复、Run 延迟样本和 TUI 审批续跑回归均包含在组合测试内。
- `py_compile` 覆盖本轮改动文件通过；`git diff --check` 无空白错误。pytest 仍有一条既有 `cache_dir` 未知配置警告。
- 测试进程使用临时 `APPROVAL=on` 和 `MCP_SERVERS=[]`，只影响子进程；没有修改持久配置。

## 仍未验收的边界

- Agnes 真实上游首块后断流 canary 已完成，但故障注入发生在客户端与本机代理之间；**没有让 Agnes 网关自身宕机或切断其上游网络**，也没有验证生产账号的 fallback 通道。
- TUI 审批闭环通过 Textual Pilot，不是物理终端人工验收。
- Runtime 磁盘满通过 SQLite 页数上限注入，不是操作系统卷真实耗尽。
- **已修复清洁 EOF 误报成功**：HTTPX Chat Completions SSE 适配层现在校验 `data: [DONE]`。缺少结束标记时抛出 `RemoteProtocolError`，已输出内容的 Run 记录为 `interrupted`，不重放 HTTP 请求；含完整结束标记的正常流仍为 `success`。实际 Agents SDK + 本机 HTTP 动态演练见下方证据。
- **已修正 MCP 策略状态口径**：摘要同时显示配置策略与有效策略。`APPROVAL=off` 时会明确指出配置标记为 `approval` 的工具当前有效权限是直接 `allow`；保留此前用户设定，不擅自改变运行权限。该提示不表示这些工具已被改成逐项 allow 配置。
- **物理 TTY 验收已闭环**：真实 PTY + 实际 TUI 入口 + Agnes 生产模型，在隔离临时 Runtime DB 中完成一次无工具算术对话；输入输出、Provider 成功 attempt 和完成状态已相互核对。该测试禁用 MCP，仅覆盖普通对话路径。
- **操作系统卷耗尽仍未演练**：当前机器没有可用的 `New-VHD`、`Mount-VHD` 或 `imdisk` 隔离介质工具；C: 尚余约 39.9 GB，F: 尚余约 21.6 GB。没有在共享系统盘制造真实空间耗尽。此前 SQLite 页数上限故障注入仍只证明 Runtime 数据库层能处理 `SQLITE_FULL`。

## 证据

- MCP stdio 故障与重连：`var/acceptance/mcp-lifecycle-20260927/result.json`
- Provider HTTP 503 恢复：`var/acceptance/provider-http-fault-20260927/result.json`
- Agnes 真实上游首块后断流 canary：`var/acceptance/agnes-live-interrupt-20260927/result.json`
- 物理 PTY TUI + Agnes 普通对话：`var/acceptance/tty-live-canary/result.json`
- 首 token 后 Provider 流超时：`var/acceptance/provider-stream-interrupt-20260927/result.json`
- Provider attempt / Run trace 回灌：`var/acceptance/provider-trace-20260927/result.json`
- Runtime SQLite 磁盘满：`var/acceptance/runtime-disk-full-20260927b/result.json`
- JSONL 轮转：`var/acceptance/log-rotation-20260927/result.json`
- Chat Completions 清洁 EOF 中断：`var/acceptance/provider-clean-eof-20260927/result.json`
- 含 `[DONE]` 的正常 SSE 完成：`var/acceptance/provider-valid-done-20260927/result.json`
