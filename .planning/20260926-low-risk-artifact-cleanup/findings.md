# Findings: Low Risk Artifact Cleanup

- Four tracked historical reports from 2026-09 were moved from `docs/` into `docs/archive/2026-09/`. SHA-256 checks confirm each moved report is byte-identical to its `HEAD` version.
- The moved report set: `BASELINE_30_REPORT_2026-09-21.md`, `EPISODE_RECALL_EXPERIMENT_REPORT.md`, `INTERVAL_CALIBRATION_REPORT_2026-09-21.md`, and `VERIFY_FULL_REPORT_2026-09-20.md`.
- Updated the one active body reference in `docs/TOOL_INVENTORY_2026-09-19.md`; annotated the historical plan reference in `docs/REPAIR_PLAN_2026-09-19.md`; added old-to-new path index at `docs/archive/README.md`.
- Moved 4 smoke/regression logs and 4 pytest XML files from `archive/` root to existing `archive/logs_2026-09/` and `archive/reports_2026-09/`. These payloads were already ignored by Git; `archive/README.md` now maps names and destination directories.
- Left active benchmark data (`benchmark/layer_behavior/`) and `_audit/` evidence in place because they are current/reference-linked.
- Local Markdown links in 9 touched/reference files resolve. `git diff --check` passes; only existing line-ending normalization warnings were emitted.
