# CONFIG PRECEDENCE TRUTH

> Phase 1 交付物 2。本表按**真实代码**逐 source 坐实最终值由谁决定、谁覆盖谁。
> 证据锚点：`load_dotenv` 调用点 + `os.getenv` 读取点（全部读自当前生产代码）。

## Source 清单（真实优先级，高 → 低）

| 优先级 | Source | 证据 | 说明 |
|---|---|---|---|
| 1（最高） | Process environment | `cli/app.py` 调用前由 shell 注入的 env 已存在于 `os.environ` | 最高。`load_dotenv` 默认 `override=False`，**不覆盖已存在的 process env** |
| 2 | `.env`（`load_dotenv(BASE_DIR/".env")`） | `main.py:33`、`agent.py:57`、`runtime/runner.py`（间接经 main 导入）等 14 处 | 只填**当前 os.environ 缺失的键**。对已设的 process env 键无效 |
| 3 | 代码默认（`os.getenv("X", default)` 的 `default` 参数） | 每个读取点内联 | 最低。`.env` 或 process env 都没给时生效 |

### 关键事实：`load_dotenv` 默认不覆盖 process env
`python-dotenv` 的 `load_dotenv(path)` 默认 `override=False`——**已存在的 process env 键优先于 `.env`**。
这是本表成立的前提，直接决定了"谁赢"。

## 每个关键 setting 的最终值 + 谁赢 + 被谁覆盖

| Setting | 最终值（生产 resolved） | 谁赢（Source） | 被覆盖的 Source |
|---|---|---|---|
| `AGENT_MODEL` | `agnes-2.5-flash` | `.env`（process env 未设时） | 代码默认（`None`） |
| `OPENAI_BASE_URL` | `https://apihub.agnes-ai.cn/v1` | `.env` | 代码默认（`None`） |
| `OPENAI_API_KEY` | `***NfGw`（已脱敏，末尾 4 位） | `.env` | — |
| `OPENAI_USE_RESPONSES` | `false` | `.env` | 代码默认（`true`） |
| `FORGE_MODEL_PREF` | `gateway` | `.env` | 代码默认（`gateway`，agent.py:334 / runner.py:1781 一致） |
| `FORGE_LOCAL_MODEL_NAME` | `G:\models\Spark-X2.5-4B.gguf` | `.env` | — |
| `FORGE_LOCAL_MODEL_BASE_URL` | `http://localhost:8080/v1` | `.env` | — |
| `FORGE_LOCAL_MODEL_API_KEY` | （空） | `.env`（空串，等价未设） | — |
| `FORGE_COMPLETION_READY` | `off` | `.env`（显式，2026-09-20） | 代码默认（`""` → 关，`runner.py:697`） |
| `FORGE_REDUNDANT_GUARD` | `off` | `.env`（显式，2026-09-20） | 代码默认（`""` → 关，`runner.py:677`） |
| `FORGE_DECISION_HINT` | （空 = 走代码默认） | 代码默认 | `.env` 未设 |
| `FORGE_OBLIGATION_GATE` | （空 = 默认 **on**） | 代码默认 `runner.py:91` → `"on"` | `.env` 未设 → 默认开启 |
| `FORGE_OBLIGATION_FEEDBACK` | （空 = 走代码默认） | 代码默认 | `.env` 未设 |
| `FORGE_VERIFICATION_FOCUS` | （空 = 走代码默认） | 代码默认 | `.env` 未设 |
| `FORGE_REPEAT_GUARD` | `on` | `.env`（显式，2026-09-20） | 代码默认 `"on"` → 开（`runner.py:762`） |
| `TOOL_BUDGET_TOTAL` | `20` | `.env`（显式，2026-09-20） | 代码默认 `20`（`runtime/runctx.py::_env_int`） |
| `TOOL_BUDGET_WEB_SEARCH` | `5` | `.env`（显式，2026-09-20） | 代码默认 `5`（`runtime/runctx.py::_env_int`） |
| `FORGE_APPROVAL_TTL_SECONDS` | （空 = 代码默认 `3600`） | 代码默认 `runner.py:1661` | `.env` 未设 |

### 结论
- **唯一生效的高优先级 source 是 `.env`**（本机 process env 对这些键均未预设）；
- **2026-09-20 起，现役护栏开关与执行预算已由 `.env` 显式锚定**：
  `FORGE_REPEAT_GUARD` / `FORGE_REDUNDANT_GUARD` / `FORGE_COMPLETION_READY` /
  `TOOL_BUDGET_TOTAL` / `TOOL_BUDGET_WEB_SEARCH`。其余 `FORGE_*` 功能开关仍走代码默认。
- ⚠️ **`.env` 不入库**（含密钥，见 `.gitignore`），所以对外可见的载体是 `.env.example`
  —— 在那里同步了声明的开关（保持默认值一致，避免"两份默认值"漂移）。
  `tests/test_tool_roster_consistency.py::GuardLeverConsistencyTests` 断言 `.env`
  的预算值与代码默认值一致；无 `.env` 的克隆/CI 环境自动跳过。
- **任何 benchmark 若在进程内 `os.environ[...]` 直接赋值，会因优先级 1 压过 `.env` 与代码默认**——这是"评测漂移"的机制来源（本表只记录，不改）。

## [待核实]
- 各 benchmark driver 在 run 前是否**直接写 `os.environ`**（优先级 1）覆盖上述键。需读 driver 启动段确认（本阶段不修改，仅记录机制）。
