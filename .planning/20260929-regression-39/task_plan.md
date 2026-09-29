# Regression failure remediation

## Goal
Re-run the 1,422-test offline suite and resolve the 39 failures/errors reported on 2026-09-29, preserving the production `.env` approval setting.

## Phases
1. [complete] Reproduce the failures with explicit, test-only environment settings and record the baseline.
2. [complete] Fix test isolation/configuration failures (approval environment, shared Web auth state, memory binding context).
3. [complete] Update stale assertions and fixtures for the current Runtime execution contract.
4. [in_progress] Re-run focused tests and the full offline regression suite; report any remaining Runtime defect separately.

## Constraints
- Do not change `.env` or production approval behavior.
- Keep each repair limited to the test contract unless an isolated failure demonstrates a Runtime defect.
- Preserve the user's unrelated worktree changes.

## Errors encountered
| Error | Attempt | Resolution |
| --- | --- | --- |
| Initial 39 failures/errors from 2026-09-29 | Full suite under repository `.env` and UI test flag | Reproduce with explicit test-only settings before changing code |
