# 架构评审：my_creative_agent vs 2026 前沿 Agent 架构

日期：2026-09-19
基线：本仓库当前代码（顶层 11.6k 行 / runtime 14.3k 行 / cli 2.9k 行 / tests 101 文件 20k 行）
运行时依赖：OpenAI Agents SDK + 第三方 OpenAI 兼容网关（chat/completions）

---

## 一、先定性：这是一个「Harness 型」项目，不是「Agent 型」项目

2026 年业界的共识是：模型不再是产品差异的主体，**围绕模型的 harness（执行外壳）才是**。
你的项目恰恰是把绝大部分工程量放在了 harness 上——工具门禁链、证据账本、收口门、审计、
可恢复状态机——而把「脑子」整个交给了模型。这个分工是对的，且是当前主流方向。

但它的形态是 **单 Agent + 单主链 + 极厚外围治理**。这一选择在当下语境里的准确定位：
它规避了多智能体的大部分成本（token、调试、横向注入），代价是**失去了上下文隔离**。

---

## 二、你已经做对的事（这些不要动）

| 你的做法 | 前沿对应概念 | 评价 |
|---|---|---|
| Execution Evidence 执行账本 + Completion Gate 默认拒绝收口 | Generator / Evaluator 分离 + default-FAIL contract | **领先**。别人用第二个模型当 Evaluator（有自评偏差 + 双倍成本），你用确定性规则 + 真实调用账本，零额外 token 且不可被模型说服 |
| 6 类 GateVerdict + 有界 repair 回路 | Replan / Reflexion 的结构化替代 | 规避了 Reflexion 的自评偏差：验证标准是「有没有执行证据」，不是「模型觉得自己对不对」 |
| 双预算闸（墙钟 + token）+ 幂等键 + Write-Ahead 台账 | Durable execution / cost controls | 已具备，且落地在 DB / store 层而非 prompt 层 |
| Session compact + 硬窗口兜底 | Context compaction / context editing | 已具备（Anthropic 内部评测中 context editing 一项带来约 29% 提升，二手引述） |
| Tool Router（41→8~16）+ Model Router | Routing optimization | 已具备，但见下方 P1-3 |
| run_id = trace_id + OTel JSONL + 50 case 评测集 | Observability / Eval loop | 已具备，缺的是「评测结果回流到策略」这一段 |

一句话：**你在「可信收口」这条线上做得比大多数 2026 年的生产 harness 都扎实，问题不在治理，在上下文与记忆。**

---

## 三、真正的差距（按性价比排序）

### P0-1　上下文隔离 / 临时子任务委托　（最高性价比）

- **现状**：全仓 grep 无 handoff、无 sub-agent、无 Planner（`completion.py:397`、`runctx.py:302`
  注释里明确写了「不新建 Planner」）。一次 `deep_research`（一轮约 6 次搜索 + 3 次模型调用）的
  全部检索碎片都塞进同一个 `sessions.sqlite` 会话，永久污染后续所有对话的上下文。
- **前沿做法**：lead agent 把重活委托给独立上下文窗口的子任务，只把压缩后的结论带回主链。
  二手引述：Anthropic 内部 research 场景 lead+subagent 相对单 agent 提升约 90.2%，但 token 消耗约 15 倍。
- **建议**：不要上「常驻多智能体」，只做**按需隔离会话**。复用现有设施（`get_or_create_container`
  + 独立 `SQLiteSession`），把 `deep_research`、`code_loop`、`compact 摘要`、` --daemon 定时任务`
  这几类明确无副作用的重活放进临时 session，回传 ≤2k token 摘要；evidence 仍走既有的
  `rctx.note_execution_identity` 登记路径，这样 Completion Gate 一行都不用改。
- **注意**：如果把委托做成模型可见的工具让它自主调用，Single Active Run 约束、FileScope、
  Approval 会出现所有权模糊——所以委托入口应该硬编码在 `runtime` 层的少数几个已知位置，
  不要把「开子任务」暴露成工具。

### P0-2　scratchpad / think 工具　（零成本）

- **现状**：全仓 grep 无 `think` 工具、无 scratchpad。模型在两个工具调用之间没有「可以写字的地板」。
- **前沿做法**：Anthropic 的 think 工具是一个 no-op 占位工具，二手引述 τ-bench airline 上有 54% 的相对提升。
- **建议**：加一个零副作用工具 `think(text)`，只写 Run 级 scratchpad（落 `tool_calls` 或一个
  `run_notes` 表），不进 main model context 或只进最后一条。你的 `runctx.decision_hint()`
  已经是「运行时→模型」的单向事实通道了，缺的就是「模型→自己」的这一半。

### P0-3　把 MemoryGraph 的钱花在 episode 层上

- **现状**：`task_events` 26334 行、`tool_calls` 6541 行、`model_calls` 4246 行在库里躺着，
  从未被用来回答「这类任务上次是怎么成功的」。长期记忆只有一张 `memories` 表，**当前 0 行**，
  `recall_memory` 是关键词召回。前沿 2026 的 5 层记忆层级（in-context / scratchpad / episodic /
  semantic / procedural），你实际只有第 1 层和弱的第 4、5 层，**第 3 层（episodic）完全空白**。
- **建议**：不要上 Neo4j 图库——个人助手这个数据量不值当。用现有 SQLite：按
  `request fingerprint（意图类别）+ outcome（completed/failed + terminal.kind）` 建一张
  episode 索引，同类任务开跑前注入一条「上次成功用了 X 工具、Y 轮，失败过 Z」的结构化事实。
  这一步是把你已有的 26k 事件从「审计废料」变成「可用资产」的唯一桥梁。

### P1-1　prompt caching

- **现状**：`agent.py` 的人设 + 41 个工具的 schema 每轮重发；全文无 prompt cache 配置。
- **建议**：网关若支持 OpenAI 兼容的 cache，就直接吃收益；不支持时也应把**前缀顺序固化**
  （人设 → 工具 schema → 技能片段，顺序每次完全一致），并把 `tool_router` 裁剪后的子集按固定
  次序排列，减少无意义的前缀 diff。纯省钱/省时延项，不改任何语义。

### P1-2　effort / reasoning 预算透出

- **现状**：`provider_gateway.py` 里没有 effort / reasoning budget 相关字段，
  也没有 temperature 等 model settings 的策略化。
- **建议**：先做一次网关能力探测（是否支持 effort / reasoning budget 等参数），再把抉择接到
  现有的 duty routing 上：简单问答走低 effort，涉及 mutation / verification 义务的任务走高 effort。
  这比继续堆人设有效得多——前沿明确警告：系统提示与 schema 合计超过约 150~200 条指令后，
  即便 frontier 模型的规则遵循率也会下降。**你现在真正的风险是「规则太多」，不是「规则太少」。**

### P1-3　Tool Router 语义化

- **现状**：`runtime/tool_router.py` 46KB 纯关键词 / 别名表规则。
- **建议**：用你本地已有的 bge-small-zh ONNX 向量（`rag.py` 已经在用）做一级召回，
  现有关键词规则退化为「点名必选 + 基础集永不裁」的硬约束兜底。规则表不再随工具数量线性膨胀。

### P2　可以不急

- `run_python` 沙箱目前是进程级（路径围栏 + 密钥剔除 + 超时强杀），非 OS/容器级；
  冻结报告里已诚实标注为「已接受的残余限制」。个人本地场景可接受，公开部署前必须解决。
- 评测 → 自改进闭环：现在 50 case 跑完只产出一份报告，断在了最后一步。下一步把失败 case 的
  `terminal.kind` 分布自动映射回策略调整（哪些>✔该调 Router、哪些该调审批名单、哪些该改提示），
  这才是真正意义上「系统随时间变好」的那一段。

---

## 四、明确不建议跟风的事

| 潮流 | 为什么在你这里不合适 |
|---|---|
| 常驻 multi-agent swarm | token 约 15 倍；你的所有治理收益都建立在「单一主链 + 单一 Run」上，拆开后 Single Active Run、FileScope、Approval、Write-Ahead 的所有权全部失效 |
| Neo4j 记忆图库 | `memories` 表现在 0 行。SQLite + 关系表足够，图库带来的是运维成本不是能力 |
| 引入 LangGraph / CrewAI 等编排框架 | 会和你已冻结的 `runtime/state_machine.py` 形成两套状态机，直接违反「一个真相源」原则 |
| 把 Completion Gate 换成 LLM-as-judge | 你现在最值钱的东西就是「判定不依赖模型」，换掉等于自毁最强的那条防线 |

---

## 五、如果只做一件事

做 **P0-3 episode 记忆层**。理由：你已经持续落盘了每一次成功与失败的完整事实
（`task_events` 26334 行 / `tool_calls` 6541 行），唯独缺少一条把它们送回模型的回路。
这是目前唯一一处「只搭一跳线，整个系统就开始积累经验」的升级，且完全不碰冻结的 Runtime 主链。

它的最小实现路径（约 3 个文件、不动 `runner.py` 主链）：

```text
① task_manager.py 增 episode 视图：按 run_id 聚合 traj
   （request 指纹 / 工具序列 / 轮数 / 终态 kind / 是否 produced artifact）
② 新增 runtime/episode_recall.py：同类任务开跑前命中历史 → 返回一条结构化事实
③ context.py Session Preparation 里注入（复用既有 source-injection 信任边界，标为 data 非指令）
```

验收方式（沿用你现有的评测口径）：
先跑 50 case 基线 `--all`，注入开启后再跑一轮对比，指标看
Behavior Pass Rate 是否上升、**Safety Violation Rate 必须为 0**（新增记忆注入不得绕过任何一道闸）。
