# Task Plan: Low Risk Artifact Cleanup

## Goal
Move low risk reports, logs, and experiment outputs into clear archival locations while preserving Git history and traceable links.

## Phases
- [complete] Inventory candidates, Git tracking, and inbound references.
- [complete] Choose target taxonomy and move only confirmed generated/archive materials.
- [complete] Update indexes, READMEs, scripts, and in-repository links.
- [complete] Verify links, Git rename detection, and working-tree scope.

## Decisions
- Preserve tracked-file lineage with Git moves and leave an index at stable entry points.
- Do not move active source code, current benchmark baselines, or user-authored materials without evidence they are historical/generated.

## Errors Encountered
- None.
