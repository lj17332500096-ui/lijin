# CURRENT_RUNTIME_ENTRYPOINTS

> FORGE Runtime Truth Audit · Phase 0（一）
> 来源：**当前生产代码**（证据等级 2）。非按 Phase 报告绘制。

## 证据优先级声明
本文件只从真实代码入口梳理；历史 Phase 报告仅作旁证，不作为事实来源。

## 单一 Agent 本体
- `agent.py::assistant_agent` —— 全局唯一的 `Agent` 实例（模块级单例）。
- 工具面（37 个）：native 工具 30 + 技能/工具动态注入 `*SKILL_TOOLS`（来自 `skills_loader`）。
- 人设与输出契约：`instructions` 强制每轮输出 `AgentReply` JSON（`kind ∈ {answer,plan,note,questions,done}`）。
- 本地模型变体：`agent.local_model_instructions()` / `local_model_name()`，仅当 `FORGE_MODEL_PREF=local` 时 `run_turn` 克隆注入。

## 真实入口（按运行形态分组）

### A. 交互式 CLI
| 入口 | 路径 | 语义 |
|---|---|---|
| 主 REPL（含审批/取消/多会话） | `main.py::chat_async` → `AgentRuntime.get_default().run_turn(...)` | 同步聊天循环，登记 Task，`mode=stream/async/sync` |
| CLI 消息平台（进程内直连） | `cli/app.py::ChatApp.submit` → `runtime.run_turn` | 唯一交互式入口（UI 已冻结），`stream_events_cb=renderer.on_event` |
| 后台诊断/任务管理 | `runtime/__main__.py::main` | `--tasks/--task/--cancel/--recover/--snapshot/--restore/--schedules/--runs/--tools`，全部委托 `TaskManager` |

### B. 定时/守护
| 入口 | 路径 | 语义 |
|---|---|---|
| 定时任务触发 | `scheduler.py`（由 `main --daemon` 拉起） | 定时到期 → 新建 Run，`channel=scheduled`，审批门对 `scheduled/daemon` 渠道**自动拒绝**高风险工具（`AUTO_DENY_CHANNELS`） |

### C. Benchmark / 评测（与生产路径是否一致见 ACTIVE_AGENT_PARITY.md）
| 入口 | 路径 | 语义 |
|---|---|---|
| benchmark 驱动器 | `benchmark/__main__.py`、`benchmark/cases.py` | 走 `execute_turn` / `Runner`；`benchmark_fixture` 是评测工作区 |
| 自检/资格 | `benchmark/microbenchmark.py`、`decision_*.py`、`spark_preflight.py` | 微基准与预检 |

### D. 进程内被调（非独立入口，但被多路复用）
| 函数 | 路径 | 被谁调用 |
|---|---|---|
| `execute_turn` | `main.py` | `AgentRuntime.run_turn`（L2234 延迟导入）；benchmark 路径 |
| `_run_attempt` | `main.py` | `execute_turn` 内部；`Runner.run_sync / run_streamed / run` 三模式 |
| `route_agent` | `runtime/runner.py` | `run_turn` 内模型入口选择 |
| `spawn_run_task` / `finalize_cancelled` | `runtime/runner.py` | Web 主链 / 取消收口（Web 入口已冻结，方法保留） |

## Stream / Async / Sync 路径
- 三模式都在 `_run_attempt` 内：`sync`→`Runner.run_sync`；`stream`→`Runner.run_streamed`+消费 `stream_events()`；`async`→`await Runner.run`。
- 仅 `stream` 把 `final.content.delta` 通过 `stream_events_cb("final.content.delta")` 外发，且要求 `active_agent._public_final=True`（收口门关闭工具后的终稿调用）——单行终稿无打字机效果为**预期设计**。

## Resume / Approval Resume 路径
- **新建 Run**：`run_turn(task_id=None)` → `get_or_create_container` + `create_task` → 首个 Run（`submitted→running`）。
- **恢复 Run**：`run_turn(task_id=<run_id>)` → `is_resumable` 校验 → `WAITING_APPROVAL/PAUSED/WAITING_USER` 可恢复；审批通过后走此路。
- **审批续跑**：`_resolve_approvals_interactive`（CLI/main）→ `run_turn(task_id=...)` resume；resume 时 `run_turn` 会 `get_approved_unexecuted(task.id)` 自动执行已批准未执行的 invocation（Phase 38，L2308）。

## 配置驱动的真实 provider 选择
`run_turn` 内：`model_pref = FORGE_MODEL_PREF`（缺省 `gateway`）；`local` 且 `local_model_configured()` 时克隆本地 agent + `run_provider=local_model_provider()`，否则用 `main.current_assistant_agent()`。模型档位再经 `route_agent`（`route_profile` → `cheap/reasoning/default`）二次克隆。详见 RUNTIME_CONFIG_TRUTH.md。
