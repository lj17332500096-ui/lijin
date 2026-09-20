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
- **run_id = trace_id**：事件/消息/审计全键。
- **清理路径**：`except (Exception, SystemExit)`，保留 `KeyboardInterrupt`。
- **同文件多处编辑**：串行 Edit 或整文件 Write，禁止并行（互相覆盖且都报成功）。
- **禁止 `cat >> 文件 << 'EOF'`**：Git Bash 会写到文件头部并覆盖。追加用 Write 或 Python。
- **脏工作区提交**：绝不用 `git add -A`。精确暂存 = `git ls-files --others --exclude-standard` + `git diff --name-only --diff-filter=M`，用 `--pathspec-from-file` 喂。
- **Git Bash `find` 是 Windows find.exe**，结果不可信。`git ls-files/status` 中文路径要 `-c core.quotePath=false`。
- **`git rm -r` 删多子目录会波及父目录**（退出码 0 ≠ 删对了）。删完核对 `git diff --name-only --diff-filter=D`。
- **同一事实的多种表示只能取其一，相加必错**（明细 vs 聚合、原始 vs 派生）。

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

## 6. 待办
- **n=200 对照**（4×n=50 各模型合并）检验 agnes-3.0 vs 2.5 的 −8pp 是否真实。
- **区间口径按 3.0 重新校准**：T001/T031/T023/T050 的 `min_tool_calls`/`max_tool_calls`
  是按 2.5 基线定的，3.0 的工具调用节奏不同，部分 case 区间需调整。
- **T040 false_completion 单独立 case**：3.0 上该追问没追问，需补对抗用例。
- **T008 评测口径复查**：`BLOCKED_NEEDS_USER_INPUT` 的期望终态是否应从 `completed` 改为 `waiting_user`。
- **第二轮 A/B**：per-tool 预算 8→12 放宽或改为「仅对失败/低新颖度调用计数」。
- **提交状态**：P0~P2 核心改动已提交（`3405cfe`）；区间口径校准已提交（`a4df1b9`）；
  `.env` 模型切换（agnes-3.0-flash）未入库（`.env` 不入库）；运行产物（`runs_*/`、`report_*.json`）不入库。
