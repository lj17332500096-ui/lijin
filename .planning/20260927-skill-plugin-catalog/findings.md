# Findings

- `skills_loader.py` 原来在启动时同时读取所有已启用 Skill 正文并注入 instructions；已改为只注入轻量本地目录，完整正文通过 `extension_manager(action="load_skill", name=...)` 按需读取。
- Skill 文件只允许从本地 `skills/` 下已启用、非 symlink 的目录读取；每次加载只读 Markdown，不动态导入新 Python 代码。已有已启用 Skill 的 `tools.py` 仍在启动时注册。
- Skill 工具继续以 `_tool_origin="plugin"` 进入 Runtime Registry；extension_manager 已登记为低风险只读非文件工具。
- 统一扩展清单由本地目录与实际 Agent Runtime 状态派生，包括已安装/启用 Skill、实际注册的 Skill tools，以及 MCP 配置、allowlist 与当前连接状态；不返回命令或环境变量。
- 能力查询直注入 Runtime 事实块，因此也补充了 Skill 与 MCP 配置状态，避免因旁路工具导致清单遗漏。
- Tool Router 仅在有启用 Skill 且请求可能需要工具/Skill 时暴露加载管理入口；普通寒暄和概念问答仍走原短路径。
- ZIP 中 Harness 代码未导入；只采用按需 Skill 与统一 inventory 的设计。
