# FORGE Agent Final Tool Exposure — Phase 21 报告

> ⚠️ **重要更正**：本报告关于 **M1 恒 0%** 的结论已被
> 《FORGE-Agent-Final-Tool-Exposure-Phase21-Addendum》**推翻**。
> 根因是 Phase 20 的 M1 LIVE snapshot 在 **mutation 失败** 后仍被标记 `verification_due=true`，
> 属于 benchmark checkpoint 抓取缺陷。有效 checkpoint 下 M1 为 60–100%（非 0%）。
> 请以 Addendum 为准。

> 日期：2026-09-12
> 主题：Final Tool Exposure Boundary + Capability-Focused Routing Causal Proof
> 基线：《FORGE-Agent-Structured-Commitment-Phase20》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**；本轮不修改生产 Runtime/主循环。
> 模型入口：`FORGE_MODEL_PREF=local`，`FORGE_LOCAL_MODEL_BASE_URL=http://localhost:8080/v1`。

---

## 1. Executive Summary

Phase 20 发现的“synthetic 14 tools vs live 120 tools”在本轮被完整解释并做因果 ablation：

**根因（确定性代码事实）**：`main._run_attempt` 的 **async 分支使用全局 `assistant_agent`，
忽略了 `run_turn`/`execute_turn` 传入的 Tool Router 克隆 `active_agent`**：

```python
# main.py:375
active_agent = agent or assistant_agent
if mode == "sync":   ... active_agent ...
if mode == "stream": ... active_agent ...
# main.py:437  ← async（CLI / benchmark / scheduler 默认）：
return await Runner.run(assistant_agent, message, ...)   # 忽略 active_agent
```

同时 `mcp_bridge._mount_tools` 把 79 个 MCP 工具**无条件 append** 进全局 `assistant_agent.tools`：
`41 native/skill + 79 MCP = 120`。因此 async 路径下 **Router Selection (14) ≠ Final Exposure (120)**。

**因果 ablation（LIVE_SNAPSHOT，唯一变量 = tools exposed）**：

| 变体 | 暴露 | M1 semantic | M4 semantic | Combined |
|---|---|---:|---:|---:|
| **E0** Full live | 120 | 0/10 | 5/10 | 5/20 (25%) |
| **E1** Router-authoritative | 14 | 0/10 | **9/10** | 9/20 (45%) |
| **E2** Verification-focused | 11 | 0/10 | 7/10 | 7/20 (35%) |
| **E3** Verify + prep | 4 | 0/10 | 4/5 | 4/10 (40%) |
| **E4** Verify-only（ceiling） | 2 | 0/5（run_python 5/5 全为探索） | **5/5** | 5/10 (50%) |

结论：
- **Post-router exposure pollution = CONFIRMED**：E1 把 M4 从 50% → **90%**（+40pt），
  证明 async 暴露边界确实是 M4 的一级原因。
- **但 E2/E3/E4 无法修复 M1**：M1 在仅剩 `run_tests + run_python` 时，模型 **5/5 选 run_python 且全为探索语义**
  （semantic 0/5）。→ 主瓶颈不是工具数量，而是 **M1 上下文下模型“先探索/读取再验证”的选择先验**（Case D）。
- **Verification-focused exposure = NEUTRAL**（E2 35% 低于 E1 45%；E4 仅把 M4 拉满，M1 不动）。
- **Structured Action Commitment = REJECTED FOR PRODUCTION**（§三十八）。
- **run_tests = EXPERIMENTAL**；Full E2E = **NOT NEEDED**；M3 = **NOT RUN**。
- Controls：M6/M7/M8 基本 pass（M7 出现 1/2 多余 verification，N=2 弱信号）；
  **Plugin/MCP control FAIL**：Router 对 “列出我的 gitee 仓库” **未选择任何 gitee MCP 工具**
  → 简单“Router 即最终权威”会切断 Plugin/MCP 能力，生产修复必须同时补 plugin/MCP domain approval。

---

## 2. Phase 20 Truth Corrections

正式修正 Phase 20 最终口径（§四）：

| Phase 20 写法 | 修正为 |
|---|---|
| `L1 causal effect = NOT PROVEN` | **`L1 positive causal effect = NOT OBSERVED`**（不再暗示下一轮扩大 L1） |
| `L1 production candidate = NO` | 保持 **NO** |
| `Full E2E needed = NO` | 区分为：**L1 qualification Full E2E = NOT NEEDED**；**eventual production candidate Full E2E = REQUIRED** |
| Oracle VERIFY | 已证明**不足以约束真实 tool choice** |

Phase 20 的 `M1 semantic 0/10、M4 5/10、combined 25%` 与 `Checkpoint validity = PARTIAL` 维持有效。

---

## 3. L1 Final Decision

```text
Structured Action Commitment = REJECTED FOR PRODUCTION
```

原因（Phase 20 证据）：`Oracle VERIFY follow-through only 20%`、`No reliable causal lift`、
`+116s/run latency`。除非模型/provider 发生重大变化，**禁止为 verification 重新引入 L1**。

---

## 4. Regression Manifest

| 项 | 值 |
|---|---|
| command | `.venv\Scripts\python.exe -X utf8 tests\run_tests.py --exclude test_theme_cdp` |
| env | `FORGE_MODEL_PREF=gateway`（离线替身）、`PYTHONUTF8=1`、`TMP/TEMP` 固定长路径 |
| discovery paths | `tests/test_*.py`（**仅顶层**，72 个模块；非递归） |
| collected / ran | **715** |
| passed | **715** |
| failed | 0 |
| skipped | 0 |
| duration | 74.3s（unittest）/ 82.9s（wall） |
| exit | 0 |
| python | 3.11.15 |
| tests+runtime sha256 | `c5a91e6d27a381aa` |

**与 Phase 18 的差异解释**（禁止简单写“715 PASS”=“738 PASS”）：

- Phase 18：`738 passed, 1 skipped`。
- Phase 21 offline：`715 passed, 0 skipped`。
- 计入浏览器用例 `test_theme_cdp`（8 tests，需 Edge headless）后 full = **723**，仍 ≠ 738。
- 差异来自 **测试集组成随 phase 变化（新增/删除/重命名）**，不是同一套测试。
- 因此本轮正式建立三个口径：

```text
Full Test Suite              = 723 collected（含 test_theme_cdp 8）
Runtime Regression Suite     = 715 collected（offline，--exclude test_theme_cdp）
Phase-specific Tests         = benchmark/decision_qualification.py、benchmark/exposure_phase21.py（不在 unittest suite 内）
```

→ 建议把 `tests/run_tests.py --exclude test_theme_cdp` + 上述 sha 固定为 **Regression Test Manifest**。

---

## 5. Final Tool Assembly Chain

`phase21/audit/trace.json`：

```text
① Base Agent Tools            = 41   (CORE_RUNTIME 41；coding_related 13；unrelated 28)
      ↓ Tool Router (select_tool_names)
② Routed Tools                = 14   (M1/M4 相同)
      ↓ ensure_mcp → mcp_bridge._mount_tools 无条件 append 79 MCP
③ Materialized Tools          = 120  (CORE 41 + MCP 79)
      ↓ main._run_attempt async 分支（忽略 active_agent）
④ Final SDK Tools             = 120  (global assistant_agent)
      ↓ provider payload
⑤ Actual tools[]              = 120
```

关键对照：

| 路径 | Final Exposure | Router 是否权威 |
|---|---:|---|
| `mode="async"`（CLI/benchmark/scheduler 默认） | **120** | **NO** |
| `mode="stream"`（web SSE） | 14 | YES |
| `mode="sync"` | 14 | YES |

→ 生产 async 路径存在 **exposure boundary 断裂**：Router 输出被丢弃。

---

## 6. Tool Provenance

`phase21/audit/tool_provenance.json`（每条含 `tool_name/source/source_detail/capability/category/side_effect/risk/schema_hash`）。

来源判定**基于真实注册路径属性**，不靠名字猜：

| source | 判定依据 | count |
|---|---|---:|
| `CORE_RUNTIME` | 非 MCP、非 skill 的 native 工具 | 41 |
| `MCP` | `tool._mcp_source == "mcp"`（`mcp_bridge._build_tool` 设置） | 79 |
| `SKILL` | `tool._tool_origin == "plugin"`（`skills_loader.collect_skill_tools` 设置） | 0（当前 skill 未导出 tools.py） |
| `PLUGIN/CONNECTOR/SYSTEM/OTHER` | 本轮无 | 0 |

79 个 MCP 工具来自：`chrome 29 + gitee 24 + playwright 23 + youtube 2 + fetch 1`（obsidian/sqlite 本轮连接失败被跳过）。

---

## 7. M1 120-Tool Composition

`composition(120)`：

```text
Total = 120
CORE_RUNTIME = 41
MCP          = 79
coding_related = 13          (capability ∈ {VERIFICATION, CODE_EXECUTION, MUTATION_VERIFICATION_LOOP}
                              ∪ category ∈ {code, filesystem})
unrelated_to_coding_verification = 107
```

按 capability（注意：`capability_of` 对未知 MCP 工具走 `side_effect=True → MUTATION` 兜底）：

```text
MUTATION = 93  (native 14 + MCP 79)
READ = 5, DISCOVERY = 8, EXTERNAL_FACT = 3, MEMORY = 3,
COMMUNICATION = 3, CODE_EXECUTION = 1, VERIFICATION = 1, MUTATION_VERIFICATION_LOOP = 1, OTHER = 2
```

→ 与当前 coding+verification 任务**无关的工具 = 107/120（89%）**。
（附带发现：79 个 MCP 工具被 capability 兜底误判为 MUTATION，可能影响 routing/obligation 语义，建议后续单独修。）

## 8. M4 120-Tool Composition

与 M1 **完全相同**（同一全局 `assistant_agent`）：Total 120、CORE 41、MCP 79、
coding_related 13、unrelated 107。Router 对 M4 选择同样 14 个工具。

---

## 9. Router Authority Audit

**结论：Router 不是 Final Exposure Authority（async 路径）。**

- `route_agent` 正确产出 14 个任务相关工具（含 `run_tests`/`run_python`/`code_loop`/read/mutation）。
- `main._run_attempt` async 分支（`main.py:437`）调用 `Runner.run(assistant_agent, ...)`，
  **未使用 `active_agent`**（sync/stream 分支使用了）。
- 因 `run_turn` 默认 `mode="async"`，CLI、`benchmark/coding_experiment`、`benchmark/microbenchmark`、
  scheduler 等路径全部绕过 Router。

→ **Router Selection ≠ Final Exposure 真实成立。**

---

## 10. MCP/Plugin/Skills Materialization

真实调用链（`mcp_bridge.py`）：

1. `ensure_connected()` → 逐 server `attach_server_tools()` → `_build_tool()` 设 `_mcp_source="mcp"`；
2. `_mount_tools()`：`assistant_agent.tools = existing + mounted`，并 `assistant_agent.mcp_servers = []`
   （关闭 SDK 自动挂载通道，防重复执行）；
3. `runtime.runner.refresh_tool_wrappers()` / `_patch_agent_tools()` 对全部工具重包（审批+记账）。

→ MCP 工具进入最终 exposure 的路径是 **全局 agent 变异 + async 分支使用全局 agent**，
并非经过 Router。这是 **routing boundary bug（async 路径）**，而非 SDK 默认行为。

---

## 11. Tool Schema Context Cost

`phase21/audit/trace.json → schema_cost`（近似：bytes/4；chars/3；无精确 tokenizer）：

| 变体 | tool_count | schema_bytes | approx tokens (bytes/4) |
|---|---:|---:|---:|
| E0 | 120 | 36,545 | ~9,136 |
| E1 | 14 | 10,267 | ~2,566 |
| E2 | 11 | 8,606 | ~2,151 |
| E3 | 4 | 3,105 | ~776 |
| E4 | 2 | 2,358 | ~589 |

→ 120 tools 相对 Router 14 tools 多出 **~26.3KB / ~6,570 tokens** 的 schema 负担（≈ 3.6×），
对本地 32k 上下文与注意力构成实质挤占。但 M1 在 E4（~589 tokens）仍 0% → 说明 **context cost 不是 M1 的决定因素**。

---

## 12. E0 Full Exposure（Control）

复用 Phase 20 LIVE baseline（同一 snapshot，schema hash 一致），N=10：

| case | preferred run_tests | semantic | discovery drift | latency avg |
|---|---:|---:|---:|---:|
| M1 | 0/10 | 0/10 | 4/10 | 38.3s |
| M4 | 4/10 | 5/10 | 5/10 | 14.64s |
| Combined | 4/20 | **5/20 (25%)** | 9/20 | — |

---

## 13. E1 Router-Authoritative Exposure

暴露 = 真实 Router 选择的 14 个工具（`run_tests/run_python/code_loop/read/list/search/edit/write/...`），N=10：

| case | preferred | semantic | discovery drift | latency avg |
|---|---:|---:|---:|---:|
| M1 | 0/10 | **0/10** | 5/10 | 23.68s |
| M4 | 9/10 | **9/10** | 1/10 | 11.3s |
| Combined | 9/20 | **9/20 (45%)** | 6/20 | — |

→ **M4 +40pt（50%→90%）；M1 无变化（0%）。**

---

## 14. E2 Verification-Focused Exposure

暴露 = `run_tests/run_python/code_loop` + 读测试入口的 READ/DISCOVERY + 必要 MUTATION（11 个），N=10：

| case | preferred | semantic | discovery drift | latency avg |
|---|---:|---:|---:|---:|
| M1 | 0/10 | **0/10** | 6/10 | 14.97s |
| M4 | 7/10 | **7/10** | 3/10 | 17.84s |
| Combined | 7/20 | **7/20 (35%)** | 9/20 | — |

→ 比 E1 **更低**（M4 90%→70%）；移除 `calculate/get_current_datetime/index_workspace` 未带来收益。

---

## 15. E3 / E4 Causal Ceiling

**E3**（`run_tests/run_python` + `read_code_file/list_code_files`，4 个），N=5：

| case | semantic | first tools |
|---|---:|---|
| M1 | **0/5** | `read_code_file ×5` |
| M4 | **4/5** | `run_tests ×4, list_code_files ×1` |

**E4**（`run_tests + run_python` only，2 个），N=5：

| case | semantic | first tools | 语义分类 |
|---|---:|---|---|
| M1 | **0/5** | `run_python ×5` | OTHER×4, DISCOVERY×1（用 run_python 探索，不是跑测试） |
| M4 | **5/5** | `run_tests ×5` | SEMANTIC_VERIFICATION×5 |

→ **M1 因果上限 = 0%**：即使 choice set 只剩两个 verification 工具，模型仍把 `run_python` 当探索工具用。
→ **M4 因果上限 = 100%**。模型行为在 M1/M4 之间出现本质分叉。

---

## 16. Exposure Causal Lift

Semantic Verification Lift（combined）：

```text
E0 = 25%
E1 = 45%   (E1 - E0 = +20pt；M4 +40pt / M1 0pt)
E2 = 35%   (E2 - E1 = -10pt)
E3 = 40%   (E3 - E2 = +5pt，N 不同)
E4 = 50%   (ceiling；M4 100% / M1 0%)
```

- Router-authoritative exposure：**+20pt combined（M4 +40pt）** → 对 M4 有真实因果贡献。
- Verification-focused phase routing：**无正贡献（NEUTRAL，甚至 -10pt）**。
- 上限仍 <90% combined，且 M1 恒 0%。

---

## 17. M1 Results

| 变体 | 暴露数 | semantic | first action |
|---|---:|---:|---|
| E0 | 120 | 0/10 | write_project_file / read / list |
| E1 | 14 | 0/10 | write_project_file / read / list |
| E2 | 11 | 0/10 | write_project_file / read / list |
| E3 | 4 | 0/5 | read_code_file ×5 |
| E4 | 2 | 0/5 | run_python ×5（探索语义） |

→ M1 的 0% 与暴露数量**无关**；是模型在 M1 上下文下的选择先验（先读/先探索）。

## 18. M4 Results

| 变体 | 暴露数 | semantic | first action |
|---|---:|---:|---|
| E0 | 120 | 5/10 (50%) | run_tests 4 / run_python 1 / list 5 |
| E1 | 14 | 9/10 (90%) | run_tests 9 / list 1 |
| E2 | 11 | 7/10 (70%) | run_tests 7 / list 3 |
| E3 | 4 | 4/5 (80%) | run_tests 4 / list_code 1 |
| E4 | 2 | 5/5 (100%) | run_tests 5 |

→ M4 对 exposure 高度敏感；**Router-authoritative（E1）即可达 90%**，无需 verification phase special routing。

---

## 19. Controls

`phase21/controls`（N=2）：

| case | verification_due | first tools | unnecessary verification |
|---|---|---|---:|
| M6 read-only | false | `NO_ACTION ×2` | 0/2 |
| M7 mutation-no-verify | false | `run_tests ×1, run_python ×1` | **1/2** |
| M8 keyword false-positive | false | `list/read ×2` | 0/2 |

→ Control regression ≈ 0（M7 的 1/2 属模型自然选择，非 L1 激活；`verification_due=false` 时未注入 commitment）。
L1 activation = 0。**Unnecessary Verification 出现 1/2（弱信号，N=2 不足以定论）。**

## 20. Plugin/MCP Control

| 控制 task | Router 是否选择对应能力 | 结果 |
|---|---|---|
| C_WEB「搜索北京天气」 | `web_search` ✅ | PASS |
| C_MEMORY「记住邮箱」 | `remember` ✅ | PASS |
| **C_MCP「列出我的 gitee 仓库」** | **无任何 gitee MCP 工具**（只给 `fetch_github_repo`/`read_spreadsheet`） | **FAIL** |

- 路由级：`mcp_tools_exposed = []`、`plugin_control_pass = false`。
- 决策 probe（E1 暴露）：`get_current_datetime` / `NO_ACTION`，`mcp_first_rate = 0/2`。

→ **当前 Router 不会为需要 MCP 的任务批准 MCP 工具**。因此
“让 Router 成为 Final Exposure Authority”这一修复**必须同时补 plugin/MCP domain approval**，
否则会切断已安装的 Plugin/MCP 能力（违反 §二十六）。

---

## 21. Final Exposure Invariant（生产候选设计）

```text
Final model tools  ⊆  Router-approved tools  +  explicit system-required tools
```

- `Plugin / MCP / Skill` 工具进入最终 exposure，必须经 **router/domain approval**
  或拥有明确 `always-required` 系统理由。
- **Always-Required Tools 必须极少**（候选：`get_current_datetime`、`calculate`、
  `read_workspace_file`、`list_workspace_files` = `BASE_TOOLS` 4 个；每个都需证明所有任务都需要）。
- 修复点（最小机制）：
  1. `main._run_attempt` async 分支改用 `active_agent`（与 sync/stream 一致）；
  2. `tool_router` 增加 **plugin/MCP domain approval**（如 gitee/浏览器/数据库意图命中时批准对应 MCP 工具）。
- **不全局禁用 MCP/Plugins**；只做 `Installed ≠ Always exposed`。

## 22. Production Candidate

**NO**。§三十三生产进入条件未满足：

```text
E1/E2 semantic >= 90% (LIVE)  -> E1 45% / E2 35%，FAIL（M4 90% 但 M1 0%）
Control regression = 0        -> 基本 PASS（M7 1/2 弱信号）
Provider error = 0            -> PASS
invalid tool calls 不上升      -> PASS（0）
```

→ 仅保留 **production candidate design**（§21 invariant + async 暴露边界修复），本轮不 merge。

---

## 23. Full E2E（若达标）

**NOT RUN**。§三十四：E1/E2 decision probe 未达标（combined <90%，M1 0%）→ 禁止浪费 Full E2E。
（eventual production candidate 的 Full E2E 仍是 **REQUIRED**，但需先修 M1 行为。）

## 24. M3（若达标）

**NOT RUN**。进入条件（M1/M4 verification selection 可靠）未满足。

---

## 25. run_tests Qualification

| 条件 | 状态 |
|---|---|
| Tool Contract Security | PASS |
| Raw API / Minimal SDK semantic | PASS（Phase 16 100%） |
| **Decision selection reliable（LIVE）** | ❌ M1 0/10、M4 5/10 |
| **Exposure fix 生效** | ⚠️ E1 使 M4 90%，但 async 边界未修（生产仍 120） |
| M1/M4 Full E2E | NOT RUN |
| M3 ≥2/3 | NOT RUN |
| False Completion / Claim | 0 |
| Control regression | ~0 |
| Provider error / invalid tool calls | 0 |

→ **run_tests = EXPERIMENTAL**。

## 26. Runtime Regression

- 本轮**未修改** `runtime/` 与生产主循环（仅新增 `benchmark/exposure_phase21.py` + `phase21/` artifacts）。
- `regression.ps1` / `tests/run_tests.py` → **715 passed, 0 failed, 0 skipped, exit 0**（见 §4 Manifest）。
- `Completion/Obligation Gate`、Repair、Approval、FileScope、Tool Budget、run_tests 实现均未触碰。
- **False Completion = 0**；**False Failure after satisfied obligations = 0（确定性）**。

## 27. Local Microbenchmark Status

**NO**。M1 恒 0%（E0–E4），M4 依赖 exposure 且 combined <90%；M3 未执行。

## 28. Production Status

```text
Runtime Semantic Baseline            = YES（未修改）
run_tests Tool Contract Security     = PASS
run_tests Execution Boundary         = KNOWN ACCEPTED LIMITATION
Full Agent Verification Baseline     = NO（LIVE：M1 0/10、M4 5/10）
Final Exposure Boundary (async)      = BROKEN（Router 输出被忽略；120 tools）
Router = Final Exposure Authority    = NO（async）
Post-router Exposure Pollution       = CONFIRMED
Verification-Focused Exposure        = NEUTRAL
Structured Action Commitment         = REJECTED FOR PRODUCTION
Verification Repair Loop             = PASS（确定性）
Finalization Closure                 = PASS（确定性）
Local Microbenchmark Baseline        = NO
```

## 29. Next Direction

1. **最小生产修复候选**：修 `main._run_attempt` async 分支使用 `active_agent`，
   并补 Router 的 plugin/MCP domain approval（否则 MCP 能力不可达）。
2. **M1 行为瓶颈**：模型在 M1 上下文下即使只剩 `run_tests+run_python` 也用 `run_python` 探索 →
   需研究 **provider/model-specific choice enforcement**（例如 run_python 语义门 / 工具级 intent 约束），
   而不是继续 exposure 数量优化或 L1。
3. **M3**：待 M1/M4 verification selection 可靠后再进入。

---

## 最终输出

```text
E0 final tool count =
120

E1 final tool count =
14

E2 final tool count =
11

E0 semantic verification =
5/20（25%）

E1 semantic verification =
9/20（45%；M4 9/10、M1 0/10）

E2 semantic verification =
7/20（35%；M4 7/10、M1 0/10）

E3 semantic verification =
4/10（40%；M4 4/5、M1 0/5）；E4 ceiling = 5/10（M4 5/5、M1 0/5）

Router is final exposure authority =
NO（async 路径：Router 14 vs Final 120）

Post-router exposure pollution =
CONFIRMED（main._run_attempt async 忽略 active_agent + mcp_bridge 无条件 append 79 MCP）

Verification-focused exposure =
NEUTRAL（E2 35% < E1 45%；E4 仅修复 M4，M1 恒 0%）

Structured Action Commitment =
REJECTED（REJECTED FOR PRODUCTION）

Full E2E needed =
NO（decision probe 未达标；eventual production candidate 才 REQUIRED）

run_tests =
EXPERIMENTAL

Primary remaining bottleneck =
M1 上下文下模型的选择先验：即使只暴露 run_tests+run_python，仍 5/5 用 run_python 做探索
（exposure 数量与 L1 commitment 均不能改变）

Production fix =
main._run_attempt async 分支改用 router 选择的 active_agent，使
Final model tools ⊆ Router-approved tools + 极少 always-required；并补 Router 的 plugin/MCP domain approval

Next component to modify =
main._run_attempt 最终暴露边界（+ tool_router 的 plugin/MCP domain approval），
而非继续 Action Commitment / exposure 数量
```

---

## 附：本轮新增文件

| 文件 | 改动 |
|---|---|
| `benchmark/exposure_phase21.py` | **新增** Phase 21 Harness：Exposure Trace / Provenance / E0–E4 / Controls / Regression 支撑 |
| `phase21/audit/trace.json`、`tool_provenance.json` | **新增** 工具形成链 + 120 工具 provenance/组成/schema cost |
| `phase21/exposure/` | **新增** E1/E2/E3 因果实验（E0 复用 Phase 20 baseline） |
| `phase21/ceiling/` | **新增** E4 verification-only 因果上限 |
| `phase21/controls/` | **新增** Router authority / M6-M8 / plugin-MCP control |
| `phase21/regression_full.log` | **新增** Regression Manifest 原始日志 |

> 生产 `runtime/` 与 `main.py` 本轮无任何改动；所有修复仅为 candidate design。
