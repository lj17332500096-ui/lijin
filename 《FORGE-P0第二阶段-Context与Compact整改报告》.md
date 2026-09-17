# 《FORGE-P0 第二阶段 - Context 与 Compact 整改报告》

> 项目：`F:\Byong-hermes\Byong-hermes\my_creative_agent`（FORGE）
> 日期：2026-09-06 · 事实基线：《FORGE-Agent-Runtime完整审计报告.md》+ 第一阶段整改代码
> 阶段范围：Session History / Context Budget / Compact 接入 Web 主链 / History Window / 长对话回归。
> 未触碰（按阶段纪律）：Completion Gate、Approval、Permission、WorkLocation、Memory Scope 本体、Model Router、Sources RAG、MCP、ToolBroker、UI 架构、Agent Prompt、并发 Runtime。
> 第一阶段完成性验证：本阶段所有真实运行都继续经过 Completion Gate（E2E 中 `completion.check.started/passed` 均出现，run.completed 语义未变）。

---

## 1. 原 Web Session 调用链（修改前）

```
/api/projects/{pid}/stream → api_stream(resume_task) → AgentRuntime.run_turn
  → execute_turn → SDK Runner.run_streamed(session=SQLiteSession(container_session_key))
  → SDK prepare_input_with_session：每轮 get_items() 全量读回该容器 session 全部历史
```

- session key：Project/Task 容器自己的 `tasks.session_id`（`proj-*`/`sess-*`/`personal`）；
- `run_turn` 的 `history_limit` 默认 `None` → SDK 全量；
- tool call / tool result / model 中间 items 都作为 SDK items 落 `sessions.sqlite.agent_messages`；
- 实测库内：单会话最高 1547 条 SDK 历史（personal），多个 `proj-*` 会话 160/95/75/…条；
- 无任何窗口、无 compact、无 token/字符级护栏 → 每轮 input 随历史线性增长，最终网关 400。

## 2. 原无限增长根因

1. compact.py 唯一真实调用点在 CLI/语音的**事后**钩子（`main.py _auto_compact`），Web/Project/定时链路从未调用；
2. SDK SQLiteSession 非 compaction-aware，项目侧没有 pre-run 收敛；
3. 没有“读历史之前先看规模”的入口——每次都是先全量构造 items 再谈其他。

## 3. compact.py 原调用范围（修改前）

- `main.py:317 _auto_compact`（每轮后调用 `compact.maybe_compact`）→ 仅 chat_async(557) 与 chat_voice(762)；
- Web（webapp.py 全文件）与定时链路：零调用；
- `maybe_compact` 单发摘要：把“保留 KEEP 轮之前的全部旧消息”一次塞进一个 transcript（cap=TRANSCRIPT_CAP，超出即截断较早部分）→ 对 1000+ 条巨型历史会截断且只摘要最老一段，中间信息丢失。

## 4. 新 Web Compact 接线位置

**Runtime 层（run_turn 内、execute 之前）**——Web / CLI / Voice / 定时（有 session 时）全部生效：

```
runner.run_turn(...)
  ├─ 建 Run / 转 RUNNING / 写 user Message（agent.db）
  ├─ Project Context / 附件清单组装（不变）
  ├─ ★ Session Preparation（新增，runtime/context.py::prepare_session_context）
  │    1. quick_stats：直接 SQL 数行数 + 估字节 + 近似用户轮数（零模型成本）
  │    2. 未达阈值 → 只记一条 context.metrics，放行（10 轮短对话零模型调用）
  │    3. 达到 Soft Limit（AUTO_SUMMARY_*）→ compact.compact_history（分块摘要）
  │    4. 未成功/未触发但超 Hard Limit（FORGE_HISTORY_HARD_*）→ window_history 硬窗口
  │    5. 事件/指标：context.compaction.* / context.history_windowed / context.metrics
  └─ execute_turn（SDK 在本轮开始时才把 Current User Message 写入 Session → 永不被本次压缩）
```

- webapp.py **零业务逻辑改动**（只经由既有 run_turn 链路自动获得护栏）；
- CLI/语音复用同一入口；`--no-auto-summary` 通过 `run_turn(context_guard=…)` 仍可关闭；
- 禁止另起第二套 compact：分块/合并全部复用 compact.py 的 `_build_transcript / summarize_transcript / _summary_item / _atomic_replace / _append_summary_file` 与 `AUTO_SUMMARY_*` 阈值。

## 5. Context Budget 算法

三层保护（不用单一指标）：

| 层 | 判定 | 配置（默认） | 动作 |
|---|---|---|---|
| 快路径 | SQL 行数/字节/近似轮数 | — | 都低于阈值 → 零成本放行（只记 metrics） |
| Soft Limit | 精确用户轮数 ≥ TRIGGER_TURNS **或** 精确字符 ≥ TRIGGER_CHARS（且 ≥ MIN_TURNS、> KEEP_TURNS） | `AUTO_SUMMARY_TRIGGER_TURNS`(60) / `AUTO_SUMMARY_TRIGGER_CHARS`(180000) / `AUTO_SUMMARY_MIN_TURNS`(6) | 调用 compact_history 分块摘要（hysteresis：落地到 KEEP_TURNS(20) 轮，不会每轮重复摘要） |
| Hard Limit | 精确字符 ≥ HARD_CHARS **或** 条数 ≥ HARD_MESSAGES（compact 后仍超也查） | `FORGE_HISTORY_HARD_CHARS`(260000，自动 ≥ 软字符阈值) / `FORGE_HISTORY_HARD_MESSAGES`(500) | 硬窗口（不依赖模型） |

- “Token 预算”以字符粗估实现（与 compact 既有 `_item_rough_chars` 同口径，CJK≈1 字符≈1 token 的保守估算），事件里同时给出 chars 与 est_tokens；
- 预留说明：System/Tools/Project Context/当前消息/输出所需空间不占用 History 预算——软/硬阈值即 History 自己的天花板；固定基线（人设+工具 schema）见 §18 测量。

## 6. Soft / Hard / Target 阈值（实际值）

- Soft：全部复用 `AUTO_SUMMARY_*`（默认 60 轮 / 180,000 字符；摘要后目标 ≈ KEEP 20 轮原文 + 一条摘要）；
- Hard：`FORGE_HISTORY_HARD_CHARS` 默认 260,000（自动不小于软字符阈值）、`FORGE_HISTORY_HARD_MESSAGES` 默认 500；
- Target：窗口/摘要后的目标 = `AUTO_SUMMARY_KEEP_TURNS`（默认 20 轮原文）；
- 总新增配置只有 2 个数值 + 1 个开关（`FORGE_HISTORY_HARD_CHARS / _MESSAGES / FORGE_CONTEXT_GUARD`），已写入 .env.example 与 README。

## 7. History Window 算法

`runtime/context.py::window_history(items, keep_turns, max_chars, max_messages)`（纯函数）：

1. 找全部用户消息起点；
2. 头部保留：第一个用户消息之前出现的旧摘要 system 行（少量）；
3. 候选窗口 = 最近 keep_turns 个用户轮次；在预算内**尽量保留更多轮**（从最早的候选起点试起）；
4. 仍超预算则丢弃最旧轮次继续收紧；极端情况只保留最近一轮（并限条数）。

## 8. Tool Call / Result 完整性处理

- 切割点**只允许出现在用户消息起点**：被丢的是一整轮（该轮内 tool_call↔tool_result 一并丢弃），保留的轮次内成对完整；
- Compact 的 transcript 只取 user/assistant/system-摘要行，工具行不进入摘要材料（与既有 compact 语义一致；工具执行结果的重要结论已由 assistant 正文携带）；
- 离线测试直接断言窗口后 `function_call` 与 `function_call_output` 数量相等且 call_id 集合一致（§38 用例）。

## 9. Summary 数据结构

沿用 compact.py 既有结构（不造第三种数据源）：
- 单条 `system` 消息：`[此前对话的自动摘要 · <时间>]\n<正文>`（`compact._SUMMARY_MARK`）；
- 会话头部为最新一条摘要；随后是最近 KEEP 轮原文；
- 摘要正文来自既有 `_SUMMARY_SYSTEM` 提示词（保留目标/决定/偏好/文件产出/待办；省略寒暄/过程/stdout/重复错误栈）。

## 10. Summary 持久化

- 主存储 = **SDK Session 本身**（clear + add summary+tail 原子替换，失败回滚原历史）——重启后重开同一 Session 即读到摘要态，不会恢复旧全量（E2E/测试双证）；
- 追加落盘 `summaries/<session_id>.md`（既有机制，继续用于人工回溯）；
- 没有新建任何“第三套对话数据库”。

## 11. Summary 最大尺寸

- 摘要正文 ≤ `SUMMARY_MAX_CHARS`=1200（`summarize_transcript` 与 `_merge_summaries` 双边界）；
- 多轮 compact 会“旧摘要+新旧轮次 → 重新压成一条新摘要”，绝不 append 累积；
- 测试：5 个周期后 summary ≤ 3000 且会话内只存在 1 条头部摘要（§43 用例）。

## 12. Compact 失败降级

- `compact_history` 任一步异常 → 返回 `(None, "failed")`，Session 原样保留（回滚）；
- `prepare_session_context` 记录 `context.compaction.failed` 后继续（不中断 Run）；
- 若此时历史仍超 Hard Limit → 自动切 Hard Window（不依赖模型）→ 本轮照常执行；
- 摘要失败**绝不**导致 run.failed（用户只可能看到极少数 `tool: 上下文整理` 帧或完全无感）。

## 13. 旧超长 Session 迁移策略

- 预置 2400+ items（≈600 轮）的离线实测：`TRANSCRIPT_CAP`(默认 150k) 内自动**分块**，每块单独摘要，最后**分层合并**成一条有界总摘要；调用次数 >2（多块+合并），会话收敛到 [摘要 + KEEP 轮]（§37 用例；测试用 20000 字符小 cap 强制多块）；
- 无需删项目/清历史/建新项目：第一次发消息即自动完成迁移；
- 每块内单条消息仍按 `PER_MSG_CAP`(1600) 截断后再入摘要材料，超大 stdout 不会原样进模型。

## 14. Attachment 行为

- 附件上下文注入与 Session 历史无关（`attachments_for_run(run_id)` → 指令块），compact 前后行为一致；
- 离线 run_turn 集成测试：先触发 compact，再绑定附件执行“分析这个文件。”→ fake 捕获到的 agent instructions 仍含“本次消息附件 … test.xlsx”（§39 用例）；
- Current Message / 其附件永远在本轮 Run 开始后才由 SDK 写入 Session，不可能被本次压缩。

## 15. Project Context 行为

- Project 说明 / 来源清单 / 项目记忆 / WorkLocation 由 `_project_context_block` **每轮动态注入**（不属于 Conversation History）；
- Compact 只吃 Session 历史 → 不会把这些动态块写进 Summary；
- 测试：捕获摘要 transcript 全文断言不含“【当前项目/项目说明/来源文件/工作位置/记忆范围”等标记（§42 用例），多次 compact 后会话中无 ctx 副本。

## 16. Memory Scope 行为

- Compact 输入只取 Conversation History（模型历史回复中自然出现的记忆派生内容不逆向删除）；
- **不主动把 Global/Project Memory 作为独立输入加入摘要** → project_only/global 切换不会通过 Summary 造成越界固化；
- 测试：摘要 transcript 不含记忆标记（`TEST_GLOBAL_LEAK_MARKER` 等）（§40-41 用例）；本阶段未触碰记忆绑定/读写代码。

## 17. Restart 行为

- 离线测试：compact 后 `session.close()` 再以同一 db 重开（模拟重启）→ items 仍是 [摘要+≤KEEP 轮]，不回弹（§46 用例）；
- 真实 E2E：compact 已持久化进 sessions.sqlite（重启后端即同状态；本阶段用重开 Session 验证持久性，未整机重启长驻 8765 实例）。

## 18. Context Metrics（记录内容与位置）

每个带 Session 的 Run 都会在 task_events 留下（普通对话仅 1 条 `context.metrics`）：

| 字段 | 含义 |
|---|---|
| action | none / compacted / compact_failed / windowed / window_failed |
| reason | turns / chars / auto_summary / hard_window … |
| messages/chars/user_turns_before、after | compact/窗口前后规模 |
| est_tokens_before/after | 粗略 token（≈字符） |
| summary_chars | 摘要正文长度 |
| hard_window_applied | 是否触发硬窗口 |

另：运行过程帧（SSE `tool: 上下文整理`）只在发生 compact/window 时出现；普通对话 UI 不展示任何 token/条数信息。最终 provider input_tokens 照旧在 model_calls/usage 里。

**固定基线测量（§49，不做 Prompt/ToolRouter 改动）**：assistant_agent.instructions ≈ **32,352 字符**；36 个工具 JSON schema ≈ **9,058 字符**（均值 251/工具；ToolRouter ≤16 个时 schema 侧约 4k）。审计实测“1+1”单轮 input=13,204 tokens 与上述字符量同量级（中英混合 tokenization 折损约 0.4–0.5 字符/token），即每轮固定基线主要由人设+技能块构成——已在报告 §“未决问题”中记为后续 P1/P2（不扩大本阶段范围）。

## 19. 修改文件

| 文件 | 改动 |
|---|---|
| `runtime/context.py` | **新增**：quick_stats / precise_stats / window_history / prepare_session_context / 硬阈值配置 / 事件常量 |
| `compact.py` | `maybe_compact` 重构为 `compact_history` 薄封装；新增分块（`_split_transcript_chunks`）、分层合并（`_merge_summaries`）、原子替换助手（`_atomic_replace`）；对外行为与既有测试语义不变 |
| `runtime/runner.py` | `run_turn` 增加 `context_guard` 参数；execute 前调用 `prepare_session_context`，进度经 `_context_progress` 写 task_events + SSE 帧 |
| `main.py` | chat/voice 的 run_turn 传 `context_guard=auto_summary`（`--no-auto-summary` 语义保留） |
| `.env.example` / `README.md` | 配置文档与长会话摘要说明更新（统一 Web/CLI/Voice 语义） |
| `tests/test_context_guard.py` | **新增** 14 项离线测试（见 §20） |

## 20. 新增测试（tests/test_context_guard.py，14 项）

| 任务书 | 用例 | 断言 |
|---|---|---|
| §34 | 短对话零模型调用/不触发 | action=none、summarize 零调用、completed |
| §35 | Soft 触发（prepare 层与 run_turn 层） | action=compacted、摘要保存、recent=KEEP、事件齐全、run completed |
| §36 | 摘要失败 + 超硬阈值 | compact_failed+windowed 事件、窗口后变小、本轮不 failed |
| §37/29 | 2400 items 分块迁移 | summarize 调用 >2、收敛 <100 条、est < cap |
| §38 | Tool Pair 完整性 | call/result 数量一致、call_id 集合一致、窗口边界干净 |
| §39 | compact 后附件仍注入 | instructions 含“本次消息附件/test.xlsx” |
| §40-42/15-16 | 摘要不含 Project Context/记忆 | transcript 无 ctx/记忆标记；5 周期后仅 1 条摘要 |
| §43 | Summary Growth | 5 周期 size ≤3000、单条摘要 |
| §44 | Token 曲线 60 轮 | compact≥3、后期 max<25k、总体有界 |
| §46 | Restart | 重开 Session 不恢复旧历史 |

## 21. 原 320 项回归结果

`tests/run_tests.py` 全量真实运行：**Ran 354 tests（原 340 + 新 14），OK**；原有用例零新增失败（含 Completion Gate/Approval/Memory/Attachments/Sources/工具安全/Guardrails/SSE 全部保持绿色）。

## 22. Web E2E 结果（真实网关 + 当前代码 + 独立 8799 实例，测试后已关闭）

阈值实例环境：`AUTO_SUMMARY_MIN_TURNS=3 / TRIGGER_TURNS=8 / KEEP_TURNS=3 / TRIGGER_CHARS=80000`。

| 步骤 | 观测 |
|---|---|
| 建 Project（tk_92208967，session proj-8298ce81），预置 16 个用户轮次（32 items） | 库内 32 条 SDK 历史 |
| 真实消息 `POST /api/projects/{pid}/stream`（总结项目进度） | SSE run.started/reply/run.completed 正常；无 run.failed |
| task_events | `context.compaction.started`(reason=turns, 32→…) → `context.compaction.completed`(messages 32→7, users 16→3, summary_chars=68) → `context.metrics`；另 `completion.check.started/passed`（第一阶段能力未破坏） |
| compact 后 Session 实况 | items=22（=摘要+保留 3 轮+本轮新增）、头部为 system 摘要、users=4 |
| 第二条真实消息 | 不再 compact：仅 1 条 `context.metrics`(action=none, 22 条稳定)；run completed |

## 23. 50–100 Turn token 曲线（离线实测 60 轮，§44）

阈值：trigger=12 turns / keep=4；曲线 = 每轮 `prepare` 后会话 est_tokens（≈字符）：

```
est@10: 2,275 → 触发(step12) → est≈994 → est@20: 994 → est@30: 1,458 → … 周期回落
compactions: 7 次；每次 compact 后 est 稳定 ≈ 994（=KEEP 4 轮原文）
max(est) 全程 = 2,618；step 51-60 min/max = 994 / 2,618
```

结论：**锯齿型有界曲线**（增长→阈值→回落→再增长），全程无线性累积；上限远低于 Hard 阈值（80,000）。

## 24. 1000+ History 测试（离线，§37）

- 2400 items（600 轮，含 tool 轮）单次 prepare：多块摘要（summarize 调用 >2）+ 合并 → 收敛到 [摘要+4 轮]（<100 条，est < 20,000）；无 OOM、无截断丢中间（块遍历覆盖全量旧历史）；
- Hard 兜底场景（摘要模型抛错 + 超硬阈值）：240 轮×4 items → compact_failed → windowed（960→约 16-20 条量级），本轮不失败。

## 25. 尚未解决问题（如实记录）

1. `summarize_transcript` 为同步阻塞调用（既有 compact 模式），compact 期间会短暂占用事件循环（数秒级）——Web 并发请求场景列入并发 P1 处理。
2. 快路径的“近似用户轮数”用 SQL LIKE 估算（`"role": "user"`），极端内容可能误匹配；只影响“是否进入精确判定”，不会误删历史。
3. Summary 由网关模型生成，质量受模型波动影响；失败已有硬窗口兜底，但“窗口降级”会丢弃超过 KEEP 轮的最旧原文（属设计内最终兜底，事件中 hard_window_applied=true 可查）。
4. 固定基线（人设 ~32k 字符）本身偏大——已测量记录，建议后续（P1/P2，不动 Prompt 的专项）压缩技能/人设或按需注入。
5. Legacy `/chat` 内联执行链（webapp 直跑分支）不经过 run_turn → 不享受 Session Preparation——与第一阶段同款遗留，收敛到 Phase 5“web 双链统一”一并解决。
6. 8765 长驻实例仍是旧代码，需重启后端后本阶段行为才在其生效（E2E 均在独立实例完成）。

---

## 最终回答（阶段验收）

1. **Web / Project 是否还会每轮全量读取无限历史？** —— **不会**。Run 前 Session Preparation 先按 SQL 快路径判定；超 Soft 阈值进分块 compact，摘要失败或超 Hard 阈值进硬窗口；历史被收敛为 [摘要 + 最近 KEEP 轮原文]，且重启不反弹。Web E2E（32 条→7 条）与 60 轮曲线实证。

2. **compact 是否已经真正进入 Web 主链？** —— **是**。`api_project_stream → run_turn → prepare_session_context → compact.compact_history`，真实网关 E2E 中 task_events 出现 `context.compaction.started/completed`，事件 payload 显示 32→7 收敛；CLI/Voice 复用同一链路。

3. **compact 模型失败时是否仍有硬保护？** —— **是**。`compact_history` 失败回滚且不中断 Run；`context.compaction.failed` 后若仍超 `FORGE_HISTORY_HARD_CHARS/_MESSAGES` 自动执行 `window_history` 硬窗口（纯 Runtime、不依赖模型）；离线测试（摘要抛错 + 超硬阈值）实证本轮不 failed。

4. **长 Project 能否持续使用而不线性增加 Context？** —— **能**。60 轮离线曲线 7 次 compact，est_tokens 全程 ≤2,618（锯齿有界）；E2E 连续两条真实消息均 completed 且第二条不再 compact。

5. **是否需要用户删除旧 Project 才能继续？** —— **不需要**。2400 items 级旧历史第一次发消息即自动分块迁移收敛；无需删除/清空/重建（§13/§37 用例）。
