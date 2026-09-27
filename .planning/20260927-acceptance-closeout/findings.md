# Findings

## 前序动态验收报告中的未覆盖点
- Provider/MCP：没有杀停本轮 MCP 子进程或真实 Provider 断网；退出时 observed stdio cancel-scope 错误。
- 数据库/Runtime：仅以隔离 SQLite 原语验证磁盘满、损坏；未走 Runtime 全链路或备份恢复。
- TUI：Headless suite 通过；真实交互终端与 Provider、审批后续跑未验收。
- 策略：核对总数和 deny 项，仍需逐 MCP server/tool scope 验证。
- 项 8/10：既有回归通过，但需复验 session isolation、cost/latency/Run trace correlation、JSONL rotation、Provider attempt 回灌。

## 安全边界
所有新增故障注入必须使用隔离临时数据路径和独立进程；不会在当前生产 `.env` 下调用有副作用工具。
## Implementation observations
- `chat_sync()` pre-connects MCP using one `asyncio.run(ensure_mcp())`, then starts the chat loop in another event loop. This can bind anyio task groups to a loop that is already closed.
- `chat_voice()` runs each Runtime turn and auto-compact in separate `asyncio.run()` calls, while the module-level MCP server connection survives across calls. This reproduces the loop-affinity/lifecycle hazard.
- MCP bridge currently retains connected servers but exposes no orderly async close/reset API; `main.chat_async()` closes only the SQLite session.
- The Agents SDK `MCPServerStdio` is an async context-managed lifecycle. Its streams should be entered, called and cleaned up in the same event loop/task lifetime.
- Existing `runtime.snapshot.restore_snapshot()` gives a safe recovery path, so full Runtime disk-full and corruption drills can use copies under `var/` and a disposable snapshot.
- 本地真实 MCP stdio fixture：`ping` 成功；fixture 自行 `os._exit(71)` 后调用返回 `McpError: Connection closed`，没有重放；同 loop `close_servers()` 清理错误为 0、模块状态归零；重新连接后 `ping` 成功，最终清理错误为 0。
- MCP harness 初次启动有两次 harness 脚本问题（repo sys.path 未加入、PowerShell 将换行写成字面 `\\n`），均发生在 MCP 连接之前；修复脚本后一次完成实测，原始 JSON 已保存在 `var/acceptance/mcp-lifecycle-20260927/result.json`（下一步保存）。
- `close_servers()` 观察到异常子进程退出后可干净重连，暂未复现之前的 AnyIO cancel-scope error。
## Database recovery defect
- Controlled code inspection found `restore_snapshot()` rejected a corrupted current DB before replacing it (`schema_version(...) is None`), so a verified healthy snapshot could not repair the exact corruption case from the prior report.
- Changed restore compatibility checks: compare schemas for readable current DBs, allow explicit restore from a hash-valid snapshot when a current DB is unreadable, preserve the original DB files byte-for-byte as `.pre_restore` rollback files, replace both stores, then require consistency verification.
- Added regression `test_restore_recovers_corrupted_current_database`; together with snapshot roundtrip and failed second replace rollback, targeted tests pass (3/3).
## Provider/Run observability fault drill
- Isolated the provider attempt JSONL directory via `FORGE_DATA_DIR`, recorded a failed attempt, cleared process memory to emulate restart, rehydrated it, handed it into TaskManager, exported its Run trace, then acknowledged/deleted the durable JSONL. Result has one provider attempt, one trace span, `run.cost_estimate` event, and successful ack.
- The first drill exposed `run_latency_summary.p95_ms=0` when an explicit 3210ms monotonic sample was passed: `record_run_latency()` replaced it with a whole-second wall timestamp difference. Fixed persistence to use `max(monotonic_sample, wall_clock_elapsed)` so restored Runs retain full wall span while short Runs never become false zero samples. Added regression coverage; rerun yields p95 3210ms.
- TUI real terminal interaction isn't possible in this headless execution, but `Textual App.run_test()` now drives the complete app state flow with an injected Runtime, including pause, approval key, same-Run resume and final answer; 1 passed.
