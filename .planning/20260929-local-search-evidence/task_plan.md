# Local search routing and evidence repair

## Goal
Correct project-folder person searches so explicit local search uses local document search, and blocked calls cannot satisfy execution or completion evidence.

## Phases
1. [complete] Inspect task planning, tool gate, evidence merging, and test seams.
2. [complete] Implement local-vs-web search intent routing and focused planner tests.
3. [complete] Harden blocked invocation evidence/phase completion and add regression coverage from the observed Run.
4. [complete] Run focused plus full regression, inspect diff, and commit the repair.

## Constraints
- Preserve current `.env` and approval settings.
- Do not re-run the user's production search or alter historical Run records.
- Keep changes scoped to Runtime and tests; follow repository file placement rules.
