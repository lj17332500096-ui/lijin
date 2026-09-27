# Ordered remediation: API, TUI, Runtime

## Goal
Implement the user's eight ordered fixes, with focused regression coverage and sequential verification.

## Rules
- Preserve existing user changes and prior audit artifacts.
- Complete and verify one item before starting the next.
- Run focused tests using the repository `.venv` after each meaningful group.
- Do not call production providers, MCP services, or external network endpoints.

## Ordered checklist
- [x] 1. Fix non-stream API terminal-event consumption and add complete route regression tests.
- [x] 2. Enforce authentication for non-loopback serving and verify startup/request behavior.
- [x] 3. Connect TUI Escape to Runtime cancellation; lock ordinary input while a turn is active.
- [x] 4. Align `/tools` safety, Run state, and side-effect evidence contracts.
- [x] 5. Correct API model selection/validation and usage semantics.
- [x] 6. Bound stream cache memory and give concurrent streams stable identities.
- [x] 7. Persist TUI settings to project root and make model lifecycle stubs explicit.
- [x] 8. Move SDK turn execution helpers out of `main.py` into Runtime/Provider layer.
- [x] Final focused regression suite, diff review, and report updates.

## Verification log
- Item 1: `.venv\\Scripts\\python.exe -m pytest tests/test_api_layer_phase_c.py -q` — 27 passed.
- Item 2: same focused suite — 29 passed.
- Items 1–8 final focused regression set: `tests/test_api_layer_phase_c.py tests/test_tui.py tests/test_stream_pipe.py tests/test_completion_gate.py tests/test_task_runtime.py tests/test_api_layer_phase_b.py tests/test_phase22_router_authority.py` — 211 passed, 1 third-party deprecation warning.
- `py_compile` passed for changed runtime/API/TUI entry modules; `git diff --check` passed (only Git line-ending normalization notices).
- Initial system Python invocation lacked installed `agents` dependency; use repository `.venv`.
