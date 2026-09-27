# Integrations

This package contains adapters that connect the Agent to external services and repository hosts.

## Put here

- External service clients and their input/output adapters.
- Integration-specific parsing, transport, and boundary validation.
- Optional capabilities built on external search/model providers, such as deep research.
- Document format adapters for Office file reading and generation.
- Vision model adapters that handle image inputs.
- MCP server transport and tool-policy adapters.
- `extensions.py` exposes a read-only Skill/MCP inventory and the on-demand local Skill instruction loader; it does not install packages or execute newly discovered code.

## Keep elsewhere

- Agent orchestration and tool policy belong in `agent.py` or `runtime/`.
- Shared runtime state and project paths belong in `runtime/` and `runtime_paths.py`.
- Integration tests belong in `tests/`.

Modules in this package should derive project configuration from `runtime_paths.PROJECT_ROOT` rather than assuming their package directory is the project root.
