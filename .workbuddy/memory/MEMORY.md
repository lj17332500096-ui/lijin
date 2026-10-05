# 项目长期记忆 — my_creative_agent

> 2026-10-05 二次精简。**只留跨会话可复用的铁律与结论指针**，具体过程/数据/清单一律
> 查报告与日志：排查过程见 `.workbuddy/memory/YYYY-MM-DD.md`，定级报告见 `docs/audits/`、
> `docs/acceptance/`、`docs/architecture/`。发现本文件超限被截断时，优先补 §0/§2/§6，
> 已完成事项只留一行指针。

## 0. 路线决策（用户 2026-10-05 定，已结案）
- **路线：工具路由用 LangGraph，不再接回 Laya 路由头。** 用户明确决策「laya 不接回了，
  用 LangGraph 代替」。此项**已由管家节点裁决结案**，不再作为待办，也不要再以
  「Laya 是主攻路线」为由建议接回或重投。
- 决策理由（用户认可的技术判断，非"放弃"）：关键词 Tool Router 用规则做语义判断，
  违反 `AGENTS.md` 分工宪法，已被 LLM 选择器正确取代；Laya 独立pipeline 与 LangGraph 图
  重复且绕过 Runtime 授权链。详见 `docs/audits/MIGRATION_POST_REVIEW_2026-10-04.md` §6.1
  的三件区分表（该节是**决策依据的存档**，不是待实施方案）。
- **资产现状（保留，不清理）**：`laya/_laya_inspect/laya-main/` 是 2.7M 源码 fork；
  另有权重 `var/models/laya-tool-route-v2/`（2.1G，含 safetensors + 多个 gguf）与
  训练数据 `data/laya_forge/`（3.2G）。**这两处被 `.gitignore` 排除（`var/*` 第 43 行、
  `data/` 第 20 行），从未进 git，无法用任何 git 操作恢复**，只存在本机 F 盘。
  历史基线留档：GPU(8099 SYCL) 关键词 74.2%、CPU 微调前 30%、1 epoch 15.43%（坍缩）。
  若将来需要复现基线或训练新头，这两处是唯一来源，**清理前必须先备份**。
- Laya 接入层索引约定护栏见 §7（仅在重新启用该方向时才有意义）。

## 1. 架构与边界
- **给「要用户拍板的建议」之前，必须先读约束文件**。10-05 教训：我在没读
  `.wiki-creator/wiki/SCHEMA.md` 的情况下给了 4 个主题名建议，而该文件第 18 行写着
  「主题**永不合并**，仅 >1000 页才建议拆分」（且`lint.py` 只实现 `oversized_topics`、
  **无任何合并逻辑**）⇒主题划分**近似不可逆**。读完后发现建议方向完全错。
  **建议的方向性错误比措辞错误严重得多**：把「按内容分类」误当成无害练习，
  实际是消耗一份不可回滚的决定。判据：若一个建议一旦被采纳就难以撤回，
  先花 1 分钟读约束再开口。
- 单 Agent + 单主链 + 极厚外围治理（harness 型）。入口 `main.py`（`--classic`/`--voice`/
  `--daemon`/`--tui`/`--run-task`）+ `cli/app.py::run_cli`。
- UI 层冻结（`webapp.py`/`llama_bridge.py`），仅 `FORGE_ENABLE_UI=1` 可起；`webapp.py`
  自 09-22 起是「前端宿主」（190 行，唯一对外协议 = llama_bridge 的 `/v1/*`+`/props`+
  `/tools`+`/slots`+`/models/*`）。边界红线（`tests/test_ui_isolation.py`）：后端/CLI 不得
  `import webapp|llama_bridge`。
- 工具层：37 native + 31 MCP（6 server + anysearch）。`_EXPLICIT_ONLY_TOOLS` 是**限制**表
  （在表内要命中语义才放行，不在表内永远放行）。
- 技能两目录：`skills/`（运行时）｜`agent-skills/`（编译产物）。`gorden-ppt` 族有意不走高门槛。
- 数据：`agent.db`(WAL) + `sessions.sqlite` + `notes/`、`exports/`。

## 2. 工程铁律

### 2.1 测试有效性（本项目最大的隐性浪费来源）
- **test vacuity**：断言因被测分支不可达而静默失去鉴别力。测「通过」≠ 断言有鉴别力。
  **每次改断言都要做中和实验**：改坏被测行为，看是否真会红。
- **⚠️ 中和实验必须先验证「注入真的生效」，再解读结果**。曾用 `sed` 精确字符串
  替换注入缺陷（字符串含转义差异，未命中），却没检查就继续跑，拿到「5 passed」
  的**假中和结论** —— 若就此收工，会把「缺陷没注入」误判成「判据没鉴别力」，
  进而错误地改掉一个其实没问题的判据。**流程：注入 → grep/diff 确认改动在 → 跑测试
  → 解读**。另外注入后必须能还原（本次用 `cp` 备份 + `try/finally`）。
- **白名单断言在新增项时天然失效**（不检查「第 N+1 个漏网的」）→ 写**可达性反向断言**
  （AST 传递闭包分析，已落地 `tests/test_p3_main_entry_guard.py`）。
- **可达性分析还要按「调用点」判，不能按「函数名」判**（第三层盲区）：同一函数被调两次时，
  按名字判定会让壳内那次**掩盖**壳外那次。必须用 `id(ast节点)` 判断归属，
  报错带上 `@lineN` 才可定位。
- **存在性断言 `assertIn` 在多处出现时无效**（2 处删 1 处仍满足）→ 用**计数断言**。
- **双向验收**：收紧判据必须同时测误伤方向与召回方向。**单侧自证无效**（自证召回用例
  恰好全在安全侧，对本次改动无鉴别力）。
- **mock 桩会掩盖生产缺陷**（桩强制真值 → 掩盖真凶）。
- **⚠️ 判据方向可能整体写反 —— 这比「判据太松」更隐蔽，因为它确实有鉴别力**。
  `test_stale_memory_probes` 初版判「记忆说无源 + 取证说已修 ⇒ 自相矛盾」。
  但那个组合**不是矛盾**：MEMORY.md 记的「曾把它当无源撤销、后来证明是真问题并修好了」
  是**正确且有价值**的纠错记录，按初版反而会被报错 ⇒ **判据把正确的记忆判成错的**。
  真正该抓的是反向：**说「无源所以撤销了」而实际还坏着**（真问题被掩埋）。
  正确形态是一对**镜像**判据：「已修却没划掉」（stale）+「无源撤销却还没修」（掩埋）。
  **写判据时要把「合法状态」和「缺陷状态」列全**，否则很容易造出「抓的是合法组合」。
- **⚠️ 恒真的判据不是「宽松」，是「无鉴别力」**。同文件里 `retire_marks` 是
  **全文裸子串匹配**，而 `「无源」` 在铁律正文出现 3 次 ⇒ `_entry_is_retired` 对该
  keyword **恒为 True**，与条目内容完全脱钩。同族问题还有两处：
  ① 判定「是否已撤销」时，keyword 与撤销措辞**分处任意两行**（已收紧为同行/紧邻行）；
  ② `_context_block` 只取 keyword 的**首处**出现，而真条目在第 4 处（前三处都是
  铁律正文里的举例）⇒ 永远读不到被测对象。**遍历所有出现处，不要只看第一次。**
  **自检手法**：把判据的每个输入分别退化成「与被测对象无关的常量」，若结果不变 ⇒ 恒真。
- **撤销标记与撤销理由不可混用**：「无源」是**理由**（不保证条目真被划掉），
  「已撤销/已修/不成立」才是**完成态标记**。把理由塞进标记表 ⇒ 恒真（见上条）。
- **诊断可疑时，单元绿而端到端不绿要以端到端为准**，回头质疑诊断，别改测试迁就诊断。
  （曾据错误诊断给 `add_event` 加唯一索引，单元 4 passed 但端到端仍 2 条。）
- **判据指标要选「对缺陷敏感」且「无量纲、无阈值」的那个，优先选全序量（排名/次序）
  而非带阈值的连续量**。`ci_shard` 这条断言改了**四版**才对（取证见 `2026-10-05.md` §2）：
  v1「≤30 个文件」把「分片总数」与「分片大小」耦合 → 正常新增测试就顶红；
  v2「文件数 ≤ 中位数」因均匀分布无区分度、中和全绿；
  v3「所在片总代价 == max」量级被算法抹平；v4「代价排名第 1」✅。
- **⚠️ 别用「被测算法自己的优化目标」当判据**（本项目最隐蔽的一次误判）。
  v3 断言「压力用例所在片总代价最高」，但 `shard()` 是贪心**均衡**算法，各片总代价
  必然接近（129 文件下极差仅 0.9），而 `places=1` 容差 0.05 ⇒ **信号 0.7 压不过噪声
  0.05，这是个随机翻转的布尔量**（同一份代码两次运行结论相反）。
  **单跑绿 ≠ 稳定**：判据若依赖 N 个文件的字节和，就随代码库演进而漂移。
  验证稳定性不能靠重跑（那是运气），要靠**注入扰动看它翻不翻**。
- **抓栈定位时必须同时看 SQL 文本**：只看调用栈会把「两条不同语句」误读成
  「同一条执行两次」，导致归因方向完全错。
- **写进记忆的「已知问题」也要有源，无源的结论要主动撤销**。但**要分两种形态**：
  ① **从未有源** ⇒ 撤销（曾把「`test_tui_artifacts` 全量负载下偶发红」记成待修，
  追溯发现 commit message 从未提该文件、4 份 junit 里一次都没失败）；
  ② **曾有源但取样负载不足** ⇒ **不撤销**（见上条「撤销前先问取样负载」）。
  任何「已知缺陷」条目都要能指出**哪份日志/哪次实跑**支持它，
  **以及那次实跑是在什么负载/条件下做的**。
- **凭"代码看起来脆弱"改代码也是错，但有实据后必须改**：`pilot.pause(数字)` 是固定
  `asyncio.sleep`（无参才走 `wait_for_idle(0)`），项目里 28 处。
  10-05 上午我按「无失败实据」判为待观察；**当天下午全量就红了** ——
  负载下复现「6 次 3 红」（约 50%），根因是 `_handle_command("/artifacts")`
  同步调用后紧跟 `pause(0.1)` 不够等命令处理完。改成**轮询到条件成立**
  （不是加大 0.1，那只是概率红变慢），负载下 8 次全绿、耗时未增。
  **其余 27 处仍不动**（无实据）。⇒ **判据是「实据」，不是「看起来脆弱」。**
- **⚠️ 撤销一条 flaky 结论前，必须问「我的取样在什么负载下做的」**。
  我曾据「4 份 junit 里它一次都没失败」撤销 `test_tui_artifacts` 的flaky 记录，
  **当天下午它就在全量下红了** —— 那 4 份 junit 都是**低负载**跑的。
  **低负载下跑 N 次全绿，对 50% 概率的 flaky 毫无说服力：
  flaky 的证伪需要「在会触发它的条件下复现」，不是「在不会触发的条件下跑通」。**
  （这是「无源结论要撤销」的第一形态要撤销、第二形态不能撤销的分界。）
  10-05 晚已修（`test_tui_artifacts.py:99` 改轮询到条件成立，**负载下复现 6 次 3 红
  → 修后 8 次 0 红、耗时未增**），探针 `tests/test_stale_memory_probes.py` 锁住该状态。
- **pytest 报出的测试名 ≠ 失败原因所在**：同一 class 内 A 用例红，真因在 B 用例。
  我把 `test_click_view...` 的失败归到「点击 + pause 不够」，真凶是
  `test_artifacts_command_...`（`test_tui_artifacts.py:99`）。**必须负载下反复跑、
  抓具体失败断言**，不能只看 pytest 报出的用例名就归因。
- **判定「本机 shim/环境造成的假失败」时，也须先看退出码与是否读到了目标**。
- **「本地全绿」这个前提本身要被检验，不能默认**。写 `var/sim_clean_runner.py`：
  移开本机 `.env` → `git archive HEAD` 导出干净副本 → 按 workflow 的job 级 env
  精确设键 → 跑全量。**这是「能否正常运行」的直接证据，比读 workflow 文本可靠**。
  实测据此抓出workflow 手段（补 `.env` / 建目录）**修不了的 P0**：
  `test_tools.py` 硬编码了仓库目录名，同一份代码仅因副本目录名不同就翻转
  （`my_creative_agent` 3 passed / `lijin` 3 failed）—— `actions/checkout` 的
  clone 目录**就是仓库名**，本地 `my_creative_agent` 只是巧合。
- **⚠️ 模拟环境必须逐条照抄 workflow 的步骤，漏一步就造出假红灯**。
  第一版模拟漏了 ci.yml 的「Prepare runner-absent directories」，多出 15 条误报
  （`web/llama-ui` 缺失 → `import webapp` 抛 RuntimeError → 波及
  `test_ui_isolation` 2 + `test_api_layer_phase_c` 10）。补上后 12 条消失。
  判据：**模拟的失败数≠ 真实失败数时，先查模拟本身**，别急着改产品代码。
- **「可达性分析的断言」自身也要防空集恒真**：加下界自检（如"识别到 ≥3 个入口"）。
- **中和实验要固化成常驻用例**，不能只留在会话里（否则权重被改回去时无人知道当初为何
  加它）。**配套写构造性用例**验证中间推论（用临时目录造文件集，不依赖真实规模），
  这样被测算法换实现时也会被咬住。落地：`tests/test_ci_shard.py` 的
  `..._caused_by_the_weight_not_by_luck` + `..._actually_isolates_the_stress_file`。

### 2.2 跨阶段结论传递（元教训）
- **传递结论必须带「落点完备性」核验，不能只带方向正确性**。曾据报告让worker
  「改 3 处 X」，实测真凶是 `runtime_paths.py`（被所有模块传递导入）、实际 13 个构造点
  → 照做会漏 11 条空转断言。**指令要写落点完备性**（"grep 出全部构造点"），或要求
  worker 独立实测落点后再改。**我本人也犯过同款**：查 `task.failed` 重复只看了
  `add_event` 的 INSERT，漏了 `mark_failure` 里`transition` 内部那条 ⇒ 归因方向全错。
- **排查前先数「该事实共有几处写入点」**，再决定从哪处切入。

### 2.3 护栏与配置
- **审批护栏 fail-closed**（10-04 建）：`FORGE_APPROVAL_FAILCLOSED` + `runtime/startup_guard.py`；
  只认显式 `FORGE_UNATTENDED=scheduled|daemon|both`，非法值视为未声明（反启发式：渠道名/
  run_id 前缀不得被当声明）。
- **安全阻断异常必须继承 `BaseException`**（`StartupGuardBlocked(BaseException>`），使
  `except Exception` 结构上无法吞掉——实测 bare `except` 曾让 fail-closed 降级为 fail-open。
- **配置在 import 期加载 = 隐藏全局单例**。`.env` 不入库 → 换机器必需键会缺。
- **重试权只属于 provider 网关**：SDK `max_retries=0`；单 provider
  `FORGE_PROVIDER_MAX_ATTEMPTS=3`；primary+fallback 合计 `..._MAX_TOTAL_ATTEMPTS=6`；
  L3 格式闸 `FORGE_FORMAT_RETRY_MAX=1`。判据写 `local_attempts + 1 < max_attempts`
  （`local_attempts` 记的是**重试**次数）。

### 2.4 数据与记录
- **run_id 是事件/消息/审计的主键**。~~run_id = trace_id~~ **错**（`runner.py` 0 命中，
  曾致`spans` 恒 `[]`）；`tool_calls.task_id` 装 run_id 而 `messages.task_id` 装容器 id
  ——同列异义，join 必错。
- **同一事实的多种表示只能取其一，相加必错**（明细 vs 聚合、原始 vs 派生）。
- **判据字符串必须与生产端写出的值对齐**（曾钩子只认 `"ok"` 而生产写 `"success"` → 整列恒 NULL）。

### 2.5 并发与资源
- **SQLite：跨线程连接 churn 比 WAL 退化更常为故障源**。治本 = 按 `db_path` 复用 **RLock**
  串行化连接生命周期（`runtime/task_manager.py::_connect`），不是加重试。
- **改共享资源创建方式前先看测试断言的意图**——两全解是进程级共享池
  （`_shared_no_proxy_client()`），不是私有池。
- **事件可见性按消费者区分，不能无条件转发**（`response.output_text.delta` 用显式 opt-in
  `_public_stream`，仅 TUI 通道）。
- **「允许取代旧 Run」不能扩大到执行中状态**（submitted/running/paused 取代 = 静默取消）。
- **取消要杀同步工具进程树**（`runtime/cancel_scope.py`）。

### 2.6 Git 纪律
- **绝不用 `git add -A`**；精确暂存用 `git ls-files --others --exclude-standard` +
  `git diff --name-only --diff-filter=M` 喂 `--pathspec-from-file`。
- **提交后必须 `git show HEAD:<文件>` 抽查**——只看工作区会漏（`git mv` 后改内容会提交旧快照）。
- **「既有失败」必须对比 HEAD 才能下结论**（实测 10 条里 8 条是工作区改动引入的回归）。
  逐个读 diff 判断改动是否触及该断言契约，而非看「像不像老问题」。
- **「测试通过」不能替代「实现正确」**（worker 假报完成实测两次）；用 `git show --stat` 核对。
- pre-commit：判据在 `scripts/ci_gate.py::CHANGE_TEST_MAP`（是 `list[tuple]`，**不是 dict 正则**），
  已知失败登记 `.ci/known_failures.txt`（**必须写原因**）。绕红=摘出分批提交，**不用 `--no-verify`**。
- 其他易踩：`cat >> f << 'EOF'`(Git Bash 写头部)｜`.gitignore` 行内注释失效｜Git Bash `find`
  = Windows find.exe｜`git rm -r` 多子目录波及父目录｜同文件多处编辑禁并行。
  完整清单见本文件历史版本或日志。
- **清理路径** `except (Exception, SystemExit)` 保留 `KeyboardInterrupt`（本机 shim 劫持
  `rmtree`）；**测试自己的**清理用 `PYTHONPATH=` 屏蔽。**跑全量**
  `PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/ -q`（否则 9~11 条假失败）。

### 2.7 CI
- `windows-*` **默认 shell 是 pwsh**；YAML 合法 ≠ 步骤能跑。`--stdin < file` 的 `<` 在 pwsh
  是保留运算符 → 用 `Get-Content f | python x.py --stdin`。仓库有
  `scripts/check_workflow_shells.py` 按各步骤声明的 shell 实际解析。
- **shell 校验器不能假设「没写 shell 就是 bash」**（误判过一次；正确反应是修判据非改对代码）。
- **⚠️ workflow 要过三层校验，三层互补、缺一不可**（10-05 首跑 `jobs=0` 秒失败真因）：
  ① `yaml.safe_load`（文件结构，**查不出**表达式问题，已实测）
  ② `check_workflow_shells.py`（**各步骤声明的 shell** 能否解析 `run:` 脚本）
  ③ **actionlint**（**表达式语法**，含注释里的）。仓库已有
  `tests/test_ci_workflow_expr_syntax.py`（含 actionlint 调用 + 离线兜底判据）。
- **⚠️ GitHub 求值表达式时【不区分代码与注释】**。在 `run:` 块或任何 workflow 的
  注释里写「双花括号表达式」的**字面量示例**，同样被求值 ⇒ 解析失败 ⇒
  **整个文件被拒绝执行**，表现为 `completed/failure` 但 **jobs=0、check-runs=0、
  耗时同一秒**。想举例子必须写到**文件外**的文档里。
  ⚠️ 同一个坑有镜像版：**写判据时也会被注释里的字样满足**
  （`_EXPECTED_MATRIX_EXPR` 就是为此把匹配限定在表达式内）。
- **workflow 报0 jobs / 秒失败 ⇒ 先怀疑「文件被拒绝执行」，不是「测试挂」**。
  诊断顺序：① `actionlint`（含注释）→ ② `jobs` 数（0 ⇒ 文件级问题）
  → ③ YAML 解析 → ④ 才看步骤日志。
- **CI 红了但本地全绿 ⇒ 逐条排除，别猜**。已排除清单（10-05，shard 0 为例）：
  env 变量（用 CI 全部 env 跑 ⇒ 370 passed）、Python 版本（本地 venv 也是 3.11.15）、
  收集阶段（--co 正常）、pwsh 变量类型（`$files` 是单行 String）。
  **别误判 requirements 缺包** —— `ci.yml` 里已显式 `pip install pytest pypdf`。
  排除法只能排除，**不能定位**；真正定位要靠 CI 自己报（在 `Run tests` 里
  `Tee-Object` 落盘 + 失败写 `$GITHUB_STEP_SUMMARY`，免登录即可看）。
- **PS 5.1 的 `Get-Content` 对无 BOM 的 UTF-8 按 ANSI 代码页解码** ⇒ 中文变乱码
  ⇒ 引号被破坏 ⇒ parser 报**假语法错误**。读 UTF-8 文件必须用
  `[IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)`。`check_workflow_shells.py`
  曾因此报假错（已修，中和 4/4）。
- **pwsh 脚本里不要写成对反引号**（被当转义符）——要 Markdown 围栏用
  `[string][char]96 * 3`。
- **护栏报红要先确认对象是对的**。曾差点去改 workflow，实际错的是校验器自己的
  编码假设 —— 与「pytest 报出的用例名 ≠ 根因」同源：**报错的出处 ≠ 出错的地方**。
- **本机拉 actionlint**：`releases/download` 被拦（HTTP 000），走
  `api.github.com/repos/rhysd/actionlint/releases/assets/<id>` +
  `Accept: application/octet-stream`。已装于
  `C:\Users\Administrator\actionlint\actionlint.exe`。

### 2.8 Windows 路径与编码（本项目最大的跨平台隐性bug 源）
- **⚠️ 路径落根必须「对称规范化」**：凡 `X.relative_to(Y)`，`X` 与 `Y`
  **都要 `.resolve()`**。只做一侧 ⇒ Windows 上可能指向同一目录却**字符串不等**
  ⇒ `ValueError` ⇒ 把「明明在根下」误判成越界。
  两个方向都真实发生过：`target.resolve().relative_to(root)`（root 未规范化，
  `tools.py`/`rag.py`）与 `target.relative_to(root.resolve())`。
- **8.3 短名是主要触发条件**：GitHub runner 的 `tempfile.gettempdir()` 返回
  `C:\Users\RUNNER~1\...`（**短名**），`target.resolve()` 展开成长名
  `C:\Users\runneradmin\...` ⇒ 不等。**本地 tempdir 目录本身没有短名**
  （短名只在其下的新建子目录）⇒ **本地全绿不是证据**。
  护栏：`tests/test_path_normalization_symmetry.py`（用 `GetShortPathNameW`
  真造短名根后跑那6 个文件），已挂 `CHANGE_TEST_MAP`。
- **同一对路径在不同函数里必须得出相同结论**：`project_edit.py` 里
  `_validate_target` 两边都 resolve（判「在根下」通过）、
  `_rel_display` 只 resolve 一边（显示绝对路径）⇒ **写入成功但输出是绝对路径**。
  改这类代码时**成对核查**。
- **修路径显示/输出时必须保住输出格式**：`relative_to(...).as_posix()` 产出的是
  给用户看的相对路径，改成绝对路径会让断言红、输出也没意义。
  （agent 实测确认 `已写入 demo/utils/math.py` 未变。）
- **子进程 stdout 编码**：`print` 中文在 `PYTHONIOENCODING=cp1252` 下抛
  `UnicodeEncodeError` ⇒ 父脚本判「构建失败」。修法是**入口重配
  `sys.stdout/stderr` 为 utf-8 + `errors="replace"`**（覆盖所有输出点，
  含解释器自己写的 traceback），不是给每处 print 糊 try/except。
  用 `getattr(stream, "reconfigure", None)` 守卫，pytest 捕获下的 StringIO 没有该方法。
  **父子两侧都要改**：子进程改 UTF-8 后，父进程 `subprocess.run(text=True)`
  会按 locale 解码变乱码 ⇒ 要同步 `encoding="utf-8"`。
  ⚠️ 非 BMP emoji（`⚠️`）连 cp936 也编码不了，是最易触发的字符。
- **`ast.parse` 读带 BOM 的文件会抛** `SyntaxError: invalid non-printable
  character U+FEFF`（`tools.py` 有 BOM）⇒ 判据自身崩掉而不是报出该报的。
  必须用 `encoding="utf-8-sig"`。
- **正则字符白名单是隐藏的路径契约**：8.3 短名里的 `~`（`RUNNER~1`）会让
  `^(?:[A-Za-z]:)?(?:[A-Za-z0-9_.\-/\\]+)$` 这类**整串锚定**的字符白名单**整串 match
  失败** ⇒ 受信判定返 None。真实案例：`code_exec.py::TRUSTED_PROJECT_PATH_RE`
  （10-05 CI shard 0 唯一一条红的根因，本地 tempdir 不含 `~` 所以全绿）。
  **加字符前必须问**：真正的越界防线在哪一层？放行合法路径字符**不会**削弱
  「按路径分量比较 + 拒 `..` 段」的那道防线⇒ 只按实据加（这次只加 `~`，
  没扩空格/中文）。**字符白名单 + `$` 锚定 = 少一个字符整条路径报废**。
- **8.3 缩写有长度阈值**：目录名太短（如 `ci8_root`，6 字符）时
  `GetShortPathNameW` **原样返回**（ret=50）⇒ 造短名的实验静默 NOOP。
  探针目录名要够长（护栏里用 26 字符）。
- **⚠️ 判据里凡引用「解释器/运行时绝对路径」都是 portability 雷**：
  硬编码 `.venv/Scripts/python.exe` ⇒ CI 上（用 `actions/setup-python` 的
  hostedtoolcache 解释器，**根本没有 .venv**）抛 `FileNotFoundError: [WinError 2]`
  ⇒ 判据自身崩。**本地有 venv 时永远发现不了**。用 `sys.executable`。

### 2.9 SQLite FTS5 的中文是结构性失效（不是写法问题）- FTS5 默认 `unicode61` 分词器把**中文整段当一个 token**。已最小复现：内容
  「按钮必须放在右下角且带确认弹窗。」用 `"按钮"` / `"右下角"` / `"确认弹窗"` 查，
  命中数一律 **0**。**把查询改成二元组 OR 同样 0 hits** ⇒ **任何查询侧改法都无效**。
- **本项目有两套检索实现，一套好一套坏**：`rag.py` **压根没用 FTS5**，
  是纯 Python 倒排 + BM25（`_bm25_rank` + `_tokens` 对中文产**相邻二元组**），
  **中文一直正常**；`sources/retriever.py` 走真 FTS5 ⇒ 中文恒失效。
  修法是给 sources 侧补 Python 侧 BM25 兜底（`cjk_bigrams` +
  `_python_lexical_search`），**不改 FTS5 建表**（不需重建索引/升 INDEX_VERSION）。
- **为什么长期没人发现**：本地有 ONNX 向量把结果兜住了。而
  `requirements.txt` **无 onnxruntime**、模型目录 `models/bge-small-zh-v1.5`
  **不入库** ⇒ **CI 上向量必然不可用** ⇒ 掩盖消失。
  **「本地绿」在有外部资源兜底时不是证据** —— 要问「这依赖什么，它在 CI 上在吗」。
- 护栏 `tests/test_cjk_retrieval_without_vectors.py`（钉住前提 + 双向端到端
  + 反空转自检），已挂 `CHANGE_TEST_MAP`。
- **注入实验后必须看到标记，否则「修复前也绿」是假绿**。本日连续四次栽：
  ① TEMP 指短名 ⇒ mkdtemp 拼出双层路径；② `-p conftest` 找不到模块；
  ③ 对 **tempdir 本身**取短名（本机无 8.3 缩写）⇒ `[INJECT-NOOP]`
  ⇒ 那一刻的正确动作是**中止**并说「实验无鉴别力」，不是接受两个 exit=1；
  ④ 目录名不够长不触发缩写（与 ③ 同类，被NOOP 掩盖）。
- **护栏里的「条件表达式语句」是空动作**：`os.rmdir(p) if os.path.isdir(p) else None`
  语法合法但**什么都不做** ⇒ 每次跑留垃圾目录 ⇒ 短名序列漂移 ⇒ 后续注入 NOOP。
  必须写成真正的 `if` 块。
- **⚠️ 依赖开放上界 = 没有 CI 护栏**（10-05 实据，本项目最贵的一次排查）：
  `requirements.txt` 曾写 `openai-agents>=0.22.0`（**无上界**）。本地固定 0.22.0，
  CI 每次装当时最新版 ⇒ **同一份代码两边结论不同**。实际踩到：新版
  `default_tool_error_function` 出于安全加固**删掉了 `Error: {error}` 后缀**
  ⇒ 一条断言 CI 红本地绿，且**本地怎么复现都复不出**（短名/无向量/同形短名根
  三条件齐备仍绿）。现已全部加**兼容上界**。
  - 上界取「下一个 minor 的下一位」（`<0.24` 而非 `==0.22.0`）：
    `==` 过紧（安全补丁装不上），无上界等于没有护栏。
  - **「本地绿」的前提是依赖集相同**。看到 `>=` 无上界就要意识到这一点。
  - **「无 detail 的错误文案」是依赖版本变化的强信号**：grep 上游源码确认
    该串**只有一处**产生且**总是带后缀** ⇒ 输出不带 ⇒ 只能是版本不同。
    这个推理能一步定位到依赖层，比调路径快得多。
  - 业务上必须让模型看到的错误 ⇒ 用官方 `failure_error_function`
    （`runtime/tool_errors.py`），**只放行 `ValueError`**，
    内部异常（含本机路径）不放行。护栏 `tests/test_tool_error_visibility.py`
    **自己模拟新版 SDK**（本地对新版零鉴别力，不模拟等于没测）。

### 2.10 CI 诊断可见性（排查前先确认能拿到失败详情）
- 匿名可读性实测：`jobs/<id>/logs` **403**、artifact **401**、
  step summary 只能网页渲染、`check-runs/<id>/annotations` 200
  但**只有 "exit code 1" 无测试名**、**`raw.githubusercontent.com` 200** ✅。
- 本项目已让 CI 失败时把摘要推到 `ci-diagnostics` 分支
  （`ci-diag/run-<run_id>-shard-<n>.md`），**文件名必须带 run id**
  ——`fail-fast: false` 下多 shard 并发，同名会互相覆盖。
- **教训：遇到「CI 红本地绿」，第一步是打通失败详情的自动获取，
  再开始猜。** 10-05 那次根因只是一行依赖声明，
  但因日志读不到 + 依赖集不同，白白花了 4 轮 CI。
- **诊断脚本也要本地实跑**（不能只靠 YAML 合法）——
  在真 git 仓库里跑一遍，当场抓到 `git config user.name"..."` 缺空格这类错。

## 3. 评测方法论
- n=50 检 ≈±28pp，n=100 ≈±20pp，检 10pp 需 n≈400。任何「提升 X%」必给样本量与 CI。
- 绝对值判据（「必须为 0」）在随机 harness 上不可满足 → 改「不劣于基线」检验。
- 跑间：结果层翻转 0%，但工具序列发散 67%、终稿一致仅 8% → 单次观察不足为凭。
- 注入类 A/B 先证注入未改工具可用集（`TOOL_ROUTER=off` 结构性保证）。
- **性能怀疑先证伪再追**（+84s 曾被误归因于新修复，实为某测试「真跑完」而非变慢；
  用 `git worktree` 独立基线微基准证伪）。

## 4. 情节记忆层（定级：离线工具）
- 三臂 n=100：A/B/C 43.0/42.0/43.0%。**A vs B 无效**（工具集 46/46 不同）；**C 组 pull
  从未被检验**。保留价值：CLI `/episode` 离线复盘 + 47 条单测；只是喂模型不划算。
- 唯一重启条件：高信噪比定向信号（同类任务连续失败 ≥3 次后定向注入教训）。

## 5. 已完成里程碑（细节查对应报告，勿在本格展开）
- **09-19~21**：评测器双倍计数、TERMINALIZE 后继续受理工具调用、日志通道隔离、
  WORKSPACE_ROOT 钉项目根、模型切 3.0（n=50 68.0% vs 2.5 的 76%，−8pp 不显著）。
- **09-22 API 层**：Phase A/B + C（C3 错误信封、C6 反 CSRF）完成，**Phase D 未开始**。
  恢复点 tag `archive/api-routes-pre-delete-20260922`。
- **10-04~05 复审 + CI**：P0/P1 全修完、P2-1/2 恢复 CI（3 workflow + 分片 + shell 校验）、
  P2-10/11 修掉。**复审报告 6 处事实错误已在 §11 勘误表更正**（原文保留存审计痕迹）。
  10-05 又完成：`task.failed` 双写（真实生产缺陷，端到端 2→1）、护栏审计漏报运行期 MCP 工具、
  `test_ci_shard` 判据根因（详见 `2026-10-05.md`）。全量基线以最新一次实跑为准，
  `known_failures` 保持零条目。

## 6. 待办（按用户优先级）
- **P2 技术项全部清空**（10-05，核对方式：全量 1565 passed / EXIT=0）。
  **wiki 知识库首批已编译**（1 桶 `agent-harness-engineering` / 9 页，
  见 `.wiki-creator/wiki/SCHEMA.md`，lint 零硬告警）。
- **唯一剩余待用户决定项**：是否 push 触发三个 workflow 在真实 GitHub runner 首跑
  （本地全绿不代表 CI 绿：`.env`/`web/llama-ui`/`var/*` 在 runner 不存在，
  `windows-*` 默认 pwsh）。**已用干净副本模拟预演**：`lijin/` 目录下全量
  1565 passed / EXIT=0（`tests/test_stale_memory_probes.py` 之外的独立验证，
  过程见 `2026-10-05.md` §12）—— 但真跑仍有 3 条本地无法证伪的硬上限：
  总时长是否撞 runner 超时、依赖拉取可达性、并发压力用例在共享 runner 的表现。
  **验证入口**：`.github/workflows/ci.yml`（跑法）+ `docs/operations/
  CI_FIRST_RUN_HANDOVER_2026-10-05.md` §2（首跑后必做清单，含归因纪律）。
- ~~三个 workflow 从未在真实 GitHub runner 上验证~~
  **10-05 已跑 9 次；`ci.yml` 首次全绿 = run `37354853191`（`c2ce000`），
  随后 `bda43cc` / `041890a` 连续绿。** 历程与各次真因：
  - **第 1 次（`f418183`）**：`jobs=0` 秒失败 ⇒ workflow 被拒绝执行，真因是
    **注释里的双花括号字面量**（GitHub 连注释一起求值）。**已修 + 加护栏**。
  - **第 2 次（`20aa47b`）**：`jobs=4` 全部真跑 ⇒ 表达式修复生效；4 片全红。
    **已让 CI 把失败摘要写进 `$GITHUB_STEP_SUMMARY`**。
  - **第 3 次（`f16888d`）**：拿到日志 ⇒ **8.3 短名导致路径规范化不对称**
    （14 条红）+ **cp1252 编码**（1 条）。中和实验 14 failed → 61 passed。
  - **第 4 次（`1c5a516`）**：shard 1/2/3 绿，shard 0 剩 1 条 ⇒
    `TRUSTED_PROJECT_PATH_RE` 字符白名单**不含 `~`**（`RUNNER~1`）⇒ 已修（§2.8）。
  - **第 5 次（`ce2c6e9`）**：4 条红。① 我的护栏硬编码 `.venv` ⇒ CI `WinError 2`
    （§2.8 portability 雷）；② **FTS5 对中文恒失效**（§2.9），被本地向量掩盖。
  - **第 6 次（`82e9eba`）**：2 条红，**根因是 `requirements.txt`依赖开放上界**
    ⇒ CI 装到新版 SDK，其错误文案**删掉了 `Error:` 后缀**（§2.9）。已修+ 加护栏。
  - **第 8 次（`c2ce000`）**：✅ **四片全绿，无异常步骤**。
  - 已知既有失败（非本轮引入，HEAD 对照证实）：`test_rag` 两条断言与
    `test_sources_rag` **同进程组合跑**才红（单跑 13 passed）。详见 `2026-10-05.md` §20。
  - **TUI 负载 flaky 已修**（真因是 `textual` `NoMatches`：**屏 push 成功但子树
    还在 compose 中**，屏是屏、组件是组件）。全仓 32 处 `pilot.pause(0.x)` 已用
    **负载压测逐条实测**（4 压测进程 × 6 采样）：13 条**全部6/6 绿** ⇒
    按「无实据不改」其余 12 处不动。护栏 `tests/test_tui_load_stability.py`。
  - `guard-consistency.yml` 只在 PR 触发、`nightly.yml` 是 cron ⇒ push 本就不跑。
- ~~跨会话遗留三条~~ **10-05 已全部撤销：记忆过期，实际早已修完**。逐条实测：
  ① `provider_internal_error` 的「详情见运行记录」不再为空话 ——
     `95888af` 加了 `_error_fields()`（脱敏后写 `error_type`/`error_message`/`error`
     三个键，5 处调用点接入），`tests/test_provider_error_diagnostics.py` 10 passed；
  ② MCP 退出噪音 —— `tests/test_mcp_teardown_noise.py` 12 passed；
  ③ 审批死分支 —— `bf0616e` 已删除，`llama_bridge.py:915` 起有两条独立实测证据的说明。
  **教训同「无源结论要撤销」：记忆条目也会因对应修复已完成而失效，
  必须靠commit 追溯 + 跑测试双重取证，不能凭「记忆里写着待修」就动手。**
  ⚠️ 加这条时发现同一待办在本文件里出现了两次（新写的带验证入口、旧的没带）——
  **记忆自身也会发生「同一事实写两遍」的漂移**，与代码里名册漂移同源。已合并去重。
- **评测遗留**：n=200 对照 3.0 vs 2.5；区间口径按 3.0 校准（T001/T031/T023/T050）；
  T040 false_completion 单独立 case；T008 终态 completed→waiting_user 复查；per-tool 预算 8→12。
- **有意不提交**：`benchmark_fixture/_inline_*.py`（churn，建议 gitignore）、`code_sandbox/*`、
  `delivery/*.md`、`.env` 与运行产物。⚠️ 跑 `--daemon`/`--run-task` 会**真执行**
  `config/tasks.json` 定时任务（曾污染该文件）。

## 7. Laya 接入层索引约定护栏（仍有效）
- `laya/agent.py::predict` 按 `crit` 字典序反查选项（`choice = keys[p.argmax()]`）；训练侧
  target 同样按 `crit` 序逐位对齐。
- **铁律**：任何按 `render_options` 喂 target 的代码，索引必须动态解析渲染序，禁止硬编码
  `[text, tool]`（曾因数据 crit 是 `{tool,text}` 序，5 epoch 全押 tool、评测 0.2% 假象）。
- 基线：GPU 关键词 74.2%；CPU 微调前 30%；1 epoch 15.43%（坍缩）。XPU 栈
  `pip install --force-reinstall "torch==2.14.0+xpu" --index-url download.pytorch.org/whl/xpu`。
- `_fixed` 目录需拷 `rl_agent_config.json` 才能被 `Agent(dir)` 加载。
