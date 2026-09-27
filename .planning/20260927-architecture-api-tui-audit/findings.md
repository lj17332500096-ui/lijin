# Findings

## Architecture and workflow

- Main interactive paths (CLI/TUI and chat completions) generally converge on `AgentRuntime.run_turn()`. Runtime still calls SDK execution helpers from `main.py` (`runtime/runner.py` around line 3354), a module-boundary inversion to clean up later.
- `webapp.py` calls itself frozen but still mounts live `llama_bridge` APIs. `/tools` is a direct Broker side-effect path outside normal Run creation/context; it is an exception to unified Run state/evidence.
- `runtime/__main__.py` is operations/diagnostics, not another user-chat production loop.

## Confirmed findings

1. **P1 non-stream chat completion reads the first activity event as its final result.** Runner emits `run.started`; `_run_chat_turn` queues all events including terminal `__end__`; `api_chat_completions` reads only the first queue item then drains the rest. `llama_bridge.py:768-795`, `runtime/runner.py:2711-2722`.
2. **P1 remote-bind opt-in has no application auth.** `webapp.py:179-195` only warns the operator to configure a reverse proxy; request identity in `llama_bridge.py:168-193` is caller-controlled. Active surfaces include direct tools, chat, and run control.
3. **P1 TUI Escape is display-only.** `cli/tui/app.py:1628-1635` prints “sent interrupt request” but never calls Runtime `cancel_run()`.
4. **P1/P2 TUI accepts overlapping submissions.** No running guard in `on_input_submitted`; every turn resets shared UI state and starts a nonexclusive worker (`cli/tui/app.py:855-896`, `995-1017`). Runtime rejects overlap (`runtime/runner.py:2517-2522`), and the rejected worker sets shared header failed (`cli/tui/app.py:1060-1065`) while the first can still run.
5. **P2 direct `/tools` skips full Run context/evidence.** It calls `rt.broker.execute()` directly without `run_turn()` (`llama_bridge.py:385-430`); broker ToolContext has `context=None`; multiple safeguards and write-ahead evidence are conditional on RunContext/run_id.
6. **P2 API request `model` is echoed/session-keyed but not used to choose actual model.** `llama_bridge.py:761`, `463-493`, `783-788`; runtime `run_turn` signature has no model argument at `runtime/runner.py:2388-2405`.
7. **P2 usage contract reports character count as tokens.** `llama_bridge.py:795`.
8. **P2 stream cache memory/recovery gaps.** Unbounded bytearray per stream, active entry eviction on overflow, key replacement, process-local storage: `llama_bridge.py:535-565`, `622-652`.
9. **P2 TUI `.env` write path depends on process CWD**, unlike project-root loading: `cli/tui/app.py:767-796`, `main.py:20,35`, `agent.py:8,60`.
10. **P3 model load/unload endpoints are success-shaped stubs** (`llama_bridge.py:286-293`).

## Scope and evidence limits

- Static source inspection only; no tests, Agent runs, provider/MCP requests, or fault injection were started.
- Remote exposure risk is conditional: default web host is loopback. It becomes serious when non-loopback is explicitly enabled without an enforcing auth proxy.
- `/tools` has an API-level approval check; finding is specifically that it lacks the complete Run context/state/evidence pipeline, not that every `/tools` call is automatically unapproved.
