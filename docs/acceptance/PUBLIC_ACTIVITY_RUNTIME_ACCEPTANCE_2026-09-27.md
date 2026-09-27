# Public Activity Runtime Acceptance

Date: 2026-09-27

## Scope

The active backend projection at `runtime/public_activity.py` derives user-visible progress from existing Run/tool/verification facts, persists envelopes in the existing `task_events` table, and sends them through the existing Runtime stream callback. It excludes tool arguments and raw observations, filters control events through a state-derived allowlist, redacts secrets/internal reasoning, and streams only sanitized final-answer deltas.

## Acceptance

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_public_activity.py tests/test_stream_pipe.py tests/test_ui_isolation.py -q
```

Result: 34 passed. No real Provider calls or external side effects were made.

## UI boundary

The browser Activity Card is not an active deliverable in this repository state. `ui_frozen.py` marks the web host, `llama_bridge.py`, and the llama-ui frontend as frozen and opt-in. The checked-in source tree has no maintained Activity Card frontend source; `web/llama-ui/` is an ignored build output. The historical `tests/public_activity_e2e/` scripts and screenshots call `/api/*`, which `webapp.py` says were removed on 2026-09-22. Those artifacts document an earlier UI and do not constitute current browser acceptance.

Reactivating or replacing the web UI requires a separate explicit architecture scope. The current Runtime projection is verified independently of that frozen layer.
