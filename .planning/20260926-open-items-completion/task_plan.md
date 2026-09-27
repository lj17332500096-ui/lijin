# Plan: Complete Remaining Repository and Runtime Work

## Goal
Complete the previously identified gaps one at a time, with focused acceptance after code changes and a final repository review.

## Phases
- [x] Enforce benchmark, test, and temporary output locations.
- [x] Classify and organize clear historical docs and safe generated artifacts.
- [x] Continue package restructuring with import/path audit and per-module acceptance for Runtime-owned modules.
- [x] Reconcile Layer benchmark/trace plan; document the starter-set label-review and provider-payload parity release gates.
- [x] Complete remaining production cost measurements or document hard evidence limits.
- [x] Reconcile the Public Activity implementation plan with Runtime acceptance and the intentionally frozen UI.
- [x] Run final focused/full verification and summarize remaining risks.

## Remaining external release gates

- The 31-case Layer dataset still needs an independent human label review and expansion before release thresholds are used; a blind review worksheet is prepared under `benchmark/layer_behavior/review/`.
- Provider request schema fingerprint parity with the exact serialized outbound request body remains unverified.

These are evidence gates, not unfinished repository implementation. Close them by returning a dated independent label review (then adjudicate and version the expanded dataset) and by capturing a sanitized request body from the configured Provider path and comparing its tool schemas with the recorded fingerprint. Do not use release thresholds or claim exact payload parity before those artifacts exist.

## Local completion

All repository-controlled items in this plan are complete. Final offline acceptance passed: `.venv/Scripts/python.exe -m pytest -q` — 1461 passed, 6 skipped, 9 subtests passed; changed Python files compiled and `git diff --check` passed. Provider billing was not invoked; actual USD pricing remains unmeasured as documented in `docs/acceptance/PRODUCTION_PATH_ACCEPTANCE_AND_COST_2026-09-26.md`.

## Guardrails
- Preserve pre-existing user changes and tracked evidence.
- Do not delete tracked data or user content without a clear classification.
- Move one module at a time and run its focused acceptance before continuing.
- No paid Provider calls or external side effects unless necessary and explicitly authorized.
