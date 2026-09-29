# Findings

- The recorded request `在项目文件夹中搜索韩海庆` matched the generic `external_research` keyword because `搜索` alone always selected `web_search`; Runtime therefore required web search first.
- The model's local `search_documents` attempt was blocked as out of phase. A later `read_workspace_file` attempt was blocked because the plan had already advanced to complete.
- Runtime emitted `tool.invocation.execution_status=blocked`, but SDK AuditCollector saw a normal function-call output and stored the same invocation in `tool_calls` as `succeeded`. This caused completion evidence to overstate successful execution.
- Fix path: distinguish local folder search from explicit online search in Runtime task planning; carry the SDK invocation ID into phase-block events; reconcile AuditCollector status against Runtime's per-invocation event before writing the durable audit row.
- Keep local and online search as separate ordered phases only when the user explicitly requests both. Laya was disabled in the observed Run and is not on this failure path.
