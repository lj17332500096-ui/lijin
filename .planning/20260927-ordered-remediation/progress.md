# Progress

- 2026-09-27: Began the ordered remediation without modifying or overwriting prior audit artifacts.
- Item 1 implemented: non-stream route ignores activity events until `__end__`/`__error__`; route tests cover started/delta/end and error terminal. Focused API suite passed (27).
- Item 2 implemented: non-loopback startup now requires explicit opt-in plus a 32+ character `FORGE_UI_API_TOKEN`; middleware enforces Bearer auth on all HTTP paths, including static UI and APIs. Tests cover missing token refusal and accepted/rejected requests. Focused API suite passed (29).
- Default system Python lacks the `agents` dependency; repository `.venv` is the supported test interpreter.
- Item 3 in progress.
- Item 3 implemented and covered by Pilot test: Escape invokes `AgentRuntime.cancel_run`; ordinary input is disabled during execution and guarded against programmatic duplicate submissions; approval wait re-enables Y/N input.
- Item 4 implemented: direct `/tools` accepts only read-only, non-gated tools; mutations return 409 and must flow through `run_turn`. Permitted read-only calls create a durable task, bind RunContext/file scope, pass through the patched tool wrapper, and persist terminal/evidence events.
- Item 5 implemented: API rejects model values other than the configured model alias before creating a Run; terminal response uses actual model-call model and provider token records when available, and omits usage if unavailable.
- Item 6 implemented: per-stream 4 MiB and global 32 MiB buffer budgets; streams receive run-scoped stable IDs; byte-offset gaps return 410 and mid-read truncation emits an SSE error rather than silently replaying a gap.
- Item 7 implemented: TUI persists settings under `runtime_paths.PROJECT_ROOT`; model lifecycle stubs return HTTP 501/unsupported/false.
- Item 8 implemented: `_run_attempt`, `execute_turn`, RunConfig assembly, retry handling, and stream printing now live in `runtime/execution.py`; Runtime imports the provider adapter directly. `main.py` keeps compatibility re-exports for frontends, and tests patch the new module boundary.
- Remote auth operations guide added at `docs/operations/REMOTE_UI_AUTH.md`; the audit report remains the pre-remediation snapshot.
- Final focused verification passed: 211 tests passed across API, TUI, stream, task, completion, and Router coverage. `py_compile` and `git diff --check` passed; pytest reports one third-party Starlette/AnyIO deprecation warning. No provider or MCP endpoints were called by the new tests.
