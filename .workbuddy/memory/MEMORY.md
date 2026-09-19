# 项目长期记忆 — my_creative_agent

> 2026-09-19 精简重写（原文件 16KB 超注入上限被截断）。只留跨会话可复用的结论；
> 逐条排查过程见 `.workbuddy/memory/YYYY-MM-DD.md`。

## 1. 阶段与边界
- **只优化后端，前端 UI 层冻结**：`webapp.py`/`llama_bridge.py`/`web/llama-ui`/`web/shared` 保留不删，
  默认拒绝启动（exit 2），仅 `FORGE_ENABLE_UI=1` / `--enable-frozen-ui` 可起；单一开关 `ui_frozen.py`。
- 入口：`main.py`（`--classic` 裸 REPL；`--voice`/`--daemon`/`--tasks`；**`--tui` 走 Textual 界面**）+ `cli/`。
- **边界红线**（`tests/test_ui_isolation.py` 强制）：除两个冻结文件自身，任何后端/CLI 模块不得
  `import webapp|llama_bridge`；导入 `cli.*` 不得把 UI 模块拉进 `sys.modules`。
- 过程输出归属：后端不自己 print，调用方用 `main.set_process_prints(False)` 关闭 `[调用工具]`/`[流事件]`。

## 2. 分层速查
- `cli/app.py` 主循环 `ChatApp.submit` → `AgentRuntime.run_turn(stream, stream_events_cb=renderer.on_event)`；
  `render.py` 过程渲染｜`commands.py` 斜杠｜`store.py` 会话视图｜`diagnostics.py`（`E-FINAL-EMPTY`/`E-BUSY`/`E-PROVIDER`）｜`theme.py`。
- 事件口径 `cb(channel, payload)`：`activity`（`metadata.activities`，按 activity_id 去重）/ `assistant_delta`
  （`metadata.delta`）/ `control`（`run.*`、`assistant.reply`）/ `tool`。`session_name` = LLM 上下文键 = 容器 session_id。
- `runtime/`：`run_turn` = Run 生命周期包 `execute_turn`（内层 SDK Agent Loop）；`state_machine`（唯一改 state）、
  `task_manager`（agent.db WAL）、`broker`（唯一工具入口 + Policy/审批）、`approval`（原子 `WHERE pending`）、
  `completion`（声明↔执行证据共决终态）、`filescope`（fail-closed）、`context`、`tool_router`（37→8~16）、
  `router`、`budget`+`token_gate`（双闸）、`audit`、`artifacts`（sha256）、`trust`、`snapshot`。
- 工具层：**实测 37 个 native 工具**（技能 0 启用 / MCP 0 挂载）。
  ⚠️ 旧记的"41 工具"是 `tool_router.TOOL_TERMS` 的**条目数**（含 5 个指向不存在工具的死引用），
  不是工具数 —— 见 `TOOL_INVENTORY_2026-09-19.md`。
  文件工具是**三层作用域隔离**（只读工作区 / 仅 code_sandbox 可写 / 真实项目可写+自动备份），
  **故意分开，禁止合并**。`_EXPLICIT_ONLY_TOOLS` 里 `code_loop_tool` 是错名（应为 `code_loop`）。
  对账探针：`delivery/probe_tool_inventory.py` + `probe_tool_redundancy.py`
  + `probe_code_loop_gate.py` + `probe_sandbox_smoke.py`；
  护栏测试：`tests/test_tool_router_consistency.py`（路由表 ⊆ 注册表 ∪ 技能工具）。
- ⚠️ **沙箱真身在 `WORKSPACE_ROOT/code_sandbox`，不在项目目录内**
  （`code_exec.SANDBOX_ROOT = WORKSPACE_ROOT / "code_sandbox"`；实测有 24 个真实项目）。
  项目内那个同名 `code_sandbox/` 是 `tools_phase1_case2_4.py:141` 硬编码
  `BASE/"code_sandbox"` 的残留 —— **统计沙箱使用情况时会看错地方**，别踩。
- ✅ **已定论（2026-09-19 晚，真实写入实测）**：111 条"原文件已备份到 `logs/backups/`"里
  **没有一条撒谎的备份能力，全是文案在撒谎**。
  - 覆盖已有文件 → `logs/backups/<stamp>_<uuid>_<name>` 真的落盘，内容一致 ✓
  - 新建文件 → 目录根本不出现，而回执**照样**声称已备份 ✗
  - 根因：`_backup()` 对不存在的文件直接 `return`，而回执文案是无条件拼接的。
    所以"目录从不存在"只说明**历史上所有写都是新建**，不说明备份坏了。
  - 已修：`_backup()` 返回真实路径或 `None`，回执写明具体备份文件名 / 或明说"无原文件可备份"。
  - **教训**：回执文案必须由真实结果生成。否则事后无法从记录区分"功能坏了"和"记录在撒谎"，
    这个疑点因此挂了一天。测试 `tests/test_project_edit.py` 已锁 3 条。
- `search_sources` 的库当前为空（`source_chunks`/向量/FTS 均 0 行，`project_sources` 仅 1 条
  `parse_status=ok` 却无切片）→ 已从 `_WEB_SUPPORT` 移出（否则纯联网题白捎带，实测 11/50 题）。
  `search_documents` 有 2MB 索引且被调用 355 次，**必须留在族里**（已加对照组测试）。
- ⚠️ **门槛表语义易记反**：`_EXPLICIT_ONLY_TOOLS` 是**限制**表 —— 在表内要命中语义才放行，
  **不在表内直接 `return True`（永远放行）**。所以表里写错名的后果是"保护静默失效、
  工具被无条件放行"，不是"派发不出去"。判断加严/放宽必须读 `_allowed()` 的返回语义。
- ⚠️ **测试隔离**：`router_enabled()` 每次读 `os.environ`（无缓存），`TOOL_ROUTER=off` 时
  `select_tool_names` 与 query 无关、直接返回全量。**新测试文件必须显式 `setUp` 里设
  `TOOL_ROUTER=on`**，否则单独跑绿、全量跑被别的测试污染成红（本轮真实踩过）。
- 插件层：`skills/` 可插拔，**白名单默认全关**（`.env` **没有** `SKILLS` 行；`skills_loader.py:7`「不填=不启用」）。
  27 个技能里**仅 3 个自带 `tools.py`**（`gorden-ppt` 4 工具＝第三方 PPT 技能 v1.0.17 非商用 /
  `dep_doctor` 1 / `skill-assets` 1），其余 **24 个纯提示词**（只注入 `skill.md`，不注册工具）。
  另有 `mcp_bridge.py`（`<server>_<tool>` 挂载，**默认 deny**；`MCP_SERVERS` 配了 gitee 但未挂上）
  与 `resilience.py`（full-jitter 重试 / `tool_slot` 限流 / `idempotency_key`）。
- **技能有两个目录，别混**：`skills/` = **运行时**（`skills_loader` 只读这里，27 个，属「合法待启用」，
  `TOOL_INVENTORY` §33-39 已定性）｜`agent-skills/` = **编译产物 / 安装源**（2026-09-19 由 19 删到 10）。
  链路：`tools/skill_compiler.py`（需手动给 `--src` zip）→ `agent-skills/` →
  `tools/install_compiled_skills.py` → `skills/`。技能健康度探针 `delivery/probe_skill_health.py`（可传目录）。
  2026-09-19 删除判据：这 9 个**从未被安装进 `skills/`**（3 个 Claude 平台专属 + 6 个与内置原生工具重复/依赖缺失），
  存档 `delivery/removed_skills_manifest.txt`。
  ⚠️ gorden 族**有意**不走高门槛（靠 `_GORDEN_PPT_INTENT` 意图族补齐，防 "Tool not found"
  真实故障）→ 代价：纯文字「总结」请求也带 4 个 PPT 工具（4→8，**已接受**）。**别把它们
  收编进 `_EXPLICIT_ONLY_TOOLS`**。
- ⚠️ **「两张表脱节」有三种成因，判据是"看当前行为有没有被依赖"，不是看表**（同日连踩三次）：
  ① **写了没接线**（本意就是约束，名字写错/漏登记）→ **补**。例：`_EXPLICIT_ONLY_TOOLS` 的
  `code_loop_tool` 错名、`evaluator.RUN_TOOLS` 漏 `run_tests`。
  ② **旧设计没清理**（表在"假装"一件已经不做的事）→ **删**。例：`_HIGH_GATE_TERMS` 的 4 条 gorden 词条。
  ③ **前置分支覆盖**（表里没有，但代码在前面显式处理了）→ **不动**。例：`fetch_github_repo` 不在
  filescope 三表内，但 `authorize_tool()` 第一段就带理由地 DENY。
  **唯一可靠的判定手段是"改一下，看谁会红"**（既有测试 / 探针 / 全盘 grep），
  以及"有没有权威定义"（如 `completion.py:VERIFY_TOOLS` 就是运行/验证类名册的权威）。
  ⚠️ 反向错误也真实存在：`_HIGH_GATE_TERMS` 里"写了词条却没进生效集"会让词条永不求值（死数据）。
- ⚠️ **护栏的作用域本身就是漏洞**：`tests/test_tool_router_consistency.py` 只守
  `runtime/tool_router.py`，结果同一错名 `code_loop_tool` 在 **8 个生产文件**里存活
  （`benchmark/`×7 + `readiness_gate`）。已加 `tests/test_tool_roster_consistency.py`（8 条，
  AST 静态扫描全项目模块级名册，**不 import**）。新增护栏时先问"它守得住哪些文件、守不住哪些"。
  ⚠️ 用"变量名形状"筛选名册会误报（`_NON_READY_INDEX` 因含 "READY" 被当成 READ 表）——
  必须**同时**要求"至少一个条目落在工具词表里"，且把"整表失效会漏掉"写进 docstring。
- ⚠️ **benchmark 名册漂移有真实度量后果**：`evaluator.allows_tool()` 是「执行了不在
  `tools_allowed` 里的工具 → hard_fail」，漏 `run_tests` 使**每个评测臂的通过率被压低 4~6pp**。
  修正后三臂 = **A 43.0% / B 42.0% / C 43.0%**（原 39/36/38），Safety 1/3/0 不变；
  B−A 由 −3.0 → **−1.0pp**、C−A 由 −1.0 → **0.0pp** → **"记忆层无收益"的判定更强**。
  历史 run 的原始观测未变，**重评分即可、无需重跑 Agent**（`delivery/probe_benchmark_roster_audit.py`）。
- 数据：`agent.db`（tasks/runs/messages/events/approvals/tool_calls/model_calls/memories/schedules/artifacts/episodes）
  + `sessions.sqlite` + 文件产出（`notes/`、`exports/`）。

## 3. 工程铁律（血泪）
- **run_id = trace_id**：事件/消息/审计全键，`trace_export.py` 无需改表。
- **清理路径绝不能抛异常**：`SystemExit` 继承 `BaseException`，`except Exception` 拦不住 →
  用 `except (Exception, SystemExit)`，并保留 `KeyboardInterrupt` 传播。
- ⚠️ **同一文件的多处编辑不要并行下发**（实测并行 Edit 互相覆盖且都报成功）：同文件要么串行 Edit，
  要么整文件一次 Write；跨文件并行安全。
- ⚠️ **禁止 `cat >> 文件 << 'EOF'` 追加代码到已有 .py**（Git Bash 实测会写到**文件头部**并覆盖开头几行 +
  混入非法字节，整文件 utf-8 解码失败）。追加一律用 Write 工具或 Python `io.open(...,'a',encoding='utf-8')`。
- 交付物行尾/编码跟原文件保持一致（外部 skill 的 `scripts/main.py` 是 CRLF），否则 diff 全文件重写。
- Bash 传 Python 时**反引号会被 shell 当命令替换执行**；含 backtick 的长文本一律走 Write 落盘。
- `git status` 看不到 untracked 用 `--untracked-files=all`；**Git Bash 的 `find` 是 Windows find.exe**，结果不可信。
- ⚠️ **脏工作区提交：绝不用 `git add -A`**（会把工作区删除一起暂存）。精确暂存 =
  `git ls-files --others --exclude-standard`（新增）+ `git diff --name-only --diff-filter=M`（修改），
  分批用 `git add --pathspec-from-file=- --pathspec-file-nul` 喂进去，
  校验 `git diff --cached --diff-filter=D` 必须为空。回滚用 `git restore --staged --pathspec-from-file=-`。
- ⚠️ **Git Bash 下 `awk`/`sed` 管道输出会被吞**（同一命令走 Python `subprocess` 正常）→ 分析类命令一律 Python。
- ⚠️ `git ls-files/status` 默认给中文路径加引号 → 必须 `-c core.quotePath=false`，否则按 `/` 切分统计全错。
- `sessions.sqlite-shm/-wal` 不被 `*.sqlite` 匹配、会冒充未跟踪；属易失状态，**不要入库**。
- ⚠️ **`git rm -r` 删多个子目录会波及父目录**（2026-09-19 实测）：删 9 个技能目录后，父目录 `agent-skills/`
  被整体从工作区抹掉（连本该保留的 10 个目录 / 70 个文件一起），**而退出码是 0**。删完必须核对
  `git diff --name-only --diff-filter=D | wc -l` 的**未暂存**部分，非 0 就 `git checkout -- <父目录>` 复原。
  → **退出码 0 ≠ 删对了**；不可只看退出码。
- 单行回答只在终帧一次性发出（`FinalContentStream._publish` 按完整行 trim，防半截 JSON）
  → 单行回答没有打字机效果，属预期设计。

## 4. 情节记忆层（episodic）
- `runtime/episode.py`（分词/意图/指纹/相似度，纯函数）/ `episode_store.py`（DAO）/ `episode_recall.py`（渲染 + trust）；
  测试 `tests/test_episode_memory.py`。表：`episodes`（447 条种子）+ `episode_ingest_cursor`。
- 零侵入：不碰 `runner.py`，从 `task_events.run.terminal` **后置采集**（`ingest_pending()` 幂等，可回填）。
- episode **故意不存用户请求原文**（只存 digest + topic tokens）——安全底线；副作用：规则改动后无法离线重算，
  必须 `store.reindex()` 从原始事件重建。
- 踩过的坑：`needs_user_input`/`needs_approval` **不是失败**（走 `normalize_outcome()`）；中文 2-gram 虚字只按
  **首字**判定；路径先 `strip_paths` 否则同项目 episode 全部假相似；intent 桶**不能硬过滤**候选（会 0 召回）；
  英文术语是强信号，需与中文 gram 分档加权。
- 运维：CLI `/episode stats|ingest|reindex|recall <文本>|on|off|clear yes`（别名 `/mem`）。
- 三臂 n=100 原始数值（**仅供索引，推论已作废**）：A 39.0% / B 36.0% / C 38.0%；B safety 3、C safety 0。
- **⚠️ 2026-09-19 晚追查：上述两臂的推论全部作废，别引用。**
  - **A vs B（push）无效**：注入块以「路径：a → b → c」列出历史工具名，撞中 router 规则 1
    「查询里出现工具名 → 必选」→ 实测**46/46 题工具集都不同**（平均 +9 个，最多 T042 0→16），
    并因 `max_tools=16` 把 `index_workspace`(16题)/`search_documents`(11题)**挤出去**。
    两组跑的不是同一套工具 → −3.0pp **不能归因于记忆层**。
  - **C 组零调用 ≠ 模型不想用**：`BASE_TOOLS` 只有 4 个、`recall_memory` 属 `_MEMORY_NOTE_TOOLS`
    「默认不常驻」，`_memory_tool_allowed()` 要求 query 命中 `_RECALL_INTENT`
    （"我之前/回忆/我的记忆/recall…"）→ 实测 **50 题里可见 0 题**，工具从未派发。
    → pull **从未被检验**。
  - 仍成立的只有：**pull 的工具在默认配置下不可见**。
- **⚠️ 方法论级铁律（比记忆层本身值钱）**：
  **凡"用 prompt 前缀拼接模拟注入"的实验，必须先证明注入没改变工具可用集。**
  开跑前跑 `delivery/probe_injection_confound.py` 确认两组工具集一致。
  - ❌ 注入块**文本脱敏无效**（实测 0/46 恢复）：中文别名又命中关键词分组
    （"运行测试"含"测试"→coding 族、"联网检索"含"检索"→web 族）。**语义层绕不过去，混淆是结构性的。**
  - ✅ 唯一有效：**固定工具集**（`TOOL_ROUTER=off` → 46/46 一致，结构性保证）。
  - 三条修法排序（详见报告 §10.6）：① 评测固定工具集（已验证）② `run_turn` 加 instructions 形参、
    注入走 system 层不进 router 输入 ③ `select_tool_names(query, context="")` 隔离（顺带堵注入面）。
  - 不要做：把 `recall_memory` 加进 `BASE_TOOLS`（与 `test_convergence_hardening.py:119`
    「北京天气不得暴露 memory 工具」的既有设计冲突，且重新引入空转）。
- 探针（保留复用）：`probe_recall_visibility.py` / `probe_recall_gate.py` /
  `probe_injection_confound.py` / `probe_sanitize_fix.py`。
- 保留价值：CLI `/episode` 作离线复盘好用；三层实现 + 47 条单测正确，只是"喂给模型"不划算。
  除非出现高信噪比的新信号（如"同类任务连续失败 N 次后定向注入教训"），否则不要再调阈值/条数/位置。
- ⚠️ 留一法粒度必须**逐 case**：排除整个评测集会把 447 砍到 112 → 召回 0、pull 空转。
- **未接入主链**（`EPISODE_RECALL` 默认开但不拼 prompt）；评测注入只能做 prompt 前缀近似
  （`run_turn` 无 instructions 形参，真实接主链应走 system/developer 层）。

## 5. 评测方法论硬约束（本项目所有 A/B 通用）
- 每臂 n=50 只能检出 ≈±28pp，n=100 ≈±20pp，检 10pp 需 n≈400 → **50 case 一轮只能发现大问题**；
  任何「提升 X%」结论必须同时给出样本量与置信区间。
- 绝对值判据（「必须为 0」）在随机 harness 上不可满足 → 改写成「不劣于基线」的统计检验。
- 跑间不确定性实测（留一法天然对照 n=12）：结果层翻转 0%，但**工具序列发散 67%、终稿文本一致仅 8%**
  → 单次观察不足为凭；看到差异先跑 `eval_episode_ab_attrib.py`。
- 已否证：注入块「高频工具」字段**没有**造成工具锚定，别再讲这个故事。
- 站得住的副作用：注入使 `recall_memory` 调用 1→13（元认知唤醒），而它查的 `memories` 表 0 行 = 空转；
  调用过它的 case 变差率 37.5% vs 未调用 6.7%（Fisher p=0.053，事后子组，不可当因果）。
- 脚本：`benchmark/eval_runner.py --inject-episode`、`delivery/eval_episode_ab.py`。
  一轮 50 case ≈25~30min；`runs_eval/` 是历史产物，不能直接当基线。

## 6. 已作废的旧结论（别按它改代码）
- ⚠️ `tests/test_approval_execution_parity.py`「7 条失败」**已作废**（三种跑法全绿）
  → **不要改 `runtime/runner.py:1133` 的 rid 传递**，那是按假信号改真代码。
- 外部技能（agent-orchestration-pro 三 skill）10 条缺陷已修复，以补丁包交付：
  `delivery/patches/{orig,fixed}` + `apply_skill_fixes.py`（`--check`/`--restore`）、`verify_skill_fixes.py`（1/15→15/15）。
  它们位于市场插件缓存会被升级覆盖。现在直接走各技能自己的 `run.py`。
  ⚠️ 其自检曾写坏真实配置 `memory.json`/`hooks.json`/`rules.json`（后者 `forbidden` 是真拦截规则），已清理，勿手工重建。

## 7. TUI（`cli/tui/`，2026-09-19 入库）
- 结构：`ForgeTuiApp → TuiPanels → StatusHeader / MessageLog / InputBar(SlashPopup + Input + Footer)`；
  `InspectorScreen`（Ctrl+O）。启动 `main.py --tui`。
- 快捷键：Tab 补全（`priority=True`，否则 `Key('tab').name_aliases` 含 `ctrl_i` 会被 App binding 抢）、
  Ctrl+Q 退出、Ctrl+L 清屏、Ctrl+O Inspector、Esc 关 popup / 中断。
- ⚠️ **Textual 8.x 布局陷阱**（三条真实踩过）：
  1. `containers.Vertical` 的 DEFAULT_CSS 是 `height: 1fr` → 子容器不显式写 `height: auto` 会与兄弟抢半屏；
  2. `width/height` 是**边框盒**尺寸 → `height: 1` + `border: solid`（上下各 1 行）= 内容 0 行，
     `Input` 的 placeholder 完全不渲染，需 `height: 3`；
  3. `SlashPopup` 用 `display: none` 做默认隐藏（`show()` 用 inline style 覆盖）；`Footer` 默认 `dock: bottom`，
     在 auto 容器里 margin 不参与宽度计算会溢出屏幕，改 `dock: none` 走流式布局。
  另：`dispatch_key()` 调 `key_<event.key>` 而非 `on_key`；`border: solid` 合法，`rounded`/`border: solid #hex` 非法。
- **排查手法**：`app.screen._compositor.render_strips()` 把 headless 界面还原成字符网格（`strip.text`），
  配合 `widget.region` / `content_size`，无需真实终端即可拿到精确布局证据（比读 SVG 截图可靠）。
- hygiene：Textual 路径的 `except Exception` 已精确化为 `NoScreen`/`NoActiveAppError`/`MountError`；
  运行时逻辑（`chat.dispatch`/`run_turn`/`_resolve_approvals`）保留 `Exception` 渲染给用户。
- 测试 `tests/test_tui.py` 47 条（含 7 条布局回归：MessageLog 吃满高度 / 三段高度和 == 屏幕行数 /
  Input 内容行 ≥1 / popup 默认隐藏 / popup 展开收起还原 / Footer 不溢出 / 80x24 窄屏）。
- ✅ **已修（2026-09-19）**：`_update_footer()` 曾因 `self._hint_label` 从未创建而全程 no-op
  （被调 14 次什么都不显示）。现 `InputBar.hint`（`.forge-hint`，height:1，位于输入框与
  Footer 之间）+ `on_mount` 接线 + `_footer_mode` 记录，四态 idle/running/approval/slash
  均真实渲染。测试 `tests/test_tui.py` 47 → 51 条（含接线/渲染/切换/高度预算 4 条）。

## 8. 验证与待办
- 全量 pytest：2026-09-19 **1189 passed / 6 skipped / 0 failed**（94s，**沙箱外**）；
  收集数会在 1133~1191 间随新增测试漂移。
- 🔴 **血泪：`pytest tests/` 的通过与否取决于「跑在不在沙箱里」**。实测同一天同一套代码：
  - **沙箱内** → **11 个确定性失败**（同样 11 个，连跑两次完全一致）：
    `test_approval::test_code_loop_cannot_bypass_approval`、`test_cleanup_resilience`×3、
    `test_codex_loop`×4、`test_concurrency_stress::test_mixed_parallel_runs_...`、
    `test_project_model::test_04_05_...`、`test_skill_gorden_ppt::test_build_strict_...`
  - **沙箱外** → **0 failed**，且这 11 个**单独跑也都通过**。
  - 判据：**"全量失败 + 单独通过 + 跨文件共现" ⇒ 先怀疑沙箱，不要改代码**。
    确认手法：`pytest tests/ -x --tb=short -q`（在沙箱外跑，`-x` 能让汇总正常打印；
    不加 `-x` 时进程会在收尾清理临时目录时被「批量删除护栏」杀掉，**断言段根本不写出**，
    于是你只能看到进度条上的一串 F 而拿不到任何 traceback）。
  - ⚠️ 旧记录写的"失败集合每次漂移"**不准确**：实测是**沙箱内确定性失败同一批**。
    别拿"漂移"当借口跳过归因。
- 唯一已知 warning：`tests/test_markdown_js.py:171` 的 `DeprecationWarning: invalid escape sequence '\s'`
  （docstring 里的正则示例，历史遗留，不影响结果）。
- **router-on 50-case 基线（2026-09-19，首个真实配置数字）**：`runs_eval_baseline_router_on/`
  + `report_baseline_router_on.json`，21m13s。**Behavior Pass 50.0%**（25/24/0/1 not_evaluable），
  E2E 58.0%，**Safety 6 项全 0（50/50 verified_safe）**。
  ⚠️ n=50 只可检出 ≈±28pp，**这不是"提升/退步"结论**。
  分析探针 `delivery/probe_baseline_router_on.py`（失败归因 + 调用数分布 + 死循环画像）。
  **失败主因不是判据口径**（24 fail 里只有 2 个涉及工具名单，且都是真实越权 T038/T044）
  而是**尾部过度探索**：中位 10 次调用、均值 15、**最大 70**，23/50 超上限。
  死循环集中在 `list_workspace_files`（19/15/12 次，**无任何重复护栏**）与
  `web_search`（17/16 次，有 `_too_repetitive` 却疑似因措辞微变未拦住）。
  → ~~下一轮最高杠杆的改动是给查询类工具加"同工具同参数重复"抑制~~
  **⚠️ 该结论已被 2026-09-20 实测推翻**：用评测库真实 `arguments_json` 重放生产
  `repeat_target_key`，全 374 次调用里**同工具同目标同参数最多只出现 3 次**（cap=3 的第 4 次才拦）
  → "同参数重复"根本不存在，这个修法修的是不存在的问题。真实形态是
  **空参数**（`T038` 传 `{}` → `args.get("directory")` 空 → `repeat_target_key` 返回 `None`
  → 连计数都不进）与**参数漂移**（`T014` 15 次 list，`directory` 一路换写法 → key 每次都不同）。
  → 正解是「**工具级**调用预算 + 低新颖度」（不依赖参数），外加"默认目标规范化"。
  详见 `REPAIR_PLAN_2026-09-20.md` 与 `delivery/probe_repeat_guard_db.py`。
  （口径不变：T017 因 provider_error 不进分母、T020 直接 pass，
  **改这两条判据不会提升这一轮的数字** → 已拍板不动。）
- **⚠️ 勘误（2026-09-20 当天自我推翻）**：上一版记的"重复抑制四层防线层层有洞、护栏 0 触发"
  **是错的**。真相：护栏**一直在拦** —— 审计表 `tool_calls` 有 **43 条真实拦截**
  （权限审批 13 / `web_search` 5 次上限 12 / 收敛 10 / 单 Run 20 次上限 5 / TERMINALIZE 3）。
  基线 `tool_truth.blocked=0` 也是真的，但它数的是 `task_events.tool.invocation`
  —— **被拦的调用不产生该事件**（根本没进入执行）。两条记录链各自正确，结论却相反。
  ⚠️ **铁律**：核对"某个机制有没有生效"必须用**两条独立记录链**交叉验证，
  并先确认**每条链收录的是什么集合**；单链 `count=0` 可能是"没发生"，
  也可能是"发生了但这条链不收录"。探针 `delivery/probe_audit_gap.py`。
- **重复抑制的真实缺口（2026-09-20 已修）**：
  ① **拦得晚**：拦截主要在 20 次总上限与 `web_search` 5 次上限之后才落下，
     而 `list_workspace_files` 当时**没有任何独立上限**（T038 单 run 35 次里它独占 19 次）
     → 已把 web_search 特例泛化为 **per-tool 预算**
     （`runtime/runctx.py::per_tool_budget()`，可用 `TOOL_BUDGET_PER_TOOL` 覆盖）。
  ② **空参数/参数漂移对目标解析隐形**：`list_workspace_files(directory=".")` 传 `{}` → key 为 `None`
     → 连计数都不进（T038）；T014 的 `directory` 一路漂移 → key 每次不同。
     → 已加**默认目标回落**（`_DEFAULT_TARGET_IDENTITY`），并补上遗漏的 **`project` 参数名**
     （`list_code_files` / `read_code_file` 此前恒为 `None`）；三份互不一致的目标解析
     收敛为 `resolve_target` / `logical_target` **单一实现**。
  ③ 收敛硬闸 `blocked_repeat_count >= terminalize_after + 1` **已改为 `>=`**（T014 曾差 1 次不终止）。
  ④ Guard A（`FORGE_REDUNDANT_GUARD`）仍为 **off**；但 `.env` 现已**显式声明全部护栏开关**
     （`FORGE_REPEAT_GUARD=on` / `REDUNDANT_GUARD=off` / `COMPLETION_READY=off` + 三项预算），
     并由 `tests/test_tool_roster_consistency.py::GuardLeverConsistencyTests` 守住
     "同一件事有了两份默认值"这一新风险（含负向验证）。
- **审计缺口（2026-09-20 查清，与初判不同）**：50 case 里 **10 个本就无工具调用**（纯回答/提问，非缺口），
  **7 个**有执行但审计零记录（多为 `waiting_user`）。
  **最大断点 = 100/374（27%）的执行调用连 `invocation_id` 都没有** → 无法关联审计/审批/artifact。
  ⚠️ `runner.py:2207` 兜底回填把参数写成 `{"ledger": "<args字符串>"}`，**读库须再解一层**。
- ⚠️⚠️ **评测器双倍计数 bug（2026-09-20 已修，基线 +18pp）**：
  `benchmark/evaluator.py::observation_from_raw` 同时累加 `tool_calls` 明细与 `_exec` 聚合计数表
  —— **二者是同一批调用的两种表示**，相加使每个调用被计两次
  （实测全 50 case 精确 **2.00 倍**，总和 748 vs 374；T040 = 24 vs 12）。
  → `tool_count` 判据 `[1,12]` 被污染成事实上的 `[1,6]`，"实际只调 7 次"被判超限失败。
  修复后**同一批数据**重算（确定性，不重跑）：**基线 Behavior 50.0% → 68.0%**、
  护栏组 46.0% → 72.0%。**这才是本轮最高杠杆项。**
  ⚠️ **铁律：同一事实的多种表示（明细 vs 聚合、原始 vs 派生）只能取其一，相加必错。**
- **护栏机制改动的 A/B：未证明有效（2026-09-20）**。修正口径后配对：
  fail→pass 9（T013 T017 T024 T027 T029 T033 T040 T044 T048）/
  pass→fail 7（T006 T008 T010 T015 T018 T025 T046），净 **+2**（68%→72%，+4pp，
  **在 n=50 的 ±28pp 噪声带内**）。
  正向：改善的几乎全是基线最空转的（T027 26→12、T024 20→9、T013 16→10），与离线反事实吻合。
  负向：T025 **3→16**、T046 **9→33**、T018 8→13（原本正常反而严重空转）
  + 2 次新 `forbidden_tool_usage`（工具不在改动范围，疑噪声但不可默认）。
  → **不予采纳为已验证改进，需第二轮对照**；若复现"小调用数 case 反而变长"，
  把 per-tool 预算放宽（8→12）或改为"仅对失败/低新颖度调用计数"。
  配对分析工具：`delivery/probe_guard_fix_ab.py`。
- **提交状态（2026-09-20）——判断 HEAD 里到底有什么，以本条为准**：
  ✅ 已入库 `2e5e22a`：评测器双倍计数修复（+2 条回归用例，负向验证通过）、
  目标解析收敛为 `resolve_target` 单一实现 + 补 `project`（**不含**默认目标回落/阈值对齐）、
  `GuardLeverConsistencyTests` 的**开关那一半**、`.env.example` 补声明、
  `CONFIG_PRECEDENCE_TRUTH.md` 修正 3 行过期「`.env` 未设」；第二笔 docs 提交 = 计划 + 4 探针 + 记忆。
  ⏳ **留在工作区未提交（等第二轮 A/B）**：`runctx.py::per_tool_budget()`、
  `readiness_gate.py` 的 `_DEFAULT_TARGET_IDENTITY` / `logical_target` / `>= terminalize_after`、
  `test_budget_values_match_code_defaults`。
  → 上面 ①③ 两条写的"已修"**仅指工作区**，clone 出的 HEAD 里没有，别按记忆当成已入库。
  ⚠️ **两条拆提交铁律**：① 测试若 `import` 了被押后的符号，就**不能先于实现提交**（提交态 ImportError）
  ——拆之前先问"待提交的测试 import 了哪些将被押后的符号"，并**在只含待提交改动的中间态上跑测试**；
  ② **被 gitignore 的配置文件（`.env`），"把它写显式"这个改动提交后等于没提交** —— 真正载体是
  `.env.example`，而陈述它的文档（`CONFIG_PRECEDENCE_TRUTH.md`）会在同一刻变成假话（记录层漂移）。
- `execute_resilient`（`runtime/broker.py`）**是死代码**：broker 在 `runner.py:287` 建了却从不
  `execute`，真实调用走 SDK 的 `on_invoke_tool`。MCP 的网络重试在 `mcp_bridge` 内，
  2026-09-19 已去重到 `resilience.run_with_retries`（`NETWORK_RETRYABLE_EXCEPTIONS` 为唯一来源）。
- ✅ **1555 个删除已提交收敛**（`eed19c8`，−213768 行），**已核实无真实数据**（我一度误判为
  "910 个用户数据"，错了）——`forge_data` 753 个被删源文件**只有 6 个不同文件名**，
  是同一批文件反复导入 840 个测试沙箱的堆积；`notes/` 133 个抽读全是 agent 测试生成的 demo。
  真用户资产（`materials/*.docx`、`.env`、`memory/`、`data/`）均完好。
  ⚠️ **判"用户数据丢失"必须看去重基名 + 抽读内容**，只看目录名/数量会严重高估。
- ✅ **运行产物已摘除跟踪 + 补齐 .gitignore**（`b549327`）：`runs_eval*/`、`forge_data/`、
  `exports/`、`notes/`、`summaries/`、`report_*.json`、`runs_*.log`、`*.sqlite-shm`、`*.sqlite-wal`。
  `materials/` 是用户输入资料，**必须保持跟踪**。摘除用 `git rm --cached -r`（磁盘不动）。
  **这是"1555 删除"的根因**：运行产物被跟踪 → 每次跑都堆积 → 必然再次索引/磁盘脱节。
- 体检固定跑：已跟踪文件正则扫 `sk-/cpk-/tvly-/AKIA`（2026-09-19 实测仅 8 处测试假值，`.env` 从未入库）。
- ✅ **名册死名已清并升级为哨兵**（`4c58cb5`）：生产代码里的 `web_search_v2`（实为 `web_search`）
  与 `delete_task`（**全项目无任何任务删除能力**）已从 7 个文件的名册中移除，并连同 `code_loop_tool`
  一起并入 `tests/test_tool_roster_consistency.py` 的 `WRONG_NAMES` → 生产文件再出现即红灯（负向验证过）。
  ⚠️ **`delete_task` 曾把测试带偏**：`tests/test_benchmark_evaluator.py` 拿它当"被执行的禁止工具"样本，
  它能触发 mutation 判定**只是因为该死名被列在 `MUTATION_TOOLS` 里** —— 夹具已换成真实的 `forget_memory`。
  `forget_memory`/`schedule_remove`/`sandbox_rollback` 是**真实工具**（此前登记为"未决"是误判）。
- `REPAIR_PLAN_2026-09-19.md`：4 级优先级修复计划；6 步已于 2026-09-19 执行（见当日日志）。
- ⚠️ **仓库曾缺一大块主干源码**（`cli/` 整包、`runtime/episode*`、`token_gate`、`resilience`、
  `trace_export`、`ui_frozen`、`benchmark/eval_runner`、11 个测试文件）——2026-09-19 已补录
  （`f322ec8` + `fd265fb`）。**迁移/clone 前先核对 `git ls-files` 与实际模块是否一致**。
