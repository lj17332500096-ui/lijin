# Task Plan: Package Structure Adjustment

## Goal
Inspect imports, hardcoded absolute paths, and configured paths; move one module at a time and run focused acceptance after each move.

## Phases
- [complete] Read repository instructions and inventory package/import/path dependencies.
- [complete] Select low-risk integration modules with focused acceptance boundaries.
- [complete] Move each selected module individually and repair imports/path references.
- [complete] Run focused acceptance after each module move.
- [complete] Move low-risk Runtime-owned modules individually; retain high-coupling public entrypoints and façades at the repository root.

## Guardrails
- Preserve existing user changes; do not overwrite or reset.
- Only one module move per acceptance cycle.
- Keep core runtime and UI entrypoints in place for a separately audited pass.
