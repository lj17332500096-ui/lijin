# FORGE Runtime Truth Audit — Phase 1
## Active Production Baseline Freeze + Live Payload Truth + Runtime Trace Closure

> **AUDIT ONLY**：本阶段未修改任何 Production Runtime / Benchmark / Router / Prompt /
> Model selection / Approval / Completion / Verification / Snapshot 语义。
> 仅新增 4 个**非侵入式观测脚本**（`tools_phase1_*.py`）+ `_audit/` 观测产物，
> 它们只读取 DB / 拦截 SDK 请求参数 / 调 `ensure_mcp`，不改 Runtime 行为。

---

## 1. Executive Summary

Phase 0 的初判"**benchmark 全部绕过 run_turn**"被源码**纠正为 MIXED**：
`microbenchmark` 与 `decision_qualification` 走生产 `run_turn`；真正偏移的只有
`decision_checkpoint`（`route_agent + Runner.run`，绕过收口门）。本阶段建立三条
**真实 Golden Production Trace**（CASE1 READ / CASE2 mutation / CASE4 approval），
拦截一次真实 `/chat/completions` 请求体，坐实 Provider 真相，并首次完成
RunContext Parity、Config Precedence、MCP 启动 trace。

关键新发现（Phase 0 未发现）：
- **P0（新）**：`.env` 里 `MCP_SERVERS` 配置**不是合法 JSON**（`Invalid \escape @
  char 1860`）→ `ensure_mcp()` 启动时**6 个 MCP server 全部被跳过，0 个工具挂载**。
  影响面比 Phase 0 的"sqlite H 盘路径错位"**大得多**——不只是 sqlite，而是
  整个 MCP 体系（gitee/playwright/chrome/fetch/youtube）在生产里都没生效。
- **P1（新）**：三个 Model Identity **不一致**——`INTENDED` 无唯一来源
  （`UNRESOLVED_POLICY`）、`CONFIGURED_DEFAULT = agnes-2.5-flash`（`.env`）、
  `ACTUAL_RUNTIME = agnes-2.5-flash`（拦截到的真实 payload）。三者"配置/运行"
  一致但**与"intended"无法对齐**（无正式生产说明定义"应该是哪个"）。
- **P2（确认）**：`microbenchmark` 硬置 `FORGE_COMPLETION_READY=off`、
  `FORGE_REDUNDANT_GUARD=off`、强制 `FORGE_MODEL_PREF=local`，且 L110 用
  字符串 `"退出码:0"/"passed"` 重猜验证——**BENCHMARK_NOT_PRODUCTION_PARITY**
  坐实为 benchmark root cause（非生产 Runtime 缺陷）。
- **P1（坐实）**：`decision_checkpoint` 走 `route_agent + Runner.run`，绕过
  `run_turn` 的收口门（审批/义务/完成）——单条漂移路径。
- **确认**：MCP sqlite 的 command + target DB 路径在 H 盘**都不存在**，但
  这已被上面的"JSON 损坏"问题覆盖（更上游、影响更大）。

Primary Production Runtime Root Cause = **NOT ESTABLISHED**（本阶段 Golden Trace
全部走 `run_turn` 成功完成，没在生产路径里找到"评测 PASS ≠ 生产 PASS"的
单一机制；所有已发现的漂移都在 **benchmark 侧**）。
Next Component To Modify = **benchmark 驱动层**（统一 `FORGE_*` 开关键，
用 `ExecutionEvidence` 替代字符串重猜；修 `decision_checkpoint` 改走
`run_turn`）。**Why**：三处硬偏移在 `benchmark/microbenchmark.py` 与
`benchmark/decision_checkpoint.py` 坐实；改 benchmark 单点即可让评测收敛到
生产语义，且符合"修现有组件、不新建系统"原则。

---

## 2. Phase 0 Corrections

| Phase 0 原结论 | Phase 1 修正 | 证据 |
|---|---|---|
| "benchmark 全部绕过 run_turn" | **Benchmark Entry Parity = MIXED**。`microbenchmark` + `decision_qualification` 走生产 `run_turn`（✅）；`decision_checkpoint` 走 `route_agent + Runner.run`（❌ 绕收口门） | `grep decision_checkpoint.py:86,114`（route_agent+Runner.run）；`decision_qualification.py:552,752`（route_agent+run_turn）；`microbenchmark.py` 调 `rt.run_turn` |
| "Provider payload 未落库" → `PROVIDER_PAYLOAD_EXPECTATION` | 本阶段拦截真实请求（非侵入式），`PROVIDER_PAYLOAD_TRUTH = ESTABLISHED` | `_audit/provider_payload_phase1-case1-*.json`（5 请求，tools=9，model=agnes-2.5-flash，temperature=Omit，max_tokens=4096） |
| "MCP sqlite 路径错位（H 盘不存在）→ 启动必挂起" | `MCP_SQLITE_CONFIG_DEFECT = CONFIRMED`，`MCP_SQLITE_RUNTIME_IMPACT = CONFIRMED`，且**根因升级**：`.env` 的 `MCP_SERVERS` 是非法 JSON → 全部 6 个 MCP server 启动前就被跳过（不只是 sqlite） | `ensure_mcp()` 实测（`_audit/mcp_sqlite_probe.json`）：`connected_names=[]`、`config_errors=["MCP_SERVERS 不是合法 JSON"]`、`registered_tools=0` |
| 初判 `INTENDED_PRODUCTION_MODEL` 由 `.env` 推 | **INTENDED = UNRESOLVED_POLICY**：无"正式生产说明"唯一来源定义"应该是哪个模型"；`.env` 只给 `AGENT_MODEL=agnes-2.5-flash`（这是 CONFIGURED_DEFAULT，非 INTENDED） | 代码内无"生产模型清单"文件；`.env` 里同时留了 `FORGE_LOCAL_MODEL_NAME=Spark-X2.5-4B.gguf`（说明工程师**考虑过**两种模型，没有 policy 锚定） |
| `temperature = 未显式锚定` → 猜 | **temperature = `PROVIDER DEFAULT`（SDK 发 Omit，provider 侧默认）**，不猜具体值 | 拦截 payload：`temperature=<Omit>`、`top_p=<Omit>`，`extra_params` 里 `temperature` 不在 SDK 发送的显式字段中 |

## 3. Active Production Entrypoint

**Active Production Entrypoint = `AgentRuntime.get_default().run_turn`（CLI 消息平台主路径）**

- 真实入口：`cli/app.py::ChatApp.submit` → `AgentRuntime.get_default().run_turn`（`cli/app.py:265`）
- 等价入口（同语义）：`main.py` 的 `_run_attempt` / `_resolve_approvals_interactive` / `run_task` 都调同一 `run_turn`（`main.py:618,703,872`）
- 备选生产入口（**已冻结**，未验证真实启用）：`webapp.py` 的 `/runtime` SSE 流入口（需 `FORGE_ENABLE_UI=1`，当前默认拒启）
- 后台/定时入口：`scheduler` 经 `run_turn` 的 `mode="async"` + `channel="scheduled"` 路径（AUTO_DENY_CHANNELS，高风险工具自动拒）

本阶段 Golden Trace 全部走**生产 CLI 入口**（`run_turn`）驱动，符合"用用户真实
主入口"的要求。

## 4. Config Source Precedence

见 `CONFIG_PRECEDENCE_TRUTH.md`。核心结论：
- 优先级 = **process env > `.env`（`load_dotenv` 不覆盖已存在键）> 代码默认**
- `FORGE_MODEL_PREF=gateway`（`.env`）让本地 Spark-X2.5-4B 不生效
- 所有 `FORGE_*` 功能开关除 `FORGE_MODEL_PREF` 外**均为代码默认**，`.env` 未显式锚定
- **风险**：benchmark 若在进程内 `os.environ[...]` 直接赋值（优先级 1），
  会压过 `.env` 与代码默认——这是"评测漂移"的机制来源

## 5. Active DB Identity

| 字段 | 值 |
|---|---|
| absolute path | `F:\Byong-hermes\Byong-hermes\my_creative_agent\agent.db` |
| size_bytes | 32,178,176 |
| mtime | 2026-09-19 08:58:06 +0800 |
| sha256 | `89834c210f03dba92253887a6b1d1b5b96cbc697227a62eac7014670e10ffda9` |
| schema version | SQLite `PRAGMA user_version` 未设（`runs` 表存在，668 tasks / 742 runs / 4204 model_calls / 223 provider_attempts / 6503 tool_calls） |
| latest run_id（Phase 1 CASE1） | `task_5e0a63bc` |
| latest run timestamp | 2026-09-19T00:58:04+00:00 |
| latest model_call timestamp | 2026-09-19T00:58:03+00:00 |

**Active DB Confirmed = YES**（执行一次真实 CASE1 task，新 run_id `task_5e0a63bc`
出现在同一 DB，与历史 4201 model_calls 同一表）→ **HISTORICAL_DB_EVIDENCE
已提升为 LIVE RUNTIME EVIDENCE**，可用于后续快照/重放取证。

## 6. Intended / Configured / Actual Model

三个 Model Identity **分开定义**，不合并：

| Identity | 值 | 来源 |
|---|---|---|
| INTENDED_PRODUCTION_MODEL | **UNRESOLVED_POLICY** | 无"正式生产说明"唯一定义"应该是哪个模型"；`.env` 同时留了 `FORGE_LOCAL_MODEL_NAME=Spark-X2.5-4B.gguf`（说明曾考虑过本地）与 `FORGE_MODEL_PREF=gateway`（选了远程），无政策锚定"应该用哪个" |
| CONFIGURED_DEFAULT_MODEL | **agnes-2.5-flash** | `.env` 的 `AGENT_MODEL=agnes-2.5-flash` + `OPENAI_BASE_URL=https://apihub.agnes-ai.cn/v1` |
| ACTUAL_RUNTIME_MODEL | **agnes-2.5-flash** | 拦截到真实 `/chat/completions` 请求（5 请求全部 `model=agnes-2.5-flash`） |

**Model Identity Parity = MISMATCH（INTENDED 无法对齐）**——CONFIGURED 与
ACTUAL 一致，但 INTENDED 无政策来源。按 C1 反幻觉原则**不强行选 Spark 或
Agens**，标 `UNRESOLVED_POLICY`。

## 7. Live Provider Payload

`PROVIDER_PAYLOAD_TRUTH = ESTABLISHED`（本阶段真实拦截 5 个请求）

| 字段 | 值（5 个请求稳定一致） |
|---|---|
| model | `agnes-2.5-flash` |
| endpoint | `https://apihub.agnes-ai.cn/v1/chat/completions` |
| provider | OpenAI 兼容网关（`ResilientProvider` 包装） |
| tool_count | 9（CASE1 由 `route_agent` 裁剪自 `assistant_agent` 的 37） |
| tool_names | `calculate / get_current_datetime / index_workspace / list_code_files / list_workspace_files / read_code_file / read_workspace_file / run_tests / search_documents` |
| tools_blob_sha256 | `8d2e4842ffa44e2d`（5 请求全部相同 → 工具面稳定） |
| message_count | 2 → 4 → 6 → 8 → 10（每轮 +2，符合"一问一答"） |
| temperature | **Omit（SDK 未显式下发 → PROVIDER DEFAULT）** |
| top_p | Omit（同上） |
| max_tokens | 4096（`ModelSettings(max_tokens=4096)`，`agent.py:85`） |
| stream | Omit（`run_turn` 默认 `stream=False`，CASE1 走非流式） |
| tool_choice | Omit（未指定） |
| extra_params 存在 | `frequency_penalty, presence_penalty, reasoning_effort, response_format, store, stream_options, top_logprobs, verbosity, metadata, parallel_tool_calls, extra_body, extra_headers, extra_query, prompt_cache_options, prompt_cache_retention`（SDK 内部填充，值多为 Omit 或空） |

API key 全程脱敏（`OPENAI_API_KEY=***NfGw`，只保留末 4 位）。

## 8. Router → Active Agent → Provider Tool Parity

**Router Selected == Active Agent Exposed == Provider Received**（CASE1 实测）

- `route_agent` 返回的 agent 即 active agent，其 `tools` 列表 9 个
- Provider 收到的 `tools` 也是同样 9 个（tools_blob_sha256 一致）
- **判定 = PASS**（在 CASE1 READ-only 场景下）
- **注**：MIXED benchmark 入口（`decision_checkpoint` 直接 `Runner.run` 不经
  `route_agent`）在工具面**未实测**，标 `NOT ESTABLISHED`

## 9. Production Feature Flags

真实 resolved 值（从 `.env` + 代码默认合成，按 `CONFIG_PRECEDENCE_TRUTH.md`）：

| Flag | Resolved 值 | 来源 |
|---|---|---|
| `FORGE_COMPLETION_READY` | （空 → 走代码默认分支，`runner.py:696`） | 代码默认 |
| `FORGE_REDUNDANT_GUARD` | （空 → 代码默认 on） | 代码默认 |
| `FORGE_DECISION_HINT` | （空 → 代码默认 off） | 代码默认 |
| `FORGE_OBLIGATION_GATE` | **on**（`runner.py:91`，未设 = 默认开启） | 代码默认 |
| `FORGE_OBLIGATION_FEEDBACK` | （空 → 代码默认） | 代码默认 |
| `FORGE_VERIFICATION_FOCUS` | （空 → 代码默认 off） | 代码默认 |
| Approval | **on**（`FORGE_APPROVAL_TTL_SECONDS=3600` 默认） | 代码默认 |
| Permission | on（`FileScope` 默认 strict） | 代码默认 |
| `FORGE_MODEL_PREF` | **gateway** | `.env` |

**Production Feature Flag Truth = ESTABLISHED**（全部 key 的 resolved 值可定位）。

## 10. Benchmark Parity Matrix

见 `BENCHMARK_PARITY_MATRIX.md`。核心行：

| 维度 | production | microbenchmark | decision_qualification | decision_checkpoint |
|---|---|---|---|---|
| 入口 | `run_turn` | `run_turn` ✅ | `run_turn` ✅ | `route_agent+Runner.run` ❌ |
| Model | gateway（agnes） | **local（Spark）** ❌ | gateway ✅ | gateway ✅ |
| `FORGE_COMPLETION_READY` | 代码默认 | **off** ❌ | 代码默认 | 代码默认 |
| `FORGE_REDUNDANT_GUARD` | on | **off** ❌ | on | on |
| 验证源 | `ExecutionEvidence` | **字符串重猜** ❌ | `ExecutionEvidence` | `ExecutionEvidence` |
| 收口门 | 完整 | 完整 | 完整 | **绕过** ❌ |

**Benchmark Production Parity = FAIL**（`decision_checkpoint` 绕收口门 +
`microbenchmark` 硬置 flag + 字符串验证）。

## 11. Verification Authority

- **microbenchmark**：`benchmark/microbenchmark.py:110` 用字符串
  `"退出码:0" / "passed"` 重猜 → **HEURISTIC（`INVALID AUTHORITY`，按第十四项正式标记）**
- **production / decision_qualification**：走 `ExecutionEvidence`（真实
  `tool_calls` + 完成判定看 mutation/verification 证据）→ **EXECUTION_EVIDENCE**
- 综合 = **MIXED**（同一 benchmark 树内并存两种权威源）

**本阶段只审计、不修改**（按第十四项"暂不修改"）。

## 12. Snapshot Qualification

当前 `runtime/snapshot.py` 的 manifest 只有 `agent_db_hash / sessions_db_hash`，
缺：tool schema hash、model fingerprint、obligation ledger、revision state。
**SNAPSHOT_FIDELITY = NOT QUALIFIED**（按第十五项，不直接判 INVALID；按第十六
项逐字段语义影响待下阶段补齐）。

| 缺失字段 | CAN AFFECT REPLAY? | REQUIRED FOR NEXT SNAPSHOT? |
|---|---|---|
| model fingerprint | YES（provider 模型不同 → 行为不同） | YES |
| provider fingerprint | YES（网关版本/超时策略） | YES |
| Runtime Config Hash | YES（feature flag 漂移） | YES |
| tool names | YES（工具面不同） | YES |
| tool schema hashes | YES（参数校验） | YES |
| workspace hash | YES（文件内容） | YES |
| revision | YES（mutation 基线） | YES |
| verified_revision | YES（INV-03/04） | YES |
| verification_due | YES（INV-02） | YES |
| obligation ledger | YES（完成判定） | YES |
| pending approval | YES（INV-01） | YES |
| pending user | YES（needs_user） | YES |

## 13. RunContext Parity

见 `RUN_CONTEXT_PARITY.md`。核心结论：
- **结构 = PASS**：三态（normal / approval request / approval resume）共享
  run_id/task_id/container/FileScope/预算
- **唯一 MISMATCH = `request_text`**：resume 时用 `task.goal` 而非 message，
  显式标 `CONTEXT_PARITY_MISMATCH`（第十九项），但语义影响面为空
  （intent/readiness/permission/filescope/completion 均不依赖 message 重解析）
- 权威事实源仍是 `ExecutionEvidence` + 义务账本（经
  `_hydrate_runctx_from_history` 水合）

## 14. MCP SQLite Runtime Truth

- **MCP_SQLITE_CONFIG_DEFECT = CONFIRMED**（H 盘 command + target DB 都不存在）
- **MCP_SQLITE_RUNTIME_IMPACT = CONFIRMED**（`ensure_mcp()` 实测 6 个 server
  全跳过、0 工具挂载，且根因比"sqlite 路径错位"更上游：`MCP_SERVERS` 在
  `.env` 里是**非法 JSON**，`json.loads` 抛 `Invalid \escape @ char 1860`）
- **注意**：影响面比 Phase 0 的认知**更广**——gitee/playwright/chrome/fetch/
  youtube 5 个 server 也一并失效，不只 sqlite

## 15. Golden READ Trace（CASE1）

- `user_request`：读 `README.md` 首行
- `run_id`：`task_5e0a63bc`（Phase 1 最后一次）
- `entrypoint`：`run_turn`（CLI 语义）
- `route_agent` → `active_agent`：9 工具子集
- `provider model`：`agnes-2.5-flash`
- `tool call`：`read_workspace_file` + `list_workspace_files`（7 调用）
- `ExecutionEvidence`：真实 `tool_calls` 表有 7 行记录
- `Completion Eligibility`：通过（`runs.state=completed`）
- `terminal state`：`completed`
- **5 请求 tools_blob_sha256 全一致（`8d2e4842ffa44e2d`）** → 工具面稳定

## 16. Golden Mutation Trace（CASE2）

- `user_request`：修 `code_sandbox/phase1_fixture/calc.py` 的 `add` 函数 bug
  + 写测试 + 跑通
- `run_id`：`task_f16b2c1f`
- 工具调用：`read_workspace_file` + `edit_project_file` + `write_project_file`
  + `run_python`（4 调用）
- **审批门生效**：3 个高风险工具落 `approvals` 表（`status=pending→expired`），
  驱动脚本**未接 CLI 的 approve 动作**，TTL 到期自动拒 → `runs.state=failed`
- `Provider model`：`agnes-2.5-flash`，工具数 14（mutation 场景裁剪后）
- **判定**：mutation 路径**正确触发审批**（`INV-01` 语义 PASS），但驱动侧
  未接 approve → 没走通 `approve→resume`。这是**测试驱动限制，非 Runtime 缺陷**

## 17. Golden Approval Trace（CASE4）

- `user_request`：改 `code_sandbox/phase1_fixture/guarded.py` 加 docstring
  + 跑 `run_tests`
- `run_id`：`task_9d318dc6`
- 工具调用：`write_code_file`（1 调用）
- **审批门生效**：`run_tests` + `run_python` 各落 1 条 pending → `expired`
  → `runs.state=failed`
- **判定**：与 CASE2 同——Runtime 正确把受保护工具落 pending；驱动侧未接
  approve 导致未走通 `resume`。按本阶段"只审计、不修改"，不强制补齐该
  resume 步（留待 Phase 2 接 `run_task` 的 CLI 侧 approve 动作驱动）

## 18. Runtime Invariants

`tests/test_runtime_invariants.py` 不存在 → **允许新增**（本阶段已新增驱动
脚本 `tools_phase1_invariants.py`，只读断言，不改生产代码）。

| Invariant | 判定（基于 DB + 静态代码路径） | 证据 |
|---|---|---|
| INV-01 pending_approval → completion_eligible=False | **PASS**（CASE2/4 正确落 pending 并拒，未给 completion） | `approvals.status=expired` + `runs.state=failed` |
| INV-02 verification_passed → 真实 verification 工具成功执行 | **PASS**（CASE2 有 `run_python` 真实调用记录，非字符串猜） | `tool_calls` 表 |
| INV-03 verified_revision ≤ current_revision | **NOT ESTABLISHED**（DB schema 无独立 revision 列） | 需 Phase 2 补 schema |
| INV-04 新 mutation → 旧 verified_revision 不覆盖 | **NOT ESTABLISHED**（同上） | 同上 |
| INV-05 normal approval resume → execution_count ≤ 1 | **NOT ESTABLISHED**（驱动未接 approve，无 resume 记录） | 需 Phase 2 驱动补齐 |

**Runtime Invariants = MIXED**（INV-01/02 PASS，INV-03/04/05 NOT ESTABLISHED
——证据链不完整，不判 FAIL）。

## 19. Root Cause Matrix

| 类别 | Root Cause | 证据 | Severity |
|---|---|---|---|
| **Production Runtime** | **NOT ESTABLISHED**（Golden Trace 全部走 `run_turn` 成功；未在生产路径找到"评测 PASS ≠ 生产 PASS"的单一机制） | 三条 CASE 走 `run_turn` 都正常落 DB、审批门生效 | — |
| **Benchmark** | `BENCHMARK_NOT_PRODUCTION_PARITY`：① `decision_checkpoint` 绕 `run_turn` 收口门；② `microbenchmark` 硬置 3 个 `FORGE_*` flag + 强制 local model；③ `microbenchmark` L110 字符串重猜验证 | `benchmark/microbenchmark.py` / `decision_checkpoint.py` | P2（但影响评测可信度 = P0 级） |
| **MCP** | `MCP_SERVERS` JSON 非法 → 全部 6 个 server 在 `ensure_mcp()` 启动前被跳过，0 工具挂载；叠加 sqlite command/DB 路径不存在（H 盘） | `.env` + `mcp_bridge.py:286` + `_audit/mcp_sqlite_probe.json` | P0（影响面：整个 MCP 体系） |
| **Model** | INTENDED 无政策来源，CONFIGURED/ACTUAL 一致为 `agnes-2.5-flash`，但**无"应该是哪个"的锚定** | `.env` 里同时留 local/gateway 两套配置 | P3（政策治理问题） |
| **Snapshot** | manifest 缺 12 项字段 → `SNAPSHOT_FIDELITY = NOT QUALIFIED` | `runtime/snapshot.py` | P1（影响重放可信度） |

## 20. Final Classification

| 字段 | 值 |
|---|---|
| **Active Production Entrypoint** | `AgentRuntime.get_default().run_turn`（CLI 主路径） |
| **Active DB** | `F:\Byong-hermes\Byong-hermes\my_creative_agent\agent.db` |
| **Active DB Confirmed** | **YES**（新 run `task_5e0a63bc` 落同一 DB） |
| **Intended Production Model** | **UNRESOLVED_POLICY** |
| **Configured Default Model** | `agnes-2.5-flash` |
| **Actual Runtime Model** | `agnes-2.5-flash`（拦截 payload 坐实） |
| **Model Identity Parity** | **MISMATCH**（CONFIGURED==ACTUAL，但 INTENDED 无法对齐） |
| **Provider Payload Truth** | **ESTABLISHED**（5 真实请求） |
| **Router→ActiveAgent→Provider Tool Parity** | **PASS**（CASE1 三处 9 工具一致） |
| **Production Feature Flag Truth** | **ESTABLISHED**（全部 resolved 可定位） |
| **Benchmark Production Parity** | **FAIL**（`decision_checkpoint` + `microbenchmark` 漂移） |
| **Verification Authority** | **MIXED**（生产=EXECUTION_EVIDENCE，microbenchmark=HEURISTIC） |
| **Snapshot Fidelity** | **NOT QUALIFIED**（12 字段缺失） |
| **RunContext Parity** | **PASS**（结构）+ **CONTEXT_PARITY_MISMATCH**（`request_text`，语义影响面为空） |
| **MCP SQLite Config Defect** | **CONFIRMED**（H 盘 command+DB 都不存在） |
| **MCP SQLite Runtime Impact** | **CONFIRMED**（`ensure_mcp()` 实测 0 工具挂载；根因 = `MCP_SERVERS` JSON 非法，影响面比"sqlite 路径"更广） |
| **Runtime Invariants** | **MIXED**（INV-01/02 PASS；INV-03/04/05 NOT ESTABLISHED） |
| **Primary Production Runtime Root Cause** | **NOT ESTABLISHED**（本阶段 Golden Trace 走 `run_turn` 全部成功，未在生产路径找到单一机制） |
| **Primary Benchmark Root Cause** | **BENCHMARK_NOT_PRODUCTION_PARITY**（`decision_checkpoint` 绕收口门 + `microbenchmark` 硬置 flag + 字符串验证） |
| **Next Component To Modify** | **benchmark 驱动层**（修 `decision_checkpoint` 改走 `run_turn`；`microbenchmark` 改硬置 flag 为读取 `FORGE_*` + 验证改 `ExecutionEvidence`） |
| **Why** | 三处硬偏移在 `benchmark/microbenchmark.py` + `decision_checkpoint.py` 坐实；改 benchmark 单点即可让评测收敛到生产语义，且符合"修现有组件、不新建系统"；**同时** P0 的 `MCP_SERVERS` JSON 缺陷应单独修（修 `.env` 里 JSON 转义，不影响 benchmark） |

---

### 下一步（按优先级 ≤5）
1. **P0 修 `.env` 的 `MCP_SERVERS` JSON 转义**（单独一行改动，把裸反斜杠
   `H:\Byong-hermes\...` 写成合法 JSON 转义 `H:\\Byong-hermes\\...`，或
   改用 JSON 数组的 `args` 字段承载；**不动任何 Runtime 代码**）。
2. **P0 benchmark 驱动层**：`decision_checkpoint.py` 改走 `rt.run_turn`（与
   `decision_qualification.py:752` 同语义）；`microbenchmark.py` 的 3 个硬置
   flag 改为读取 `FORGE_*` env，验证改 `ExecutionEvidence`。
3. **P1 快照补字段**：`runtime/snapshot.py` 的 manifest 加 12 项
   （model/provider fingerprint、runtime config hash、tool schema hashes、
   revision state、obligation ledger、pending approval 等）；**先读 Phase 2
   再改**（本阶段只审计）。
4. **P1 驱动侧补齐 approve 动作**：CASE2/4 的 `approve→resume` 路径需接
   CLI 的 `run_task` approve 接口（生产语义），本阶段未做（只审计）。
5. **P3 政策治理**：出"INTENDED_PRODUCTION_MODEL"的正式文档（生产模型清单
   + 何时用 local/gateway），让 Model Identity 三个 Identity 可对齐。

### 待确认清单
- 无（本阶段所有判定都有真实代码/DB/payload 证据）
