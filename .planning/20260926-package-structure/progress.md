# Progress: Package Structure Adjustment

- Completed five individually tested moves into `integrations/`.
- Updated all discovered Python imports, including RAG's delayed Office adapter import and benchmark/runtime MCP imports.
- Updated package and root directory documentation; corrected machine-specific example paths without changing the user's `.env`.
- Focused tests, import/registration smoke checks, compilation, stale-import scan, and whitespace checks passed.
- Completed individual core moves: `observability.py`, `trace_export.py`, `resilience.py`, `guardrails.py`, and `compact.py` now live under `runtime/`; each move had import/path references updated and focused acceptance run.
- Kept public entrypoints/facades and higher-coupling modules at root; package and root READMEs describe this boundary.
