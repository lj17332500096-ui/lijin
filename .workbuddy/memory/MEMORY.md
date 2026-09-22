# 项目长期记忆 — my_creative_agent

> 2026-09-20 精简重写。只留跨会话可复用的结论；逐条排查过程见 `.workbuddy/memory/YYYY-MM-DD.md`。

## 1. 架构与边界
- 单 Agent + 单主链 + 极厚外围治理（harness 型）。入口：`main.py`（`--classic`/`--voice`/`--daemon`/`--tui`）+ `cli/`。
- 前端 UI 层冻结（`webapp.py`/`llama_bridge.py`），仅 `FORGE_ENABLE_UI=1` 可起。
- 边界红线（`tests/test_ui_isolation.py`）：后端/CLI 不得 `import webapp|llama_bridge`。
- 工具层：实测 37 个 native 工具。`_EXPLICIT_ONLY_TOOLS` 是**限制**表（在表内要命中语义才放行，不在表内永远放行）。
- 技能两目录：`skills/`（运行时，27 个）｜`agent-skills/`（编译产物/安装源，10 个）。`gorden-ppt` 族有意不走高门槛。
- 数据：`agent.db`（WAL）+ `sessions.sqlite` + 文件产出（`notes/`、`exports/`）。

## 2. 工程铁律
- **run_id 是事件/消息/审计的主键**。~~run_id = trace_id~~ 是**错的**（2026-09-22 实测纠正）：
  `runner.py` grep `trace_id` = 0 命中；`trace_export.py` 曾按 `trace_id == run_id` 过滤而恒假 → `spans` 恒 `[]`。
  已改为记录盖 `run_id` 字段（不篡改 OTel 的 `trace_id`），过滤键用 `run_id`。
  另：`tool_calls.task_id` 装 run_id，`messages.task_id` 装容器 id —— 同列异义，join 必错。
- **清理路径**：`except (Exception, SystemExit)`，保留 `KeyboardInterrupt`。
  （本机 WorkBuddy 的 Python shim 会把 `shutil.rmtree` 劫持成 trash 并在失败时抛 `SystemExit`，
  所以这条不是教条而是必需；但**测试自己的**清理调用被劫持时要靠 `PYTHONPATH=` 屏蔽，别去改生产清理路径。）
- **重试权只属于 provider 网关**：SDK `max_retries=0`；单 provider `FORGE_PROVIDER_MAX_ATTEMPTS=3`；
  primary+fallback 合计 `FORGE_PROVIDER_MAX_TOTAL_ATTEMPTS=6`；L3 格式闸 `FORGE_FORMAT_RETRY_MAX=1`。
  判据写法 `local_attempts + 1 < max_attempts`（`local_attempts` 记的是**重试**次数）。
- **同一事实的多种表示只能取其一，相加必错**（明细 vs 聚合、原始 vs 派生）。
- **判据字符串必须与生产端写出的值对齐**：`record_attempt` 的 kind 钩子曾只认 `"ok"`
  而生产写 `"success"` → 钩子静默失效、整列恒 NULL。任何「按名字/标记分流」的地方都要有回归用例。
- **同文件多处编辑**：串行 Edit 或整文件 Write，禁止并行（互相覆盖且都报成功）。
- **禁止 `cat >> 文件 << 'EOF'`**：Git Bash 会写到文件头部并覆盖。追加用 Write 或 Python。
- **脏工作区提交**：绝不用 `git add -A`。精确暂存 = `git ls-files --others --exclude-standard` + `git diff --name-only --diff-filter=M`，用 `--pathspec-from-file` 喂。
- **Git Bash `find` 是 Windows find.exe**，结果不可信。`git ls-files/status` 中文路径要 `-c core.quotePath=false`。
- **`git rm -r` 删多子目录会波及父目录**（退出码 0 ≠ 删对了）。删完核对 `git diff --name-only --diff-filter=D`。
- **本机跑全量的正确命令**：`PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/ -q`。
  不屏蔽 shim 会有 9~11 条 `SystemExit: 1` 假失败（测试自身清理被劫持，非代码问题）。
  pytest 的 summary 行有时不落盘，拿失败清单要用 `--junitxml=`。

## 3. 评测方法论
- n=50 只能检出 ≈±28pp，n=100 ≈±20pp，检 10pp 需 n≈400。任何「提升 X%」必须给样本量与 CI。
- 绝对值判据（「必须为 0」）在随机 harness 上不可满足 → 改「不劣于基线」统计检验。
- 跑间：结果层翻转 0%，但工具序列发散 67%、终稿文本一致仅 8% → 单次观察不足为凭。
- 注入类 A/B 必须先证明注入没改变工具可用集（`TOOL_ROUTER=off` 结构性保证）。

## 4. 情节记忆层（定级：离线工具）
- 三臂 n=100：A 43.0% / B 42.0% / C 43.0%（修正后），Safety 1/3/0。
- **A vs B 无效**（工具集 46/46 不同）；**C 组 pull 从未被检验**（工具从未派发）。
- 保留价值：CLI `/episode` 离线复盘好用；47 条单测正确；只是「喂给模型」不划算。
- 唯一重启条件：高信噪比定向信号（同类任务连续失败 ≥3 次后定向注入教训）。

## 5. 已修问题（2026-09-19 ~ 09-21）
- **备份文案撒谎**：`_backup()` 对不存在文件直接 return 但回执声称已备份 → 已修。
- **名册死名**：`code_loop_tool`/`web_search_v2`/`delete_task` 已从 7 文件清除，`tests/test_tool_roster_consistency.py` 守住。
- **评测器双倍计数**：`observation_from_raw` 同时累加明细与聚合 → 已修，基线 +18pp（50%→68%）。
- **per-tool 预算**：`runctx.py::per_tool_budget()` 可覆盖；`list_workspace_files` 上限 8。
- **TERMINALIZE 后继续受理工具调用**（P0-2，2026-09-20 晚修）：
  `runner.py` 预算分支加 `budget_block_key` + `note_blocked_reason >= 2 → ConvergenceTerminated`。
  验证：T046 33 次调用（24 被拒）→ 0 次调用，直接 completed。
- **TERMINALIZE 收口空回答兜底**（P1-2）：`ConvergenceTerminated` 分支 `assistant_text` 为空时强制给一段说明。
- **日志通道隔离**（P0-1）：`guardrails._log_guardrail_failure` 按 `FORGE_TEST_MODE`/`FORGE_EVAL_MODE` 分流，测试桩不再污染生产日志。
- **WORKSPACE_ROOT 钉到项目根**（P1-1）：`benchmark/eval_runner.py::main` 开头调 `_pin_workspace_root()`，fixture 落入子树。
- **模型切换**（2026-09-21 凌晨）：`AGENT_MODEL` 从 `agnes-2.5-flash` 切到 `agnes-3.0-flash`。
  全量 n=50 新基线 68.0%（vs 2.5 的 76%），−8pp 不显著（CI 含 0）。
  3.0 上 P0~P2 修复部分生效（T008/T010/T020/T049 通过）；
  追问类 case 退化（T040 false_completion、T007 final_text 空）需单独跟踪。

## 6. API 接口层整改（2026-09-22《FORGE-API-LAYER-AUDIT》§17 顺序）
- 定级 **C**：Runtime 内核 B+，工具层 C，事件协议 C-，HTTP 接口层 D，可观测性 C-。
- **HTTP 层整体停用**（`ui_frozen.py:37 UI_FROZEN=True` + `webapp.py:1999 require_ui_enabled`）；
  活动路径只有进程内 `main.py` / `cli/app.py` → `AgentRuntime.run_turn`。
  报告里每项都标了 `[活动]` / `[潜伏]`，潜伏项不要因"反正没启用"忽略。
- **Phase A（A1–A9）完成**，验收 `tests/test_api_layer_phase_a.py`：
  A1 参数校验失败不再记成功、A2 事件白名单由 `TaskState` 派生、A3 `webapp` 补 `task_id`、
  A4 进程级退出信号统一收口（`ProcessExitInterrupted`，SystemExit→failed / Ctrl+C 仍穿透）、
  A5 审批名单由 `side_effect` 派生、A6 目录补 `think`/`search_sources`、A7 受信根加门禁、
  A8 四条审计出口统一脱敏、A9 墙钟限值判据修正。
- **Phase B（B1–B9）完成**，验收 `tests/test_api_layer_phase_b.py`（56 条）：
  B1 `transition` 改 CAS、B2 关 SDK 内建重试 + 尝试预算、B3 分层超时显式化、
  B4 `waiting_user` 进单 Active Run（**方案修正**：新 Run 取代而非拒绝，否则打断追问继承）、
  B5 取消杀同步工具进程树（**新增 `runtime/cancel_scope.py`**）、B6 `spec.timeout_seconds` 真正接线、
  B7 `tool_calls` 补耗时/轮次/归一化参数/指纹、B8 记录盖 `run_id` + 追踪改 opt-in、
  B9 `auto_recover` 阈值方向与 `budget_json` 死列。
- **Phase C（C1–C8）/ Phase D（D1–D5）未开始**。C 的前置决定：`/api/*` 是"复活"还是"归档"
  （报告建议归档 60+ 条无消费者的 `/api/*`，只保留 llama-bridge 的 `/v1/*`）；C 阶段必须一次性做完。
- 执行记录与 4 条「审计结论被实施修正」的细节见报告新增的 **§19**。

## 7. 待办
- **n=200 对照**（4×n=50 各模型合并）检验 agnes-3.0 vs 2.5 的 −8pp 是否真实。
- **区间口径按 3.0 重新校准**：T001/T031/T023/T050 的 `min_tool_calls`/`max_tool_calls`
  是按 2.5 基线定的，3.0 的工具调用节奏不同，部分 case 区间需调整。
- **T040 false_completion 单独立 case**：3.0 上该追问没追问，需补对抗用例。
- **T008 评测口径复查**：`BLOCKED_NEEDS_USER_INPUT` 的期望终态是否应从 `completed` 改为 `waiting_user`。
- **第二轮 A/B**：per-tool 预算 8→12 放宽或改为「仅对失败/低新颖度调用计数」。
- **Phase C/D**：见 §6，未开始；C 需先定 `/api/*` 的去留。
- **提交状态**：Phase A/B 全部改动**尚未提交**（工作区里 `runtime/*`、`main.py`、`code_exec.py`、
  `webapp.py`、`.env.example`、`tests/test_api_layer_phase_{a,b}.py`、新增 `runtime/cancel_scope.py`
  与报告 `FORGE-API-LAYER-AUDIT-2026-09-22.md` 均为 M/??）。更早的 P0~P2 改动已提交。
  `.env` 模型切换（agnes-3.0-flash）与运行产物不入库。
