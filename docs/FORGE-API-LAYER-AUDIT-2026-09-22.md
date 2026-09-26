# FORGE API Layer Audit Report

> 审计日期：2026-09-22
> 审计范围：`F:/Byong-hermes/Byong-hermes/my_creative_agent`（排除 `.venv` / `benchmark_fixture` / 归档产物）
> 审计性质：**查验现状、识别问题、确认风险、给出整改方案** —— 本轮未修改任何代码
> 标注约定：`[活动]` = 当前默认路径下真实生效；`[潜伏]` = 被 UI 冻结层挡住，一旦 `FORGE_ENABLE_UI=1` 即刻生效

---

## 1. Executive Summary

### 1.1 一个必须先说清楚的前提

**当前默认路径下，HTTP API 层整体是停用的。**

`ui_frozen.py:37` `UI_FROZEN = True`，`webapp.py:1999-2000`：

```python
if not require_ui_enabled(sys.argv[1:]):
    raise SystemExit(2)
```

`webapp.py` 的 **66 条 `Route(`** 与 `llama_bridge.py` 的 22 条 OpenAI 兼容路由全部挂在这个被门禁挡住的 Starlette app 上。真正活动的"API"是**进程内直连**：

```
main.py / cli/app.py  →  AgentRuntime.run_turn(...)  →  Runtime 主链
```

这决定了本报告的判断口径：**Runtime 内核的问题现在就在发生；HTTP 接口层的问题是"复活即引爆"的休眠缺陷**。两者都不能按"反正没启用"处理，因为 `@` 前缀的最近 10 次提交全部在改 Runtime，而 `webapp.py:517` 那个漏传 `task_id` 的坑一直在那儿。

### 1.2 成熟度判断

| 层 | 成熟度 | 依据 |
|---|---|---|
| **Runtime 内核**（状态机 / 收敛 / 门控 / 文件权限） | **B+ 良好** | 状态写入全局单点（`task_manager.py:933`）、强制迁移校验、终态幂等、FileScope fail-closed、Write-Ahead 台账、1301 个离线测试 |
| **工具 API** | **C 有实质缺口** | 参数校验失败被记为"执行成功"、14 个写工具无审批、无统一超时、无 result schema |
| **事件 / 流式协议** | **C- 已漂移** | `run.waiting_user` 被闸口静默丢弃；同一状态两种事件名；无 event id / 无心跳 / 无游标续传 |
| **HTTP 接口层** | **D 需系统整改** | 无认证、无版本、无 OpenAPI、无统一错误结构、60+ 端点零消费者、契约测试已死 |
| **可观测性** | **C- 断链** | `run_id = trace_id` 的说法不成立；Web 路径 OTel 从未安装；`tool_calls` 缺 latency/turn_number |

### 1.3 最大风险（按"活动 × 影响"排序）

1. **工具参数校验失败被记成 `TOOL_EXECUTED`** `[活动]` —— 模型可以在"零次成功执行"下被判定为任务已完成。
2. **14 个写/副作用工具不在审批名单** `[活动]` —— `write_code_file`、`save_note`、`save_word_doc/excel/ppt`、`deep_research`、`fetch_github_repo` 可无审批持久写盘/联网落盘。
3. **`run.waiting_user` 事件被静默丢弃** `[活动]` —— 状态机说 `waiting_user`，事件协议只认 `waiting_for_user`，追问态在 UI/CLI 上表现为"什么都没发生"。
4. **`webapp.py:517` 漏传 `task_id`** `[潜伏]` —— 一旦启用 UI，每个 Run 都卡在 `submitted` 且 SSE 谎报 `run.completed`。
5. **`POST /tools` 无鉴权绕过全部门控执行工具** `[潜伏]` —— 绑 `0.0.0.0` 即等于开放本机任意工具执行。

### 1.4 是否适合继续扩展

**适合继续扩展 Runtime 能力，但不适合在现有接口层上加新消费者。**

理由：Runtime 内核的骨架（状态单点写入 + 强制迁移 + 终态幂等 + fail-closed 权限）是健康的，往上加门控/工具是安全的。但接口层现在是"三套协议并存且互不校验"（`/api/*` 命名 SSE、`/v1/*` OpenAI 帧、进程内 `stream_events_cb`），**任何新客户端接进来都会踩到至少一个漂移点**。

### 1.5 结论

> **当前 FORGE API 层是：C —— 功能可运行，但接口层需要系统整改。**

不是 D（不存在阻断性架构问题，1301 个测试全过、CLI 主链可跑完），也不是 B（接口层的漂移不是局部变量，是协议级的：`state`/`status`、`task_id` 双义、事件名两套、契约测试已死）。

---

## 2. Current API Architecture

### 2.1 真实调用链 A —— 进程内主链（**当前唯一活动路径**）

```
用户输入
  ↓  cli/app.py:265  TurnApp._run_once  /  main.py:703  (REPL 主循环)
     ✅ 传 task_id=task.id（main.py:711）   ❌ cli/app.py 未传（走 runtime 自建分支）
AgentRuntime.run_turn(...)                    runtime/runner.py:1731
  ↓
TaskManager.create_task / transition          runtime/task_manager.py:723 / 910
  ↓  （状态唯一落库点：task_manager.py:933 `UPDATE runs SET`）
execute_turn(...)                             main.py:490
  ↓
_run_attempt(...)                             main.py:403
  ↓
Runner.run_streamed(...)                      agents SDK
  ↓
ResilientModel.stream_response                runtime/provider_gateway.py:244
  ↓
AsyncOpenAI                                   .venv/.../agents/models/openai_provider.py:144
  ↓ （无显式 timeout，走 SDK 默认 httpx2.Timeout(600, connect=5.0)）
工具派发：_patch_agent_tools 包装             runtime/runner.py:1094
  ↓
门控链（详见 §7）：
  Clarification → Budget → Duplicate/Fingerprint → FileScope → Approval → 执行
  ↓
FunctionTool.on_invoke_tool                   runtime/runner.py:936-953
  ↓
工具实现                                       tools.py / code_exec.py / office_docs.py / ...
  ↓
收口：completion.py + readiness_gate.py        runtime/completion.py:413 / readiness_gate.py:1013
  ↓
终态：mark_success / mark_failure             runner.py:2250 / 2156
  ↓
stream_events_cb
  ↓
RunActivityProjector.emit                     runtime/public_activity.py:132
  ↓  落库 task_events（public.<kind>）+ 推浏览器（节流）
public_wire  ← 白名单闸口，不匹配即 return None    runtime/public_activity.py:362/371
  ↓
CLI 渲染器 on_event                            cli/render.py:68
```

### 2.2 真实调用链 B —— HTTP / llama-ui 桥（**已冻结**）

```
web/llama-ui（fetch + ReadableStream，非 EventSource）
  ↓
POST /v1/chat/completions                     llama_bridge.py:627
  ↓
run_turn（内部） → _ConvStream 缓冲            llama_bridge.py:419
  ↓
GET /v1/stream?conv_id=&from=<字节偏移>        llama_bridge.py:228（有真正的断点续读）
  ↓
_openai_chunk / _openai_done                  llama_bridge.py:566 / 584
```

### 2.3 真实调用链 C —— `/api/*` 命名 SSE（**已冻结，且无消费者**）

```
GET /api/runs/{run_id}/stream                 webapp.py:623
  ↓
_subscribe_generator                          webapp.py:558
  ↓
_LiveRunSession.feed（进程内内存字典）          webapp.py:300 / 330
  ↓
public_wire                                   runtime/public_activity.py:362
  ↓
_sse()  → "event: {name}\ndata: {json}\n\n"   webapp.py:105
```

### 2.4 三套协议并存关系图

| 通道 | 传输 | 事件名体系 | 消费者 | 续传能力 |
|---|---|---|---|---|
| 进程内 `stream_events_cb` | 直接回调 | `run.*` / `tool.*` / `assistant.delta` | CLI `cli/render.py` | 无（单进程内不需） |
| `/v1/*`（llama_bridge） | OpenAI SSE 帧 | `data: {...}` + `[DONE]` | llama-ui | ✅ 字节偏移 `?from=` |
| `/api/*`（webapp） | 命名 SSE | `event: activity/assistant_delta/control/error/done` | **无** | ❌ 只能全量重放 |

### 2.5 同步 / 异步

- 主链：**asyncio**（`uvicorn` 单 worker；CLI 用 `asyncio.run`）
- 工具：同步 `def` 由 SDK `asyncio.to_thread` 卸载（`.venv/.../agents/tool.py:2647`）—— **这是取消无法中断同步工具的根因**
- 数据库：**全部同步** `sqlite3`，在协程内直接调用，`runner.py` 全文件 0 处 `to_thread`

### 2.6 是否存在重复 API 层 / 失效 endpoint

**存在，且严重。** 详见 §3.5 与 §15。核心结论：`/api/*` 的 66 条路由中，可确认仍在被消费的只有 llama_bridge 那 22 条（且也是冻结态）。`web/runtime/` 前端目录已删除，`web/index.html.legacy.archive` 是归档不 serve，`webapp.py:244/255/260` 三个页面入口全部 301/302 跳 `/llama-ui/`。

---

## 3. Endpoint Inventory

### 3.1 活动入口（进程内，非 HTTP）

| Endpoint | Method | Request | Response | Caller | Implementation | Risk |
|---|---|---|---|---|---|---|
| `run_turn` | 进程内调用 | `text, session, session_id, mode, max_turns, history_limit, task_id, metadata, stream_events_cb` | `RuntimeResult(ok, task, assistant_text, ...)` | `main.py:703`、`cli/app.py:265`、`benchmark/eval_runner.py:108` | `runtime/runner.py:1731` | `cli/app.py` 未传 `task_id`，与 `main.py:711` 不一致 |
| `stream_events_cb` | 回调 | `(channel: str, payload: dict)` | None | Runner 内部 | `runtime/public_activity.py:132` | 事件名不受 schema 约束，漂移无报警 |

### 3.2 Run / Task / Message 主接口（`/api/*`，已冻结）

| Endpoint | Method | Request | Response | Caller | Implementation | Risk |
|---|---|---|---|---|---|---|
| `POST /api/tasks/{id}/runs` | POST | `{message\|content, client_message_id?, attachments?}` | `{ok, idempotent, task_id, run_id, stream, run}` 202 | 无（旧前端已删） | `webapp.py:707` → `_create_run_and_start:645` | **P0** `_start_run_session:517` 漏传 `task_id` |
| `POST /api/tasks/{id}/messages/create` | POST | 同上 | 同上 | 无 | `webapp.py:1736` → 同一 `_create_run_and_start` | 与上一端点重复，两者无串行化 |
| `GET /api/runs/{id}` | GET | — | `_task_payload` 13 字段 + 可选 details | 无 | `webapp.py:1778` | `state` 字段；非 debug 下 `events/tool_calls/model_calls` 静默置空（`:1785-1791`） |
| `GET /api/runs/{id}/events` | GET | `?debug=1` | 默认只返 `public.*` | 无 | `webapp.py:1800` | 审计面默认收窄 |
| `GET /api/runs/{id}/stream` | GET | — | 命名 SSE | 无 | `webapp.py:623` → `_subscribe_generator:558` | 无心跳/无 id/无游标 |
| `POST /api/runs/{id}/pause` | POST | — | `{ok, run}` | 无 | `_run_action:1816` | 陈旧读；注释自述"前端已不再暴露" |
| `POST /api/runs/{id}/resume` | POST | — | `{ok, run}` | 无 | `_run_action:1816` | 12s shield + 强制 finalize |
| `POST /api/runs/{id}/cancel` | POST | — | `{ok, run}` | 无 | `_run_action:1816` | **P0** 12s 超时后无条件标 CANCELLED，底层仍在跑 |
| `GET /api/tasks/{id}` | GET | — | `_container_payload`（`status`） | 无 | `webapp.py:1713` | **契约漂移**：容器用 `status`，Run 用 `state` |
| `POST /api/approval` | POST | `{approval_id, decision}` | `{ok}` / `{error}` 400 | 无 | `webapp.py:752` | 业务失败降级为 400 |

### 3.3 llama-ui OpenAI 兼容接口（已冻结）

| Endpoint | Method | Request | Response | Implementation | Risk |
|---|---|---|---|---|---|
| `POST /v1/chat/completions` | POST | OpenAI messages | OpenAI chunk 流 / completion | `llama_bridge.py:627` | **P0** 错误时 HTTP 200 + `finish_reason="error"` |
| `GET /v1/stream` | GET | `?conv_id=&from=<offset>` | 字节流 | `llama_bridge.py:228` | ✅ 唯一有真正续读的端点 |
| `DELETE /v1/stream` | DELETE | `?conv_id=` | `{cancelled}` | `llama_bridge.py:242` | 与 `/control` 语义重复 |
| `POST /v1/chat/completions/control` | POST | — | 取消 | `llama_bridge.py:734+` | — |
| **`POST /tools`** | POST | `{tool, params}` | `{plain_text_response}` | **`llama_bridge.py:277`** | **P0 绕过全部门控 + 无鉴权** |
| `GET /tools` | GET | — | 工具清单 | `llama_bridge.py` | 与 `/api/tools` 同名不同义 |
| `GET /v1/models` `/props` `/slots` `/models/*` | GET | — | 能力信息 | `llama_bridge.py` | 无鉴权 |

### 3.4 其余 `/api/*`（已冻结，无消费者）

`/api/sessions`、`/api/history`、`/api/stream`、`/api/approvals`、`/api/artifacts*`、`/api/projects*`（18 条）、`/api/worklocations*`、`/api/tools`、`/api/runtime/status`、`/api/schedules`、`/api/memories*`、`/api/settings/memory`、`/api/search`、`/api/notifications`、`/api/tasks/{id}/archive|pin|restore|delete|events|pause|resume|cancel`。

### 3.5 已失效 / 历史遗留清单

| 位置 | 对象 | 状态 |
|---|---|---|
| `webapp.py:550/614/1357/1891` | `GET ?message=` 建 Run | 已废弃，返回 error 事件（**改造正确**） |
| `webapp.py:244/255/260` | `/` `/chat` `/runtime` | 301/302 → `/llama-ui/` |
| `webapp.py:1961-1965` | legacy run-level 5 条 | 注释"旧页面仍可用"**已过期**，旧页面不存在 |
| `web/runtime/` | 旧前端目录 | **已删除** |
| `web/index.html.legacy.archive` | 旧页面 HTML | 归档，不 serve |
| `web/shared/markdown.js` | 共享渲染器 | `llama_bridge.py` 零引用，只被 `tests/markdown_runner.js` 加载 |
| `tests/test_contract.py:22` | 契约测试 | 因 `web/runtime/types.js` 不存在**永久 skip** |

---

## 4. Runtime API Findings

### 4.1 状态机真实定义（以代码为准）

`runtime/task.py:18-26` —— 全项目唯一枚举，**8 个状态**：

```python
class TaskState(StrEnum):
    SUBMITTED = "submitted"          # ← 用户说的 queued 在代码里叫这个
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"                # ← 用户没列，代码里有
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

| 用户列举 | 真实名称 | 结论 |
|---|---|---|
| queued | **`submitted`** | ❌ 无 queued |
| running | `running` | ✅ |
| waiting_user | `waiting_user` | ✅ |
| waiting_approval | `waiting_approval` | ✅ |
| completed / failed / cancelled | 同名 | ✅ |
| — | **`paused`** | ⚠️ 多一个 |

迁移表 `runtime/state_machine.py:10-27`，校验 `assert_transition` 在 `task_manager.py:920` **强制执行**，非法迁移抛 `AgentError`，绝不静默放行。**这是本项目做得最扎实的部分。**

### 4.2 逐项回答用户的 7 个问题

| 问题 | 结论 | 证据 |
|---|---|---|
| Run 已结束但 API 仍显示 running？ | **会**（HTTP 路径）。`webapp.py:517` 漏传 `task_id` → 新建分支 → 原 Run 永留 `submitted`；`webapp.py:462` `else: # completed` 兜底把它广播成 completed | `webapp.py:517/462`；对照 `main.py:711` 正确 |
| 可能重复提交 Run？ | **会，但有条件**。前一个 Run 处于 `waiting_user` 时不被 `ACTIVE_RUN_STATES` 拦（该元组漏了 `waiting_user`）→ 2 个并发 Run | `task_manager.py:815`；DB 实证 `waiting_user` 54 条 |
| retry 会创建重复任务？ | **不会**。幂等靠 `client_message_id`（`task_manager.py:477` 唯一主键），但**完全可选**，不带就退化成新建 | `webapp.py:654-665` |
| 多请求同时改同一 Run？ | **会**。`transition()` 是 read-modify-write，跨两个连接，UPDATE 无 `WHERE state=?`，`runs` 表无 version 列 | `task_manager.py:917/920/932-933` |
| 刷新页面能否恢复真实状态？ | **能恢复状态**（DB 持久化，`_subscribe_generator:568` 从 DB 重放）；**但丢失在飞事件流与取消句柄**（`_LIVE_RUNS` 是内存字典） | `webapp.py:330/568-579` |
| 中断后 Run 是否可继续？ | **部分可**。`RESUMABLE_FROM` 含 `submitted/running/waiting_user/waiting_approval/paused`；但 resume 后 `live.history` 归零，resume 前的活动历史不再可见 | `state_machine.py:29-34`；`webapp.py:502` |
| clarification 有明确 API 状态？ | **状态有**（`WAITING_USER` + `run.terminal.kind="needs_user_input"`），**但事件名漂移**：发的是 `run.waiting_user`，闸口只认 `run.waiting_for_user` → **静默丢弃** | `public_activity.py:311` vs `:371` |

### 4.3 终态收口完整性

| 路径 | 是否写终态 |
|---|---|
| 正常完成 `_succeed` | ✅ `runner.py:2250` |
| 通用异常 `except Exception` | ✅ `runner.py:2949→2987` |
| `ConvergenceTerminated` / `CompletionReadyTerminated` / `FinalResponseFailed` | ✅ |
| 审批拒绝 / 待审批 / 待输入 | ✅ / 非终态（设计如此） |
| `asyncio.CancelledError` | ✅ `spawn_run_task:1642-1645`（且正确重抛，未被吞） |
| **`BaseException`（SystemExit/KeyboardInterrupt/MemoryError）** | ❌ **穿透，终态不写，Run 永久卡 running** |

`runner.py:2949` 是 `except Exception`，接不住 `BaseException`；`run_turn` 主体**无 `finally`**。代码自己承认这类风险存在（`runner.py:2307-2314` 注释："沙箱里删文件可能抛 SystemExit…若让它冒泡会终止整个 uvicorn 进程"）。

### 4.4 终态幂等（正面）

`runner.py:2134-2155` `_fail` 与 `runner.py:1666-1667` `finalize_cancelled` 都有终态预检，注释明确写了"修复 cancel 与 fail 双收口时的竞态"。**正确，不要动。**

---

## 5. Streaming Findings

### 5.1 实现

- **无 WebSocket**（全仓业务代码 0 命中）
- **三套 SSE**：见 §2.4
- 帧格式 `webapp.py:105`：`event: {name}\ndata: {json}\n\n` —— **无 `id:`、无 `retry:`**
- 事件泵 `webapp.py:558-603`：`asyncio.Queue` + 20Hz 忙轮询（`await asyncio.sleep(0.05)`）
- 背压：`q.qsize() > 5000` 时**静默丢弃最旧一条**，无日志无计数（`webapp.py:318-327`）

### 5.2 心跳 / 重连 / 顺序

| 能力 | 状态 | 证据 |
|---|---|---|
| heartbeat / keepalive | ❌ **未找到** | 全仓 grep 0 命中；队列空时不下发任何字节 |
| `Last-Event-ID` | ❌ | `_sse()` 不产 `id:`；全仓 grep 0 命中 |
| 断线续传 | ⚠️ 只能全量重放 | `_subscribe_generator(run_id)` 无游标参数 |
| event id 单调 | ❌ `uuid4`，且重放时**重新生成** | `public_activity.py:135` / `:373` |
| 顺序保证 | ✅ 单队列 FIFO | — |
| 重复事件 | ⚠️ 重放与实时流可能重叠，无去重键 | — |
| completion event | ⚠️ `type=="runtime.done"` 是退出条件；但也有 `done` 通道名 | `webapp.py:592` |
| error event | ⚠️ HTTP 200 + `_sse("error",...)` | 9 处，见下 |
| 断连后后端继续？ | ✅ **设计如此**：`webapp.py:601` 注释"断连 ≠ 取消：这里不做任何 DB 状态变更" | 无 `is_disconnected` / `GeneratorExit` 处理 |
| 取消传到底层？ | ⚠️ 异步工具能，同步工具不能 | `runner.py:950-953` + `asyncio.to_thread` |

### 5.3 当前所有 event type（完整去重清单）

**传输层 `event:` 行**（5 个）：`activity` / `assistant_delta` / `control` / `error` / `done`

**载荷 `type`**（`RunActivityProjector.emit` 的 kind，落库为 `public.<kind>`）：
`run.started`、`phase.started`、`phase.completed`、`tool.started`、`tool.completed`、`tool.failed`、`files.changed`、`verification.started`、`verification.completed`、`verification.failed`、`status.update`、`run.waiting_for_user`、`response.generating`、`assistant.delta`、`assistant.reset`、`run.<state>`

其中 `run.<state>` 展开为：`run.submitted` / `run.running` / **`run.waiting_user`** / `run.waiting_approval` / `run.paused` / `run.completed` / `run.failed` / `run.cancelled`

**`public_wire` 白名单（`public_activity.py:371`）**：
```
runtime.done, runtime.error, approval.required, assistant.reply,
run.started, run.completed, run.failed, run.cancelled,
run.waiting_approval, run.waiting_for_user, source.not_ready
```

**被静默丢弃（`return None`）的真实事件**：
`task.cancelled`、`task.failed`（`webapp.py:393/401/457/461` 真发了）、**`run.waiting_user`**、`tool`、`stream_reset`、`final.content.delta`

> **这是本次审计最典型的"接口能跑但 contract 已漂移"**：生产端真的在发 `run.waiting_user`，闸口真的在丢，双方都觉得自己没错。

**`task_events.event_type`**（落库层）：`public.*` 全集 + `task.created/result/failed/checkpoint/recovered/<state>`、`task.readiness`、`tool.invocation`、`tool.side_effect_unknown`、`tool.blocked_needs_user_input`、`tool.permission_error`、`approval.executed/suspended`、`convergence.forced`、`run.terminal`、`run.terminal_idempotent_skip`、`run.token_usage`、`provider.model_attempts` 等

**终态 kind**（`terminalization.py:31-44`，14 种）：`completed`、`needs_user_input`、`needs_approval`、`refused`、`cancelled`、`timeout`、`provider_error`、`parser_error`、`completion_rejected`、`no_progress`、`bounded_failure`、`final_response_failed`、`token_budget`、`unknown`

### 5.4 统一事件协议建议

```
event: message
id: <task_events.id 单调序号>      ← 用 DB 自增 id，不要用 uuid4
data: {
  "v": 1,
  "run_id": "...",
  "seq": <单调 int>,
  "type": "<domain>.<action>",     ← 单一命名空间，不再有 run.xxx / task.xxx 两套
  "status": "running|ok|error|waiting_user|waiting_approval",
  "ts": "...",
  "payload": {...}
}
```
外加：`: heartbeat\n\n` 每 15s；`retry: 3000`；`GET /stream?since=<seq>` 游标续传。

---

## 6. Provider Integration Findings

### 6.1 抽象层：有收口，但身份判断仍散落

`runtime/provider_gateway.py:518` `ResilientProvider(OpenAIProvider)` + `ResilientModel(Model)` —— 是**装饰器式包装**，不是多 Provider 接口抽象。无 `ProviderAdapter` 基类 / 注册表 / Protocol。全项目**无 Anthropic 业务引用**。

**业务代码里直接判断 provider/model 身份的分支共 12 处**：

| 类型 | 位置 |
|---|---|
| 按 `base_url` 判能力 | `provider_gateway.py:193`、`agent.py:78`、`reply_parser.py:425-429` |
| 按 `model` 判身份 | `router.py:103`、`runner.py:443` |
| 按 `env` 字符串判偏好 | `agent.py:336-337`、`agent.py:354` |
| **按异常模块名判来源** | `runner.py:2956`、`terminalization.py:86`（`type(exc).__module__.startswith("openai")`） |
| **按错误文本子串判来源** | `runner.py:2957-2959`、`terminalization.py:88-89`（`any(m in text for m in ("Error code:", "No available channel", ...))`） |

后两类在**两个文件里各抄了一份** —— 改一处忘另一处即漂移。这是"Provider 泄漏到上层业务代码"的真实形态。

### 6.2 5 个辅助直连点绕过 Provider 抽象

`compact.py:98`（90s）、`multimodal.py:57`（90s）、`research.py:55`（120s）、`runtime/codex_loop.py:45`（120s）、`runtime/reply_parser.py:115`（60s）—— 全部 `from openai import OpenAI` 新建**同步**客户端，**不走 `ResilientModel`** ⇒ 无总墙钟 deadline、无 fallback、无首 token/空闲超时、无 `provider.model_attempts` 审计。

### 6.3 超时

**主链 HTTP 超时从未显式设置。** `agents/models/openai_provider.py:144-152` 与 `provider_gateway.py:536-545` 构造 client 都不传 `timeout` ⇒ 走 SDK 默认 `httpx2.Timeout(timeout=600, connect=5.0)`。

而 `FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS` 默认也是 **600s**（`provider_gateway.py:213`）—— **两者完全相等**，一次挂死请求即可吃满全部 provider 预算，且 deadline 只在"尝试之间"检查（`:272`/`:458`），**无法中断已在飞行的那一次请求**。

`.env` 与 `.env.example` 中**完全没有** TIMEOUT/RETRY 相关配置，全部走代码默认值，未做生产标定。

### 6.4 流式 / usage / overflow

- 首 token 超时 90s、空闲 120s（`provider_gateway.py:262-263`）✅
- **首 token 后失败不重放**，记 `interrupted` 后上抛（`:346-354`）✅ 语义正确
- malformed JSON：入站解析完全依赖 SDK，项目自身**无畸形帧兜底**
- usage：主链有（`runner.py:1920/2492`、`token_gate.py:61`）；**但 `llama_bridge.py:663` 伪造**：`"prompt_tokens": 0, "completion_tokens": len(content)` —— 用字符数冒充 token 数
- context overflow：`provider_errors.py:60` `is_context_overflow` → `main.py:531-545` 压缩后重试一次，**分层正确**

---

## 7. Tool API Findings

### 7.1 请求中描述的门控链：逐项确认

| 环节 | 状态 | 证据 |
|---|---|---|
| Tool Call | ✅ | `runner.py:1094-1119` `_patch_agent_tools` |
| **Clarification Gate** | ✅ 已实现（正则判定，非 LLM） | `runner.py:364-386`、`readiness_gate.py:263` 9 类场景规则 |
| **Budget Gate** | ✅ | `runctx.py:228` `can_execute_tool`；总 20 / web_search 3+5 / per-tool 表 |
| **Duplicate / Fingerprint Gate** | ⚠️ **部分**（3 层，生产只开 1.5 层） | 精确冗余 Guard A `.env:49 FORGE_REDUNDANT_GUARD=off` ❌；per-target cap=3 ✅；破坏性同参重放 ✅（仅 3 个工具） |
| Execute | ✅ | `runner.py:936-953` |
| Result | ❌ **无统一 schema**，全部 `-> str` | 38 个工具全部返回字符串 |
| Progress / Convergence | ✅ 四级收敛 | `readiness_gate.py:1013` |

### 7.2 参数校验（**关键缺陷**）

校验点只在 SDK 内层（`.venv/.../agents/tool.py:2612-2630`），项目自定义 wrapper `runner.py:1104-1111` **直接替换 `on_invoke_tool`，无任何校验**。

实测行为（真实执行）：

| 输入 | 结果 |
|---|---|
| `{"max_chars":100}`（缺 `path`） | 返回字符串 `'An error occurred while running the tool...'` |
| `{"path":123}`（类型错） | 同上 |
| `{"path":"x",...,"bogus_unknown":"zzz"}` | **静默忽略未知字段，正常执行** |
| `{"path":"agent.py","max_chars":"500"}` | **静默 str→int 强转，正常执行** |

**连锁后果（P0）**：SDK 兜底函数（`agents/tool.py:1872`）把 `ModelBehaviorError` 转成普通字符串，**不抛异常** ⇒ `runner.py:1012` 记为 `TOOL_EXECUTED` ⇒ `completion.py:493` 把它算作"执行证据" ⇒ **模型可在零次成功执行下被判定为已完成**。

### 7.3 工具 result schema / 异常伪装

**13+ 处 `except → return "错误：..."`**，全部被记为 `TOOL_EXECUTED`：
`office_docs.py:351/355/404/469/482/495`、`rag.py:738/764`、`github_fetch.py:271`、`multimodal.py:111`、`research.py:265`、**`sandbox_snapshot.py:240`（破坏性工具 `sandbox_rollback`，回滚失败也返回"成功形状"）**

（runner 层 `runner.py:965-984` 是记录后原样上抛，正确；问题在工具内部自己 catch 的那层。）

### 7.4 Approval

- 默认**开启**（`approval.py:106`，`.env` 未设 `APPROVAL`）
- 需审批 8 个：`code_loop`、`edit_project_file`、`forget_memory`、`run_python`、`run_tests`、`sandbox_rollback`、`schedule_remove`、`write_project_file`
- **不在审批名单的写/副作用工具（实测 14 个）**：
  `write_code_file`、`save_note`、`save_word_doc`、`save_excel_workbook`、`save_ppt_deck`、`deep_research`、`fetch_github_repo`、`index_workspace`、`remember`、`sandbox_snapshot`、`schedule_add`、`schedule_set_enabled`、`search_sources`、`think`
- **`FORGE_TRUSTED_CODE_ROOTS` 审批绕过在生产生效**：`.env:22` 指向 `benchmark_fixture`，`approval.py:192-203` 让该目录下 `run_python/code_loop/run_tests` **完全无审批直接执行**（评测配置泄漏进生产）

### 7.5 Timeout / Cancellation / 幂等

- **统一工具超时：无**。`runner.py:936-953` 无 `wait_for`；`spec.py:26/179` 声明的 `timeout_seconds=30` **从未被任何代码读取**。`index_workspace`、`search_documents`、`save_*`、`write_project_file` 全部无超时
- 唯一兜底是 Run 墙钟 1800s，且 `budget.py:31` `if limit <= 0 or limit >= 3600: return await coro` —— **设成 ≥3600 会静默失去唯一的卡死兜底**
- **取消无法中断同步工具**：`run_tests`/`code_loop` 是 `def`，经 `asyncio.to_thread` 执行，`task.cancel()` 只取消 Future，**线程与子进程继续跑完**，副作用已落盘
- 幂等：大部分非幂等（`save_note` 时间戳命名、`run_python` 写临时文件、`schedule_add` 每次新 id）
- 备份：仅 `project_edit.py:103` 两个工具；dry-run：仅 `sandbox_rollback` 一个

### 7.6 权限（**这部分做得好**）

- 三层路径拦截：工具级 `_resolve_under_root`（`tools.py:131`）+ Run 级 FileScope（`filescope.py:183`，`realpath`+`normcase` canonical 比较，**fail-closed** 于 `:224-229`、`:593-603`）+ 沙箱（`code_exec.py:157`、`project_edit.py:68`）
- **SSRF：出网点全部硬编码**（`api.tavily.com` / `lite.duckduckgo.com` / `www.bing.com` / `api.github.com`），无用户可控 URL 工具 ✅
- 子进程密钥剥离 ✅（`code_exec.py:168`）；**全项目 `shell=True` 0 命中** ✅；Windows Job Object 杀进程树 ✅
- Prompt injection：`trust.py:42` `tag` 覆盖 11 类数据源；**但 `_DANGEROUS_PATTERNS`（`trust.py:19-24`）是死代码，无调用点**；`episode_recall.py:29` 只用 `sanitize` 无 tag 边界

### 7.7 工具目录漂移（实测）

```
MISSING_IN_CATALOG:     ['search_sources', 'think']
CATALOG_NOT_REGISTERED: ['scan_dependencies']
```
`spec.py:176` 对未登记工具给保守默认 `side_effect=True` ⇒ 明确"零副作用"的 `think`（`tools.py:687`）被判 `SIDE_EFFECTING` ⇒ 在 DISCOVERABLE/NEEDS_USER 下被 Readiness 门**拦掉**。

（工具总数：实测注册 **38**；文档 `CURRENT_RUNTIME_ENTRYPOINTS.md:11` 说 37，`ARCHITECTURE_REVIEW_2026-09-19.md:73` 说 41 —— 三处不一致。）

---

## 8. Error Handling Findings

### 8.1 底层 exception 如何到达用户（真实链路）

```
SDK/Provider 异常
 → provider_gateway._mark() 挂 provider_kind/provider_public/provider_request_id  (:157-164)
 → runner.execute 的 except Exception                        (runner.py:2954-2987)
 → text = public or text   ← 非 provider 错误时 text = str(exc) 原样   (runner.py:2984)
 → _fail(text, ...)                                          (runner.py:2986)
 → mark_failure(task.id, str(error_text)[:2000])             (runner.py:2156)
 → webapp._synthesize_run_end: err = (run.error_message or "任务失败")[:300]  (webapp.py:459)
 → SSE: feed("run.failed", {"message": err})                 (webapp.py:460)
```

### 8.2 `_friendly_error` 是死代码（**P0**）

```python
# webapp.py:109
def _friendly_error(exc: Exception) -> str: ...
```

全项目引用：
```
tests/test_webapp.py:49   def test_friendly_error_classification
tests/test_webapp.py:52   text = webapp._friendly_error(MaxTurnsExceeded(...))
webapp.py:109             定义
```

**生产路径零调用。** 即使被调用，兜底分支也是 `str(exc)` 原样透传（`:118-121`）。

### 8.3 无全局异常处理

`webapp.py:1907` `app = Starlette(routes=[...])` —— **无 `middleware=`、无 `exception_handlers=`**；grep `Middleware|exception_handler|ServerError` 在 `webapp.py`/`llama_bridge.py` 0 命中；`uvicorn.run(..., log_level="warning")` 未开 debug。

⇒ 未覆盖异常由 Starlette 默认返回 **HTML/纯文本 500**，前端 JSON 解析崩溃。

### 8.4 是否有统一错误结构 —— **没有**

全项目 `retryable` 仅 6 处命中，**全部是异常类型元组语义**（`resilience.py:59/72/78/85`、`mcp_bridge.py:223`、`broker.py:85/113`），**不是响应字段**。响应体只有 `{"error": "<字符串>"}`。

前端无法区分 429（可重试）与 401（需改配置），只能盲重试或干脆不重试。

### 8.5 HTTP 状态码语义（**不是"全是 500"，是"几乎不用 500"**）

`grep -o "status_code=[0-9]*" webapp.py` 分布：
```
404 × 29   400 × 26   201 × 5   409 × 4   变量 × 4
200 × 3    301 × 2    500 × 1   410 × 1   302 × 1   202 × 1
```
`JSONResponse(` 共 127 行，其中 **54 行未指定 status_code（默认 200）**。

**真问题在两端**：
- 业务失败被**降级为 400**：`webapp.py:684` 创建 Run 失败、`:752` 审批失败、`:1612` promote 失败
- **9 处错误以 HTTP 200 的 SSE 返回**：`webapp.py:551/564/618/630/1352/1360/1363` + `api_task_stream` 的 4 处（`1886/1895/1900` 等）。其中 `container is None` 应为 404、"run 不属于该 Task" 应为 403/404，全部降为 200
- `llama_bridge.py:691-696`：运行错误发 `finish_reason="error"` chunk + `[DONE]`，**HTTP 仍 200** ⇒ OpenAI 协议客户端会把失败当成功收尾

### 8.6 泄漏检查

| 项 | 结论 |
|---|---|
| 内部绝对路径 | ⚠️ **泄漏**。非 provider 异常（如 `FileNotFoundError: F:\...\xxx.docx`）经 `str(exc)` 进 SSE，截断 300 字符（`webapp.py:459`）；`/api/runtime/status` 直接返回 `sandbox_root`/`materials_dir`/`db_path`（`webapp.py:956-957`）；`webapp.py:101` 返回 `type(exc).__name__: {exc}` |
| prompt | ✅ 未发现回填前端 |
| API key | ✅ 无硬编码，100% `os.getenv`，`.env` 已 gitignore；`config_summary` 只暴露后 4 位（**但 key 长度 ≤4 时等于明文**，`provider_gateway.py:190/195`） |
| stack trace | ✅ **未发现** `traceback.format_exc()` 进响应或结构化日志（仅 `cli/tui/app.py:1071` 打 stdout） |
| 全包 500 | ❌ 不是（只有 1 处 500） |

---

## 9. Retry / Timeout / Idempotency Findings

### 9.1 五层 retry 全清单

| 层 | 位置 | 次数 | 退避 |
|---|---|---|---|
| **L0 SDK 内建** | `.venv/.../openai/_base_client.py` | `DEFAULT_MAX_RETRIES=2` ⇒ **每次尝试 = 3 次 HTTP** | `min(0.5*2^n, 8s)` + 抖动 |
| **L1 Provider 主链** | `provider_gateway.py:441-515` | 429/5xx=3；timeout/network=2；401/403/400/overflow=1；可 +1 fallback ⇒ **最多 6 次尝试** | `{0:0.5, 1:1.2}`（`provider_errors.py:204`） |
| **L2 辅助直连** | `provider_errors.py:250-277` | 护栏 6 次 | 同上，同步 `time.sleep` |
| **L3 Runtime 格式闸** | `main.py:490-528` | `while attempt < 3` ⇒ 最多 **2 次 LLM 完整重跑** | 无退避，立即重试 |
| **L3' 上下文溢出** | `main.py:531-545` | 额外 1 次 | — |
| **L4 工具层** | `resilience.py:54-98` | 3 次 | `min(8.0, 0.5*2^(n-1))` + full-jitter |
| **L5 Web/API** | — | **0** | — |
| **L6 前端** | llama-ui bundle | 1 次（503）/ MCP 2 次 | 单次 setTimeout / 1s→30s×1.5 |

### 9.2 放大推导

```
1 次用户消息
 × L3 最多 2 次重跑
 × L1 最多 6 次尝试
 × L0 每次 3 次 HTTP
 = 最坏 36 次 HTTP 请求 / 用户一轮
```

**L0 是 Provider 层完全不知情的一层** —— `provider_gateway.py` 的 `record_attempt` 只记 L1 的 6 次，**真实 HTTP 次数不可观测**。墙钟上被 `FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS=600` 与 Run 墙钟 1800s 双重截断。

前端（1~2 次）与 Web 层（0 次）**不是主要放大源**。

### 9.3 有副作用接口的幂等性

| 接口 | 幂等机制 | 一次重试是否执行两次 |
|---|---|---|
| `POST /api/tasks/{id}/runs`（send message） | `client_message_id` 唯一主键（`task_manager.py:477`），**可选** | **不带 key 时会** |
| `write_project_file` | 覆盖写 ✅ | 否 |
| `edit_project_file` | ❌ 多次替换结果不同 | **会** |
| `save_note` | ❌ 时间戳命名 | **会**（同 Run 内 `runner.py:388-404` 有补偿） |
| `run_python` | ❌ 写临时文件 | **会** |
| `sandbox_rollback` | ❌ `rmtree` 不可撤销 | **会**（有 `DUP_GUARD_TOOLS` 同参重放拒绝，`runner.py:873`） |
| `POST /api/projects/{id}/delete` | ❌ 无 | **会**（但删两次≈删一次） |
| `POST /api/approval` | ⚠️ 靠 `approval_id` | 否 |
| 上传 | ❌ 无 idempotency key | **会** |

---

## 10. Concurrency Findings

### 10.1 并发模型

- **asyncio 单进程单 worker**（`webapp.py:2029` 未传 `workers=`）
- `webapp.py:331` 用 `asyncio.Lock()`，非 `threading.Lock`
- SQLite：WAL ✅、`sqlite3.connect(timeout=10)` ✅、**无连接池**（`_connect()` 每次新建，93 处调用点）、**无 `BEGIN IMMEDIATE`**（DEFERRED 隐式事务，读写锁升级风险）、只捕获 `IntegrityError` **不捕获 `OperationalError`**

### 10.2 Race Condition 清单

| # | 级别 | 竞态 | 证据 |
|---|---|---|---|
| **R1** | P0 | webapp 漏传 `task_id` → Run 卡 `submitted`，SSE 谎报 completed | `webapp.py:517` vs `main.py:711` |
| **R2** | P0 | `transition()` 无 CAS：校验在连接 A、写入在连接 B；UPDATE 无 `WHERE state=?`；无 version 列 ⇒ 并发 cancel/complete 可双写 | `task_manager.py:917/920/932-933` |
| **R3** | P0 | `run_turn` 无 `finally`，`BaseException` 穿透 ⇒ 终态不写 | `runner.py:2949` + `:2307-2314` 注释 |
| **R4** | P1 | `waiting_user` 不在 `ACTIVE_RUN_STATES` 也不在唯一索引 ⇒ 同容器 2 个并发 Run | `task_manager.py:815`、`:485`；DB 实证 54 条 |
| **R5** | P1 | 幂等完全可选；`find`→`insert` 之间 SELECT-then-INSERT 竞态抛的 `IntegrityError` 未显式捕获 | `webapp.py:654-665`、`task_manager.py:843-859` |
| **R6** | P1 | `auto_recover` 只在进程启动时跑一次，固定 900s 阈值；`updated_at` 只在 transition 更新 ⇒ **正常长跑 Run（墙钟 1800s）重启后被误判崩溃强标 failed** | `task_manager.py:1111-1117`、`:1197` |
| **R7** | P1 | `_LIVE_RUNS` 进程内字典，多 worker 各一份、重启全丢 ⇒ 刷新后无法再暂停/取消在飞 Run | `webapp.py:330/352-363` |
| **R8** | P1 | 无 `BEGIN IMMEDIATE`；`create_task` 只捕 `IntegrityError` | `task_manager.py:494-502`、`:761` |
| **R9** | P2 | `_run_action` 陈旧读（`:1831` 读一次快照复用到底）；cancel 分支 `:1843` 有 await 让出点 | `webapp.py:1831-1848` |
| **R10** | P2 | `_start_run_once` 单飞用普通 set；`_LIVE_LOCK` 声明后未见 `async with` 使用 | `webapp.py:330-344` |
| **R11** | P2 | `messages/create` 与 `runs` 两入口共用同一函数，无 per-container 锁 | `webapp.py:718/1750` |
| **R12** | P2 | `client_messages` 登记晚于 Run 创建 ⇒ 中途崩溃则幂等失效 | `webapp.py:675-696` |

### 10.3 cancel 与 complete 同时发生（单进程下结论）

**结果正确**：`webapp.py:1846` 有重读 + `:1847` 终态判断 + `finalize_cancelled` 自身幂等（`runner.py:1666`）⇒ complete 赢则 cancel 让位。
**但跨进程不可靠**：`R2` 导致两处 `assert_transition(RUNNING, ...)` 可能都通过 ⇒ 出现"`final_output` 已完成但 `state=cancelled`"。

### 10.4 optimistic UI

前端无 optimistic UI 逻辑（llama-ui 是 fetch + ReadableStream 直读）；但后端存在**伪 optimistic**：`webapp.py:462` `else: # completed` 兜底把未知状态一律当 completed 广播。

---

## 11. Security Findings

**前提**：当前 `127.0.0.1` + 无 CORS + 无外部消费者，是**自洽的单用户本机信任模型**。但 `--host` 是命令行参数（`webapp.py:1989`）且**无代码级阻断**。

| 项 | 状态 | 证据 |
|---|---|---|
| **Authentication** | ❌ **完全没有** | `webapp.py:1907` 无 `middleware=`；`llama_bridge.py:75-81` 的 Bearer **只做 session 哈希键，不校验**（任意 `Bearer a` 接受） |
| **Authorization** | ❌ | 无用户/角色概念；容器归属不校验（`api_material_upload` 无项目校验） |
| **CORS** | ➖ 无（设计上不需要，同源托管） | grep 0 命中；`Mount("/llama-ui")` 同源 |
| **CSRF** | ❌ **无 token** | grep `csrf|xsrf` 0 命中 ⇒ 任意网页可 `fetch(POST /api/projects/{id}/delete)` |
| GET 写库 | ✅ **已清零** | `_sse_deprecated_create`（`webapp.py:550`）+ 三处拦截（`:614/1357/1891`），**改造正确** |
| API key / secret | ✅ 无硬编码、无日志打印、`.env` gitignore | 唯一隐患：`config_summary` 后 4 位对超短 key 等于明文 |
| **文件上传** | ⚠️ | `_safe_upload_name`（`:1522`）防穿越 ✅（basename + 扩展名白名单 + 控制字符）；**但先 `await request.body()` 全量读入内存再判大小**（`:1564→1568`，50MB；`api_material_upload:76→82`，20MB）⇒ 无 `Content-Length` 前置校验，单次 POST 可耗尽内存 |
| 上传归属 | ⚠️ `api_material_upload`（`webapp.py:70-102`）**未复用 `_safe_upload_name`**，只做 `Path(name).name`，且**无项目归属校验** | 对比 `api_project_sources_upload:1387` 有 404 校验 |
| MIME | ⚠️ 仅按文件名 `guess_type` | `webapp.py:1579/1408` |
| **命令执行** | ✅ 硬化较好 | `shell=True` **0 命中**；`_sanitized_env` 剥离密钥；Job Object 杀进程树。**但 `run_python` 实质是完整本机 RCE**，`netpolicy.py:4-8` 自承"OS Network Enforcement 未实现"，默认策略 `approval` 放行 |
| **SSRF** | ✅ 出网点全硬编码 | `tools.py:206/217/249`、`github_fetch.py:69/105`；无用户可控 URL 工具；Zip-Slip 已防（`github_fetch.py:110-130`） |
| **Prompt injection** | ⚠️ 覆盖 11 类数据源，但 `_DANGEROUS_PATTERNS`（`trust.py:19-24`）**是死代码无调用点**；`episode_recall.py:29` 无 tag 边界 | — |
| Rate limit | ❌ 无 | — |
| **payload size** | ⚠️ 消息 20000 字符（`webapp.py:284`）；上传见上 | — |

---

## 12. Observability Findings

### 12.1 能否串联 user request → task_id → run_id → message_id → model request → tool call → tool result → final response

**部分能。** 串联靠 `run_id`（`task_xxxxxxxx`），但：

| 环节 | 可追踪？ | 载体 |
|---|---|---|
| user request → run_id | ✅ | `task_events` / `messages` |
| run_id → message_id | ✅ | `messages.run_id` |
| run_id → model request | ⚠️ | `model_calls`（有 `turn_number`/`latency_ms`/tokens） |
| run_id → tool call | ✅ | `tool_calls` + `task_events(tool.invocation)` |
| tool result | ⚠️ | `result_excerpt` 截断 1000；`task_events` 里 `result_summary` 截断 200 |
| final response | ✅ | `checkpoints` / `task.result` |

### 12.2 `run_id = trace_id` —— **不成立（P0）**

- run_id 格式：`task_manager.py:723` `id = "task_" + uuid.uuid4().hex[:8]`
- OTel trace_id 来自 SDK（`observability.py:181`），**`runner.py` 中 grep `trace_id` = 0 命中**，主链从不设置也不读取
- `trace_export.py:119` `if str(rec.get("trace_id") or "") == run_id` ⇒ **恒为假，`spans` 永远是 `[]`**
- `install_local_tracing` **只在 `main.py:1046` 调用**，`webapp.py` 从未安装 ⇒ **Web 路径 OTel 完全关闭**（已验证 `traces/` 目录不存在）

### 12.3 表结构缺口

```sql
-- task_manager.py:181-189
CREATE TABLE tool_calls (
  id, task_id, tool_name, arguments_json, status, result_excerpt, created_at
);  -- + 迁移补的 invocation_id
```

| 用户要求字段 | 有？ |
|---|---|
| `tool_name` | ✅ |
| `arguments` | ✅（截断 8000，**已 redact**） |
| `normalized_arguments` | ❌（只在 `task_events` payload 里，**且未 redact**） |
| `status` | ✅ |
| `latency` | ❌ **无**（对比 `model_calls` 有 `latency_ms`） |
| `result_excerpt` | ✅（截断 1000） |
| `fingerprint` | ❌（`task_events` payload 里有 `result_fingerprint` 前 16 位） |
| `progress_event` | ❌（`task_events` payload 里有） |
| `replan_count` | ❌ **全项目 runtime/ 下 grep `replan` 0 命中** |
| `turn_number` | ❌ **无** ⇒ 无法对齐到模型轮次 |

**`task_id` 列语义重载**：`tool_calls`/`model_calls`/`task_events` 里装的是 **run_id**，而 `messages.task_id` 装的是**容器 id**。同列异义，join 极易错。

### 12.4 "为什么调了 7 次 web_search" —— 能回答一半

**能查到**（`task_events` 中 `tool.invocation`）：7 行的 `tool_name`、`normalized_args`（600 字符，含真实 query）、`result_fingerprint`、`progress_event`、`canonical_target`、`workspace_epoch`、`execution_status`。

**查不到**：
1. 每次耗时（`latency_ms` 无）
2. 是哪一次模型决策触发的（`turn_number` 无）
3. 重复守卫是否命中过（`tools.py:82` `_too_repetitive` 用**模块级内存字典** `_last_repeat_calls`，不落库）
4. `replan_count`（不存在）

### 12.5 Redaction 不一致（P0）

- ✅ `tool_calls` 落库前 `redact_value` / `redact_text`（`task_manager.py:1532`，`runtime/audit.py:18-40` 的 `_SECRET_PATTERNS`）
- ❌ **`task_events` 的 `normalized_args` 未脱敏即落库**：`runner.py:1147` `args_note = json.dumps(arguments, ...)[:600]` → `:1208` 写 payload，全程无 redact
- `eval_runner.py:172` 注释声称"脱敏前；敏感参数走 `_record_tool` 的 redaction" —— 但 `_record_tool` 里**并没有** redaction 调用，**注释与实际不符**

### 12.6 日志

- **无 `logging.basicConfig`**（0 命中），仅 `getLogger`
- 无结构化日志、无 JSON formatter
- `uvicorn log_level="warning"` ⇒ 连访问日志都没有
- `logs/` 下是旁路埋点 JSONL（`guardrail_failures.jsonl`、`router_metrics.jsonl`、`tool_router.jsonl` 5.9MB），非应用日志

---

## 13. Performance Findings

| 项 | 发现 | 证据 |
|---|---|---|
| **async 内同步阻塞 IO** | ❌ **存在**。`webapp.py:266` `async def api_sessions` 直接调同步 `_list_sessions_json`（`:145` 同步 `sqlite3.connect`）；`runner.py` 全文件 **0 处 `to_thread`**，所有 `self.tasks.*` 同步 sqlite 在协程内执行 | `webapp.py:266/145`；`runner.py` |
| **数据库连接** | ❌ **93 处 `with self._connect()`**，每次新建+关闭。典型 5 工具 / 4 轮 Run ≈ **25–40 次连接创建** | `task_manager.py:494-502` |
| **N+1** | ❌ **严重**。`webapp.py:1677` 循环 `_container_payload(c)` × limit 500 ⇒ **最多 1000 次连接**；`:1043-1052` 通知循环 50 × 2 ⇒ ≈100 次；`_task_payload:797-802` 5 次独立查询，且 `:800/:801` 用 `len()` 取计数却拉全表 | `webapp.py:1677/1043/797` |
| **首 token 延迟** | ⚠️ 从 POST 到第一个 SSE 字节共 **7–9 次串行 DB 往返**；且客户端**必须再发第二个请求** `GET /stream` 才能收到流 | `webapp.py:645-704` + `:491-526` |
| **会话历史** | ❌ **全量加载**。`runtime/context.py` 有 7 处 `await session.get_items(limit=10_000_000)`（`:340/408/428/565/585/614/638`），先全量拉进内存再裁剪 | 对比 `webapp.py:164` 只取 200 条 |
| **SSE 轮询** | ⚠️ 20Hz 忙轮询（`asyncio.sleep(0.05)`）⇒ 每事件最多 50ms 抖动 + 多订阅者纯 CPU 空转 | `webapp.py:600` |
| 大消息 | ✅ 有界。`_MAX_MESSAGE_CHARS=20000`（`:284`）；`read_workspace_file` max_chars 钳制 `max(200, min(x, 60000))`（`tools.py:760`） | — |
| 序列化 | ⚠️ `_task_payload` with_details=True 时 5 次查询（见上） | — |

---

## 14. Test Coverage Findings

### 14.1 总量

| 指标 | 数值 |
|---|---|
| `tests/*.py` | **115** 个 |
| pytest 实际收集 | **1301**（`pytest tests/ --collect-only -q`，4.83s） |
| `def test_` 计数 | 1351（差值 ≈51 为冻结层拦下、未进入收集） |
| 因 UI 冻结未收集 | **51**（`test_webapp.py` 3 + `test_llama_bridge.py` 23 + `test_llama_stream_resume.py` 17 + `test_theme_cdp.py` 8） |
| 使用替身的文件 | 44 / 115 |
| **真实联网 / 真调模型的 pytest 用例** | **0** ✅ |

### 14.2 用户要求的 10 个场景

| # | 场景 | 结论 | 证据 |
|---|---|---|---|
| **A** | 正常 Run | ⚠️ **有，但"端到端"是假的** | `test_task_runtime.py:274`、`test_production_closure.py:110` 都把 `main.execute_turn` 换成返回硬编码 JSON 的 fake |
| **B** | Provider timeout | ✅ 有（仅总墙钟） | `test_provider_deadline.py:98/136`；**无首 token/空闲超时断言** |
| **C** | Tool timeout | ⚠️ 部分 | `test_code_exec.py:87`（真超时杀进程）、`test_resilience_token_gate.py:130`（槽位等待）；**无统一工具超时（本来就没实现）** |
| **D** | Tool invalid args | ❌ **实质缺失** | `test_approval_execution_parity.py:275` 唯一相关用例断言的是"**不抛异常**"（`# If we get here without exception, test passes`），而非"被拒" |
| **E** | Missing required info | ✅ 有 | `test_phase5_runtime_proof.py:80`、`test_phase7_reliability.py:77`、`test_task_readiness.py:99` |
| **F** | Stream disconnect | ⚠️ **只有一半** | `test_llama_stream_resume.py:264/185`（OpenAI 侧 pump 解耦 + 偏移续读）；**`/api/*` 命名 SSE 的"断连≠取消"零覆盖** |
| **G** | User cancel | ✅ 有 | `test_production_closure.py:72`、`test_llama_stream_resume.py:236` |
| **H** | Duplicate request | ⚠️ Runtime 层有，HTTP 层无 | `test_production_closure.py:241/211`；`webapp.py:657/667` 的幂等与 409 分支**零用例** |
| **I** | Retry | ⚠️ Provider 侧充分，**工具侧零** | `test_aux_provider_retry.py:116/129/143`、`test_provider_errors.py:153/167/269`、`test_production_closure.py:282/321` |
| **J** | Concurrent messages | ✅ 有 | `test_concurrency_stress.py:145/240`；⚠️ 但 `:84` setUp 里把 `FORGE_OBLIGATION_GATE=off`，**并发路径的义务门未被验证** |

### 14.3 契约测试已死（P0）

```python
# tests/test_contract.py:22-23
if not (ROOT / "web" / "runtime" / "types.js").exists():
    pytest.skip("旧 Web UI 已下线（web/runtime/ 已移除），契约探针无被测对象")
```
`web/runtime` 已删除 ⇒ **永久 skip**。`tests/contract_probe.js:17-20` 声明的 5 类字段集（`task`/`run`/`approval`/`artifact`/`tool`）**在后端无任何一处断言与之对应**。

同类死测试：`test_frontend_smoke.py:16`（映射到已不存在的 `web/runtime.html`）。

### 14.4 benchmark 不算测试

`benchmark/*.py` 30 个文件无一以 `test_` 开头，pytest 收集不到；产出是 `report_*.json`；`RESPONSIBILITY_MAP.md:36` 已标记 `BENCHMARK_NOT_PRODUCTION_PARITY` —— 它绕过 `run_turn` 的 FileScope 注入 / 审批包装器 / 义务门 / `run.terminal` 一致性检查。**其结论不可外推到生产。**

---

## 15. Architecture Drift

### 15.1 是否已偏离原 FORGE Runtime 设计

**Runtime 主链未偏离**（`CURRENT_RUNTIME_CALL_GRAPH.md:12` 描述的 `run_turn → execute_turn → _run_attempt → Runner` 与实测一致）。

**偏离发生在"入口"与"契约"两层**：

| 文档 | 说法 | 代码实测 | 判定 |
|---|---|---|---|
| `CURRENT_RUNTIME_ENTRYPOINTS.md:41` | `spawn_run_task` 注"Web 入口已冻结，方法保留" | `webapp.py` 仍有 **66 条 Route**，`llama_bridge.py` 22 条 | **漂移**：文档当"保留方法"，实际是一整套可运行服务（只被门禁挡住） |
| `CURRENT_RUNTIME_ENTRYPOINTS.md:11` | 工具面 37 个 | `ARCHITECTURE_REVIEW_2026-09-19.md:73` 说 41，实测注册 38 | **三处不一致** |
| `FORGE-ARCHITECTURE-AUDIT-2026-09-21.md:44` | 全量回归 1239 passed | 实测 pytest 收集 1301 | 口径不一致（unittest vs pytest） |
| `FORGE-ARCHITECTURE-AUDIT-2026-09-21.md:53` | P0-A：episode 未接主链 | 实测 `runner.py`/`main.py` 仍 0 命中 | 一致，**未修** |
| `FORGE-ARCHITECTURE-AUDIT-2026-09-21.md:277` | P0-C：think 工具"grep=0，空白" | **已实现并测试**（`tools.py:687`、`tests/test_think_tool.py` 7 用例） | **文档已过期** |

### 15.2 冻结层带来的副作用（最关键的结构性问题）

`ui_frozen.py` 冻结的是"网页界面"，但**连带把整条 Web 契约链一起冻结了，而这条链恰好是唯一被前端消费的链**：

- `test_webapp.py`(3) / `test_llama_bridge.py`(23) / `test_llama_stream_resume.py`(17) 共 **43 个用例被 `skip_if_frozen()` 在 import 期拦掉，永不运行**
- `llama_bridge.py` 的 `/v1/chat/completions` 是 llama-ui **唯一入口**，它的契约（`_openai_chunk` / `[DONE]` / `approval_required`）在默认 CI 下 **零断言**
- 与此同时 `/api/*` 那 60+ 条**已无消费者**的端点也一并失去断言

⇒ **"有消费者的桥"和"没消费者的旧 API"被同等对待了，结果是唯一重要的那份契约没人守。**

### 15.3 契约漂移实例汇总

| # | 漂移 | 证据 |
|---|---|---|
| 1 | **`state` vs `status`**：容器 `_container_payload:1154` 用 `status`，Run `_run_payload_short:1171`/`_task_payload:780` 用 `state`，活动信封 `public_activity.py:375` 又用 `status` | 三处三种 |
| 2 | **`task_id` 双义**：`_approval_payload_pub:371` 装 run_id；`_create_run_and_start:700` 装 container_id；`webapp.py:392-393` **相邻两行两种语义** | 极高危 |
| 3 | **`run.waiting_user` 被静默丢弃**：生产发 `run.waiting_user`（`public_activity.py:311`），闸口只认 `run.waiting_for_user`（`:371`） | CLI 也受影响 |
| 4 | `_reply_payload` 字段集随分支变化（成功 11 字段 / 降级 7 字段）⇒ 前端拿到**不齐次数组** | `webapp.py:124-137` |
| 5 | `stat.latest_run` 在无 Run 时整块消失 | `task_manager.py:637-644` |
| 6 | `llama_bridge.py:663` usage 伪造；`:584` `_openai_done` 返回 `object:"chat.completion"` 却带 `choices: []` | 非合法 OpenAI 对象 |
| 7 | `schemas.py` 是**模型输出契约**（`AgentReply`），不是 HTTP API schema；HTTP 层**零 schema 定义** | `webapp.py:125` 只做一次 isinstance |
| 8 | `broker.py:3-4` docstring 声称"schema 校验 → Policy 检查 → 执行"，实测 `:39-55` **两者皆无** | 文档骗人 |
| 9 | `eval_runner.py:172` 注释声称的 redaction **不存在** | 注释与实际不符 |

### 15.4 silent fallback 清单（字段缺失时静默给默认值）

| 位置 | 片段 | 风险 |
|---|---|---|
| `webapp.py:1154` | `"status": container.get("status") or "active"` | **状态缺失静默假定 active**（fail-open 方向） |
| `webapp.py:1157` | `"memory_scope": ... or "project_only"` | 缺失 → 静默降级到**次严格**档 |
| `webapp.py:808` | `"summary": snap.get("final_summary") or snap.get("error") or ""` | 失败摘要静默当成功摘要渲染 |
| `webapp.py:1785-1791` | 非 debug 下 `events/tool_calls/model_calls/metadata/checkpoint` 置空 | 前端无法区分"无数据"与"被脱敏" |
| `webapp.py:1790` | 所有 error 统一改写为 `"执行遇到问题，未能完成。"` | 错误细节全丢 |
| `webapp.py:687` | `except Exception: bound = 0` | 附件绑定失败静默记 0 |
| `webapp.py:422/474` | `except Exception: pass`（包住 assistant 消息回放） | 取不到正文 ⇒ **静默跳过 reply 事件** |
| `public_activity.py:347` | `row.get("status", "pending")` | 审批状态缺失 → 静默假定 pending |
| `public_activity.py:359` | 无事件时返回**假的空快照** | 与"有事件但快照为空"不可区分 |
| `webapp.py:650` | `body.get("message") or body.get("content") or ""` | 两个字段名并存，任一缺失静默取另一个 |

---

## 16. Problems

> 每条含：Problem / Evidence / File / Function / Impact / Root Cause / Recommended Fix
> `[活动]` = 当前默认路径生效；`[潜伏]` = UI 冻结层挡住，启用即生效

### P0 Critical

---

#### P0-1　工具参数校验失败被记为 `TOOL_EXECUTED`，成为"完成证据"　`[活动]`

- **Problem**：模型传错参数 / 缺必填参数时，SDK 把 `ModelBehaviorError` 转成普通字符串返回，runner 记为 `TOOL_EXECUTED`，Completion Gate 把它算作执行证据，模型可在零次成功执行下被判定为已完成。
- **Evidence**：`.venv/.../agents/tool.py:1872` `return f"An error occurred while running the tool..."` → `_FailureHandlingFunctionToolInvoker.__call__` 捕获后 `return result` **不抛异常** → `runner.py:1012` `self._record_tool(name, effective_args, TOOL_EXECUTED, str(result)[:400], ...)` → `completion.py:493` `sum(1 for call in self.tool_calls if call.get("status") == TOOL_EXECUTED)`
- **File / Function**：`runtime/runner.py` `_record_tool`；`runtime/completion.py` `evaluate_completion_eligibility`
- **Impact**：Completion Gate 失效；预算被"假执行"消耗；benchmark 的 `executed_count` 虚高（`report_*.json` 的历史结论都受污染）。
- **Root Cause**：自定义 wrapper（`runner.py:1104-1111`）直接替换 `on_invoke_tool` 且不做校验；SDK 默认 `failure_error_function` 是"吞异常返回字符串"；项目未给任何工具传 `failure_error_function`。
- **Recommended Fix**：在 `runner.py:947` 拿到 result 后做错误文本识别（匹配 `"An error occurred while running the tool"`）改记 `TOOL_ERROR`；**更彻底**：给 `_patch_agent_tools` 构造的 `FunctionTool` 显式传 `failure_error_function=_raise_tool_error`，让校验失败走异常路径。

---

#### P0-2　14 个写/副作用工具默认不在审批名单　`[活动]`

- **Problem**：`write_code_file`、`save_note`、`save_word_doc/excel/ppt`、`deep_research`、`fetch_github_repo`、`index_workspace`、`remember`、`sandbox_snapshot`、`schedule_add/set_enabled` 可无审批持久写盘 / 联网下载落盘。
- **Evidence**：`approval.py:36` `GATED_DEFAULT = EXECUTION_TOOLS | DESTRUCTIVE_TOOLS | REAL_FILE_EDIT_TOOLS` 只含 8 个；实测 `NOT_GATED_BUT_WRITES` 14 个
- **File / Function**：`runtime/approval.py` `ApprovalGate.should_gate` / `GATED_DEFAULT`
- **Impact**：用户无感知的静默写盘；`deep_research`/`fetch_github_repo` 无审批联网 + 落盘。
- **Root Cause**：审批名单按"手工枚举的工具名集合"维护，与 38 个工具的真实副作用分类（`spec.py`）无机械关联，逐步漂移。
- **Recommended Fix**：把 `GATED_DEFAULT` 改为**由 `spec_for(name).side_effect` 动态派生**（`side_effect=True ⇒ 默认需审批`），手工名单只做豁免；并加测试断言"所有 side_effect 工具 ⊆ 审批名单 ∪ 显式豁免表"。

---

#### P0-3　`run.waiting_user` 事件被闸口静默丢弃　`[活动]`

- **Problem**：状态机进入 `WAITING_USER` 时，`finish(state)` 发的是 `run.waiting_user`；`public_wire` 白名单只有 `run.waiting_for_user` ⇒ **`return None`，事件既不落库也不推前端**。
- **Evidence**：`public_activity.py:311-312` `self.emit("run." + state, ...)`（state 来自 `TaskState`，值为 `waiting_user`）vs `public_activity.py:371` 白名单 `{"run.started","run.completed","run.failed","run.cancelled","run.waiting_approval","run.waiting_for_user",...}`；`cli/render.py:175` 也只处理 `run.waiting_for_user`
- **File / Function**：`runtime/public_activity.py` `public_wire`
- **Impact**：追问态在 UI/CLI 上表现为"什么都没发生"；用户不知道 Agent 在等他；`waiting_user` 的 54 条历史 Run 全部受影响。
- **Root Cause**：事件名有两套来源（状态枚举 `waiting_user` vs 手写常量 `waiting_for_user`），白名单是硬编码字符串集合，无测试覆盖"所有 emit 的 kind 都能过闸口"。
- **Recommended Fix**：白名单改为**由 `TaskState` 枚举派生**（`{"run." + s.value for s in TaskState} | {...}`）；并加测试：遍历所有 `RunActivityProjector.emit` 调用点，断言每个 kind 都能通过 `public_wire`。同时统一 CLI 渲染器与 webapp 的事件名。

---

#### P0-4　`webapp.py` 启动 Run 漏传 `task_id`　`[潜伏，启用 UI 即触发]`

- **Problem**：`run_turn` 走"新建 Run"分支，导致第 3 步建好的 Run 永远停在 `submitted` 无终态；而 SSE 侧把非 waiting/failed/cancelled 的状态一律当 completed 广播。
- **Evidence**：`webapp.py:517-525` `runtime.run_turn(existing.goal, session=..., session_id=..., mode="stream", max_turns=..., metadata=..., stream_events_cb=_cb)` —— **无 `task_id=`**；对比 `main.py:711` `task_id=task.id` 正确；`benchmark/eval_runner.py:108` 也正确。兜底广播 `webapp.py:462` `else:  # completed`
- **File / Function**：`webapp.py` `_start_run_session`
- **Impact**：启用 UI 后每个 Run 都卡 `submitted`，用户看到"已完成"但 DB 是 `submitted`；`find_active_run` 反复命中 ⇒ 或直接 `AgentError` 起不来。
- **Root Cause**：CLI 主链改造时统一了 `task_id` 传递，webapp 被冻结未同步 —— **冻结层掩盖了这次不一致**。
- **Recommended Fix**：一行改动，`webapp.py:517` 补 `task_id=run_id`（对齐 `main.py:711`）。同时把 `webapp.py:462` 的 `else: # completed` 改为显式枚举 + 兜底走 `run.failed` 或 `status.update`，不要猜。

---

#### P0-5　`POST /tools` 无鉴权、绕过全部门控直接执行工具　`[潜伏]`

- **Problem**：直接 `rt.broker.execute(tool, params)`，跳过 needs_user 门、预算门、FileScope、重复护栏、网络策略、收敛门、mutation 上限、intent 门、Write-Ahead 台账；仅按工具名做一次审批检查。
- **Evidence**：`llama_bridge.py:277-306` `api_tools_execute`；`:303` `result = await rt.broker.execute(tool, params)`；路由 `:775` `("/", api_tools_execute, ["POST"])`；无认证中间件（`webapp.py:1907` 无 `middleware=`）；Bearer 只做 session 哈希（`llama_bridge.py:75-81`）
- **File / Function**：`llama_bridge.py` `api_tools_execute`
- **Impact**：绑 `0.0.0.0` 时，任何人可执行 `write_project_file`、`forget_memory`、`sandbox_rollback` 等非审批名单内的写/破坏工具，且无审计台账。
- **Root Cause**：该端点是给 llama-ui 的"工具直调"便利接口，绕过了 `run_turn` 统一入口；`ToolBroker` 的 docstring 还虚假声称有"schema 校验 → Policy 检查"（`broker.py:3-4` vs 实测 `:39-55`）。
- **Recommended Fix**：短期——删除该端点或强制加 `127.0.0.1` 绑定断言 + 工具白名单（只读子集）；中期——改走 `run_turn` 的统一 gate 链。

---

#### P0-6　状态迁移无 CAS + `run_turn` 无 `finally`　`[活动]`

- **Problem**：(a) `transition()` 校验在连接 A、写入在连接 B，UPDATE 无 `WHERE state=?`，无 version 列 ⇒ 并发 cancel/complete 可双写；(b) `run_turn` 主体无 `finally`，`BaseException` 穿透 ⇒ 终态不写，Run 永久卡 running。
- **Evidence**：(a) `task_manager.py:917` `get_task` / `:920` `assert_transition` / `:932-933` 新连接 `UPDATE runs SET ... WHERE id = ?`；schema `:85-102` 无 version 列；(b) `runner.py:2949` `except Exception`（接不住 `BaseException`），`spawn_run_task:1642` 只接 `CancelledError`；代码自承风险 `runner.py:2307-2314`
- **File / Function**：`runtime/task_manager.py` `transition`；`runtime/runner.py` `run_turn`
- **Impact**：(a) 跨进程下出现"`final_output` 已完成但 `state=cancelled`"；(b) 卡死 Run 只能等下次进程启动的 `auto_recover`（而它 900s 阈值还会误杀长任务）。
- **Root Cause**：(a) 用"重读 + 断言"的软保护替代 CAS；(b) 清理路径未覆盖 `BaseException`（项目记忆里已有铁律"`except (Exception, SystemExit)`，保留 `KeyboardInterrupt`"，此处未落实）。
- **Recommended Fix**：(a) 改为单连接 `BEGIN IMMEDIATE` + `UPDATE runs SET ... WHERE id=? AND state=?`，`rowcount==0` 判并发失败；(b) `run_turn` 主体包 `finally`，退出时若 state 仍非终态则强制 `_fail`。

---

#### P0-7　错误脱敏缺失 + 无全局异常处理　`[活动 / 潜伏]`

- **Problem**：`_friendly_error` 是死代码；非 provider 异常经 `str(exc)` 直达用户（含绝对路径、异常类名）；无 `exception_handlers` ⇒ 未覆盖异常返回 HTML 500。
- **Evidence**：`webapp.py:109` 定义，全项目仅 `tests/test_webapp.py:52` 调用；`runner.py:2984` `text = public or text`；`webapp.py:459` `err = (run.error_message or "任务失败")[:300]`；`webapp.py:1907` `app = Starlette(routes=[...])` 无 middleware/exception_handlers
- **File / Function**：`webapp.py` `_friendly_error` / `app` 构造；`runtime/runner.py` `execute`
- **Impact**：内部路径与异常类名泄漏；前端 JSON 解析崩溃；错误信息不可控。
- **Root Cause**：脱敏函数写了但从未接线；错误处理靠每个 handler 各自 `try/except`，无统一收口。
- **Recommended Fix**：加 `exception_handlers={Exception: json_error_handler}` + 统一错误结构 `{"code","message","retryable","details","request_id"}`；`_friendly_error` 接线或删除；`str(exc)` 出口统一走 `redact_text` + 路径剥离。

---

#### P0-8　retry 四层叠加，最坏 36 次 HTTP / 用户一轮　`[活动]`

- **Problem**：L0 SDK 内建 2 次重试（每次尝试 = 3 次 HTTP）× L1 Provider 最多 6 次尝试 × L3 格式闸 2 次重跑。
- **Evidence**：`.venv/.../openai/_constants.py:8` `DEFAULT_MAX_RETRIES = 2`；`provider_gateway.py:490` 最多 6 次；`main.py:491` `while attempt < 3`；`.env` / `.env.example` 中 **TIMEOUT/RETRY 相关配置 0 命中**
- **File / Function**：`runtime/provider_gateway.py` `ResilientModel.get_response`；`main.py` `execute_turn`
- **Impact**：单次用户操作的成本与延迟不可预测；SDK 层重试对 Provider 审计完全不可见（`record_attempt` 只记 L1）。
- **Root Cause**：分层时未约定"重试归属"，SDK 内建重试未被关闭；无全局重试预算。
- **Recommended Fix**：显式给 `AsyncOpenAI` 传 `max_retries=0`，把重试全部收到 L1；引入单次用户请求的**总重试预算**（如 ≤6 次 HTTP）；把 SDK 尝试次数也写进 `provider.model_attempts`。

---

### P1 High

---

#### P1-1　SSE 无心跳 / 无 event id / 无游标续传　`[潜伏]`

- **Problem**：`_sse()` 只产 `event:`+`data:`；`_subscribe_generator(run_id)` 无游标参数；队列空时不下发任何字节。
- **Evidence**：`webapp.py:105-106`；`:558-603`；全仓 grep `heartbeat|keepalive|Last-Event-ID` 0 命中；`public_activity.py:135/373` event_id 是 uuid4 且重放时重新生成
- **Impact**：审批等待/长工具执行期间链路静默 ⇒ 代理/浏览器判死连接；断线只能全量重放（长 Run 拉取全量 payload，无 LIMIT）；无去重键。
- **Root Cause**：SSE 协议实现不完整，当初只满足"能推"。
- **Recommended Fix**：`id:` 用 `task_events.id`；加 15s `: heartbeat`；`retry: 3000`；`GET /stream?since=<seq>`。

---

#### P1-2　取消无法中断同步工具 / 子进程　`[活动]`

- **Problem**：`run_tests`/`code_loop` 是 `def`，经 `asyncio.to_thread` 执行；`task.cancel()` 只取消 Future，线程与子进程继续跑完。
- **Evidence**：`runner.py:950-953`；`code_exec.py:489/566`；`runtime/codex_loop.py:98/168`；`.venv/.../agents/tool.py:2643-2647`
- **Impact**：用户取消后写文件/跑测试的副作用仍发生，结果被丢弃但副作用已落盘 —— **取消语义对用户是谎言**。
- **Root Cause**：同步工具无法被 asyncio 取消是结构性限制，未做进程级补偿。
- **Recommended Fix**：为同步工具记录句柄/进程组，取消时走 `code_exec.py` 已有的 Job Object `KILL_ON_JOB_CLOSE` 路径统一杀进程树；或在工具入口登记"可中断点"。

---

#### P1-3　工具层无统一超时　`[活动]`

- **Problem**：`runner.py:936-953` 无 `wait_for`；`spec.py:26/179` 的 `timeout_seconds=30` **从未被任何代码读取**。
- **Evidence**：`runner.py` 全文件 grep `timeout` 只有 2 处且都是 MCP 槽位等待（`:941/945`）；`index_workspace`（`rag.py:725`）最多 5000 文件 + 可选向量计算，无超时
- **Impact**：单个工具可无限期阻塞；唯一兜底 Run 墙钟 1800s，且 `budget.py:31` `if limit >= 3600: return await coro` 会让 ≥3600 的配置**静默失去兜底**。
- **Root Cause**：`ToolSpec.timeout_seconds` 定义了但没接线。
- **Recommended Fix**：`runner.py` 执行处包 `asyncio.wait_for(result, timeout=spec_for(name).timeout_seconds)`；`budget.py:31` 改为 `if limit <= 0:`（只跳过"未配置"）。

---

#### P1-4　`except → return "错误字符串"` 把失败伪装成成功　`[活动]`

- **Problem**：13+ 处工具内部 catch 后返回错误字符串，被记为 `TOOL_EXECUTED`。
- **Evidence**：`office_docs.py:351/355/404/469/482/495`、`rag.py:738/764`、`github_fetch.py:271`、`multimodal.py:111`、`research.py:265`、**`sandbox_snapshot.py:240`（破坏性 `sandbox_rollback`）**
- **Impact**：模型收到"看起来像结果"的错误文本，可能据此宣称成功；破坏性工具失败也返回成功形状。
- **Root Cause**：工具返回类型统一为 `str`，无 `{"ok": false}` 结构，错误与结果不可区分。
- **Recommended Fix**：引入 `ToolResult` 结构（`ok/result/error/kind`），runner 按 `ok` 分流记账；至少先给 P0-1 的文本识别兜底。

---

#### P1-5　`waiting_user` 不在单 Active Run 集合　`[活动]`

- **Problem**：`ACTIVE_RUN_STATES` 与单 Active Run 唯一索引都漏了 `waiting_user` ⇒ 同容器可并发 2 个 Run。
- **Evidence**：`task_manager.py:815` `("submitted","running","waiting_approval","paused")`；`:485` 唯一索引 `WHERE state IN (...)` 同样漏；DB 实证 `waiting_user` 54 条
- **Impact**：多 Tab / 双击在追问态下产生并发 Run，共享同一份会话历史 ⇒ 消息交错、状态互相覆盖。
- **Root Cause**：`waiting_user` 既不是终态也不在活跃集，语义上悬空。
- **Recommended Fix**：加入 `ACTIVE_RUN_STATES` 与唯一索引（改索引需 DROP 重建，注意迁移）。

---

#### P1-6　`run_id = trace_id` 不成立，Web 路径 OTel 从未安装　`[活动]`

- **Problem**：两套 ID 完全无关联；`trace_export.py:119` 的过滤恒为假；`install_local_tracing` 只在 `main.py:1046` 调用。
- **Evidence**：`task_manager.py:723` `id="task_"+uuid4[:8]`；`observability.py:181` SDK trace_id；`runner.py` grep `trace_id` **0 命中**；`trace_export.py:119`；已验证 `traces/` 目录不存在
- **Impact**：项目记忆里的"run_id = trace_id"铁律是错的；跨层追踪断链；Web 路径无任何 span。
- **Root Cause**：OTel 只在 CLI 入口按需安装，未成为 Runtime 的固有能力。
- **Recommended Fix**：在 `AgentRuntime.run_turn` 入口统一设置 `trace_id = run_id`（或建立映射表）；`install_local_tracing` 移入 `AgentRuntime._ensure()`。

---

#### P1-7　`task_events.normalized_args` 未脱敏即落库　`[活动]`

- **Problem**：`tool_calls` 侧有 `redact_value`，`task_events` 侧没有；同一份敏感数据两个出口防护不一致。
- **Evidence**：`runner.py:1147` `args_note = json.dumps(arguments, ensure_ascii=False)[:600]` → `:1208` 写 payload，全程无 redact；`task_manager.py:1532` 有 redact；`eval_runner.py:172` 注释声称的 redaction **不存在**
- **Impact**：工具参数中的密钥/令牌（如用户让 Agent 处理含 token 的文件）以明文落 `task_events`。
- **Root Cause**：两条落库路径各自实现，无统一出口。
- **Recommended Fix**：`runner.py:1147` 改用 `redact_value(arguments)`；加测试断言 `task_events` 中所有 `normalized_args` 不含 `_SECRET_PATTERNS` 命中。

---

#### P1-8　`tool_calls` 缺 `latency_ms` / `turn_number` / `normalized_arguments` / `fingerprint`　`[活动]`

- **Problem**：无法把工具调用对齐到模型轮次、无法量化耗时。
- **Evidence**：DDL `task_manager.py:181-189`；对比 `model_calls`（`:168-178`）有 `turn_number`/`latency_ms`
- **Impact**："为什么调了 7 次 web_search"只能回答一半（缺耗时、缺决策轮次、缺 replan_count）。
- **Root Cause**：表结构设计时未对齐 `model_calls`。
- **Recommended Fix**：迁移补 4 列；`_record_tool` 写入。

---

#### P1-9　N+1 查询与同步 sqlite 阻塞事件循环　`[活动 / 潜伏]`

- **Problem**：`_container_payload` 循环 × limit 500 ⇒ 最多 1000 次连接；`api_notifications` ≈100 次；HTTP handler 内同步 sqlite。
- **Evidence**：`webapp.py:1677`、`:1043-1052`、`:797-802`（`:800/:801` 用 `len()` 取计数却拉全表）；`webapp.py:266` `async def api_sessions` → `_list_sessions_json:145` 同步 `sqlite3.connect`；`runner.py` 0 处 to_thread
- **Impact**：列表接口延迟随容器数线性爆炸；单次请求可长时间占用事件循环。
- **Root Cause**：无批量查询接口、无连接复用、无 sync→thread 卸载约定。
- **Recommended Fix**：`list_containers` 加批量 `stat` 接口；`len()` 改 `SELECT COUNT(*)`；`_connect()` 改线程局部连接复用；同步 DB 调用统一 `to_thread`。

---

#### P1-10　9 处错误以 HTTP 200 返回　`[潜伏]`

- **Problem**：`container is None`（应 404）、"run 不属于该 Task"（应 403/404）全部降为 200 + SSE error 事件。
- **Evidence**：`webapp.py:551/564/618/630/1352/1360/1363` + `api_task_stream` 的 `:1886/1895/1900`；`llama_bridge.py:691-696` 错误时 HTTP 200 + `finish_reason="error"` + `[DONE]`
- **Impact**：客户端无法用状态码判断失败；OpenAI 协议客户端把失败当成功收尾。
- **Root Cause**：StreamingResponse 一旦开始就无法改状态码；错误校验在流启动前未做。
- **Recommended Fix**：把校验前置到返回 `StreamingResponse` **之前**，用 404/403 `JSONResponse`；llama_bridge 错误时发 `error` 事件后不发 `[DONE]`，或改用非 200 状态码。

---

#### P1-11　上传先全量读 body 再判大小　`[潜伏]`

- **Problem**：`await request.body()` 之后才判 50MB / 20MB，无 `Content-Length` 前置校验。
- **Evidence**：`webapp.py:1564→1568`；`webapp.py:76→82`（`MAX_UPLOAD_BYTES=20MB` at `:63`）
- **Impact**：单次 POST 可令进程内存爆掉（本机单用户场景下影响有限，但仍应修）。
- **Root Cause**：大小校验位置靠后。
- **Recommended Fix**：先读 `request.headers.get("content-length")` 前置拒绝；或用流式读取并在超出时中断。

---

#### P1-12　契约测试已死 + 43 个 UI 用例永不运行　`[活动]`

- **Problem**：`test_contract.py` 因 `web/runtime/types.js` 不存在永久 skip；`test_webapp/llama_bridge/llama_stream_resume` 共 43 个被 `skip_if_frozen()` 拦掉。
- **Evidence**：`tests/test_contract.py:22-23`；`tests/_ui_freeze.py:37-42`；`tests/contract_probe.js:8/17-20`
- **Impact**：唯一被前端消费的契约（`/v1/chat/completions`）零断言；5 类接口的字段集无任何后端校验。
- **Root Cause**：冻结策略把"有消费者的桥"和"无消费者的旧 API"同等对待。
- **Recommended Fix**：冻结时**保留 llama_bridge 的契约测试**（它测的是协议不是页面）；重写 `test_contract.py` 改为断言 `_task_payload`/`_container_payload`/`_run_payload_short`/`public_wire` 的字段集与事件名白名单。

---

#### P1-13　工具目录漂移：`think`/`search_sources` 未登记，`scan_dependencies` 反向　`[活动]`

- **Problem**：实测 `MISSING_IN_CATALOG: ['search_sources','think']`、`CATALOG_NOT_REGISTERED: ['scan_dependencies']`；未登记工具被保守判为 `side_effect=True` ⇒ 零副作用的 `think` 被 Readiness 门拦掉。
- **Evidence**：`spec.py:168-181`；`tools.py:687`（think 明确"零副作用"）；实测 `think → allowed=False`
- **Impact**：P0-C 刚落地的 think 工具在 DISCOVERABLE/NEEDS_USER 状态下不可用 —— **新功能被目录漂移废掉**。
- **Root Cause**：`TOOL_CATALOG` 手工维护，与 `agent.tools` 无一致性校验；文档记的工具数（37/41）与实测（38）三处不一致。
- **Recommended Fix**：加 `tests/test_tool_roster_consistency.py`，断言 `set(TOOL_CATALOG) == set(注册工具名) == set(审批名单 ∪ 豁免) == set(FileScope 名单)`；先补 `think`/`search_sources` 条目。

---

#### P1-14　`FORGE_TRUSTED_CODE_ROOTS` 审批绕过在生产生效　`[活动]`

- **Problem**：`.env:22` 指向 `benchmark_fixture`，`approval.py:192-203` 让该目录下 `run_python/code_loop/run_tests` **完全无审批直接执行**。
- **Evidence**：`.env:22`；`approval.py:192-203`；`code_exec.py:62-65/123`
- **Impact**：评测配置泄漏进生产；受信根内的代码执行绕过审批（虽然路径 canonical 比较本身是严密的）。
- **Root Cause**：评测需要免审批跑 fixture，直接写进生产 `.env`。
- **Recommended Fix**：生产 `.env` 清空该项；代码里加"仅当 `FORGE_EVAL_MODE=1` 时才允许非空白名单"。

---

#### P1-15　主链 HTTP 超时从未显式设置，与 Provider 预算撞车　`[活动]`

- **Problem**：SDK 默认 `httpx2.Timeout(timeout=600, connect=5.0)`，而 `FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS` 默认也是 600 ⇒ 一次挂死请求吃满全部预算，且 deadline 只在尝试之间检查。
- **Evidence**：`agents/models/openai_provider.py:144-152` 不传 timeout；`provider_gateway.py:536-545` 同样；`provider_gateway.py:213` 默认 600；检查点 `:272`/`:458`
- **Impact**：单模型调用最长 600s 无产出；重试预算被一次挂死吃光。
- **Root Cause**：依赖 SDK 默认值，未在项目内显式声明分层超时。
- **Recommended Fix**：显式传 `httpx.Timeout(total=<provider 预算的 1/3>, connect=5.0)`；把首 token/空闲超时作为硬中断。

---

### P2 Medium

| # | Problem | Evidence | Fix |
|---|---|---|---|
| P2-1 | `state`/`status`、`task_id` 双义 | `webapp.py:1154` vs `:1171`；`:371`(run_id) vs `:700`(container_id) vs `:392-393`(相邻两行两义) | 统一为 `run_id` + `container_id` + `state`；旧字段保留一代并标注 deprecated |
| P2-2 | 5 个辅助直连点绕过 Provider 抽象 | `compact.py:98`、`multimodal.py:57`、`research.py:55`、`codex_loop.py:45`、`reply_parser.py:115` | 统一走 `ResilientModel` |
| P2-3 | provider 来源判断在两处各抄一份 | `runner.py:2955-2959` 与 `terminalization.py:84-89` | 抽到 `provider_errors.classify` 单点 |
| P2-4 | `_NO_CHANNEL_MARKERS` 含裸 `"channel"`，误判面过宽 | `provider_errors.py:92` | 收紧为 `"no available channel"` |
| P2-5 | `tools.py` 防重复状态是进程级全局 | `tools.py:72/34` `_last_repeat_calls` | 迁入 `RunContext` 并落库 |
| P2-6 | `RunBudget.max_tool_calls=100` 定义但从未生效 | `task.py:40` vs `runctx.py:228` | 删除或接入 |
| P2-7 | `auto_recover` 900s 阈值会误杀长任务（墙钟 1800s） | `task_manager.py:1111-1117`、`updated_at` 只在 transition 更新 | 启用 `budget_relative=True` 或长跑期周期性刷新 `updated_at` |
| P2-8 | `PRAGMA foreign_keys=ON` 只在 `__init__` 执行，92 处连接上未生效 | `task_manager.py:43` vs `:494-502` | 在 `_connect()` 里设 |
| P2-9 | 会话历史全量加载（7 处 `get_items(limit=10_000_000)`） | `runtime/context.py:340/408/428/565/585/614/638` | 改为分页 + 增量统计 |
| P2-10 | `missing_required_fields` 与 `required_questions` 两套复制正则，已偏移 | `readiness_gate.py:263` vs `:421` | 抽共享规则表 |
| P2-11 | `_run_action` 陈旧读 + 12s shield 后强制 finalize | `webapp.py:1831-1848` | 重读 + CAS |
| P2-12 | `config_summary` 对超短 key 等于明文；`/api/runtime/status` 泄露绝对路径 | `provider_gateway.py:190/195`；`webapp.py:956-957` | 路径脱敏；key 长度 <8 时不显示 |
| P2-13 | `trust.py:19-24` `_DANGEROUS_PATTERNS` 死代码；`episode_recall.py:29` 无 tag 边界 | — | 接线或删除；补 tag |
| P2-14 | `broker.py:3-4` docstring 虚假声称有校验 | vs `:39-55` | 改 docstring 或实现 pre_hook |
| P2-15 | 无 `logging.basicConfig`、无结构化日志；`uvicorn log_level="warning"` | — | 引入 JSON 结构化日志 + request_id |
| P2-16 | 重复护栏只覆盖只读类，mutation 完全豁免 | `readiness_gate.py:843`；`runner.py:203` 仅 3 个 | 为 mutation 加"同 target 同 content hash"重放拒绝 |
| P2-17 | `missing_required_fields` 是纯正则，无 LLM 参与 | `readiness_gate.py:263` | 保留（低成本高确定性），但加测试防回归 |

### P3 Low

| # | Problem | Evidence |
|---|---|---|
| P3-1 | `llama_bridge.py:663` usage 用字符数冒充 token 数；`:584` `_openai_done` 非合法 OpenAI 对象 | 依赖它的客户端计费/上下文判断全错 |
| P3-2 | `web/shared/markdown.js` 只被测试引用的死资产 | `llama_bridge.py` 零引用 |
| P3-3 | 文档自相矛盾：工具数 37/41/38；回归数 1239 vs 1301 | 三份文档 |
| P3-4 | `FORGE-...-09-21.md:277` 关于 think 工具的结论已过期 | `tools.py:687` 已落地 |
| P3-5 | 无 API 版本、无 OpenAPI；版本号只存在于路由注释 `# v2 兼容`/`（v3）` | `webapp.py:1945/1921` |
| P3-6 | SSE 20Hz 忙轮询引入最多 50ms/事件抖动 | `webapp.py:600` |
| P3-7 | `_LIVE_LOCK` 声明后未见 `async with` 使用 | `webapp.py:331` |
| P3-8 | 并发测试靠关生产门跑通（`FORGE_OBLIGATION_GATE=off`） | `test_concurrency_stress.py:84` |

---

## 17. Recommended Fix Order

**原则**：先修 correctness，再修 architecture。每一 Phase 内部按"一行改动 → 局部改动 → 结构性改动"排，且**每 Phase 结束必须跑 `pytest tests/ -q`（1301）+ `python -m benchmark.eval_runner --cases <20 case 子集>` 确认无回归**。

### Phase A：正确性 / 安全（建议先做，全部是小改动）

| 序 | 问题 | 改动规模 | 验收 |
|---|---|---|---|
| A1 | **P0-1** 参数校验失败改记 `TOOL_ERROR` | 1 处（`runner.py:947` 加文本识别） | 新增测试：传缺参 → `tool_calls.status == TOOL_ERROR` 且 `executed_count()==0` |
| A2 | **P0-3** `public_wire` 白名单由 `TaskState` 派生 | 1 处（`public_activity.py:371`）+ 1 条测试 | 新增测试：遍历所有 emit kind，断言都能过闸口 |
| A3 | **P0-4** `webapp.py:517` 补 `task_id=run_id` | **1 行** | 新增测试（冻结层内）：建 Run → 终态非 `submitted` |
| A4 | **P0-6b** `run_turn` 加 `finally` 兜底 | 1 处 | 注入 `SystemExit` → 断言落到 `failed` 而非卡 `running` |
| A5 | **P0-2** 审批名单由 `spec_for().side_effect` 派生 | 1 处 + 豁免表 | 新增测试：所有 side_effect 工具 ⊆ 审批 ∪ 豁免 |
| A6 | **P1-13** 补 `think`/`search_sources` 目录条目 | 2 行 + 1 条一致性测试 | `test_tool_roster_consistency.py` 通过 |
| A7 | **P1-14** 生产 `.env` 清空 `FORGE_TRUSTED_CODE_ROOTS` + 加 `FORGE_EVAL_MODE` 门禁 | 配置 + 1 处判断 | 生产环境下 `trusted_root_for` 返回 None |
| A8 | **P1-7** `runner.py:1147` 改用 `redact_value` | 1 行 | 新增测试：`task_events.normalized_args` 无密钥命中 |
| A9 | **P1-3** `budget.py:31` 改为 `if limit <= 0:` | 1 行 | 单测：3600 → 仍设防 |

> A 阶段**不触碰**状态机、不动事件协议命名、不动数据库 schema。目标是"止血"：让假成功、静默丢事件、卡死 Run、未脱敏这四件事停止发生。

### Phase B：运行稳定性（改结构，但不动对外协议）

| 序 | 问题 | 改动 |
|---|---|---|
| B1 | **P0-6a** `transition()` 改 CAS（单连接 `BEGIN IMMEDIATE` + `WHERE state=?`） | `task_manager.py:910-946` |
| B2 | **P0-8** 关 SDK 内建重试 + 引入总重试预算 | `provider_gateway.py` 构造处 + `main.py:491` |
| B3 | **P1-15** 显式声明分层超时（connect / read / 首 token / 空闲 / 总预算） | `provider_gateway.py` |
| B4 | **P1-5** `waiting_user` 进 `ACTIVE_RUN_STATES` + 唯一索引重建 | `task_manager.py:815/485`（含迁移） |
| B5 | **P1-2** 同步工具取消走进程树 kill | `runner.py` + `code_exec.py` Job Object 复用 |
| B6 | **P1-3** 统一工具超时接线（`spec.timeout_seconds`） | `runner.py:936-953` |
| B7 | **P1-7/B7** `tool_calls` 迁移补 `latency_ms`/`turn_number`/`normalized_arguments`/`fingerprint` | `task_manager.py` DDL + `_record_tool` |
| B8 | **P1-6** `run_turn` 入口统一设 `trace_id = run_id`；`install_local_tracing` 移入 `_ensure()` | `runner.py` / `observability.py` |
| B9 | **P2-7** `auto_recover` 改 `budget_relative=True` 或周期刷新 `updated_at` | `task_manager.py:1111` |

### Phase C：接口统一（此阶段才动对外协议，需一次性做完）

前置条件：**必须先决定 `/api/*` 是"复活"还是"归档"**。建议——**归档 60+ 条无消费者的 `/api/*`，只保留 llama_bridge 的 `/v1/*` 作为唯一对外协议**，因为前端 bundle 实测只认 OpenAI 帧（`assistant_delta`/`run_id`/`EventSource` 在 bundle 中命中数均为 0）。

| 序 | 问题 | 改动 |
|---|---|---|
| C1 | **P1-10** 错误校验前置到 `StreamingResponse` 之前，改用 404/403 | 9 处 |
| C2 | **P1-12** 重写 `test_contract.py`：断言 payload 字段集 + 事件名白名单 + 状态码 | 新增 |
| C3 | **P0-7** 统一错误结构 `{"code","message","retryable","details","request_id"}` + `exception_handlers` | `webapp.py` 构造 + 全局 handler |
| C4 | **P2-1** 字段重命名：`task_id` → `run_id`/`container_id`；`status` → `state`；旧字段保留一代 | 全量 payload 函数 |
| C5 | **P1-1** SSE 加 `id:`（用 `task_events.id`）+ 15s heartbeat + `retry:` + `?since=<seq>` | `webapp.py:105/558` |
| C6 | **P0-5** `POST /tools` 加 127.0.0.1 绑定断言 + 只读白名单，或删除 | `llama_bridge.py:277` |
| C7 | **P3-5** 引入 `/api/v1` 前缀（一次性，配合 C4 重命名同时做） | 路由表 |
| C8 | **P1-4** 工具返回结构化为 `ToolResult`（`ok/result/error/kind`） | 38 个工具 + runner |

> **C 阶段不要拆成多次半吊子改动**：字段重命名 + 版本前缀 + 契约测试必须一次落地，否则会出现"新旧字段并存"的第三种漂移。

### Phase D：性能与可观测性

| 序 | 问题 | 改动 |
|---|---|---|
| D1 | **P1-9** 连接复用（线程局部）+ 批量 `stat` 接口 + `len()`→`COUNT(*)` + 同步 DB 调用 `to_thread` | `task_manager.py:494` / `webapp.py` |
| D2 | **P2-9** 会话历史改分页增量 | `runtime/context.py` 7 处 |
| D3 | **P2-15** 结构化 JSON 日志 + `request_id` 贯穿 | 全局 |
| D4 | **P2-5** `_too_repetitive` 状态迁入 `RunContext` 并落库 | `tools.py:82` |
| D5 | 补 §14 缺失的测试：D（invalid args）、F（`/api` 断连）、I（tool retry）、H（HTTP 幂等） | 新增 4 个测试文件 |

### 明确不建议现在做的

- ❌ 重做 UI / 更换框架 / 更换模型
- ❌ 重新设计 Agent Runtime
- ❌ 为了"架构漂亮"合并 `public_activity.py` 与 `public_response.py`（它们不是重复层，是投影 + 终稿生成器）
- ❌ 未经证据删除历史兼容逻辑
- ❌ 修改 benchmark 以掩盖问题

---

## 18. Do Not Change

以下实现经代码验证是**正确的**，不要为了"最佳实践"重构：

### 18.1 状态机（本项目最扎实的资产）

- ✅ **`runs.state` 全局单点写入**：`Grep "UPDATE runs SET"` 生产代码只有 `task_manager.py:933` 一处
- ✅ **强制迁移校验**：`assert_transition`（`task_manager.py:920`），非法迁移抛 `AgentError`，绝不静默放行
- ✅ **终态不可逆**：`state_machine.py:23-25` 三个终态 `set()` 为空
- ✅ **终态幂等预检**：`runner.py:2134-2155`（`_fail`）与 `runner.py:1666-1667`（`finalize_cancelled`）—— 注释明确写了"修复 cancel 与 fail 双收口时的竞态"
- ✅ **取消不被吞**：`runner.py:954-964` 与 `runner.py:2737-2738` 都是 `except CancelledError: ... raise`
- ✅ **未决副作用不自动重放**：`task_manager.py:874-882` `interrupt_pending_side_effects`
- ✅ **进入终态自动关闭未决审批**：`task_manager.py:936-941`（避免 failed 却仍显示"等待批准"）
- ✅ **DB 级单 Active Run 唯一索引**：`task_manager.py:483-486` + `IntegrityError` 翻译（`:761-768`）

### 18.2 文件权限

- ✅ **FileScope fail-closed**：`filescope.py:224-229`（工具未登记权限 → 默认禁止）、`runner.py:593-603`（授权异常 → DENY）
- ✅ **canonical 路径比较**：`filescope.py:52-68` 用 `realpath` + `normcase`；`code_exec.py:123` `trusted_root_for` 明确防"注释里写受信根路径"绕过
- ✅ **全项目 `shell=True` 0 命中**；子进程密钥剥离（`code_exec.py:168`）；Windows Job Object 杀进程树（`code_exec.py:196-310`）

### 18.3 Provider 流式语义

- ✅ **首 token 前重试、首 token 后不重放**：`provider_gateway.py:346-354`，记 `interrupted` 后上抛 —— 这是正确的流式语义，不要改
- ✅ **context overflow 分层正确**：Provider 层不重试（`provider_errors.py:44`），Runtime 层压缩后重试一次（`main.py:531-545`）
- ✅ **用 `asyncio.timeout` 而非 `wait_for` 包 `anext`**：`provider_gateway.py:322-332` 注释明确解释"避免 contextvars 切换导致 SDK span 报错" —— 这是踩过坑的正确选择

### 18.4 工具 schema

- ✅ **schema 由类型注解 + docstring 自动生成**（`.venv/.../agents/function_schema.py:469`）⇒ 与函数签名天然同步，**不可能漂移**。不要改手写 schema。

### 18.5 安全基建

- ✅ **无 API key 硬编码**，100% `os.getenv`；`.env` 已 gitignore
- ✅ **GET 不再写库**：`_sse_deprecated_create`（`webapp.py:550`）+ 三处拦截 —— 这个改造是正确的，保留
- ✅ **出网点全硬编码，无用户可控 URL 工具** ⇒ SSRF 面实际为零
- ✅ **Zip-Slip 已防**：`github_fetch.py:110-130` `_safe_extract_zip`

### 18.6 测试基建

- ✅ **1301 个用例全部离线，0 个真联网/真调模型** —— 这是很强的资产，保持
- ✅ **Write-Ahead 台账**（`runner.py:190-200/914-928`）：副作用前落 `pending`，取消/失败/成功各自更新 —— 正确的补偿事务设计

### 18.7 值得保留的"丑但正确"

- `runner.py:2307-2314` 注释承认沙箱会抛 `SystemExit` —— 这种诚实注释比"优雅抽象"有价值，不要为了整洁删掉
- `netpolicy.py:4-8` 自承"OS Network Enforcement 未实现，不伪造已隔离" —— 同上，**不要**改成看起来有防护
- `llama_bridge.py:419-470` 的字节偏移续读 —— 这是全项目唯一真正的断点续传，不要因为 `/api/*` 没有就删它

---

## 附：本次审计的方法与边界

- 所有结论均来自真实代码读取，附 `文件:行号 + 函数名`；排除 `.venv` / `benchmark_fixture` / 归档产物
- 对标注 `[潜伏]` 的问题，已确认其被 `ui_frozen.py:37 UI_FROZEN=True` + `webapp.py:1999 require_ui_enabled` 门禁挡住
- 实测手段：`pytest tests/ --collect-only -q`（1301）、`grep` 计数、只读查询 `agent.db` 状态分布、`ls` 验证 `traces/` 与 `web/runtime/` 是否存在
- **未修改任何代码**；对发现的 Critical 项仅作标注与方案建议
- 已知未覆盖：`benchmark/` 的 30 个 py 未逐行审计（它是评测不是生产路径，且已标 `BENCHMARK_NOT_PRODUCTION_PARITY`）；`skills/` 运行时目录未逐个审计

---

## 19. 整改执行记录（Phase A / Phase B）

> 本节由整改实施阶段追加，用于核对 §17 清单的落地情况。**审计正文（§1–§18）不改**——
> 需要修正的判断单独记在 §19.3，保持"审计结论"与"实施发现"两个事实源可分辨。
>
> 全量验证（屏蔽环境 shim，见 §19.4）：**1383 passed, 6 skipped, 0 failed**（1398 collected）。
> Phase A 前基线 1327 passed，Phase A+B 主体 1350 passed，本轮新增 33 条验收用例。

### 19.1 Phase A：正确性 / 安全（A1–A9 全部完成）

| 序 | 落地位置 | 关键点 |
|---|---|---|
| A1 | `runtime/runner.py` `_sdk_tool_failure_text` + `_SDK_TOOL_FAILURE_MARKERS` | SDK 把参数校验失败 / `ModelBehaviorError` 吞成普通字符串返回；现按 `TOOL_ERROR` 收口，且**不推进** mutation epoch（失败不得创建 verification_due） |
| A2 | `runtime/runner.py` 事件白名单 | 由 `TaskState` 派生 + `control_status_of`，不再手抄 |
| A3 | `webapp.py:_start_run_session` | 补 `task_id=run_id`，否则新 Run 终态停在 `submitted` |
| A4 | `runtime/errors.py` `ProcessExitInterrupted`、`runtime/budget.py` `_convert_process_exit`、`runtime/runner.py` `_pending_reraise` | 进程级 `SystemExit` → 落 `failed` 且进程继续服务；`KeyboardInterrupt` 仍穿透到顶层（Ctrl+C 语义） |
| A5 | 审批名单 | 由 `TOOL_CATALOG.side_effect` 派生（9 项显式豁免） |
| A6 | `TOOL_CATALOG` | 补 `think` / `search_sources` |
| A7 | `_eval_or_test_mode()` 门禁 + `.env` | 生产 `.env` 清空 `FORGE_TRUSTED_CODE_ROOTS`（已核对：`dotenv_values` 读到 `None`） |
| A8 | `runtime/runner.py` `_record_tool`、`runtime/approval.py` `_args_key` | **四条**出口统一脱敏 —— 报告只点了 `normalized_args`，实测 `progress_event` / `blocked_reason` / `result_summary` / `args_key` 同样泄露 |
| A9 | `runtime/budget.py` | `limit >= 3600 → 静默不设防` 改为 `limit <= 0` |

### 19.2 Phase B：运行稳定性（B1–B9 全部完成）

| 序 | 落地位置 | 关键点 |
|---|---|---|
| B1 | `runtime/task_manager.py::transition` | 单连接 `BEGIN IMMEDIATE` + `WHERE state = ?` CAS；`rowcount != 1` 即拒绝。并发终态流转只有一个赢家 |
| B2 | `runtime/provider_gateway.py` + `main.py::_format_retry_max` | 见 §19.3-a：SDK `max_retries=0`；单 provider 上限 3、primary+fallback 合计上限 6；L3 重跑次数显式化 |
| B3 | `runtime/provider_gateway.py::_http_timeout` | 显式分层：connect 5s / read = 总预算÷3 / write 跟随 read / pool 跟随 connect；首 token 90s、空闲 120s、总墙钟 600s 维持既有实现 |
| B4 | `runtime/task_manager.py` + `runtime/runner.py` | `waiting_user` 进 `ACTIVE_RUN_STATES`，索引谓词由元组派生 + 迁移消化脏数据；**方案修正**：新 Run 对 `waiting_user` 采取"取代"（旧 Run → `cancelled` + `run.superseded` 事件），否则会打断追问继承 |
| B5 | **新增** `runtime/cancel_scope.py` + `code_exec.py::_communicate_or_cancel` + `runtime/runner.py` | 按 `run_id` 分桶登记 killer；取消时关 Job Object 句柄原子杀进程树（无句柄退化为 `taskkill /T`） |
| B6 | `runtime/spec.py::timeout_for` + `runtime/runner.py` | `spec.timeout_seconds` 由死字段变成唯一来源；表优先 → env → 默认 |
| B7 | `runtime/task_manager.py` DDL + `runtime/runner.py` + `runtime/audit.py` + `runtime/runctx.py` | `tool_calls` 补 `latency_ms` / `turn_number` / `normalized_arguments` / `fingerprint`，三条执行路径都带耗时；见 §19.3-b |
| B8 | `observability.py` + `trace_export.py` + `runtime/runner.py::_ensure_tracing` | 记录盖 `run_id`（不篡改 OTel 的 `trace_id`）；过滤键由 `trace_id` 改为 `run_id`；追踪安装改为 `FORGE_TRACE` opt-in（构造 Runtime 不再全局改 SDK 追踪并无界写文件） |
| B9 | `runtime/task_manager.py::recover_stale_tasks` + `auto_recover` | `min(max_age, 预算×2)` → `max(...)`（原方向反了，等于完全没修）；`budget_json` 死列修复（`RunBudget.to_dict()`，`@dataclass(slots=True)` 无 `__dict__`） |

### 19.3 实施阶段对审计结论的修正

**(a) B2 的真实上限与报告所述不同（名册漂移）**

报告写 "`provider_gateway.py:490` 最多 6 次"，但真正生效的上限在
`provider_errors.MAX_RETRIES`（`RATE_LIMITED=2` / `UNAVAILABLE=2` ⇒ 最多 **3** 次尝试），
`local_attempts < 6` 只是永远够不到的天花板，且写法等于"最多 7 次"，两处各说各话。

另外 SDK 默认 `max_retries=2` 让每次"尝试"= **3 次 HTTP**，而这 2 次重试对
`provider.model_attempts` **完全不可见** —— 报告里"最坏 36 次 HTTP"在观测面上根本不存在。

实现：`_sdk_max_retries()`（默认 0）+ `_provider_max_attempts()`（默认 3）
+ `_provider_max_total_attempts()`（默认 6，跨 primary/fallback 合并；
旧实现切换 fallback 时重置计数 ⇒ 合计可到 12）。每条尝试记录随带
`sdk_http_attempts` / `provider_attempt_budget`，乘法关系自证。
L1 上限判据是 `local_attempts + 1 < max_attempts`（`local_attempts` 记的是已发起的**重试**次数）。

**(b) B7 的 `kind` 判据漂移（静默死列）**

`record_attempt` 里递增 `model_turns` 的判据写的是 `kind == "ok"`，而生产侧
`provider_gateway` 一律写 `"success"` —— 该钩子**从未触发过**，
`RunContext.model_turns` 恒为 0，于是 `turn_number` 这一列虽然迁移加了、却永远取不到值。
（B7 的验收用例当时是手工调 `ctx.note_model_turn()`，因此没覆盖到这条接线。）
已改为认 `{"success", "ok"}` 并补回归用例。

**(c) 环境 shim 会伪造 `SystemExit(1)`** —— 见 §19.4。

### 19.4 全量测试的失败归因（重要，避免下次误判为回归）

在本机跑全量时会出现 9–11 条固定失败（`test_approval` / `test_cleanup_resilience` /
`test_codex_loop` / `test_concurrency_stress` / `test_project_model` / `gorden-ppt`），
报错一律是 `SystemExit: 1`。**这不是代码缺陷**，证据链：

1. 回溯终点是 `WorkBuddy/cli/vendor/shim/sitecustomize.py::_safe_shutil_rmtree → _try_trash`
   —— 环境钩子把 `shutil.rmtree` 劫持为"移入回收站"，失败时抛 `SystemExit(1)`；
2. 去掉本轮新增的测试文件后，同样 9 条失败（与 §19.2 的改动无关）；
3. 只跑 `cleanup_resilience + codex_loop` 时全绿；放进长会话才失败 → 与"本轮累计删除量"正相关；
4. `PYTHONPATH=` 清空（不加载该 shim）后，**全量 1383 passed / 0 failed**。

注意：`runtime/*` 的清理路径（如 `provider_gateway._persist_reset`）吞掉 `SystemExit` 是**正确设计**
（§18 已列为"丑但正确"）。被劫持的是**测试代码自己的** `shutil.rmtree`。
后续在本机跑全量，建议用 `PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/ -q`。

### 19.5 Phase C 前置决策已执行：`/api/*` 归档（2026-09-22）

**决策**：归档（非复活），并按"直接删除 + git 历史恢复"落地。
**恢复点**：`git tag archive/api-routes-pre-delete-20260922`（指向 `a2cb623`，该点 `webapp.py`
仍含 66 条路由 / 63 条 `/api/*`，且已包含 Phase A/B 全部修复）。

归档依据（本次实测重验，非沿用审计期结论）：

| 检验项 | 实测值 |
|---|---|
| 生产 bundle（8.5MB）中 `"/api/"` 命中 | **0** |
| bundle 中 `assistant_delta` / `run_id` / `EventSource` 命中 | **0 / 0 / 0** |
| bundle 中 llama_bridge 协议命中 | `/v1/chat/completions`=2、`/v1/stream`=2、`/v1/models`=1、`/tools`=3、`/props`=7、`text/event-stream`=7 |
| 全仓非 bundle 的 HTTP 消费者（`127.0.0.1.*/api`、`localhost.*/api`） | **0** |
| `/api/*` 的唯一测试消费者 | `tests/test_webapp.py`，文件头 `skip_if_frozen()` 冻结跳过 |
| `webapp.py` 默认能否启动 | 否（`require_ui_enabled` → `SystemExit(2)`） |
| 旧页面 | `web/index.html.legacy.archive` 已归档；`web/runtime/` 已移除 |

**落地范围**（提交 `352f321`，7 文件 +141/−1939）：

- `webapp.py` **2042 → 190 行**。摘除 63 条 `/api/*` 路由与 89 个仅由它们引用的模块级定义。
  可删集合用 `ast` 闭包计算（**注意**：种子里必须把 `/api` handler 从每一步展开中排除，
  否则 `app = Starlette(...)` 这个赋值节点会把整个 `/api` 子图重新捞回来，造成 `可删=0` 的假阴性），
  并对每个候删名字做全仓外部引用复查（命中 2 处：`_sse` 属待重写测试、`_start_run_session`
  属 `runtime/public_activity.py` 的**注释**引用，非代码依赖）。
- **连带清除 3 个既有死代码**（只定义、零引用，与本次归档无关但同属接口层垃圾）：
  `_friendly_error`（13 行）、`_LIVE_LOCK`（1 行）、`_store_upload_bytes`（16 行）。
  其中 `tests/test_webapp.py::test_friendly_error_classification` 一直在测这个不可达函数。
- 连带修正 4 处**记录层漂移**（注释/文档在删除后变成假话）：
  `runtime/public_activity.py`（`run.started` 的真实生产者是 `runtime/runner.py:2209`，
  原注释指向已删的 `webapp._start_run_session`）、`tests/test_api_layer_phase_a.py`、
  `tests/test_project_model.py`、`.learnings/ERRORS.md`（ERR-20260907-002 失效结案）。
- `README.md`：「网页界面」整节重写（原描述依赖已不存在的 `web/runtime.html` 与
  `web/index.html`）；「生成式 UI 交互卡片」加存续标注（`ui` 契约仍在后端生效——
  `schemas.py` / `agent.py` / `runtime/reply_parser.py` / `guardrails.py`——渲染器随 UI 归档）。

**验证**：全量 `1383 passed, 6 skipped, 0 failed`（415s），与归档前基线**逐项一致 → 零回归**；
`FORGE_ENABLE_UI=1` 下 UI 冻结层 44 passed；路由实测 28 条 = 3 页面 + 24 桥 + 1 挂载，`/api/*` = 0。

### 19.6 Phase C 归档后的逐项重新判定

归档把 C 阶段的**作用面整体换掉了**——原 8 项里 4 项针对的是已删除的 `/api/*`，2 项会打断
冻结的前端。逐项重判如下（这是对报告 §17 的实质性修正）：

| 项 | 原目标 | 归档后判定 | 依据 |
|---|---|---|---|
| C1 | 校验前置到 `StreamingResponse` 之前（9 处） | **消灭** | 那 4 个流式 handler（`api_stream`/`api_run_stream`/`api_project_stream`/`api_task_stream`，约 16 个返回点）已随归档删除 |
| C2 | 重写 `test_contract.py` | **已是空操作** | 该文件早已 `pytest.skip`（`web/runtime/` 已移除）；本轮另把 `test_webapp.py` 重写为归档断言 |
| C3 | 统一错误结构 `{code,message,retryable,details,request_id}` | **仍有效**（落在 `llama_bridge`） | 桥层错误响应仍是 ad-hoc：`{"error": str}` + 400/404、`{"plain_text_response": ...}` + 200 |
| C4 | 字段重命名 `task_id→run_id`、`status→state` | **消灭** | 这两个字段只存在于 `/api/*` 的 payload 里 |
| C5 | SSE 加 `id:` + 15s 心跳 + `retry:` + `?since=<seq>` | **消灭且有害** | llama-ui 的续读是**字节偏移**语义：`Y1e={CONV_ID,FROM}`、`from=` 命中 20 次；`/v1/stream?conv_id=&from=` 按字节回放。加 `id:` 会改变字节计数、打断续读（且 `Last-Event-ID` 只是其 EventSource polyfill 的通用管道） |
| C6 | `POST /tools` 加 127.0.0.1 绑定断言 + 只读白名单 | **仍有效，优先级上升** | 归档后 `/tools` 成为活协议：前端 `N2.executeTool/executeToolRaw/streamTool` 确实 POST `/tools`（`FO={EXECUTE:"/tools",LIST:"/tools"}`），服务端经 `rt.broker.execute` 执行任意工具，唯一保护是 `gate.should_gate` |
| C7 | 引入 `/api/v1` 前缀 | **有害** | 前端 fetch 多为相对路径（`./props`、`./v1/chat/completions`），`/tools` 为绝对路径；加前缀会 404 |
| C8 | 工具返回结构化为 `ToolResult` | **有害** | 前端按 `error` / `plain_text_response` 解析（`kb={ERROR:"error",PLAIN_TEXT:"plain_text_response"}`，无匹配则 `JSON.stringify` 兜底）；改结构会让渲染退化 |

**结论**：Phase C 由"8 项全面整改"收缩为 **C3 + C6 两项**，且都落在 `llama_bridge.py`
——即 `ui_frozen.py` 的冻结层内。这与"只优化后端"的阶段约定直接冲突，需先决策
（为接口层开例外 / 先抽出协议层 / 暂缓）。

**实施阶段新发现（不属于原 Phase C）**：`api_tools_execute` 的审批分支返回
`{"approval_required": True, "approval": {...}}`，而 bundle 中 `approval_required` 与
`approval` 命中均为 **0**——前端无此分支，只能靠兜底 `JSON.stringify(a)` 把原始 JSON 显示出来。
它不是静默失败（兜底救了它），但**用户看到的是原始 JSON 而不是"需要确认"的人话**。
这与 B7 的 `kind` 判据漂移、A2 的名单手抄同族：**一个分支声称处理了某场景，而对方从未实现**。

**关于 C6 可行性的实测**（决定加固是否安全）：前端 `Lb(){return{[ds.CONTENT_TYPE]:sv.JSON,...A0()}}`
→ 所有 `JT` 请求都带 `Content-Type: application/json`。因此"强制 JSON 类型 + 回环地址断言"
不会打断前端，且能挡掉 `text/plain` 的跨站简单请求（浏览器不发预检即投递、而
`await request.json()` 不校验类型）。

### 19.7 Phase C 已实施（C3 + C6，2026-09-22）

决策：C3 与 C6 **一起做完**（C 只剩这两项，正好满足报告"一次性做完"的要求）。
两项都在 `llama_bridge.py` = 冻结层内，属有意开出的**接口层例外**：改动只触及错误结构与
请求守卫，不动任何 UI 功能与前端契约。

**C6 写操作守卫**

- `_require_json_content_type()`：会直接产生副作用的 POST 端点（`/tools`、
  `/v1/chat/completions`、`/v1/chat/completions/control`）必须带
  `Content-Type: application/json`，否则 415。
  这不是格式校验而是**反 CSRF**：跨站 `fetch(url, {method:"POST", body:"..."})` 的
  Content-Type 默认是 `text/plain`，属"简单请求"**不触发预检**、浏览器直接投递，
  而 `await request.json()` **不校验**类型 → 任意网页都能在用户浏览器里触发工具执行。
  强制 JSON 会让浏览器先发预检，本服务不返回 CORS 头 → 实际 POST 被拦下。
- **只加在写端点上**。只读端点（`/props`、`/slots`、`/v1/models`、`/v1/streams/lookup`、
  `/models/*`）与无 body 的 GET/DELETE **不加**——前端那几处走 `A0()`，本就不带
  Content-Type，加了会静默打断 `/v1/stream` 续读。边界有回归用例锁住
  （`test_get_stream_is_not_guarded` / `test_delete_stream_is_not_guarded` /
  `test_readonly_post_is_not_guarded`）。
- `webapp.main()` **绑定断言**：非回环监听需显式 `FORGE_ALLOW_NONLOCAL_UI=1`，否则退出码 2。
  **为什么不选"逐请求回环断言"**：Starlette `TestClient` 下 `request.client.host` 是
  `"testclient"`（本次实测，非回环），逐请求断言会打死全部 UI 测试；而"绑定"才是真正要
  保证的不变量。两个守卫互补：**绑定管"谁能连进来"，Content-Type 管"同机其它页面能否借道"**。
- 守卫**先于**任何 Runtime 实例化返回（有用例守住），避免给拒绝路径留副作用入口。
- 未采用报告所提的"只读白名单"：`gated_names` 已由工具目录派生（所有 `side_effect=True`
  默认需审批），白名单会与它重复，且会打断前端合法的 `executeTool` 调用。
  真正的残留暴露面是 `SIDE_EFFECT_EXEMPT` 里那 9 个会写盘但豁免审批的工具
  （`save_*` / `remember` / `schedule_add` 等），已由上述两个守卫覆盖跨站与远程两条路径。

**C3 统一错误结构**

- 非 2xx 统一为 `{"error": {message, type, code, retryable}, "request_id"}`。
  形状**由前端 `kIe()` 的首选分支 `r?.error?.message` 反推确定**，不是自定的；
  该函数另兼容 `error` 为字符串与顶层 `message`，最后回退到 HTTP 状态码表。
- 已转换 4 处非 2xx：404 `stream_not_found`（保留 `conv_id`，前端要读它做本地状态清理）、
  400 `missing_tool`、400 `missing_run_id`（保留旧契约字段 `ok=False`）、
  500 `run_failed` + `retryable=True`。
- `request_id`：沿用调用方的 `X-Request-Id`（截断 64 字符），否则生成 `llmb-<hex12>`。
- **200 路径刻意不上信封**：工具结果走 `plain_text_response`（文本）；若 200 里出现 `error`，
  它必须是**字符串**——前端对它做 `String(a.error)`，传对象会渲染成 `[object Object]`。
  这条约束已写进模块 docstring 与用例（`SuccessPathContractTests`）。
- 顺带修掉 §19.6 记录的 `/tools` 审批**死分支**：命中审批门时除保留
  `approval_required`/`approval` 外，补一条**字符串** `error`（含工具名与"待审批"），
  使"被阻断"在前端有可读反馈而不是一坨原始 JSON。

**验证**：新增 `tests/test_api_layer_phase_c.py`（**25 条全通过**）——覆盖守卫生效、
守卫不越界（GET/DELETE/只读不被拦）、守卫先于 Runtime 实例化、信封形状与前端首选分支对齐、
`request_id` 透传与生成、200 路径契约不变、审批分支可读性、绑定断言四种组合。
`FORGE_ENABLE_UI=1` 下 UI 冻结层 **55 passed**（含 `test_ui_isolation` 边界守卫）。
另核对 `scripts/verify_llama_bridge.py` 的 `http()` 助手本就带
`Content-Type: application/json`（第 28 行），未被守卫打断。

### 19.8 尚未实施

- **Phase D**（D1–D5）：连接复用 / 历史分页 / 结构化日志 / `_too_repetitive` 迁 `RunContext` /
  补 4 类缺失测试。**未开始。**
- **同类残留（新发现，非原 Phase C）**：`api_chat_completions` 非流式路径在
  `choices[0].message` 上写 `approval_required=True`，而 bundle 中 `approval_required`
  命中数为 **0**——与 §19.6 的 `/tools` 死分支同族。该字段是**附加且被忽略**的，
  不造成可见缺陷（正文本身已在 `content` 里），故本次未动；将来若清理需注意
  改 OpenAI 响应体的风险高于改桥层自有字段。
