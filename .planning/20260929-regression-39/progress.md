# Progress

## 2026-09-29
- Started a fresh full-suite failure audit after commits `8e3043b` and `d93645e`.
- Prior baseline: 1,422 tests, 30 failures, 9 errors.
- Prior isolated evidence: 23 approval-related checks pass when `APPROVAL=on`; 11 API tests fail when a preceding mocked non-loopback startup leaves app auth enabled; project-memory leak reproduces after `MemoryBoundaryTests.test_08_global_scope_can_read_global`.
- Confirmed full-suite approval failures are test-only environment drift; `.env` remains unchanged. Targeted approval tests passed with a process-local `APPROVAL=on` override.
- Confirmed static/stream API failures reproduce when the mocked non-loopback startup is run first; the test leaves singleton `webapp.app.state` authenticated.
- Confirmed the memory failure reproduces with `MemoryBoundaryTests.test_08_global_scope_can_read_global` immediately before the long-term-memory test.
- Made the regression runner force a deterministic test-only UI/approval environment; `.env` remains unchanged.
- Restored module-level Web auth state after mocked startup tests and restored both memory ContextVar and DB-path state in project-memory fixtures.
- Updated the request-id assertion to inspect `runtime/execution.py`; adjusted the attachment test to assert injection independently of fake-run completion.
- Focused reruns passed: 85 related web/runtime tests and 27 approval tests.
- Full suite rerun is in progress.
## 2026-09-29 final verification
- Fixed the remaining two stream-pipe failures: `CancelSemanticsTests.tearDown()` referenced undefined `_runtime_execution`, swallowed the resulting `NameError`, and leaked its `_fake_execute_turn` into later tests. The fixture now saves and restores the current `runtime.execution.execute_turn` per test.
- Targeted verification passed: `tests.test_production_closure.CancelSemanticsTests` + `tests.test_stream_pipe` (4 tests).
- Full offline regression passed: 1,424 tests, 0 failures, 0 errors, 0 skipped; log: `var/test-reports/regression_20260929_212352.log`.
- `git diff --check` passed. Production `.env` and production code were not changed.
