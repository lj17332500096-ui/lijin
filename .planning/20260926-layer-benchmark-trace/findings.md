# Findings

- TaskManager.add_event(task_id, event_type, payload) persists to canonical task_events, so no second trace DB was added.
- route_agent emits routing.decision with decision_id, query SHA-256, Layer decision/fallback/threshold/backend, route mode, selected tools, Agent tool schema fingerprint.
- RunContext carries the route decision id; AuditCollector emits model.call events; existing tool.invocation and run.terminal events link that id.
- Event payload avoids raw user text; it records a hash. Tool args continue using existing redaction.
- Schema hash is for Agent tools assembled by route_agent, not a captured provider request body; provider-payload parity remains unproven.
- Baseline: 31 curated initial cases, action recall 0.4211 when abstentions count as misses; diagnostic starter set, not statistically stable production gold data.
- No test suite executed; changed Python files passed py_compile; offline benchmark executed and artifact stored.
