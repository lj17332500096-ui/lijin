# Pipeline Offline Boundary Acceptance — 2026-09-27

## Decision

`runtime.pipeline.run_pipeline` remains an offline experiment/evaluation prototype. It is not a production execution path and is not evidence that the production `AgentRuntime.run_turn` chain has passed acceptance.

## Gaps and disposition

| Gap | Change / acceptance | Result |
|---|---|---|
| Production entrypoint could accidentally acquire Pipeline | Added a boundary test over `main.py`, `agent.py`, `runtime/runner.py`, and `runtime/registry.py`; also searched production Python modules for `runtime.pipeline` and `run_pipeline(` | **Pass**: no production imports or calls found |
| Feature-off still invoked Laya fast screen | Pipeline now checks `FORGE_ORCHESTRATOR` before either Laya screen or router construction; the disabled-path test asserts both are not called | **Pass** |
| Pipeline wrote process memory to the shared FORGE memory DB | `run_pipeline` passes `Path(":memory:")` to `memory_layers.record_step`; the test asserts the isolated sink | **Pass** |
| Tests could exercise actual tool execution by accident | Added a disabled-path test proving callbacks are not invoked; enabled-path test proves only the explicitly injected fake executor runs | **Pass** |
| Existing seven-stage/orchestrator behavior | Ran the existing Pipeline and Orchestrator suites in the project virtual environment | **Pass**: 23 tests passed, 8 subtests passed |
| System Python dependency mismatch | Initial collection with system Python 3.14 failed because the `agents` dependency is absent there; reran with `.venv/Scripts/python.exe` | **Environment issue resolved for this acceptance** |

## Contract after this change

- `FORGE_ORCHESTRATOR=off` means no Laya inference, no injected LLM callbacks, and no tool-executor callback. The Pipeline returns its fallback outcome.
- `FORGE_ORCHESTRATOR=on` enables only the prototype's local routing logic. It still cannot reach a real tool unless a caller explicitly supplies `tool_executor`.
- Pipeline step bookkeeping is ephemeral and does not write to FORGE's shared process-memory database.
- Production stays on the existing `run_turn` path. A future integration proposal must separately prove reuse of production wrappers, approval, FileScope, WAL, ExecutionEvidence, CompletionGate, budgets, and terminalization.

## Limits

This was an offline boundary and module-level acceptance using mocks. It did not call a real Provider, MCP server, TUI, or production `run_turn`; it does not validate production behavior, end-to-end latency, or real side-effect recovery.

## Commands

```powershell
.venv/Scripts/python.exe -m pytest tests/test_pipeline.py tests/test_orchestrator_layers.py -q
# 23 passed, 8 subtests passed

git diff --check -- runtime/pipeline.py tests/test_pipeline.py
# passed
```
