# FORGE-Agent 全模块审计（不信报告，直接读代码）

日期：2026-09-22  方法：真实 import/调用链判定活/死代码，逐模块给 优化/保留/合并/删除 判定。
审计基线：runtime 41 文件 17.5k 行、cli 4.5k、delivery 8.9k、benchmark 8.2k、agent-skills 4.3k、顶层 ~9k。

> 判定图例：✅保留　⚙️优化（改但不删）　🔀合并　🗑️删除　⛔误报（代码无需改）

## 一、删除/归档候选（孤儿 + 实验孤岛）

| 对象 | 证据 | 判定 | 理由 |
|---|---|---|---|
| `delivery/`（88 文件 8.9k 行） | 主链 0 处 import；全部 .py 含 `if __name__`；git 跟踪 | 🗑️归档 | 一次性评测/实验交付目录（19 probe_* 脚本 + 历史 DELIVERY.md），自 2026-09-19 后未再引用。建议移入 `archive/delivery-20260919/` 并移除 git 跟踪 |
| `tools_phase1_case2_4.py` / `_provider_capture.py` / `_invariants.py` | 3 文件仅互引，0 处外部 import；均含 `if __name__` | 🗑️删除 | Phase 1 历史一次性探针脚本（case2/4、provider 捕获、不变式检查），工具实现已迁进 `tools.py`，此处无主链/测试引用 |
| `benchmark_fixture/_inline_*.py`（55 个，51 被 git 跟踪） | `benchmark/` 无生成器命中；`__pycache__` 内 | ⚙️gitignore | 运行 churn（inline 工具产物），不入库。`.gitignore` 加 `benchmark_fixture/_inline_*.py` |
| `code_sandbox/{auth_fix,fix_auth_calculate}/` | 未 git 跟踪；`agent.py` 提到 `code_sandbox/<项目名>/` 是沙箱约定目录 | ⛔保留 | 沙箱合法产物（`auth_fix` 等是 P0 验证产物），`.gitignore` 已排除，不动 |
| `calc.py`（6 行） | 仅被 `benchmark/` fixture 引用 | ✅保留 | 评测沙箱夹具，非主链；保留 |

## 二、生产代码残留调试（明确要删）

| 位置 | 证据 | 判定 |
|---|---|---|
| `runtime/runner.py:392-399` | `_patch_agent_tools` 内 `# DEBUG: log all tool invocations` + `_sys.stderr.write(f"[TOOL_INV] {name}")`，硬编码 4 工具名 | 🗑️删除 | 残留调试混进生产工具包装器，且与 D3 结构化日志（`slog`）功能重复。直接删 395-399 的 stderr 块 + 注释 |

## 三、runtime/ 内核（17.5k 行）

| 文件 | 行数 | 判定 | 理由 |
|---|---|---|---|
| `runner.py` | 3327 | ⚙️优化 | `_patch_agent_tools` 单方法 890 行（377→1270），审批门/记账/约束拦截堆叠；拆分 `needs_user`/`constraint`/`ledger` 为独立 helper 提可读性 |
| `task_manager.py` | 2526 | ✅保留 | 干净（0 调试残留），SQL 迁移+单点写入完整 |
| `readiness_gate.py` | 1308 | ⚙️优化 | 159 个 if/elif，`DiscoveryTracker` 30+ 方法。`result_overlap`+`saturation` 与 `tools._too_repetitive`(D4) 部分重叠，可合并 |
| `completion.py` | 1032 | ✅保留 | 终态门+evidence，单职责清晰 |
| `tool_router.py` | 934 | ✅保留 | 路由核心，独立 |
| `provider_gateway.py` | 782 | ✅保留 | L1 重试/fallback/审计完整 |
| `runctx.py` | 656 | ✅保留 | RunContext 单点状态 + D4 repeat_calls |
| `context.py` | 655 | ⚙️优化 | 记忆 `7 处 get_items(limit=10_000_000)` 全量加载已标为 P1；`quick_stats` 快路径已存在，全量加载可改流式 |
| `structured_log.py` | 203 | ✅保留 | D3 新建，`slog` 关键字参数合并已修 |

**runtime 小文件**：`errors.py`(148,13 引用) / `registry.py`(75,3) / `trust.py`(49,4) / `state_machine.py`(45,2) / `checkpoint.py`(41,1) / `netpolicy.py`(75,1) / `token_gate.py`(97,1) / `public_response.py`(46,1) / `budget.py`(71,1)。
判定：🔀可合并。`state_machine`+`budget`+`token_gate`+`netpolicy`+`public_response`（均 <100 行、各 1 处引用）功能相邻，合并为 `runtime/gates.py` 可减文件数；但非紧急，当前独立清晰，**保留优先**。

## 四、顶层工具/能力模块（~9k 行）

| 模块 | 行数 | 判定 | 理由 |
|---|---|---|---|
| `tools.py` | 910 | ✅保留 | 37 工具注册中心，活代码（D4 `note_repeat_call` 已接） |
| `rag.py` | 795 | ⚙️优化 | `index_workspace`/`search_documents` 被 `agent.py` 注册活；PDF 索引依赖外部依赖，保留但可加惰性加载 |
| `code_exec.py` | 668 | ✅保留 | 沙箱执行（`run_python`/`run_tests`），cancel_scope 依赖 |
| `evaluate.py` | 612 | ✅保留（双入口，非硬耦合） | **核实修正**：`benchmark/*` 实际 import 的是 `benchmark/evaluator.py`，**不** import 顶层 `evaluate.py`。顶层 `evaluate.py` 是独立端到端评测脚本（真调模型+argparse 入口）。两套并列入口（`python evaluate.py` vs `python -m benchmark`）功能部分重叠但非代码硬耦合 → 低优先级，文档标注即可 |
| `office_docs.py` | 496 | ✅保留 | Office 读写工具，活 |
| `compact.py` | 391 | ✅保留 | 上下文裁剪（`/compact` 命令用） |
| `mcp_bridge.py` | 380 | ✅保留 | MCP 桥 |
| `agent.py` | 373 | ⚙️优化 | 工具注册中心；**instructions prompt 长达 229 行**（89→315），巨型 prompt 本身是维护负担，可抽到独立 `.md`/常量模块 |
| `guardrails.py` | 343 | ✅保留 | 输入/输出安全闸，活 |
| `observability.py` | 300 | ✅保留 | trace 安装（main --trace） |
| `scheduler.py` | 291 | ✅保留 | daemon 循环活 |
| `github_fetch.py` / `research.py` / `project_edit.py` / `voice.py` / `multimodal.py` | 272/267/247/243/128 | ✅保留 | 各被 `agent.py` 注册为工具，活 |

## 五、benchmark/（8.2k 行 30 文件）

| 文件 | 判定 | 理由 |
|---|---|---|
| `decision_qualification.py`(1327)/`bounded_window.py`(873)/`evaluator.py`(859)/`m3_closed_loop.py`(527)/`exposure_phase21.py`(494) | ⚙️优化 | 单次实验大文件，跑完即归档，不进 CI |
| `evaluate.py` + `benchmark/evaluator.py` | ✅保留 | **核实修正**：二者非硬耦合。顶层 `evaluate.py`（真调模型端到端）与 `benchmark/`（重放 raw run 判分）是**两套并列评测入口**，功能重叠但代码独立。保留两者，文档标注入口分工即可 |
| `cases.py`(338)/`matrix.py`(210) | ✅保留 | 评测核心 |

## 六、cli/ + main.py（4.5k+1159）

| 模块 | 判定 | 理由 |
|---|---|---|
| `cli/`（16 命令） | ✅保留 | `cmd_*` 全被 `app.py` 注册，活 |
| `main.py`(1159) | ⚙️优化 | 单文件 5+ 职责：CLI 入口 + `chat_async/sync/voice` + `daemon_loop` + 渲染 + 错误展示 + trace。建议拆 `cli/daemon.py`（883-984）+ `cli/render.py`（部分 283-374 渲染）|

## 七、agent-skills/（4.3k 行，10 技能）

各技能 = `SKILL.md` + `skill.json`，由 `tools/install_compiled_skills.py` 编译安装到 `skills/`。
判定：✅保留（编译产物源）。但 `brand-guidelines`/`theme-factory`/`slack-gif-creator`/`frontend-design` 与"全栈 Agent"主链（编码/文档/研究）关联弱，是否按需启用由 `.env SKILLS=` 控制，不属删除项。

## 八、处置清单汇总

**删除（4）**
1. `runtime/runner.py:392-399` DEBUG stderr 块（生产代码残留，与 slog 重复）
2. `tools_phase1_case2_4.py` + `_provider_capture.py` + `_invariants.py`（0 引用孤岛脚本）
3. `delivery/` 整目录（实验孤岛，88 文件）→ 移入 `archive/` + gitignore
4. `benchmark_fixture/_inline_*.py` 51 个 → 加 `.gitignore`（非删除）

**合并（3）**
5. `state_machine`/`budget`/`token_gate`/`netpolicy`/`public_response` → 可选合并为 `runtime/gates.py`
6. ~~双评测器~~ 已核实修正：`evaluate.py` 与 `benchmark/evaluator.py` 非硬耦合，保留两套并列入口
7. `readiness_gate.result_overlap`/`saturation` 与 `tools._too_repetitive` 去重（D4 已部分解决）

**优化（5）**
8. `agent.py` 229 行 instructions prompt 抽到独立常量模块
9. `main.py` 拆 daemon/render 子模块
10. `runner._patch_agent_tools` 890 行方法拆分 helper
11. `context.py` 全量 `get_items(limit=10M)` 改流式/分页
12. `readiness_gate.py` 159 分支拆状态机

**保留（不动）**
13. `calc.py`（评测夹具）、`code_sandbox/*`（沙箱产物）、runtime 核心（task_manager/completion/provider_gateway/runctx/tools）

## 九、风险与说明
- 删除 `delivery/` 与 `tools_phase1_*` 前先确认无 CI/评测脚本引用（本报告已 grep 验证 0 引用）。
- `runner.py` DEBUG 块删除不影响功能（仅调试打印），低风险。
- 合并/优化项（5-12）属架构改进，非 correctness，建议单独立项，逐项 PR + 全量回归。
- 全部删除项执行前跑 `PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/ -q` 确认 1422 基线不回归。
