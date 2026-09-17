# 《FORGE Agent 能力自检与 MCP 识别修复报告-2026-09-07》

## 1. 原始问题复现

真实提问“你有哪些 MCP 技能可用？”多次出现：

- Agent 回答中把**内置/技能工具**（gorden_ppt_*、save_note、web_search、schedule_add）当作“MCP 技能”；
- 出现“这些工具已验证可用”并引用**过去生成 PPT** 的历史；
- 部分轮次先反复调用 `list_workspace_files / list_notes / read_note / search_documents / recall_memory`（20~27 次）找不到“MCP 配置”，最终仍被 Completion Gate 拦截、不给答案。

结论：Agent 对“自己有什么工具 / 哪些是 MCP / 现在是否可用”是**凭上下文+历史+工具名推断**，不是查 Runtime。

## 2. 原 Agent 为什么会错误判断

1. **来源判定失效**：MCP bridge 挂载的也是普通 `FunctionTool`，旧 `discover_from_agent` 按“类型名是否 MCP 开头”猜，MCP/技能工具全被记成 `native`。
2. **无实时能力快照**：Agent 指令里只有技能文本，没有“当前连接的 MCP 服务器 / 当前挂载工具”的运行时事实。
3. **Tool Router 能力问题不暴露 MCP**：问 MCP/技能/能力时，MCP 工具被 16 个上限裁掉，模型只能去翻文件。
4. **普通/技能/MCP 无用户可读名与状态分层**：模型把内部 id 当展示名，把注册/历史成功当成“当前可用”。
5. 历史对话里的“做过 PPT/成功调用”混进能力回答，因为没有任何机制阻止这种污染。

## 3. 当前 Tool Registry 架构（审计结果）

- 位置：`runtime/registry.py`（`ToolRegistry` + `ToolBinding` + `discover_from_agent`），元数据在 `runtime/spec.py`（`ToolSpec`：category/risk/side_effect/destructive/idempotent/source）。
- 内置工具：`tools.py/agent.py` 的 `@function_tool` → 挂到 `assistant_agent.tools` → Registry 自动发现。
- 本地技能工具：`skills_loader.collect_skill_tools()` 从启用技能目录动态收集后追加到主 Agent。
- MCP 工具：`mcp_bridge` 连接服务器→list_tools→按策略包成 `<server>_<tool>` FunctionTool→追加到主 Agent。
- `/api/tools` 与 Runtime Status 此前只暴露 registry 目录 + MCP 服务器名，**没有** origin/display/enabled/available 语义。

## 4. 当前 MCP Registry 架构

- 配置：`.env` 的 `MCP_SERVERS`（单行 JSON）+ `FORGE_MCP_ALLOWLIST`。
- 连接：`mcp_bridge.ensure_connected()` 每个进程一次；失败只记 `_skip_reasons`，`_connected_names` 记录成功连接名。
- 挂载：授权工具（allow/approval）以 `<server>_<tool>` 挂进主 Agent；deny 不挂载；approval 走 Runtime 审批门。
- 没有独立“MCP Registry 表”，真实事实源 = `mcp_bridge` 连接状态 + 主 Agent 工具列表。

## 5. 新增/补齐的 metadata

- Registry 来源判定改为真实注册标记：`_mcp_source="mcp"` → mcp；`_tool_origin="plugin"`（skills_loader 收集时打标）→ skill；其余 → native。禁止按工具名/类型名猜。
- `runtime/capability_introspection.py`：
  - `tool_id / display_name / description / origin / enabled / available`；
  - MCP 额外 `server_id / server_name / connected / policy`；
  - `mcp_servers`（服务器级：connected、tool_count）；
  - `previously_succeeded`（仅历史类问题，来自 tool_calls 表真实成功记录）。
- `/api/tools` 输出补充 origin/display_name/enabled/available/connected/server_id。

## 6. Capability Introspection 实现位置

- `runtime/capability_introspection.py`：`capability_snapshot(origin=…, available_only=…)`、`collect_tool_entries()`、`connected_mcp_servers()`、`history_success_summary()`、`capability_context_block()`、`looks_like_capability_query/history_query`。
- 接入点：`runtime/runner.py::route_agent` —— 检测到能力/历史类问题后，把只读能力事实块 clone 注入当轮 Agent instructions（不污染全局 Agent；非能力问题零注入，保持普通执行无差异）。
- 复用：不新建第二套 Registry，不改 Tool Schema 执行层，不新增大型 Capability Engine。

## 7. 用户能力名称与 Tool ID 映射

示例（完整表在 `DISPLAY_NAMES`）：

| tool_id | display_name |
|---|---|
| web_search | 联网搜索 |
| gorden_ppt_build | PPT 制作 |
| schedule_add | 新建定时任务 |
| fetch_fetch | 网页内容获取 |
| youtube_get-transcript | YouTube 字幕读取 |

普通回答默认只显示左侧；用户要技术名时才显示 `PPT 制作 [gorden_ppt_build]` 形式，ID 原样输出。

## 8. 状态定义（实现语义）

| 状态 | 含义 | 来源 |
|---|---|---|
| registered | 在 Tool Registry / 当前 Agent 工具列表存在 | registry/agent.tools |
| enabled | 配置允许参与运行（挂载且未禁） | `_enabled` / 挂载事实 |
| connected | MCP 服务器当前连接成功 | `mcp_bridge._connected_names` |
| available | 当前可调用（enabled 且 MCP connected） | 组合判定 |
| previously_succeeded | 历史真实成功次数 | DB `tool_calls.status='succeeded'` |

五者不互相冒充：registered≠available、previously_succeeded≠“已验证当前可用”。

## 9. Prompt 最小修改

仅在能力类问题注入一段事实块，约束（不放系统提示全文）：
- 只依据当前状态回答；不得按旧对话/工具名推断；
- 未连接/未知不能写成可用；无法查询≠没有；
- 默认用友好名；要技术名才给 ID；
- 描述能力用“可以/支持”，不使用“已生成/已保存/已产出/已完成”过去式措辞。

## 10. 自动化测试

新增 `tests/test_capability_introspection.py`（11 项，对应 TEST-CAP-01..10）：

- MCP 查询只返回 origin=mcp；
- registered 但 disconnected → available=false，不称“可用”；
- disabled 工具不进 available；
- 查询失败 → “无法确认当前状态”，不写成“没有 MCP”；
- tool_id 原样保留（gorden_ppt_build 不变成 gordenpptbuild）；
- 默认友好名、不带内部 id；
- 明确要求技术名时输出 `display [tool_id]`；
- 默认 MCP 回答不带历史/PPT 记录；
- connected 开关状态可实时翻转；
- registered/enabled/connected/available 组合语义一致；
- Registry origin 来自真实标记。

回归：`.\regression.ps1` **506/506 PASS**（确定性离线；完整 514）。

## 11. E2E 结果（真实 Provider）

| # | 问题 | 结果 |
|---|---|---|
| 1 | 你能做什么？ | completed，回答仅当前能力，无历史/无过去式完成措辞 |
| 2 | 你有哪些工具？ | completed，分类列出（含真实 tool_id 展示） |
| 3 | 你有哪些 MCP？ | completed，精确列出 7 台已连接服务器并区分内置能力 |
| 4 | 当前有哪些 MCP 可用？ | completed，基于配置/工具列表确认，不再断言“实时扫描” |
| 5 | 你能联网吗？ | completed，正确（联网搜索） |
| 6 | 你能生成 PPT 吗？ | completed，正确（本地技能/内置能力，不说成 MCP） |
| 7 | 有哪些工具最近成功使用过？ | completed，输出来自 DB 成功记录（含次数） |
| 8 | 技术名称列出来 | completed，按 registry ID 输出 |
| 9 | 当前 MCP Server 有哪些？ | completed，7 台服务器名+用途 |
| 10 | 当前无法使用的 MCP 有哪些？ | completed，诚实说只能看到已连接列表，不臆造 |

首轮 Q1/Q3 曾因 Completion Gate 对能力描述的过去式措辞误报而失败；通过注入块约束（用“可以/支持”，避免“已生成/已保存/已产出”）后复测 completed。

## 12. 未解决限制（诚实清单）

1. MCP “断开后实时反映”依赖进程内 `mcp_bridge` 状态：服务器进程崩溃后若未触发重连，connected 状态不会自动心跳刷新（断线恢复需重启或触发 ensure 重连）。
2. display_name 目前为登记表（builtin/常见 MCP/技能）；未登记工具的友好名回退为 tool_id 或“处理”类描述，后续可继续扩充。
3. 能力注入块按“问题是否能力类”触发，是轻量意图正则，不保证所有问法 100% 命中；误判时模型仍可能用默认工具列表作答。
4. 无法使用的 MCP（registered 但 disconnected）在普通快照里不展示为“不可用清单”，只有 0 台连接时才明确说“没有已连接”；要完整“已注册但未连接”清单需扩展读取 `.env` 配置并与连接状态比对（未做，避免范围扩大）。

## 验收结论

| 验收项 | 结论 |
|---|---|
| Capability answers grounded in runtime | **YES** |
| MCP classification grounded in registry | **YES** |
| Tool availability grounded in live state | **YES** |
| Historical context can incorrectly define current capability | **NO** |
| Unknown state can be reported as “none” | **NO** |

**CAPABILITY INTROSPECTION FIX = PASS**
