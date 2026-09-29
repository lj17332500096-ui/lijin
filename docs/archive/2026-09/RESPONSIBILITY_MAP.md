# RESPONSIBILITY_MAP

> FORGE Runtime Truth Audit · Phase 0（三）
> 来源：真实代码归属（证据等级 2）。每项标注"主责模块"与"是否出现第二责任人（DUPLICATED_AUTHORITY）"。

## 职责归属

| 职责 | 主责模块 | 实际位置 | DUPLICATED? |
|---|---|---|---|
| **Agent selection** | `runtime/router.py::agent_for` + `runner.route_agent` | 模型档位克隆（cheap/reasoning） | 否（单点：route_agent） |
| **Tool exposure** | `runtime/tool_router.py::select_tool_names` | 按查询裁剪工具子集 + C2 熔断升级 | 否（单点） |
| **Tool routing** | `runtime/tool_router.py` | 关键词表 + 意图族 + MCP domain approval | 否 |
| **Permission** | `agent.py` 工具面 + `runtime/approval.py` 门 | 高危工具落审批门；`spec.py` 标 risk | 边界重叠见注① |
| **Approval** | `runtime/approval.py::ApprovalGate` | pending/approved/denied，run_id 隔离 | 否（单点：check） |
| **FileScope** | `runtime/filescope.py::build_file_scope` | 注入 RunContext.file_scope | 否 |
| **WorkLocation** | `task_manager`（work_location 表）+ `runner._project_context_block` | 工作位置路径注入 | 否 |
| **Tool execution** | `Runner`（SDK）+ `runner._patch_agent_tools` 包装器 | 包装器拦截→审批→执行→记账 | 否 |
| **Mutation truth** | `runner._mutation_result_ok`（三态）+ `completion.ExecutionEvidence.mutation_*` | COMMITTED/FAILED/UNKNOWN | 否（单点判） |
| **Verification truth** | `completion.ExecutionEvidence.verification_*` | 来自真实 run_tests/code_loop 证据 | 否 |
| **Revision truth** | `runctx.RunContext`（revision 计数）+ `_hydration` | resume 时从历史恢复 | 否 |
| **ExecutionEvidence** | `runner._execution_evidence` → `completion.ExecutionEvidence` | 账本→证据转换 | 否 |
| **Obligation tracking** | `runctx.obligation_ledger/deficit` + `completion.extract_obligations` | 义务账本 | **重叠见注②** |
| **Completion** | `runtime/completion.py::CompletionGate` | evaluate → GateVerdict | 否 |
| **Terminalization** | `runtime/terminalization.py::classify_exception/consistency_errors` | 终态分类 | 否 |
| **Provider** | `runtime/provider_gateway.py::ResilientProvider` | 网关封装 + record_attempt | 否（单点） |
| **Snapshot** | `runtime/snapshot.py` + `sandbox_snapshot.py` | Run/沙箱快照 | 否 |
| **Replay** | `runner._execute_approved_invocation` + `_hydrate_runctx_from_history` | 审批重放 + 证据水合 | 否 |
| **Benchmark** | `benchmark/*` | 走同一 `execute_turn` 链 | **见注③** |

## DUPLICATED_AUTHORITY 标记

**注①（低）**：`Permission` 同时出现在 `spec.py`（risk 元数据）与 `approval.py`（gated 名单）。spec 的 risk 是**描述**，approval 的 gated 名单是**执行**——二者口径独立维护，存在"spec 标 high 但 approval 默认清单未纳入"的漂移可能。当前 `GATED_DEFAULT = EXECUTION ∪ DESTRUCTIVE ∪ REAL_FILE_EDIT`，而 `spec` 标 `run_python=high`、`code_loop=high`、`sandbox_rollback=high` 均在清单内，`run_tests` spec=medium 但也在 `EXECUTION_TOOLS`。**结论：当前无实际漂移，但两表无一致性断言，属潜在 DUPLICATED_AUTHORITY（维护风险，非运行时缺陷）。**

**注②（中）**：义务（obligation）在 `completion.extract_obligations`（需求侧：mutation/verification 义务判定）与 `runctx.obligation_ledger/deficit`（证据侧：已满足度）分属两模块。二者语义耦合但接口独立：`completion.evaluate` 读 `evidence`，`runner._log_obligation_block` 读 `rc.obligation_ledger`。**存在"需求判定"与"证据账本"两套事实源，不一致时只能靠 `_log_obligation_block` 的可观测出口事后对账（runner.py:94）。这是已知设计点，非重复实现，但应补一致性不变式（见二十三）。**

**注③（高，BENCHMARK_NOT_PRODUCTION_PARITY）**：benchmark 走 `execute_turn` 但**不经过 `run_turn` 的容器/RunContext/审批门/义务门**（`benchmark/cases.py` 直接驱动 `Runner`）。即 benchmark 路径绕过了：FileScope 注入、`_patch_agent_tools` 审批包装器、`_log_obligation_block` 义务门、`run.terminal` 一致性检查。**生产路径（run_turn）的完成判定与 benchmark 的完成判定不是同一套门 → 标记 `BENCHMARK_NOT_PRODUCTION_PARITY`。**
