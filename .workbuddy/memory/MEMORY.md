# 项目长期记忆 — my_creative_agent

> 2026-09-20 精简重写。只留跨会话可复用的结论；逐条排查过程见 `.workbuddy/memory/YYYY-MM-DD.md`。

## 0. 路线铁律（用户强制）
- **Laya 路线是主攻路线，永不放弃。** 任何诊断结果（包括负增益、坍缩、校准失败）都只能作为「当前 checkpoint/训练配置有问题」的证据，**不得**推导为「放弃 Laya 路线」。后续任何分析/建议中出现「放弃/停止/不再投入 Laya route 头」的措辞都是违规。

## 1. 架构与边界
- 单 Agent + 单主链 + 极厚外围治理（harness 型）。入口：`main.py`（`--classic`/`--voice`/`--daemon`/`--tui`）+ `cli/`。
- 前端 UI 层冻结（`webapp.py`/`llama_bridge.py`），仅 `FORGE_ENABLE_UI=1` 可起。
  **`webapp.py` 自 2026-09-22 起只是「前端宿主」**（190 行）：3 条页面重定向 + llama_bridge
  路由 + `/llama-ui` 静态挂载；原 63 条 `/api/*` 已归档。唯一对外协议 = llama_bridge 的
  `/v1/*` + `/props` + `/tools` + `/slots` + `/models/*`。
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
- **先改名后改内容的文件会提交旧快照**：`git mv` 后再 Edit，索引里仍是改名时的内容 → 提交态不自洽
  （实测 `laya/train_laya_forge.py` HEAD 里 LAYA_FORK 指向已不存在的路径）。
  **提交后必须 `git show HEAD:<文件>` 抽查**，只看工作区会漏。
- **`.gitignore` 不支持行内注释**：`archive/  # 说明` 整条失效（规则被当成含空格的文件名）。
  注释必须独占一行。实测本仓因此有一条忽略规则长期失效。
- **"既有失败"必须对比 HEAD 才能下结论**：只看工作区全量结果会误判。实测 10 条"既有失败"
  里 8 条是工作区改动自己引入的回归（改 provider/client 契约、改事件可见性、扩大取代范围等）。
  归因方法：逐个读 diff 判断改动是否触及该测试断言的契约，而不是看它"像不像老问题"。
- **改共享资源的创建方式前先看测试断言的意图**：为解决"系统代理不通"把共享池改成
  "每次新建 client"，会同时丢掉连接复用并撞翻断言"远程网关不持有 client"的测试。
  两全解是进程级共享池（本项目 `_shared_no_proxy_client()`），不是私有池。
- **事件可见性改动要按消费者区分，不能无条件转发**：`response.output_text.delta` 无条件
  转发会泄漏私有中间文本。做法是显式 opt-in 标记（本项目 `_public_stream`，仅 TUI 通道）。
- **"允许取代旧 Run"的范围不能扩大到执行中状态**：waiting_user/waiting_approval 可取代，
  submitted/running/paused 取代 = 静默取消用户正在跑的任务，且并发保护失效。
- **pre-commit 钩子**：判据与测试选择集中在 `scripts/ci_gate.py::CHANGE_TEST_MAP`，
  已知失败登记在 `.ci/known_failures.txt`（**必须写原因**，否则脚本拒绝加载）。
  绕过红灯的正确做法是把无关改动摘出分批提交；**不用 `--no-verify`**。
  分批提交脚本被中断会留下残存暂存区 → 下一批提交会张冠李戴，提交后要核对
  `git show --stat` 与消息是否匹配。
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
- **Phase C 前置决策已执行：`/api/*` 归档**（2026-09-22）。63 条路由 + 89 个专属定义整体删除，
  `webapp.py` 2042→190 行。恢复点 **tag `archive/api-routes-pre-delete-20260922`**（→ `a2cb623`，
  该点已含 Phase A/B 全部修复）。全量 1383 passed / 6 skipped / 0 failed，与归档前逐项一致（零回归）。
  归档依据（可复核）：bundle 中 `"/api/"`、`assistant_delta`、`run_id`、`EventSource` 命中**均为 0**；
  全仓无 HTTP 消费者；唯一测试消费者 `tests/test_webapp.py` 处于冻结跳过；`webapp.py` 默认拒绝启动。
  连带清掉 3 个既有死代码（`_friendly_error`/`_LIVE_LOCK`/`_store_upload_bytes`）与 4 处记录层漂移。
- **Phase C 归档后收缩为 C3 + C6**（对报告 §17 的实质修正）：C1（校验前置）/C4（字段重命名）
  /C5（SSE `id:`）的作用面已随 `/api/*` 消失；C2 早已是 `pytest.skip` 空操作；C7（`/api/v1` 前缀）
  与 C8（`ToolResult`）**会打断冻结前端**——llama-ui 的 fetch 多为相对路径，且按
  `error`/`plain_text_response` 解析。
- **Phase C 完成（C3 + C6，2026-09-22）**，验收 `tests/test_api_layer_phase_c.py`（25 条）。
  两项都在 `llama_bridge.py` = 冻结层内，属有意开出的**接口层例外**（只动错误结构与请求守卫，
  不动 UI 功能与前端契约）。
  **C6 = 反 CSRF 而非格式校验**：写端点（`/tools`、`/v1/chat/completions`、`.../control`）必须带
  `Content-Type: application/json` 否则 415。跨站 `fetch` POST 默认 `text/plain`，属「简单请求」
  不触发预检、浏览器直接投递，而 `await request.json()` **并不校验类型** → 任意网页都能借用户
  浏览器触发工具执行。只读端点与无 body 的 GET/DELETE **不加**（前端走 `A0()` 本就不带该头，
  加了会静默打断 `/v1/stream` 续读）。另 `webapp.main()` 加**绑定断言**（非回环监听需
  `FORGE_ALLOW_NONLOCAL_UI=1`）。**不选「逐请求回环断言」的原因**：Starlette `TestClient` 下
  `request.client.host == "testclient"`（实测，非回环），逐请求断言会打死全部 UI 测试。
  **C3 = 非 2xx 统一** `{"error":{message,type,code,retryable},"request_id"}`，形状由前端取错函数
  `kIe()` 的首选分支 `r?.error?.message` **反推**确定（不是自定的）。**200 路径刻意不上信封**：
  工具结果仍走 `plain_text_response`，且 200 里的 `error` 必须是**字符串**（前端做
  `String(a.error)`，传对象会渲染成 `[object Object]`）。
  顺带修掉 `/tools` 审批**死分支**（原只回 `approval_required`，前端命中数为 0，用户看到原始
  JSON；现补一条字符串 `error` 使「被阻断」有可读反馈）。
  全量 **1408 passed / 6 skipped / 0 failed**（对归档基线 1383 恰好 +25 = 本文件用例数 → 零回归）。
- **Phase D（D1–D5）未开始。**
- 执行记录、归档依据与「审计结论被实施修正」的细节见报告 **§19**。

## 7. Laya 路线当前状态（2026-09-25）
- **GPU（8099 SYCL）关键词基线 = 74.2%**（`eval_route_head_gpu.py`，`/embedding` + 触发词双信号，19 秒 / 500 条）
- **CPU 微调前 = 30%**（`eng_baseline_fallback.json`，真实二分类口径）
- **CPU 微调 1 epoch = 15.43%**（坍缩）
- **XPU 训练栈已配好**：`torch==2.14.0+xpu`（pip 直装，不需要 conda/oneAPI），Arc A770 可用
- **XPU 5-epoch 训练进行中**（task GLpns0）：4000 条，输出到 `forge_finetuned_5000_xpu/`，预计 30~60min
- **`eval_finetuned_5000.py` 已写好**：checkpoint 出来后跑三方对比（GPU 74.2% / CPU 30% / XPU 微调后 ?）
- **训练脚本瓶颈已解**：之前 CPU 逐 item 前向+反向太慢（17~28h），XPU 用 Arc A770 GPU 加速 15~30 倍
- **XPU 安装踩坑**：`pip install --force-reinstall "torch==2.14.0+xpu" --index-url https://download.pytorch.org/whl/xpu`（不需要 conda/oneAPI，wheel 内嵌 SYCL 运行时）

## 8. 待办
- **n=200 对照**（4×n=50 各模型合并）检验 agnes-3.0 vs 2.5 的 −8pp 是否真实。
- **区间口径按 3.0 重新校准**：T001/T031/T023/T050 的 `min_tool_calls`/`max_tool_calls`
  是按 2.5 基线定的，3.0 的工具调用节奏不同，部分 case 区间需调整。
- **T040 false_completion 单独立 case**：3.0 上该追问没追问，需补对抗用例。
- **T008 评测口径复查**：`BLOCKED_NEEDS_USER_INPUT` 的期望终态是否应从 `completed` 改为 `waiting_user`。
- **第二轮 A/B**：per-tool 预算 8→12 放宽或改为「仅对失败/低新颖度调用计数」。
- **Phase C**：已完成（C3 + C6），见 §6。C6 的可行性实测依据：前端
  `Lb(){return{[ds.CONTENT_TYPE]:sv.JSON,...A0()}}` → 所有带 body 的请求必带
  `Content-Type: application/json`，故守卫不会打断前端。
- **同类残留（C 阶段新发现，未修）**：`llama_bridge.api_chat_completions` 的**非流式**路径在
  `choices[0].message` 上写 `approval_required=True`，而 bundle 中该键命中数为 **0**
  ——与 `/tools` 审批死分支同族（**分支声称处理了某场景，对方从未实现**）。
  该字段是**附加且被忽略**的（正文本身已在 `content` 里），不造成可见缺陷；
  将来清理需注意：改 OpenAI 响应体的风险**高于**改桥层自有字段。
- **Phase D**：见 §6，未开始。
- **提交状态**：Phase A/B + 审计报告 + 归档 + Phase C **均已入库**
  （`a902658` → `a2cb623` → tag `archive/api-routes-pre-delete-20260922` → `352f321` → `f59e417` → `9b41ab1`）。
  工作区残留**有意不提交**：`benchmark_fixture/_inline_*.py`×4（运行 churn）、
  `code_sandbox/{auth_fix,fix_auth_calculate}/*`（沙箱产物）、
  `delivery/EVAL-P0-12CASE-2026-09-21.md`（上次会话交付物，不混入本次提交）。
  `.env` 模型切换（agnes-3.0-flash）与运行产物不入库。
- **可选清理（需用户决策，勿擅自回改历史）**：`benchmark_fixture/_inline_*.py` 已有 51 个同类入库
  （时间戳随机名、每个 <1KB、benchmark harness 的运行残留），属 churn，建议 gitignore。

## Laya 接入层索引约定护栏（2026-09-25）
- `laya/agent.py::predict` 把 argmax 的 idx 按 `crit` 字典序反查选项文本：`choice = keys[p.argmax()]`。
- 训练侧 `build_training_items` 的 target 也严格按 `crit` 序逐位对齐（修复后 2000/2000）。
- 两侧用同一套 `render_options`+`crit` 序，**方向天然一致**，接入层 `laya_router.py` 读 `answers.needs_tool.choice`（文本）无需改。
- **铁律**：任何按 `render_options` 喂 target 的代码，索引必须动态解析渲染序，禁止硬编码 `[text, tool]`（曾因数据 crit 是 `{tool,text}` 序，硬编码 [text,tool] 导致 5 epoch 全押 tool、评测 0.2% 假象）。
- `_fixed` 目录需拷 `rl_agent_config.json`（从 english）才能被 `Agent(dir)` 按目录加载。
