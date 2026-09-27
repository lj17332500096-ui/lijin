# 发现

## 初步接入图（静态证据）

- 本地 Skill：`skills_loader.collect_skill_tools()` 在 `agent.py` 模块构造期扫描 `.env` 的 `SKILLS`，其 `tools.py` 工具被加入 Agent 注册表；Skill 正文不默认全部灌入，而通过 `extension_manager(load_skill)` 读取。
- 插件含义有两类：本地 Skill 工具由 `_tool_origin="plugin"` 标记；MCP 工具在 MCP 连接后动态加入。统一清单读取真实注册状态，不代表外部服务连接或调用已通过动态验收。
- 记忆：`remember`、`recall_memory`、`forget_memory` 是 Agent 原生工具，进入 Runtime Registry/Router。长期数据默认 SQLite，由工具层按 RunContext 项目 scope 路由；Run 创建时绑定，退出时清理。
- 现有风险方向：插件/Skill 是启动期发现与注册，启用配置或 Skill 文件变更可能需要进程重启；MCP 连接是执行入口按需 `ensure_connected`，需检查连接失败/状态展示；记忆需继续核验 scope 的缺省/并发上下文一致性和项目隔离测试。

路径依据：`agent.py`、`skills_loader.py`、`integrations/extensions.py`、`integrations/mcp_bridge.py`、`runtime/registry.py`、`runtime/tool_router.py`、`runtime/capability_introspection.py`、`runtime/runner.py`、`tools.py`、`runtime/task_manager.py`。

## 已确认接入事实

- `agent.py` 构造主 Agent 时放入核心记忆工具、`extension_manager` 与启动期收集的 Skill 工具；Runtime `_ensure()` 从 Agent 工具列表建 Registry，再由 Tool Router 为每轮裁剪工具集合。
- `runtime/execution.execute_turn()` 每轮开始前调用 MCP `ensure_connected()`；连接成功的 MCP 工具挂进 Agent 并触发 Runtime wrapper 刷新。无配置会被标记为“已处理”，连接失败会跳过该服务；因此不能只看 `ensure_connected()` 的布尔值判断全部 MCP 健康。
- Run 前先从 task container 读取 `memory_scope` 并放入 RunContext；`remember`/`recall_memory` 优先信任 RunContext 的 `container_id + memory_scope`，项目隔离由此生效。最终清理会 reset RunContext 和工具层绑定。
- 专项测试文件存在：`tests/test_skills_loader.py`、`tests/test_capability_introspection.py`、`tests/test_mcp_bridge.py`、`tests/test_mcp_bridge_policy.py`、`tests/test_memory_store.py`、`tests/test_episode_memory.py`。仅静态盘点测试范围，未运行这些测试。

## 覆盖范围初判

- Skill 测试覆盖已启用 Skill 工具收集、空/未知配置、目录外 symlink 拒绝；能力快照测试覆盖来源分类和 connected/available 展示语义。
- MCP 测试覆盖 fail-closed 工具策略、allowlist、权限映射和 fake server 工具挂载；真实 MCP server 启动/掉线恢复未在本轮实际演练。
- 记忆测试覆盖 SQLite CRUD、旧 JSON 兼容、密钥写入拦截、容量上限、项目隔离及并发上下文作用域。情节记忆另有 Episode 的存储/召回/注入测试，需确认它是否真正挂入每个生产 Run。
