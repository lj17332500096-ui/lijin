# Findings

- Repository `.env` deliberately contains `APPROVAL=off`; old tests assume it is on.
- `webapp.main()` configures module-global `app.state` during mocked startup; tests must restore the state after each invocation.
- `tests/test_project_model.py` restores the legacy memory-binding dict but can leave its ContextVar set.
- `tests/test_api_layer_phase_d.py` checks for `new_request_id` in `main.py`, while the implementation now lives in `runtime/execution.py`.
- The attachment compaction fixture returns a final answer without simulating a read tool call; Runtime correctly reports no progress while still injecting the attachment instruction.
- `tests/test_stream_pipe.py` passes in isolation but failed in the full suite; source of order-dependent state is not yet identified.
