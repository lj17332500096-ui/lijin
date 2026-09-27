# Findings: Package Structure Adjustment

## Path and import inventory

- No `AGENTS.md`, `pyproject.toml`, `setup.cfg`, or `pytest.ini` repository instructions/config were found by the initial scan.
- The root-level integration candidates derived `.env` and `WORKSPACE_ROOT` from `__file__`; moving them would have shifted their project-root assumptions.
- The user-local `.env` defines its own `WORKSPACE_ROOT`; it was inspected without reading or printing unrelated environment values and left unchanged.
- `.env.example` and the README quickstart had stale machine-specific absolute paths. The sample now documents the default project-parent path and uses local-path placeholders for optional MCP examples.

## Completed module moves (each moved separately and tested before moving the next)

- `github_fetch.py` → `integrations/github_fetch.py`; imports in Agent and focused test updated; path root now comes from `runtime_paths.PROJECT_ROOT`.
- `research.py` → `integrations/research.py`; Agent and two tests updated; `.env` path now uses `PROJECT_ROOT`.
- `office_docs.py` → `integrations/office_docs.py`; Agent and test imports updated; workspace and exports roots now derive from `PROJECT_ROOT`; RAG's delayed adapter import updated.
- `multimodal.py` → `integrations/multimodal.py`; Agent and tests updated; `.env` path now uses `PROJECT_ROOT`.
- `mcp_bridge.py` → `integrations/mcp_bridge.py`; main/evaluate, runtime capability inspection, benchmark, and tests updated. Its unused package-relative `BASE_DIR` was removed.

## Acceptance

- `tests/test_github_fetch.py`: 9 passed.
- `tests/test_research.py` + `tests/test_aux_provider_retry.py`: 19 passed.
- `tests/test_office_docs.py`: 11 passed.
- `tests/test_multimodal.py` + `tests/test_aux_provider_retry.py`: 13 passed.
- `tests/test_mcp_bridge.py` + `tests/test_mcp_bridge_policy.py`: 16 passed.
- `tests/test_resilience_token_gate.py`: 16 passed.
- `tests/test_rag.py`: 13 passed.
- Agent import/registration smoke checks passed after integration moves; moved modules and affected imports compiled; `git diff --check` passed.
- Full suite not run.

## Boundary

- Moved `observability.py` → `runtime/observability.py` after an import/path audit; the module uses `runtime_paths.TRACE_DIR`, so its data location does not depend on package depth.
- Moved `trace_export.py` → `runtime/trace_export.py`; its CLI now runs as `python -m runtime.trace_export`, with repository root and trace storage resolved centrally.
- Moved `resilience.py` → `runtime/resilience.py`; updated Runtime, MCP adapter, and tests to use the package import.
- Moved `guardrails.py` → `runtime/guardrails.py`; file validation roots continue to resolve through `runtime_paths.PROJECT_ROOT`, and failure logs use `LOG_DIR`.
- Moved `compact.py` → `runtime/compact.py`; `.env` lookup remains rooted at the project and generated summary files now go to `RUNTIME_ROOT/summaries`.
- Root CLI/Web entrypoints, public Agent/tool façades, project edit/exec, RAG, scheduling, voice, and shared reply schema remain in place because they define cross-package or public interfaces.
- Historical audit documents retain path names that were accurate when written.

## Core move acceptance

- Updated `main.py`, `runtime/runner.py`, affected tests, and README links to import `runtime.observability`.
- `tests/test_observability.py` + `TraceRunIdLinkageTests`: 9 passed.
- Expanded `tests/test_observability.py tests/test_api_layer_phase_b.py`: 62 passed after correcting a stale timeout assertion to match the implemented unknown-result/no-replay contract.
- Stale Python imports from top-level `observability` scan: none.
- `runtime.trace_export` CLI help and focused trace tests passed (3 tests).
- Guardrails/UI-schema/budget-log/Office acceptance: 67 passed.
- Compact/context/concurrency/provider-retry/snapshot/tool-trim/runtime-cleanup acceptance: 75 passed.
- The full suite exposed the routed-Agent cache's use of an ephemeral clone identity for multi-phase requests. Its key now uses stable base/profile plus model settings, instruction hash, and selected tools; the cache reuse regression and complete suite pass.
