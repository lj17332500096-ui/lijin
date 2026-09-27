# Progress

- Started sequential completion pass; preserving existing working tree changes.
- Routed Layer benchmark raw output to `var/benchmark-runs/layer_behavior/`; configured pytest and Ruff caches under `var/tmp/` and removed duplicate root/cache naming variants.
- Moved the M3 hidden oracle and an unused scratch calculation fixture under `benchmark/fixtures/`; replaced machine-specific benchmark paths with repository-relative paths.
- M3 analyzer acceptance: `.venv\\Scripts\\python.exe -m pytest tests/test_m3_analyzer_truth.py -q` — 22 passed. System Python was not used because it lacks the project `agents` dependency.
- Historical `_audit/` and `.test_artifacts_20260904/` data remains preserved and now has a directory-level purpose note; no tracked evidence was deleted.
- Added purpose notes for `_audit/`, `.test_artifacts_20260904/`, `benchmark_fixture/`, and `benchmark/fixtures/`; retained evidence/snapshots in place because they are tracked and may contain historical user content.
- Moved the new Runtime/Layer reports into `docs/architecture/`, `docs/audits/`, and `docs/acceptance/`, updated internal links, and added category READMEs/index entries. Earlier root-level reports stay in place for backward compatibility pending link-audited migration.
- The readiness E2E harness now defaults outputs and isolated fixture copies to `runtime_paths.LOG_DIR` and `runtime_paths.TEMP_DIR`; removed the now-empty root `.test_artifacts_readiness/`.
- Updated 50 case benchmark raw output default to a unique timestamped subdirectory under `RUNTIME_ROOT/benchmark-runs/eval/`.
- Moved `observability.py` to `runtime/observability.py`, updated imports and docs, and passed 62 focused observability/API tests. The timeout test now checks the implemented unknown-result/no-auto-replay contract rather than claiming a timed-out side effect was definitely aborted.
- Package follow-up moved `trace_export.py`, `resilience.py`, `guardrails.py`, and `compact.py` into `runtime/` in separate focused cycles; respective acceptance counts were 3, 32, 67, and 75 passed. Summary output now uses `RUNTIME_ROOT/summaries`.
- Reconciled the stale Public Activity plan: the backend projection/stream/UI isolation passed 34 offline tests; the historical browser suite targets a removed `/api/*` protocol, and the current UI remains deliberately frozen.
- Cost documentation now includes a provider/model-specific pricing check. Current `agnes-3.0-flash` use is observable by token count, but a USD rate for the configured `.cn` endpoint is not publicly verifiable, so actual USD remains explicitly unmeasured.
- Full-suite verification: first root invocation exposed vendored-test collection, so `pytest.ini` now scopes default discovery to `tests/`. The next full run exposed five stale-contract/cache failures; updated approval claim assertions, backup path isolation, and repaired multi-phase route Agent clone caching. Final result: `.venv/Scripts/python.exe -m pytest -q` — 1461 passed, 6 skipped, 9 subtests passed, 1 Starlette deprecation warning.
- Removed six timestamp-matched backup files created by the failed pre-fix project-edit test run; the successful final suite writes its test backup fixtures only to temporary paths. Pytest cache is under `var/tmp/pytest-cache`; no root `.pytest_cache` or `.ruff_cache` remains.
- Added a blinded 31-case Layer label review worksheet under `benchmark/layer_behavior/review/` so an independent reviewer can adjudicate labels without seeing the current expected values first.
