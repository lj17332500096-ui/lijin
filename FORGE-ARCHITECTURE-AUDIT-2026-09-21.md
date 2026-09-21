# FORGE Agent Harness 架构审计与完善项分析

**日期**：2026-09-21（专家：墨衡 · Agent Harness 架构设计师）
**基线**：仓库当前代码（`main` 分支，~5 万行）+ 9-19 架构评审 + 9-21 行为层收敛整改后的现状
**范围**：全面审计单 Agent + 单主链 + 厚外围治理的 harness 架构，定位「哪些需完善/修改」，按性价比给出优先级与落地规格。
**原则**：以顶级架构师标准交付；给方案必附权衡与理由；区分「用户已确认决策」与「默认假设」；不假装全覆盖，诚实标注未知区。

> 本报告为审计与规划，**未修改任何生产代码**。所有「现状」判定基于源码 grep + 既有审计文档交叉验证。

---

## 1. 架构全貌（基于源码核查，非仅文档）

FORGE 是**单 Agent + 单主链 + 极厚外围治理**的 harness 型项目（约 5 万行）。主链：

```
用户请求 → run_turn() → 11 道门（needs_user_input → constraint → missing_required_fields
  → persistence/idempotency → intent gate → Readiness → Approval/FileScope
  → budget/convergence_level → redundant_check → completion_ready → repeat_target）
  → 真实工具执行 → _record_tool + note_progress
→ ConvergenceTerminated / NeedsUserInputTerminated → 终态收口
```

外围治理：Execution Evidence 执行账本 + Completion Gate（6 类 verdict 默认拒绝）+
双预算闸（墙钟 + token，TOOL_BUDGET_TOTAL=20，per-tool 上限）+ 可恢复状态机
（`state_machine.py` 转换表）+ 41 工具 Router（关键词/别名裁剪 41→8~16）+
run_id = trace_id 的 OTel JSONL 审计 + 50 case 评测集。

**9-21 行为层整改已落地的部分**（`FORGE-BEHAVIOR-LAYER-CONVERGENCE-REPORT.md`）：
web_search 两级预算（soft=3/hard=5）+ Clarification Gate 9 类拦截 + trace 补
`normalized_args/result_fingerprint/progress_event/canonical_target/workspace_epoch`
5 字段 + 行为断言 6 指标（`search_call_count/duplicate_tool_calls/no_progress_calls/
replan_count/clarification_required/clarification_happened`）。全量回归 1239 passed / 0 failed。

## 2. 哪些需完善 / 修改（按性价比排序）

> 以下逐项都是**先查了源码确认实际状态**，不是只看 9-19 文档。每项给「现状核查 → 完善方案 → 权衡」。

### 2.1 P0-A　把 episode 记忆层接进 run_turn 主链（地基已打好，只差接线）

**现状核查**：`runtime/episode_store.py`（episodes 表 + `fingerprint(goal)` + `recall(goal)` +
`copy_from` 留一法 + 全局 `_RECALL_EXCLUSION` 排除）与 `runtime/episode_recall.py`
（`EPISODE_RECALL` 开关 + `recall()` + `render()` + `build_context()`）**已实现**。
但 grep 确认 `runtime/runner.py` / `main.py` / `cli/app.py` 里 `episode_recall`/
`inject_episode` 命中数为 **0** —— 即 episode 注入**只走 benchmark `--inject-episode` 路径**，
生产 `run_turn` 主链没接。9-19 评审说「episode 层完全空白」已被推翻，但「只接了一半」是新风险。

**完善方案**：
1. 在 `run_turn` 的 Session Preparation 阶段（`context.py` 既有 source-injection 信任边界处）
   调 `episode_recall.build_context(message)`，把命中的一条结构化事实（「上次同类任务用了 X 工具、
   Y 轮、失败过 Z」）注入，**标为 data 非指令**；
2. 留一法：`set_recall_exclusion({本 run 的 fingerprint})`，避免自引用；
3. Safety 红线：episode 注入**不得绕过任何一道门**（FileScope/Approval/Completion Gate 照跑），
   验收指标 Safety Violation Rate 必须为 0；
4. 新增 `tests/test_episode_recall_wiring.py`：断言 run_turn 主链命中 episode_recall。

**权衡**：这是「只搭一跳线，让 26k 事件（task_events 26334 行 / tool_calls 6541 行）变可用资产」
的最后一环，不碰冻结的 Runtime 主链语义。建议**最先做**。

### 2.2 P0-B　上下文工程三件套 + 子任务委托（用《Practical Guide to Context Engineering》落地）

> **本项设计基线**：`H:\GitHub源码文件\Practical-Guide-to-Context-Engineering-main\`（VitePress 文档站，46 个 md / ~16k 行）。该库与 FORGE 单 Agent + 单主链 + 厚治理的架构**方向完全一致**——它明确主张「先单智能体 + 上下文压缩，只在需要时加多智能体」，且「写入任务为主」的场景单智能体天然更优。FORGE 不需要引入多智能体框架，但要把它给出的**三层上下文管理体系**落扎实。

**现状核查**：全仓 grep `sub_run`/`child_session`/`task_delegation`/`handoff`（非占位）= 0；无 `ContextManager`、无 token 计数、无压缩调度、无工具消息裁剪。一次 `deep_research`（一轮约 6 次搜索 + 3 次模型调用）的全部检索碎片都塞进同一个 `sessions.sqlite` 会话，永久污染后续所有对话上下文——参考库 §1.1 把这类污染归为「**上下文污染**」，是代理"发展出无意义策略、重复行为、追求无法实现目标"的根因。

**完善方案（三层，按性价比排序，对应参考库 §3.1/§3.2）**：

**层 1 · 工具消息裁剪（参考库「上下文压缩-工具消息裁剪」，成本最低 · 最先做）**
- 参考库给 `compressContext` 的完整 TS 实现：识别工具调用轮次（`identifyToolRounds`）→ 保留最近 `keepLastToolRounds`（默认 1）轮 → 删旧工具输入/输出，system 与非工具消息保留；
- FORGE 落地：`context.py` 的 `build_context` 阶段，对 `sessions.sqlite` 历史按「**保留最近 N 轮工具调用（N 默认 2）**，旧工具结果替换为一行摘要」做裁剪；
- 参考库关键约束：工具返回**限 2000 字符**，超过部分写文件只回路径（FORGE 当前 `read_workspace_file` 无此截断，需补）；
- 收益：消除 9-21 审计定位的「`list_workspace_files` cap=8 + `read_workspace_file` 11 次」这类工具输出堆叠占上下文的问题。

**层 2 · 压缩指令（参考库「ClaudeCode 8 节 / Gemini 5 节 state_snapshot」，成本中 · 接在层 1 后）**
- 触发条件（参考库 §3.2）：当前 token ≥ `max_tokens × 0.8` 时进入压缩节点；FORGE 用既有 token 预算闸（`TOOL_BUDGET_TOTAL=20` 之外的墙钟/token 闸）做阈值判断；
- 压缩指令（参考库 §3.2 ClaudeCode 版，FORGE 直接可用）：要求模型保留 ① 主要请求与意图 ② 关键技术概念 ③ 文件/代码片段 ④ 错误与修复 ⑤ 已解决问题 ⑥ 所有用户消息 ⑦ 待处理任务 ⑧ 当前工作与下一步；
- 压缩后加「**开篇语**」（参考库明确要求）：「上下文已使用结构化 8 节算法压缩，所有必要信息已保留，可无缝继续对话」——这是避免压缩后模型"断片"的关键；
- FORGE 落地：新建 `runtime/context_compress.py`，`compact_history(messages) -> compressed` + 写 `compression_history` 表（保留最近 10 次，参考库 §一 第 3 条）。

**层 3 · 子任务委托 / 上下文隔离（参考库 §2.3 Context Quarantine，成本高 · 最后做 · 需 2 个决策）**
- 参考库明确：「**搜索的本质是压缩**；子代理通过并行操作自己的上下文窗口促进压缩，在把最重要 token 压缩给主代理前，各自独立探索问题不同方面」；且「**子智能体间上下文隔离**是致命短板」（§3.2，无好解，故单智能体优先）；
- FORGE 落地：复用 `get_or_create_container` + 独立 `SQLiteSession`，把**无副作用的重活**（`deep_research` / `code_loop` / `compact 摘要` / `--daemon` 定时任务）放进临时子会话；子会话只回传 **≤2k token 摘要**进主链（层 2 的压缩指令直接复用）；evidence 仍走既有 `rctx.note_execution_identity` 登记路径，**Completion Gate 一行不用改**；**委托入口硬编码在 `runtime` 层少数已知位置**，**不**暴露成模型可见工具（否则 Single Active Run / FileScope / Approval 所有权模糊——参考库 §3.1「主智能体与子智能体上下文中断」即此风险）。

**需要用户拍板的 2 个关键决策**（默认假设，可推翻）：
- **决策 1**：子会话的 mutation 如何在主 Run 的 FileScope / 审批域内记账？
  （建议：子会话只读 + 摘要回传，mutation 一律走主 Run 审批门；若子会话也允许 mutation，
  需扩展 `ApprovalGate` 的 run_id 隔离模型。）
- **决策 2**：哪些工具默认进子会话？
  （建议：`deep_research` / `code_loop` / `compact` 三类纯探索/纯验证/纯摘要类；
  `write_project_file` / `edit` / `run_python` 永远走主 Run。）

**落地状态**（2026-09-21）：✅ 已实现。`runtime/context.py` 新增 `identify_tool_rounds` + `trim_tool_messages` 纯函数 + 3 个开关（`FORGE_TOOL_TRIM_GUARD=on` / `FORGE_TOOL_TRIM_KEEP_ROUNDS=2` / `FORGE_TOOL_TRIM_MIN_CHARS=4000`），接进 `prepare_session_context` 的「3.5 中间档」（Soft compact 之后、Hard window 之前）；快路径加 `tool_calls` 字段避免工具碎片多时误放行。新增 `tests/test_tool_trim.py`（9 用例），全量回归 1248/0。

### 2.3 P0-C　think / scratchpad 工具（零成本 · 最先做）

**现状核查**：全仓无 `think` 工具、无 scratchpad。模型在两个工具调用之间没有「可以写字的地板」。
`public_activity.py:38` 只是**剥离模型自发的思考标签**（输出侧清洗），不是「两个工具调用之间可以写字的地板」。

**为什么是零成本高收益**：
- 加一个 `think(text)` 零副作用工具，只写 Run 级 scratchpad（落 `tool_calls` 或新建 `run_notes` 表），
  **不进主模型 context**（或只进最后一条）；
- `runctx.decision_hint()` 已是「运行时→模型」单向事实通道，think 工具补上「模型→自己」的另一半；
- 9-19 评审引用 Anthropic 实测：think 工具在 τ-bench airline 上有 54% 相对提升（二手引述，标注可信度中等）。

**完善方案**：
1. `tools.py` 加 `think(text)` 工具，标 `DISCOVERY_SAFE`（只读、无 mutation）；
2. 落地写 `run_notes` 表（run_id + 时间戳 + text），不进主 context（避免污染 token）；
3. `Completion Gate` 的 verification 判定**不依赖** think（think 不算 evidence，只算过程记录）；
4. 测试：think 被调后不触发 mutation 三态、不进 obligation ledger。

**权衡**：纯增益项、可独立回滚（去掉工具即可）。建议**最先做**，作为 P0 三件套里最安全的一块。

---

### 2.4 P1-A　Prompt Caching（省钱项 · 取决于网关能力）

**现状核查**：`agent.py` 人设 + 41 工具 schema 每轮重发；全文无 prompt cache 配置；`provider_gateway.py` 未探测 `prompt_cache_options`。

**完善方案**：
1. 先做**网关能力探测**（`AGNES_BASE_URL` 是否支持 OpenAI 兼容 cache）；
2. 支持 → 直接吃收益；不支持 → 至少做**前缀顺序固化**（人设 → 工具 schema → 技能片段，每轮完全一致）+ `tool_router` 裁剪子集按固定次序排列，减少前缀 diff；
3. 纯省钱/省时延项，**不改任何语义**。

---

### 2.5 P1-B　effort / reasoning 预算透出（治「规则过多」的正确解法）

**现状核查**：`provider_gateway.py` 无 effort / reasoning budget / temperature 策略化字段。

**关键判断（与 9-19 评审一致）**：FORGE 当前真正的风险是**「规则太多」不是「规则太少」**——9-19 评审引前沿警告：系统提示与 schema 合计超 ~150~200 条指令后，即便 frontier 模型规则遵循率也下降。FORGE 的 11 道门 + 7 类 verdict + 9 类 Clarification Gate 已逼近这条曲线。

**完善方案**：
1. 网关能力探测（是否支持 `reasoning_effort` / `thinking_budget`）；
2. 支持 → 接到现有 duty routing：简单问答（T001/T002 类）走低 effort，涉及 mutation/verification 义务（T018/T023 类）走高 effort；
3. **同步给 11 道门做减法**：把 `FORGE_REDUNDANT_GUARD` / `FORGE_COMPLETION_READY` 的默认 off 项、或 `FORGE_VERIFICATION_FOCUS` off 项，按「高频且无害」原则合并，减少规则总条数。

**权衡**：这是「行为层已收敛，下一步该收敛规则复杂度」的延伸项——与本轮行为层整改同向。

---

### 2.6 P1-C　Tool Router 语义化（bge 向量一级召回）

**现状核查**：`runtime/tool_router.py`（46KB）是纯关键词/别名表规则。`rag.py` 已用本地 bge-small-zh-v1.5 ONNX（BM25 + 向量混合检索），但**只服务 RAG 问答，没接进 router**。

**完善方案**：
1. 复用 `rag.py` 的 `OnnxEmbedEngine` 做 router 一级召回（query → 候选工具向量相似度）；
2. 现有关键词规则**降级为硬约束兜底**（点名必选 + 基础集永不裁）；
3. 规则表不再随工具数量线性膨胀（当前 41 工具的别名表维护成本已高）。

**权衡**：工程量大（向量索引 + 离线训练/选词），且需验证 bge 对「查航班/改代码/记笔记」这类意图的召回精度。建议 P0 三件做完后再做。

---

### 2.7 评测→策略回流闭环（让系统随时间变好）

**现状核查**：50 case 跑完只产 `report_*.json`，**断在最后一跳**——没有「失败 case 的 `terminal.kind` 分布 → 自动映射回策略调整（哪些该调 Router/审批名单/提示词）」的回路。

**完善方案**（与 episode 层呼应）：
1. 评测后自动跑 `episode_store.ingest_pending()` 把成功/失败 traj 落 episode；
2. 出「terminal.kind 分布报告」：approval_bypass 多 → 收紧审批名单；verification_failed 多 → 调 Coding 门槛；no_progress 多 → 调 convergence 阈值；
3. 把 episode 注入（P0-A）与回流（本项）接成「跑一批 → 落教训 → 下一批开跑前拉教训」的闭环。

**权衡**：这是把「审计废料变资产」的最后一环，与 P0-3 天然耦合。

---

## 3. 明确不建议跟风的事（与 9-19 评审一致，本轮复核仍成立）

| 潮流 | 为什么在 FORGE 这里不合适 |
|---|---|
| 常驻 multi-agent swarm | token ~15×；FORGE 所有治理收益建立在「单一主链 + 单一 Run」，拆开后 Single Active Run / FileScope / Approval / Write-Ahead 所有权全部失效 |
| Neo4j 记忆图库 | `memories` 表 0 行；SQLite + 关系表（`episodes` 已有）足够，图库只增运维成本不增能力 |
| 引入 LangGraph / CrewAI 编排框架 | 与已冻结的 `runtime/state_machine.py` 形成**两套状态机**，违反「一个真相源」原则 |
| 把 Completion Gate 换成 LLM-as-judge | FORGE 最值钱的就是「判定不依赖模型」（ExecutionEvidence + 确定性 verdict）；换掉等于自毁最强防线 |

---

## 4. 优先级路线图（建议）

| 优先级 | 项 | 成本 | 收益 | 依赖 | 建议 |
|---|---|---|---|---|---|
| **P0-C** | think/scratchpad 工具 | 零 | 高（54% 提升引用） | 无 | **最先做**（最安全、可独立回滚） |
| **P0-A** | episode 接进 run_turn 主链 | 中 | 高（让 26k 事件变资产） | P0-C 可并行 | **第二做**（地基已打好，只差接线） |
| **P0-B-1** | 工具消息裁剪（上下文工程层 1） | 低 | 中（消除工具输出堆叠） | 无 | **可最先做**（纯裁剪、零模型调用、最安全） |
| **P0-B-2** | 压缩指令 + 8 节 state_snapshot（层 2） | 中 | 高（长会话可压缩续跑） | 层 1 | 接层 1 后做 |
| **P0-B-3** | 子任务委托 / 上下文隔离（层 3） | 高 | 高（消除上下文永久污染） | 需用户拍板 2 个决策 + 沙箱等级确认 | 等层 1+2 见效后再上 |
| P1-B | effort/reasoning 预算 + 门减法 | 中 | 中（治「规则过多」） | 网关探测 | 行为层整改的自然延伸 |
| P1-C | Tool Router 语义化（bge） | 高 | 中（降低维护成本） | P0 完成 | 工具数 >45 时再做 |
| P1-A | prompt caching | 低 | 中（省钱） | 网关探测 | 纯增益项，随时可做 |
| 回流 | 评测→策略闭环 | 中 | 高（系统自我进化） | P0-A | 与 episode 层接成闭环 |

**如果只做一件事**：先做 **P0-C（think 工具）**——零风险、零依赖、独立可回滚；紧接着 **P0-A（episode 接主链）**——把已实现的 episode 骨架激活，这是「只搭一跳线，整个系统就开始积累经验」的升级点。两者做完，V 层（Context & Memory）这块最大洼地才真正补上。

---

## 5. 与本轮行为层收敛整改（9-21）的关系

本轮行为层整改（`FORGE-BEHAVIOR-LAYER-CONVERGENCE-REPORT.md`）已解决「**该不该执行 / 执行后是否收敛**」两层；本次架构审计定位的是下一层——「**脑子（think）与记忆回路（episode/上下文）是否到位**」：

| 层 | 本轮行为层整改 | 本次架构审计定位 |
|---|---|---|
| 该不该执行 | ✅ Clarification Gate 9 类 | — |
| 执行后是否收敛 | ✅ web_search 两级预算 + convergence TERMINALIZE + trace 补字段 + 行为断言 | — |
| 脑子 | ⏳ 空白 | **P0-C think 工具** |
| 记忆回路 | ⏳ episode 层已实现但没接主链 | **P0-A 接 run_turn + 回流闭环** |
| 上下文 | ⏳ 全挤一个会话 | **P0-B 子任务委托** |
| 模型能力 | ⏳ T023/T027/T050 coding 不收敛 | **P1-B effort 预算 + 门减法** |

---

---

## 7. 上下文模块参考基线：《Practical Guide to Context Engineering》

**位置**：`H:\GitHub源码文件\Practical-Guide-to-Context-Engineering-main\`（VitePress 文档站，已解压）。
本轮 P0-B 的三层设计直接采纳该库的成熟方案，关键引用如下：

| P0-B 层 | 参考库出处 | 采纳的设计 |
|---|---|---|
| 架构判断 | `docs/Agent形态/智能体系统构建策略-单智能体和多智能体.md` §四/§六 | 「先单智能体 + 上下文压缩，只在需要时加多智能体」「写入任务单智能体更优」「渐进式构建策略」 |
| 上下文失控分类 | `docs/上下文管理/上下文管理.md` §1.1~1.4 | 污染/干扰/混淆/冲突四类，FORGE 单主链 + 全量工具 schema 全中，是 P0-B 要治的根 |
| 上下文隔离 | 同上 §2.3 Context Quarantine | 子代理并行压缩 + 关注点分离 + 减少路径依赖；「子智能体间上下文隔离是致命短板」→ 隔离必须配摘要回传 |
| 工具消息裁剪 | `docs/上下文管理/上下文压缩指令...md` §三 | `compressContext` + `identifyToolRounds`（保留最近 N 轮工具、删旧调用）、工具返回 2000 字符截断、大文件写文件只回路径 |
| 压缩指令 | 同上 §一/§二 | ClaudeCode 8 节摘要 + 开篇语「上下文已压缩可无缝继续」；Gemini 5 节 `state_snapshot` + scratchpad 链式思考 |
| 压缩调度 | `docs/上下文管理/Token压缩策略.md` §二 | `max_tokens × 0.8` 触发；中间/最旧/混合三策略；效率 = 60% 减 token + 40% 留消息；保留最近 10 次压缩历史 |
| 会话存储 | `docs/会话存储模块/多后端存储设计-备份降级策略.md` | 多后端 + 读穿/写穿，FORGE 当前 SQLite 单后端，层 3 子会话可复用其隔离思路 |

**未采纳/不适用项**：
- 参考库的「多智能体 swarm / supervisor / hierarchical」架构——FORGE 单主链 + 11 道门已建立在「单一 Run」之上，拆 multi-agent 会让 Single Active Run / FileScope / Approval 所有权失效（§3 已列「明确不建议跟风」）。
- 参考库 TS 版 `ContextManager` 完整实现——FORGE 是 Python，按层 1→2→3 逐层重写，不整库搬运。

---

## 8. 遗留未知区（诚实标注，未做全覆盖判定）

1. **网关能力**（prompt cache / reasoning_effort / thinking_budget 是否支持）——需跑一次能力探测才能定 P1-A/P1-B 的落地形态，未探测。
2. **episode 层实际命中率**——`fingerprint(goal)` 是基于 goal 文本的哈希桶，命中率随任务多样性变化，未做 N 次评测统计。
3. **`FORGE_REDUNDANT_GUARD=on` 对 T038 的净效果**——9-21 已验证为「对 T038 无净改善、T038 根因在模型节奏层（#18 同源）」，故 P1-B 的门减法不能简单靠开 redundant guard 解决 coding 不收敛。
4. **子任务委托的所有权模型**（P0-B-3）——子会话的 mutation 如何在主 Run 的 FileScope/审批域内记账，需细设计；其前置依赖（层 1 工具消息裁剪 + 层 2 压缩指令）可独立先做，不阻塞层 3 的所有权决策。
5. **`decision_checkpoint` benchmark 入口仍绕 run_turn**（9-19 Phase1 坐实，`BENCHMARK_NOT_PRODUCTION_PARITY`）——本轮未改（审计口径），但会影响后续「评测结论外推生产」的可信度。

---

## 附：核查证据索引

| 结论 | 源码/文档证据 |
|---|---|
| episode 层已实现 | `runtime/episode_store.py`（episodes 表 + fingerprint + recall + copy_from）/ `runtime/episode_recall.py`（EPISODE_RECALL 开关 + build_context） |
| episode 未接主链 | grep `episode_recall\|inject_episode` 在 `runtime/runner.py`/`main.py`/`cli/app.py` = 0 命中 |
| 无 think 工具 | grep `"think"\|think_text\|scratchpad` 在 `tools.py`/`agent*` = 0；`public_activity.py:38` 仅剥离 `</think>` |
| 无子任务委托 | grep `sub_run\|child_session\|task_delegation\|handoff` 在 `runtime/`/`main.py` = 仅 `public_response.py` 占位 |
| 无 prompt cache | grep `prompt_cache\|cache_control` 在 `provider_gateway.py`/`agent.py` = 0 |
| 无 effort 预算 | grep `reasoning_effort\|effort_budget` 在 `provider_gateway.py`/`budget.py` = 0 |
| router 纯关键词 | `tool_router.py` 46KB 关键词/别名表；bge ONNX 只在 `rag.py` |
| 行为层已收敛 | `FORGE-BEHAVIOR-LAYER-CONVERGENCE-REPORT.md` §4/§5/§7（web_search 两级 + Clarification Gate 9 类 + trace 补字段） |
| P0-B 上下文工程参考基线 | `H:\GitHub源码文件\Practical-Guide-to-Context-Engineering-main\`（46 md / ~16k 行）：`上下文管理.md`（污染/干扰/混淆/冲突 + Context Quarantine）/ `上下文压缩指令...md`（ClaudeCode 8 节 + Gemini state_snapshot + compressContext）/ `Token压缩策略.md`（三策略 + 0.8 阈值 + 效率公式）/ `智能体系统构建策略...md`（先单后多） |
