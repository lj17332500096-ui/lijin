# Public Activity Stream Status

## Current status (2026-09-27)

- [x] Audit the production Runtime, RunContext, existing task events, approval/verification hooks, and SSE projection boundary.
- [x] Reuse `task_events` and RunContext for a sanitized public Activity projection and final-answer stream; no parallel state store was added.
- [x] Run offline projection, filtering, streaming, and UI-isolation acceptance.
- [x] Record current limitations and reconcile this plan with the UI freeze.
- [superseded] Add/ship an Activity Card in the web UI. `ui_frozen.py` explicitly freezes the web host, bridge, and frontend, and the maintained frontend source is not present in the repository. Reactivating UI is a separate scope and is not required for Runtime acceptance.
- [superseded] Run the old browser E2E suite. Its screenshots and result JSON target `/api/*` endpoints removed from `webapp.py` on 2026-09-22, so they are historical evidence and cannot validate the current app.

## Acceptance

- `.venv/Scripts/python.exe -m pytest tests/test_public_activity.py tests/test_stream_pipe.py tests/test_ui_isolation.py -q`: 34 passed.
- No real Provider calls or external side effects were triggered.
- Current UI launch remains opt-in and frozen by design.
