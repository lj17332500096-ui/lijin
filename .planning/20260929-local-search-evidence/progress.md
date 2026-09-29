## Progress

- Confirmed the production Run `task_86e6f737`: generic “search” was planned as `web_search`; `search_documents` and the follow-up file read were blocked by the phase gate.
- Confirmed the SDK audit collector wrote the blocked wrapper outputs as `succeeded`, because it classified textual outputs without reconciling Runtime invocation events. The Runtime event lacked invocation IDs for phase-order blocks.
- Added local-vs-web search intent handling: project/local/workspace folder searches map to `search_documents`; web search is added only when the request explicitly asks for online search.
- Phase-order blocks now retain the SDK invocation ID. Audit ingestion reconciles SDK tool output statuses with the matching Runtime `tool.invocation` event before writing the tool-call ledger.
- Added 4 focused regressions: local search, explicit web search, explicit mixed search order, and blocked Runtime event overriding success-shaped SDK output.
- Focused suite passed: 52 tests (`test_task_plan`, `test_audit`, capability introspection, CompletionGate).
- Full offline regression passed: 1,428 tests, 0 failures, 0 errors, 0 skipped.
