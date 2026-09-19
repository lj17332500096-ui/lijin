# CURRENT_RUNTIME_CALL_GRAPH

> FORGE Runtime Truth Audit · Phase 0（二）
> 来源：**真实函数调用关系**（证据等级 2），非文档。

## 主线调用图（User Request → 终态）

```
User Request (CLI/REPL/scheduled/benchmark)
   │
   ▼
AgentRuntime.run_turn(message, task_id?, ...)                runtime/runner.py:1620
   ├─ resume?  task_id → is_resumable + expire_stale_approvals + transition(RUNNING)
   └─ new?     get_or_create_container + find_active_run + create_task + transition(RUNNING)
   │
   ├─ effective_budget, effective_turns = resolve_budget(task, budget, max_turns)   runtime/budget.py
   ├─ profile = route_profile(task)                                                 runtime/router.py:36
   ├─ set_active_memory_binding(container_id, memory_scope)                         tools.py
   ├─ model_pref = FORGE_MODEL_PREF; base_agent = current_assistant_agent()        main.py:374
   │   └─ (local & local_model_configured) → clone(model, local_instructions) + run_provider
   ├─ selected_agent = self.route_agent(message, channel, profile, base_agent)     runtime/runner.py:1325
   │      ├─ agent_for(base, profile)   (cheap/reasoning 克隆档位)                 runtime/router.py:61
   │      ├─ capability_introspection.capability_context_block(message)
   │      ├─ router_enabled()? Tool Router: select_tool_names(...)                 runtime/tool_router.py
   │      │   ├─ build_tool_catalog(tools)  (MCP/plugin/native provenance)
   │      │   ├─ C2 熔断: _router_zero_streak ≥3 → 升级全量工具
   │      │   └─ clone(tools=subset)  (_agent_cache 64 条上限)
   │      └─ 返回 selected_agent（最终 active_agent）
   ├─ requested_model = selected_agent.model
   ├─ collector = AuditCollector(tasks, task.id, requested_model, profile)        runtime/audit.py
   ├─ build_file_scope(...) + RunContext(...) + _bind_runctx(runctx)               runtime/runctx.py
   │   └─ (resume) _hydrate_runctx_from_history  (恢复 mutation/verification 证据)
   ├─ ctx_block = _project_context_block(container_id, proj)
   │   └─ Sources RAG: build_reference_block → retrieval_prefix（数据块，非 system）
   │
   ▼
execute_turn(mode, message, session, agent=selected_agent, audit, provider)        main.py:465
   ├─ await ensure_mcp()   (MCP bridge 连接)                                        mcp_bridge.py
   ├─ run_config = _run_config(history_limit, session, provider)
   └─ for attempt in 0..3:
        result = _run_attempt(mode, ...)                                            main.py:379
           ├─ sync:    Runner.run_sync(active_agent, ...)
           ├─ stream:  Runner.run_streamed(...) + 消费 stream_events()
           └─ async:   await Runner.run(...)
        └─ OutputGuardrailTripwireTriggered → 重试 / FinalResponseFailed
           context_overflow → force_compact → 重试
   │
   ▼
Runner 模型循环（agents SDK Runner 内部）
   │  每一轮模型调用前：active_agent.tools（经 Tool Router 子集）随请求发给 Provider
   │  工具调用：经 _patch_agent_tools 包装器拦截                        runtime/runner.py:293
   │     ├─ needs_user_input / user_constraint / missing_required_fields 拦截
   │     ├─ persistence 幂等（save_note/remember 同 Run 一次）
   │     ├─ action intent gate（check_user_tool_intent）
   │     ├─ mutation 限额（write_project_file/edit 本地模型 2 / 其它 6）
   │     └─ ApprovalGate.check(tool, args, run_id)      runtime/approval.py:181
   │          └─ gated 工具: 已批→执行 / 已拒→拒 / pending→raise ApprovalRequired
   │
   ├─ 工具真实执行 → _record_tool(name, args, status, output) 写 _run_ledger
   │   ├─ mutation 三态: _mutation_result_ok → COMMITTED/FAILED/UNKNOWN  runtime/runner.py:64
   │   └─ ExecutionEvidence 采集（Completion Gate 证据源）              runtime/runner.py:1240
   │
   ▼
CompletionGate.evaluate(reply, evidence, request_text)   runtime/completion.py:841
   ├─ obligations: extract_obligations（mutation / verification 需求）
   ├─ verdict ∈ {PASS, CLAIM_UNSUPPORTED, VERIFICATION_FAILED, NO_PROGRESS,
   │             FACT_UNSUPPORTED, CLARIFICATION_VAGUE, APPROVAL_INCONSISTENT}
   └─ 有 obligation deficit → _log_obligation_block + 失败（P2 默认开）
   │
   ▼
收口（run_turn）
   ├─ _succeed / _succeed_waiting_user / _fail
   │   ├─ mark_success / mark_failure / transition(WAITING_USER)
   │   ├─ _emit_terminal（终态一致性检查）
   │   └─ _close_run（活动 flush / gate.clear_run / provider attempts / 幂等清理）
   │
   ▼
Run Terminal State: COMPLETED | WAITING_USER | WAITING_APPROVAL | FAILED | CANCELLED
```

## 审批续跑支线（approval resume）
```
_approval_result(user approve) → tasks.create/update approval=approved
run_turn(task_id=run_id) resume
   └─ for inv in tasks.get_approved_unexecuted(run_id):
         _execute_approved_invocation(tool, args, inv_id, run_rid=run_id)   runner.py:1164
         → 重新走 _record_tool / ExecutionEvidence（执行证据不绕过）
```

## 取消支线
```
runner.cancel_run(task_id) → 标记 _cancel_requested + asyncio.Task.cancel
   └─ _managed() catch CancelledError → finalize_cancelled
        → interrupt_pending_side_effects + transition(CANCELLED) + run.terminal
```

## 关键不变式（与 state_machine.py 对齐）
- 改 state 的唯一合法路径是 `TaskManager.transition` + `assert_transition`（`state_machine.py` 转换表）。
- 终态 `{COMPLETED, FAILED, CANCELLED}` 不可再转；`RESUMABLE_FROM` 不含终态。
- `WAITING_APPROVAL` 的合法后继只有 `{RUNNING, CANCELLED, FAILED}`——**不存在** `pending approval → convergence → failed` 的合法链（见十一）。
