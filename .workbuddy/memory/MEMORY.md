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
- **写进记忆的「已知问题」也要有源，无源的结论要主动撤销**：曾把
  「`test_tui_artifacts.py` 全量负载下偶发红」记成待修，10-05 追溯时发现
  ① commit message 里从未提该文件，② 4 份全量 junit 里它一次都没失败，
  ③ 16 核满载 + 并发跑全部 TUI 测试都复现不出来。**无源结论若留着，
  下一个会话会去"修"一个不存在的缺陷**——这比不记更坏。
  判据：任何「已知缺陷」条目都要能指出**哪份日志/哪次实跑**支持它。
- **凭"代码看起来脆弱"改代码也是错**：`pilot.pause(数字)` 确实是固定 `asyncio.sleep`
  （无参才走 `wait_for_idle(0)` 确定性等待），但**无失败实据 ⇒ 待观察而非待修**。
  这类写法全项目 28 处，大改风险远大于收益。真要改**先造出稳定复现**。
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
- **~~Laya 接回~~ 已结案**（见 §0，用户 10-05 裁决「用 LangGraph 代替」）。**不要再列为待办。**
  唯一遗留的开放项是 `.wiki-creator/` SCHEMA 主题清单待用户确认
  （建议：`harness-review`/`guardrail-config-governance`/`state-resilience`/
  `ai-architecture-migration`）—— 但该wiki 是复审知识的沉淀，与 Laya 决策无关，
  用户未批准前不启动。
- **P2 剩余**（10-05 已完成：structured_log 假异常噪声、护栏审计漏报 MCP 工具、
  `router.py` 正则路由免责注释+护栏、`test_ci_shard` 判据根因。**过程与数据见
  `2026-10-05.md` §1~§2**）。**唯一还挂着的**：`runtime_paths.py override=False`
  认知风险（未取证，按「无源不修」原则须先有实据）。
- **已撤销的无源结论**：`test_tui_artifacts.py` 偶发红（4 份 junit 里从未失败、
  16 核满载复现不出）、`ci_shard.py` 3 个零收集文件（实测 4 片全非空）。
  **`pilot.pause(数字)` 固定等待全项目 28 处属「待观察」不是「待修」** ——
  客观脆弱但无失败实据，真要改先造出稳定复现。
- **跨会话遗留三条**：① `provider_gateway.py:591 record_attempt` 不记真实异常类型/消息 →
  `provider_internal_error` 时「详情见运行记录」是空话（修时守 A8 脱敏）；② MCP 退出时 6 个
  stdio server 各报一遍 anyio cancel scope 跨 task `RuntimeError`（~50 行用户可见噪音）；
  ③ `llama_bridge.py:928-930` 审批死分支（bundle 命中数 0；同族：非流式路径
  `approval_required=True` 命中数 0——改 OpenAI 响应体风险高于改桥层自有字段）。
- **三个 workflow 从未在真实 GitHub runner 上验证。**
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
