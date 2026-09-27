# Agent Structure, Workflow, API, and TUI Audit

## Goal
Review the current implementation against its documented architecture and identify actionable defects or operational gaps across Agent structure, production workflow, API contracts, and terminal UI.

## Scope
- Production entrypoints and Runtime call graph.
- Provider/API configuration, request and response handling, retries, errors, and exposed HTTP routes.
- TUI/CLI state transitions, streaming, rendering, input, cancellation, and startup/shutdown.
- Cross-cutting state, approvals, side effects, observability, and tests/documentation alignment.

## Evidence rules
- Read-only audit; do not modify product code or invoke external services.
- Separate direct code facts, test evidence, runtime evidence, and unverified assumptions.
- Report severity, file/line evidence, impact, trigger conditions, and recommended correction.
- Do not run tests unless separately requested.

## Phases
- [x] Map entrypoints and production execution workflow.
- [x] Audit Provider/API contracts and public HTTP/API surface.
- [x] Audit TUI/CLI interaction and lifecycle.
- [x] Cross-check state/evidence/security boundaries and existing tests.
- [x] Write prioritized findings and evidence limitations.
