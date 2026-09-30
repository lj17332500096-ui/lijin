# LangGraph 对话编排迁移

## Goal

让 LangGraph 接管生产 Run 中的“分析 → 计划 → 执行 → 复核 → 最多一次补充”状态流转，同时保留 AgentRuntime 的权限、安全门、工具执行、证据账本和终态管理。

## Phases

1. **确认执行边界与依赖** — inspect production `run_turn`, existing planner and state gates; identify compatibility constraints. `complete`
2. **建立 LangGraph 工作流模块** — typed state, async graph nodes, bounded supplement edge, and runtime callback contract. `complete`
3. **接入生产 Run** — replace the inline analysis/review loop traversal with graph invocation; preserve Runtime security and persistence. `complete`
4. **安装依赖并检查实现** — add LangGraph dependency, install in project venv, run focused non-test syntax/import checks. `complete`
5. **更新迁移记录** — document behavior, limits, and any remaining unverified cases. `complete`

## Constraints

- `AgentRuntime` remains the only authority for tool permissions, approvals, execution evidence, side effects, and terminal state.
- LangGraph controls only workflow transitions and bounded retry routing.
- Keep the existing OpenAI Agents SDK for model calls in this migration.
- Do not execute live provider/tool calls.
- Automated test suites and live end-to-end provider/tool acceptance were not run in this change; only Python compilation, import, and diff-whitespace checks were performed.
- Repository rules: planning notes belong under `.planning/`; runtime/generated files belong under `var/`.

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| None | — | — |
