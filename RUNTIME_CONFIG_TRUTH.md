# RUNTIME_CONFIG_TRUTH

> FORGE Runtime Truth Audit · Phase 0（五/六）
> 证据等级：当前 .env + 代码默认值（等级 2/3）。
> 原则：`.env` 值本身不能证明 Runtime 真按此行为——每项标注"代码实际读取点"。

## 真相表（SETTING / EXPECTED / CONFIGURED / ACTUAL RUNTIME / SOURCE / MATCH）

| SETTING | EXPECTED（设计） | CONFIGURED（.env） | ACTUAL RUNTIME（代码读取点） | SOURCE | MATCH |
|---|---|---|---|---|---|
| provider base | 网关/直连 | `https://apihub.agnes-ai.cn/v1` | `build_model_provider`→`ResilientProvider(base_url=…)`；`FORGE_MODEL_PREF=gateway` 时用此 | agent.py:74 / provider_gateway.py | ✅ |
| model | AGENT_MODEL | `agnes-2.5-flash` | `assistant_agent.model=os.getenv("AGENT_MODEL")`；历史 model_calls 实测 `agnes-2.5-flash` | agent.py:84 + agent.db | ✅ |
| API scheme | 非 Responses | `OPENAI_USE_RESPONSES=false` | `use_responses=false`→只走 `/chat/completions` | agent.py:75 | ✅ |
| API key | 已配置 | `cpk-dwmD…`（已设） | `ResilientProvider(api_key=…)`；config_summary 显示 key 末 4 位 | agent.db `provider_attempts` success 验证 | ✅ |
| 本地模型 | 可选 | `G:\models\Spark-X2.5-4B.gguf` @ `http://localhost:8080/v1` | 仅 `FORGE_MODEL_PREF=local` 时克隆；**当前 pref=gateway，本地模型不生效** | agent.py:1783 | ✅（gateway 优先） |
| 模型档位 | 单模型 | `MODEL_CHEAP/MODEL_REASONING` 未设 | `profile_model` 返回 None→`agent_for` 原样返回 base（不克隆） | router.py:28 | ✅ 等价单模型 |
| max_tokens | 4096 | `MODEL_MAX_OUTPUT_TOKENS` 未设 | `ModelSettings(max_tokens=4096)` 写死；档位克隆时 `max_output_tokens()` 为 None 不覆盖 | agent.py:85 | ✅ |
| temperature | 模型默认 | 未设 | 无显式设置，用 ModelSettings 默认（SDK 默认） | — | ⚠️ 未显式锚定 |
| context/墙钟 | 1800s | `FORGE_RUN_WALL_TIMEOUT_SECONDS` 未设 | 默认取 `min(TASK_MAX_WALL, FORGE_RUN_WALL 缺省 1800, RUN_MAX_LIFETIME)` | runner.py:1744 | ✅ 默认 1800 |
| max_turns | 20 | `max_turns=20`（CLI 默认） | `resolve_budget(task, budget, 20)`；RunConfig.max_turns=20 | cli/app.py:68 | ✅ |
| Router profile | 按渠道 | `channel=cli/chat` | `route_profile`→非 scheduled/daemon 默认 `default` | router.py:36 | ✅ |
| MCP status | 6 server | `MCP_SERVERS=[gitee,playwright,sqlite,chrome,fetch,youtube]` | `ensure_mcp()` 连接；工具经 `_mcp_source` 标记 | mcp_bridge.py | ✅ |
| Approval flags | 默认开 | `APPROVAL` 未设 | `ApprovalGate.enabled`=非 off 即 True（**默认开启**） | approval.py:106 | ✅ 开 |
| Approval 清单 | EXECUTION∪DESTRUCTIVE∪REAL_FILE_EDIT | `APPROVAL_GATED_TOOLS` 未设 | 默认 `GATED_DEFAULT`（9 工具） | approval.py:135 | ✅ |
| Permission flags | 高危审批 | — | `register_gated_names` 运行时追加 MCP 策略 | approval.py:49 | ✅ |
| Completion flags | 开 | 未显式 | `CompletionGate.evaluate` 始终执行 | completion.py:841 | ✅ |
| Obligation flags | 默认开 | `FORGE_OBLIGATION_GATE` 未设 | 缺省 `"on"`→**默认开启** | runner.py:91 | ✅ |
| Verification flags | 开 | — | `ExecutionEvidence.verification_*` | completion.py | ✅ |
| Runtime budgets | 有界 | — | 墙钟+turns+tool_calls（budget.py） | runner.py:1744 | ✅ |
| workspace root | 项目目录 | `F:\Byong-hermes\Byong-hermes` | `WORKSPACE_ROOT`（tools）；注意与 `BASE_DIR` 不同 | tools.py | ✅ |
| WorkLocation | 项目绑定 | work_location 表 | `build_file_scope(work_location_path=…)` | runner.py:1817 | ✅ |
| FileScope | 绑定 | — | `build_file_scope` | runner.py:1817 | ✅ |

## 第六项原则校验：禁止"配置=行为"
- **provider**：`.env` 写 `OPENAI_BASE_URL` 不能单独证明 Runtime 走网关。实际：`FORGE_MODEL_PREF=gateway`（非 local）时 `run_turn` 取 `current_assistant_agent()`（用 `MODEL_PROVIDER`），历史 `provider_attempts` 全部 `success`+`primary`（非 fallback），证实**当前真实在跑 agnes 网关，本地 Spark 未参与**。
- **MCP**：`MCP_SERVERS` 配置 6 个 server ≠ 都连通。`tool_calls`/`provider_attempts` 中需进一步验证各 server 实际挂载（见 PROVIDER_PAYLOAD_TRUTH，标 `[待核实]`）。

## 标注 [待核实]
- 各 MCP server 是否真实 `ensure_connected` 成功（依赖网络）。
- `sqlite` MCP 指向 `H:\Byong-hermes\my_creative_agent\data\mcp_sqlite\personal.sqlite`（H 盘），而项目本体在 F 盘——**路径可能错位** `[待核实: 检查 H:\ 是否存在该 sqlite]`。
