# Progress

## 2026-09-27
- 读取 planning-with-files 技能、仓库 `AGENTS.md`、全面审计和前三项修复记录。
- 已创建本计划；开始检查 Layer/Router、Session lifecycle、插件状态清单与 latency/cost 现状。
- 已修改运行时代码；静态 `py_compile` 与 `git diff --check` 通过。
- 确认 route head 底层支持任意 choice 个数，但当前 5000 条 route 数据只用于 tool/text 二分类训练；route 样本中有具体 tool id，可用于独立训练多工具候选分类头。
- 未训练或未校准的 checkpoint 不应直接裁剪生产候选。实现会让 Laya 的工具选择采用候选集约束、置信门槛和确定性 Router 回退，写入选中理由和候选集合。
- 确认 Skill/MCP 状态上下文和 `extension_manager` 有两套清单拼装路径；当前 `available` 对内置/Skill 工具默认等同 `_enabled`，需检查工具是否真正注册/可用，并明确 Skill tool 的启用与按需加载差别。
- `TaskManager` 增加持久 `session_cleanup_queue`：clear/delete 先写入 agent.db，再处理独立 SDK Session 库；失败保留错误与重试数，启动时重试，提供 list/retry API。不存在的 Session DB 按无清理目标成功。
- 新增独立 `FORGE_LAYA_TOOL_ROUTE_CHECKPOINT` 路由 checkpoint 接口与 confidence 门控，只能选择 Tool Router 已准入候选；多阶段或多必需工具请求不收窄，并在 route trace 中记录候选和结果。现有二分类权重不适用，所以没有伪装成已训练/启用。
- Router 针对未登记新工具加入精确名称/展示名及多词目录描述匹配兜底；能力回答区分 Skill 已安装/启用/工具注册状态，MCP 区分连接、allowlist 和策略可用性。
- `runtime.pricing` 加 Agnes 模型的版本化官方列表价快照（自定义价格优先；未知模型仍 null）。Run 结束时写全生命周期墙钟时延（按首次 started_at 至当前终态，恢复 Run 更新同一记录）至保留最近 20,000 条的 agent.db 表；Prometheus 导出 Run latency P50/P95 和样本数。
- 更新 2026-09-27 审计修复记录与 `.env.example`。
- `python -m py_compile` 覆盖本轮修改的 8 个 Python 文件通过；`git diff --check` 通过，仅有 CRLF 标准化提示。依用户之前约束未运行测试、模型训练或真实 Run。
