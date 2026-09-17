# 《FORGE-Runtime 关键缺口复核报告-2026-09-07》

> 日期：2026-09-07 · 性质：Runtime 最终架构收口前的最后一次关键风险验证（Verification First，不重构、不新增抽象）
> 方法：真实代码调用链取证 + 全量离线回归 + 真实网关 LIVE E2E + 故障注入（Provider stub/HTTP 探针）+ DB 事件取证
> 基线：以《FORGE-Runtime 当前完成情况与真实架构审计报告》（2026-09-06）的评级为被复核对象
> 范围：my_creative_agent（FORGE）web / runtime / sources / skills / tests / 根模块
> 环境：webapp 于 01:24 以修复后代码重启（127.0.0.1:8765，uvicorn WARNING 级日志）；真实网关 agnes-2.5-flash 本次可用

---

## 0. 本阶段代码变更声明（诚实记录）

本阶段**未修改任何生产 Runtime 架构**，未新增模块/抽象。唯一生产代码改动发生在复核开始前的既有会话中，按命令规定在此单独标记：

- **AUDIT-REQUIRED FIX（本阶段唯一）**：`runtime/tool_router.py` gorden-ppt 触发词过窄导致 4 个 `gorden_ppt_*` 工具在常见说法（"简约商务总结汇报"、"做一份检验科基础培训的PPT"）下被裁出 16 工具窗口 → 技能指令在 system 中但工具不在模型工具列表 → 模型调用报 `Tool ... not found in agent`（调用失败，01:04 任务 task_838c33e7 实测记录）。修复 = 放宽触发词 + 2 条回归测试（tests/test_tool_router.py::test_natural_ppt_phrasing_keeps_gorden_tools、tests/test_skill_gorden_ppt.py 扩展断言）。属"明确 P0（技能工具不可达）+ 修复极小 + 不修无法继续验证"，改动已随本次复核一并验证（全量 453 通过）。
- 测试探针与 E2E 产生的临时容器（audit-probe-*、E2E 连续性验证）已在收尾时全部清除（agent.db + sessions.sqlite 联动），未留残留。

---

## 1. Executive Summary

**一句话回答：当前 FORGE Runtime 是否已达到可进行最终架构冻结/收口的条件？**

> **YES WITH CONDITIONS。**

真实主链（POST/GET 建 Run → run_turn → SDK Agent Loop → 统一工具包装 → Completion Gate → 双库落库 → SSE）单链稳定：**离线全量 453 项 OK（106s）；真实网关 LIVE E2E（TMR 验收场景）21/21 通过**——含三连消息=三个 Run 不重复、真实审批→批准→resume 同一 Run、SSE 中途断开→Run 正确取消且无悬挂、legacy 入口不崩。崩溃/并发/审批的内存态隔离（run-keyed）与终止判定（声明↔证据）经代码+测试双重成立。

但复核发现 **4 个必须收口前处理的生产级缺口（P0×1 倾向、P1×4 范围内，详见 §3/§12）**，且上一份报告的若干评级被复核为**偏乐观**（Provider streaming、Cancellation 语义、并发隔离、Trust Boundary、Wall timeout、Sources 生命周期、Token 预算），本次已按证据下调（§6/§11）。

**结论：冻结稳定主链（不做架构级改动）；修复下述条件后进入 Final Integration & Architecture Freeze。不满足条件前不建议收口。**

---

## 2. 真实 Runtime 主链（按真实代码逐跳复核版）

```
Frontend(runtime UI)
 ├─ 无附件:  GET /api/tasks/{cid}/stream?message=…&session=…   [webapp.py:1575-1587; workspace.js:1812-1836]
 │           message 在 URL query(encodeURIComponent)；GET 内联 create_task+add_message(无幂等键)
 ├─ 有附件:  POST /api/projects/{pid}/messages/create(建 Run+user 消息+绑附件) → GET /api/runs/{runId}/stream
 │           [webapp.py:1011-1044 → resume 分支 245-312]
 └─ 旧页面:  GET /api/stream?session=&message=(legacy adapter，同样建 Run)  [webapp.py:314-333]
      ↓
api_stream 单生成器 [webapp.py:204-434]
   ├─ asyncio.create_task(runtime.run_turn(task_id=…))   [265/345；50ms 轮询 drain stream_events_cb 转发事件]
   ├─ SSE 事件: run.started/tool/reply_delta/reply/run.waiting_approval/run.failed/run.completed/done …
   └─ finally: 连接断开/异常且 RUNNING → transition(CANCELLED)  [412-424]（见 §3.2 缺陷）
      ↓
AgentRuntime.run_turn  [runtime/runner.py:391-…]
   ├─ 容器解析 → resume(RESUMABLE_FROM:SUBMITTED/PAUSED/WAITING_USER/WAITING_APPROVAL) 或新建 Run(SUBMITTED→RUNNING) + user Message
   ├─ bind RunContext(contextvars: run/container/session/channel/memory_scope/file_scope)  [runctx.py:46-58]
   ├─ Context Prep: SQL 快判 → compact_history(分块摘要) → hard window(260K 字符/500 条)  [runtime/context.py; 压缩前不包含本轮消息]
   ├─ 组装 instructions(两次 clone)：默认人设 + Project Context(≤6000) + 附件(≤8) + Sources 自动检索块(≤6000, trust 文本包裹)  [runner.py:340-374, 577-617]
   ├─ route_agent(profile 恒 default；TOOL_ROUTER 子集 ≤16)
   └─ 执行循环(gate.begin→execute_turn；repair ≤1)
        execute_turn  [main.py:444] → _run_attempt(mode=stream→Runner.run_streamed；sync→run_sync；async→run)
         └─ SDK Agent Loop(model.get_response→ResilientModel，见 §8)
              tool call → runner._make_invoke 包装层 [runner.py:146-221]
                 ① ApprovalGate.check(run_id, tool, 规范化 args)  [approval.py]
                 ② FileScope.authorize_tool(strict: proj-* 会话)   [filescope.py]
                 ③ NetPolicy(run_python/code_loop)                [netpolicy.py]
                 ④ 执行 → ledger(内存, run-keyed: executed/blocked/error) + audit.ingest(attempt 结束后落 agent.db)
              → SDK 回传 → 继续 Loop(max_turns 有界)
        → FinalResponseFailed 降级门控(有执行证据→degraded completed；验证意图无通过→failed)
   ├─ ReplyParser(容错, model repair 默认关) → Completion Gate(PASS 才 mark_success；NO_PROGRESS/CLAIM_UNSUPPORTED/…→ repair ≤1 → _fail)
   ├─ 收口: assistant 消息(成功/失败各写一次) + audit backfill + provider.model_attempts 事件 + gate.clear_run + checkpoint(终态)
      ↓
落库: agent.db(runs/messages/task_events/approvals/artifacts/checkpoints/model_calls/tool_calls/source_*)
     + sessions.sqlite(SDK 会话历史缓存, 容器 session_id 键)
      ↓
事件/前端: task_events append-only → REST GET /api/runs/{id}/events(≤200)；SSE 无 cursor/replay(断连后事件不重放，历史由 REST 补)
```

辅助直连模型点（主链护栏外，复核确认清单，与基线一致）：`compact.summarize_transcript`(timeout 90)、`research.deep_research_impl`(120)、`multimodal.ask_image`(90)、`runtime.codex_loop._fix_with_llm`(120)、`reply_parser._repair_with_model`(60, 默认关)。均无 classify/有限重试/fallback/attempt 审计（openai 客户端级 max_retries=2 内嵌）。

---

## 3. Critical Gaps Verification（逐项）

状态图例：✅ 通过 / ⚠️ 有风险 / ❌ 未通过/缺口。每项给：真实实现 / 测试方法 / 测试结果 / 风险 / 评级 / 是否需修复 / 建议最小修复。

### 3.1 Run 创建与 SSE 生命周期

- **真实实现**：Web 运行时主发送路径为 `GET /api/tasks/{cid}/stream?message=…`（旧 legacy `/api/stream` 同构；附件消息走 POST 建 run 后 GET run stream）。三个 GET 端点**都内联 create_task + add_message（GET 带副作用）**（webapp.py:1067-1073、1580-1587、314-323），服务端**无幂等键/去重**。Run 独立资源 API 齐全（GET /api/runs、/api/runs/{id}/events、POST pause/resume/cancel、GET stream），但**无 POST 纯建 Run 端点**。SSE 无 Last-Event-ID/无 cursor/无 replay（历史由 REST events 端点补）。uvicorn 以 WARNING 启动，自身不落 access log。
- **测试方法**：代码取证 + 真实 HTTP 重复 GET 探针（同 URL 连续两次，legacy 会话，真实网关执行后查 DB）＋ E2E 三连消息。
- **测试结果**：
  - 正常发送（E2E T2/T3 LIVE）：同容器 3 条消息 → 恰好 3 个 Run、消息链 user/assistant×3，**无重复**。
  - **重复同一 URL（Live 探针）**：identical URL 连续 GET 2 次 → **创建 2 个独立 Run 并各自完整执行（均 completed）** → 证实无幂等去重，proxy/browser retry 会重复执行。探针后已清理。
  - SSE 断开（E2E T8 LIVE）：断开后 Run 正确落 cancelled 终态、无悬挂 RUNNING。
  - 长消息：无服务端长度上限（受 uvicorn/h11 请求行 ~16KB 限制，超长直接断连无友好 400）；DB content 上限 200000。
  - 特殊字符：URL 全量 encodeURIComponent，正常（E2E 中文消息通过）。
  - message 落 query → 若以 INFO 启动或被反向代理记录，会进 access/proxy/monitoring log 与浏览器历史。
- **风险**：重复执行（真机证实）、消息经 URL 泄露面、长消息无界。
- **评级**：PARTIAL（功能完整，幂等/独立资源语义不完整）。
- **需修复**：是（P1）。
- **建议最小修复**：① POST /api/runs + GET /api/runs/{id}/events 收敛（run 已独立资源化，只差"无消息纯建 run"端点与前端切换）；② 加客户端消息幂等键（sha256(message+session+ts))，短窗内同键拒绝；③ message 长度上限显式 400；④ 保持 uvicorn WARNING 并加 request 脱敏日志策略说明。**不要本阶段重写 SSE 架构**。

### 3.2 SSE 断连与 Run Cancellation

- **真实实现**：断连（浏览器关闭/刷新/网络中断）在服务端感知到 socket 失败后进入生成器 finally（webapp.py:412-424）：若 Run 仍 RUNNING → `transition(CANCELLED)` + 补发 done。**用户取消**走 POST /api/runs/{id}/cancel → 同样只 transition(CANCELLED)。二者 DB 语义相同。
- **核心缺陷（代码证据）**：finally **从不 cancel `asyncio.create_task(runtime.run_turn(...))` 创建的任务**（265/345 无引用、无 await/cancel）。即：**断连/取消只改 DB 状态，底层模型/工具循环继续在后台真实执行**（工具副作用继续发生）；循环自然结束后尝试 RUNNING→COMPLETED 对 CANCELLED 是非法转换 → 抛 AgentError → `_fail` 再对终态 mark_failure 再次非法 → run_task 以未捕获异常消亡，assistant 消息不再落库（用户看不到本轮结果，但副作用已发生）。
- **测试方法**：代码取证 + E2E T8（Live 断连，检查 DB 状态与悬挂）。
- **测试结果**：E2E T8 PASS——断开后 DB 状态 = cancelled、无悬挂 RUNNING（DB 层语义正确）；"后台继续执行"为代码路径确定存在但 Live 未注入长任务观测（不注入真实长副作用，避免污染）——该项 Live 证据=代码确定。
- **风险**：用户以为已取消、实际工具/文件副作用继续执行；取消后的尾部执行状态无法落 assistant 消息；网络瞬断可直接毁掉正常任务（frontend disconnect == user cancel 被当成同一件事）。
- **评级**：❌ **P0（倾向）——生产级副作用失控/任务不可恢复**。
- **需修复**：是（P0 最小修复）。
- **建议最小修复**：finally/取消路径持有 run_task 引用 → 断开时 `task.cancel()`（可配合 cancellation 时点落"已取消"checkpoint 与半程事件）；将"客户端断连"与"用户取消"分开事件/文案；Run 若在断连时已完成副作用收尾则允许从 evidence 侧写 assistant 消息而非丢结果。评估 2-3h 工作量，不改架构。

### 3.3 Streaming Provider Retry / Fallback

- **真实实现**：Web 主链 = **streaming**（Runner.run_streamed）。`ResilientModel.stream_response` **直接委托 inner**（provider_gateway.py:107-114，注释"避免对已流出 token 做重放语义"）→ **流式路径完全绕开 get_response 的分类→有限重试→fallback→attempt 记录**；流式容错仅剩 openai 库客户端级裸重试 max_retries=2（无分类/Retry-After/fallback/审计）。非流式（sync/async 单轮）才走完整 ResilientModel.get_response 包装。
- **分层重试**：Run 级 repair/输出闸重试只在完成声明/格式类失败触发（≤1 repair、≤3 输出闸尝试）；Provider 传输错误明确不进入 repair/stalled（runner.py:1016-1053）。非流式单次 model 调用最坏 ≈ 客户端 HTTP(≤3)×Resilient 重试(每 kind 0-2)+fallback(≤1 轮×再 ≤3) ≈ **有界但可到 ~18 次 HTTP**（5xx+fallback 场景）。
- **测试方法**：代码取证 + 离线故障注入（tests/test_provider_errors.py stub 注入 401/403/404/429/503/no-channel/timeout 全 kind：11 用例含 fallback 成功、双方失败有界、401 不进 stalled、attempts 事件）；流式路径无专项故障注入测试。
- **测试结果**：离线注入全部 PASS（分类/非流式重试/fallback 语义正确且有界）；**流式 = 无网关级保护（Live 无法在不影响真实任务下注入 mid-stream 断流，标 NOT CURRENTLY VERIFIED）**。
- **风险**：生产主链（streaming）在 429/503/network reset/mid-stream 断开时无分类重试/fallback/attempt 审计；非流式与流式行为不一致；usage/accounting 双口径（model_calls 记 SDK 最终 usage；attempts 仅非流式有记录，纯流式 count=0 属预期但语义易误读）。
- **评级**：❌ **PARTIAL（基线 COMPLETE 需下调：只对非流式成立）**。
- **需修复**：是（P1）。
- **建议最小修复**：流式也套同一包装——在 SDK 层面向 stream 提供"建连失败/首 token 前"重试（首 token 前重试安全、不重放 token），首 token 后仅透明断开+事件标记；把重试决策复用 retry_policy（分类一致）。不引入新架构。

### 3.4 Sources / RAG Prompt Injection 与 Trust Boundary

- **真实实现**：Source 内容经 `sources/service.py build_reference_block` → **追加进 clone Agent 的 `instructions` 最末尾**（runner.py:612-617）→ 与 System policy（agent.py 人设 87-201）**同一个 system prompt、同一特权层级**；唯一边界是 `runtime/trust.py` 的**纯文本包裹**（【外部数据 · 仅供参考】+ 开始/结束标记，trust.py:12-49）+ agent.py:125 的人设纪律条文。`_DANGEROUS_PATTERNS`（trust.py:19-24）定义后**全仓无调用点（死代码）**，无程序化拦截。input guardrail 只扫最后一条 user 消息（guardrails.py:92-109），对 system 中的 Source 文本零覆盖。工具结果回流（SDK tool result）同样只靠工具层 trust 文本。
- **测试方法**：离线注入测试（tests/test_sources_rag.py:96 注入串保留但带边界标记；test_trust.py 覆盖出口）；恶意 Source 的"忽略指令/执行 shell/删文件"语义遵循无 Live 验证手段（无第二裁判可判定模型内部遵循度）——**该项本质上无法离线证明，评级基于机制强度**。
- **测试结果**：边界标记存在于所有外部出口（13 处打 tag）；**无运行时过滤/剥离/阻断通道**。
- **风险**：注入防御完全依赖模型遵从文本标记；注入者可伪造/重叠标记；与 policy 同层。
- **评级**：⚠️ **PARTIAL（基线 COMPLETE 下调：结构性信任边界不存在，只有提示词纪律）**。
- **需修复**：是（P1）。
- **建议最小修复**：① 自动检索块/工具返回在注入前做危险模式剥离或内容闸（复用 trust._DANGEROUS_PATTERNS 激活 + 截断 + "疑似指令注入"降级为不可信引文）；② 参考块移到**独立 user 轮**（角色降权），不与 system policy 同层；③ 输出闸增加"模型声称执行了 Source 中指令"类表述的拦截样例。最小改动、不动 RAG 核心。

### 3.5 Timeout / Retry / Stalled 的真实边界（Timeout Matrix）

| 维度 | 默认/来源 | 触发方 | 超时后状态 | 是否无限 | 本次判断 |
|---|---|---|---|---|---|
| max_turns | CLI 20/run_turn 20/SSE 钳制 [5,100]（SDK 兜底 10） | SDK loop | failed | 否 | ✅ 有界 |
| Run 墙钟 | `TASK_MAX_WALL_SECONDS` **未设=关闭**；设后 (0,3600] 生效 | budget.run_with_wall_limit | BudgetExceeded→failed | 未设时是 | ⚠️ 默认关 |
| Model request | openai 客户端 600s / connect 5s；SDK ModelSettings.timeout=None | openai 库 | APITimeoutError→TIMEOUT 重试 1 | 否 | ✅ 有界(600s) |
| First-token | 同上(无独立) | — | — | — | ⚠️ 无独立 |
| Stream idle | **无** | — | — | **是** | ❌ 缺口 |
| Tool(subprocess) | run_python 40s cap 120s 强杀 | code_exec | 返回"已强制终止" | 否 | ✅ |
| Tool(库内/office/project_edit) | 无超时(容量上限) | — | — | 潜在 | ⚠️ 记录 |
| Approval | 无 TTL；终态批量置 expired | — | 跨重启保留 | 是(有界于 run 生命) | ⚠️ 设计如此 |
| Max model/tool calls | 无独立上限(max_turns 隐含) | — | — | — | ⚠️ 记录 |
| repair | completion/无进展各 ≤1；codex 修复 ≤3(实现 cap 5) | runner | failed(repair_exhausted) | 否 | ✅ |
| Provider retries | kind 表: 429/5xx=2, no_channel/timeout/net=1, 其余 0 | ResilientModel(非流式) | exhausted→fallback | 否 | ✅非流式/❌流式 |
| 输出闸重试 | execute_turn ≤1(流式先 stream_reset) | 本地 | FinalResponseFailed→降级/失败 | 否 | ✅ |
| stalled 语义 | 无 STALLED 状态；NO_PROGRESS 等价(连续 ≥3 相同调用 或 验证意图 0 执行) | Completion Gate | ≤1 repair → failed | 否 | ✅ |

- **真实实现/结论**："max_turns=20"不是有效超时保护的部分：**Run 墙钟默认关闭 + 流式无 idle 超时 + 无 first-token 独立超时** —— 模型卡流或长尾多轮 Run 无兜底（除非 provider 层 600s 单请求断开）。离线测试：test_budget_router（墙钟触发）、test_runtime_cleanup:180（env 接线）、test_code_exec:87（超时强杀）、test_productization（NO_PROGRESS 双次有界）。
- **评级**：PARTIAL（基线"Wall timeout COMPLETE"需修订为"实现存在、默认关闭"）。
- **需修复**：是（P1）：墙钟默认开启（如 1800s）+ 流式 idle/first-token 超时挂到 SDK stream 包装。

### 3.6 同一 Project 多 Run 并发

- **真实实现**：**无任何容器级 Active-Run 互斥/排队**（webapp 各 stream 端点无条件建 Run；全仓除 compact per-session 锁与 observability 线程锁外无 asyncio 锁保护 Run 执行）→ FORGE 现行为 = **B 表面形态（允许并行），但无完整 branch/session isolation**：
  - 已隔离（run-keyed）：RunContext contextvars、内存 ledger、ApprovalGate `_states[run_id]`、FileScope(每 run 构建)、记忆作用域绑定——离线并发矩阵测试全过（test_p1_reliability: 4 Run 审批交错隔离、10 轮记忆交错不串、同文件并发矩阵；test_context_guard:397 同会话并发 compact 只压一次）。
  - **仍共享/未隔离**：SDK session（同容器共享 sessions.sqlite 历史文件，并发 Run 互相读到对方刚写入的 turn）；紧凑窗口/上下文 pre 单槽 `_context_prep`；`assistant_agent.tools` 全局补丁；`tools._MEMORY_BINDING` 模块级；**同文件写入无锁**（project_edit.py:143-148 直写）；run_index = MAX+1 读后插 TOCTOU（task_manager.py:673-677）。
- **测试方法**：离线并发矩阵（已在基线+本复核跑通）+ 代码取证。
- **测试结果**：run-keyed 内存态隔离全部 PASS；**容器级并行（同文件写、会话历史交错、run_index 竞争）无真实 E2E 测试（现状即缺口）**。
- **风险**：同一容器并发任务可串写 SDK 历史/同文件丢更新/双 Run 同 index。
- **评级**：PARTIAL（基线 COMPLETE 下调：只证明 run-keyed 隔离，未证明容器级并行安全）。
- **需修复**：是（P1，产品决策）：**明确"同一容器同时只允许一个 Active Run"的串行策略**（新消息如已有 RUNNING/WAITING_APPROVAL→409 或入队）——比引入并发架构小得多，符合"不要假装支持 B"原则。

### 3.7 Approval 是否绑定 Exact Tool Call

- **真实实现**：绑定 = (run_id + tool_name + `args_key`（json.dumps(sort_keys) 规范化参数哈希）)（approval.py:53-54；approvals 表含 arguments_json 脱敏快照，task_manager.py:124-136）。**不绑定 tool_call_id**（表无此列）。批准后不自动执行：包装层门 check（runner.py:161-165）→ 模型在**同轮**继续以同参调用则直接放行执行（212-218）；前端 approve 后须 resume → **整 Run 按原 goal 重跑**，模型重新发起调用，同参命中 approved 放行、参数一字节不同则新挂 pending。denied→同键持续拒；pending 同键不重复建单。
- **测试**：离线（test_approval.py：10 用例含真实链"拦→批→执行"、code_loop 不能绕过、rollback 门先于效果、all 语义、非名单放行）+ E2E T4/T5 LIVE（真实 run_python 挂起→批准→resume 同一 Run 未新建）。
- **测试结果**：exact-arguments 绑定与 resume 同一 Run **LIVE 通过**；**并发双连击存在 TOCTOU**（decide_approval SELECT 与 UPDATE 分离且 UPDATE 无 WHERE status='pending'，task_manager.py:1031-1043）→ approve/deny 双成功可能；批准非 exact-once（resume 重跑可能重复执行先前已跑的非门内副作用工具；README:169 自认重放语义）。
- **评级**：⚠️ PARTIAL（绑定粒度合理但"批准≠执行、靠 resume 重放"带来重复副作用面；并发决定非原子）。
- **需修复**：是（P1 最小修复）：① decide UPDATE 加 `WHERE status='pending'` 原子化（一行）；② resume 语义内对"已 executed 的非门内工具"在重放时按 ledger 记忆去重或至少展示重放风险；③ 可选：为 gated 工具保存原始 tool_call 参数并在 resume 首次轮到同参调用时**自动执行**而不等待模型重新输出（需小改包装层，评估后定）。

### 3.8 Verification 与 Completion 必须分开

- **真实实现**：无独立 Verification Engine；职责实际三层：
  - Tool Evidence：任何工具经包装层自动记账（executed/blocked/error + 输出头 1500 字）→ ExecutionEvidence（runner.py:248-301）；
  - 结果解析：确定性正则解析"退出码/✅ 通过/未通过"（completion.py:83-113，"禁止把执行过当验证通过"显式注释；code_loop 才输出 ✅ 通过）；
  - Completion Gate（completion.py:338-387）：只做 **声明↔证据一致性 + 无空转**，**从不校验任务目标语义达成**（全函数无 goal 比对——"文件已修改"有 edit 执行即 PASS，不检查改对没有）；Verification 手段由模型自选（调用 run_python/code_loop），自动策略仅两条用户意图正则（"确保测试通过"等）；
  - Repair：VERIFICATION_FAILED/CLAIM_UNSUPPORTED/NO_PROGRESS → 自动 repair ≤1 → 仍败 _fail。
- **测试**：test_verification.py（exit0/1、claim+exit0 放行、意图+失败永不 completed、fail→repair→pass 全过）；test_completion_gate.py 20 用例（假修改声明拒绝、真执行空输出降级、密钥拦截不泄、repair 恰 ≤2 次调用有界）；E2E（editing→verify）历史 Live 记录。
- **测试结果**：✅ 离线全绿；"谎言检查"（声明↔证据）与"任务真完成"（目标校验）**目前是分开的，且后者不存在**——Completion 可信度 = 模型不自欺 + Gate 证据核对；无自动"该任务应如何验证"策略。
- **评级**：Verification evidence ✅ COMPLETE；Automatic verification policy ⚠️ PARTIAL（意图正则+模型自选）；Repair loop ✅ COMPLETE(有界)；Completion truthfulness ⚠️ PARTIAL（不验证目标达成——**这是设计边界，建议明确写入产品语义**：FORGE 保证"声明有据、不空转"，不保证"目标真实达成"）。
- **需修复**：P2（文档/语义明确化）；不做 Verification Engine。

### 3.9 Tool Argument Readiness / Parameter Provenance（新增维度）

- **真实实现**：Agent 人设强制"缺失关键信息必须 questions 澄清、禁止用默认值开跑"（agent.py:110-114）+ Completion Gate 对"questions 免证据 PASS"；无工具参数来源校验层。**无"关键参数缺失自动拦截"**。
- **测试**：test_guardrails（越狱/密钥类）；无"缺参调用带副作用工具"专项门。行为完全取决于模型对纪律的遵从（此前任务记录中模型多次正确追问，也出现过模型自补后进入审批被拦——门兜底了最高危的 run_python）。
- **风险**：低危工具（save_note 覆盖、schedule_add）模型自补参数无闸。
- **评级**：PARTIAL（纪律存在、门兜底高危项；无系统性 provenance 校验）。
- **需修复**：P2（轻量 Tool Readiness：仅对"删/覆盖/外发/调度"类工具在参数缺失关键字段时返回拒绝模板要求澄清，≤1 个装饰器函数，不建 Planner）。

### 3.10 Context 真正的 Token Budget

- **真实实现**：字符级预算（无 provider token 级）：Project Context ≤6000 字符、Sources ≤6000(默认 FORGE_SOURCES_MAX_CONTEXT_CHARS)、附件 ≤8 条、记忆 ≤10 条注入、工具子集 ≤16（TOOL_ROUTER on）；compact 软窗 TRIGGER_CHARS=180000/60 轮，硬窗 260000 字符/500 条；est_tokens = chars（1char≈1token，对中文低估）；**模型 context window 全仓无声明**（AGENT_MODEL=agnes-2.5-flash，max_tokens=4096 仅输出）；**41 工具全量 schema 占用从未测量**；context overflow(400) 归类 BAD_REQUEST → 0 重试 0 fallback **无压缩兜底**（provider_errors.py:143-144）——溢出不会触发自动 compact。
- **测试**：test_context_guard 16 用例（软/硬窗、摘要失败→硬窗、2400 条有界、tool result 保留、附件 compact 后仍注入、60 轮锯齿有界、同会话并发只压一次）；test_compact 6 用例。无 Live 长上下文压力。
- **结果**：✅ 有界性成立；⚠️ 无窗口基线 + 溢出无兜底 + 中文低估。
- **评级**：PARTIAL（有界工程 COMPLETE，预算准确性/溢出恢复 MISSING）。
- **需修复**：P1（小）：400 报错含 context_length 特征时 → 标记并触发一次强制 compact 重试；P2：记录各模型声明窗口配置化（MODEL_CONTEXT_WINDOW env）供预算比例校准。

### 3.11 Tool Security Matrix（全表见 §6）

- 要点复核：41 工具 = agent.py 静态 36 + 技能工具 5（gorden-ppt×4、dep_doctor×1）。默认审批门 5 个（run_python/code_loop/sandbox_rollback/forget_memory/schedule_remove；APPROVAL_GATED_TOOLS=all 时 + write/edit_project_file）；FileScope 登记 9 个路径工具；trust 出口 13 处；NetPolicy 只覆盖 run_python/code_loop（默认 approval，静态检测，**OS 级网络沙箱未实现**——记录在案）。
- **默认策略（关键）**：新增一个"零配置"工具（入 agent.tools 但无任何登记）→ 包装层审批名单外放行、FileScope 未登记参数 ALLOW（filescope.py:193-195）、授权异常 fail-open（runner.py:184-185, 566-567）→ **默认 ALLOW（fail-open），无 fail-closed 兜底**，唯一兜底是工具自身硬边界（code_exec/project_edit 自约束）。
- **评级**：⚠️ PARTIAL（安全原则"fail closed"未达成；登记 9/41）。
- **需修复**：P1：为路径类工具显式登记 + 未知新工具默认 DENY 提示（改 filescope 默认分支+包装层名单注册提示即可）；P2：异常路径 fail-open→fail-closed 评估。

### 3.12 Database / Crash Consistency

- **真实实现**：双库（agent.db 产品事实 + sessions.sqlite SDK 历史），**无跨库事务**；恢复协议=reset/delete 联动 + Snapshot/Restore 双库。崩溃窗口枚举（代码取证，关键次序）：
  1. user Message 保存后 crash → Run=SUBMITTED，recover(900s) → failed，message 残留（不重放）；
  2. RUNNING 期 crash → recover → failed；
  3. **写文件工具成功后、内存 ledger/audit.ingest 前 crash → 文件已改、agent.db 无 tool_calls 记录、进程重启后 ledger 丢失 → 副作用成为"无记录事实"，再次执行同目标时可能重放（exactly-once 缺口）**；
  4. SDK 写入 sessions.sqlite 后、agent.db 收口前 crash → 双库不一致（会话有记录、run failed）；
  5. audit.ingest 与 checkpoint 之间 crash → model_calls/tool_calls 部分行（每 attempt 一次 ingest，粒度为 attempt）；
  6. assistant 消息写后、终态前 crash → message 有、run 未终态 → recover failed。
- **测试**：test_runtime_cleanup（failed 关 pending、waiting 保留、stale recovered、wall env）；test_schedules auto_recover；**无"写盘后 ledger 前"注入式崩溃测试（现状缺口）**。
- **评级**：PARTIAL（recover/终态一致性好；**副作用↔记录窗口非原子是最大一致性弱点**）。
- **需修复**：P1：工具执行前写 pending side-effect 行（write-ahead），成功后落 executed——把"写文件"工具改造成先记后做的最小协议（≤1 处包装层改动）。

### 3.13 Sources 生命周期（单独复核）

- **真实实现**：上传（白名单/50MB/sha 去重）→ `asyncio.to_thread` fire-and-forget 索引（无任务引用、无队列、**无启动扫描恢复**）；状态 parse_status∈{pending,parsing,ok,failed}、index_status∈{none,indexing,ready,failed}（无 READY 大写/STALE/DELETED 概念）；embedding 失败降级纯 FTS（仍 ready）；替换=单事务先删后插（失败 rollback → **旧 chunk 可检索但状态 failed 的不一致**）；删除与在途索引**竞态**（async 不可取消，晚提交 replace 重插孤儿 chunk；retrieve 只 join chunk 表**不校验 source 状态** → 孤儿可检索）；**索引未完成时用户发任务 → 检索只查 index_status='ready' → 静默无参考块 → "source not ready" 零信号回 Runtime/UI**（service.py:25-57、runner.py:592-619 异常全吞）；UI 只按 parse_status 显示可用/不可用 pill，无索引状态气泡。
- **测试**：test_sources_rag 9 用例（管线/hybrid/隔离/注入标记/更新删除/冲突文档/PDF）；无"索引中发任务/删除竞态/重启中断索引"注入测试。
- **评级**：⚠️ PARTIAL（检索核心 ✅；生命周期状态/竞态/信号 ⚠️）。
- **需修复**：P1：ready 前发任务 → 事件"source 未就绪(索引中/失败)"随 run 落库并可被 UI 提示；启动扫描把 parsing/indexing 孤儿置 failed；删除时先停再删（引用索引任务句柄，最小改造）；rollback 路径状态回滚到旧版状态而非 failed。

---

## 4. Capability Verification Matrix

状态：✅=当前代码可验证成立；⚠️=部分成立/有条件；❌=缺口；记录态按上节结论。E2E/LIVE 指 2026-09-07 本次实测。

| 能力 | Implemented | Unit | Integration | E2E | Live Provider | 复核结论（相对基线） |
|---|---|---|---|---|---|---|
| Run creation | ✅ | ✅ | ✅ | ✅ | ✅(E2E 3 Run) | COMPLETE（GET 建 Run+无幂等=缺口见 §3.1） |
| Run persistence | ✅ | ✅ | ✅ | ✅ | ✅ | COMPLETE |
| Streaming | ✅ | ✅ | ✅ | ✅ | ✅(E2E reply_delta 流) | COMPLETE（无重试护栏见 §3.3） |
| SSE reconnect/replay | ❌ | ✅(无此能力) | — | ⚠️T8 | ✅(断开正确收口) | 无 cursor/replay；断连即取消见 §3.2 |
| Cancellation | ⚠️ | ✅ | ⚠️ | ✅(T8) | ✅ | DB 语义✅；在飞执行未中断 ❌（§3.2 P0） |
| Approval | ✅ | ✅ | ✅ | ✅(T4/T5 真实 run_python) | ✅ | 绑定 run+tool+args 精确；TOCTOU/resume 重放 ⚠️ |
| Resume(审批/暂停同 Run) | ✅ | ✅ | ✅ | ✅(T4/T5 同 Run 未新建) | ✅ | COMPLETE（重放语义自认） |
| Provider retry/fallback | ⚠️非流式 | ✅(11 注入用例) | ✅ | ⚠️ | ⚠️(LIVE 正常路径) | **非流式✅/流式❌ 统一=PARTIAL** |
| Context compact | ✅ | ✅ | ✅ | — | — | COMPLETE(有界) |
| Token budget | ⚠️ | ⚠️ | — | — | — | PARTIAL（无窗口基线/溢出无兜底） |
| Memory | ✅ | ✅ | ✅ | — | — | COMPLETE（作用域隔离测试） |
| RAG/Sources 检索核心 | ✅ | ✅ | ✅ | — | 历史记录 | COMPLETE（生命周期 ⚠️ 见 §3.13） |
| Tool permission(FileScope) | ⚠️ | ✅ | ✅ | — | — | PARTIAL（登记 9/41、未知默认 ALLOW、异常 fail-open） |
| Network policy | ⚠️ | ✅ | — | — | — | 策略层✅；OS enforcement MISSING（记录） |
| Wall/stream timeout | ⚠️ | ✅(env 接线) | — | — | — | 墙钟默认关、流式 idle 无（§3.5） |
| Verification(结果语义) | ✅ | ✅ | ✅ | — | 历史记录 | 结果解析✅；自动验证策略 ⚠️（§3.8） |
| Repair | ✅ | ✅ | ✅ | — | — | 有界(≤1/completion、≤3+codex) |
| Completion | ✅ | ✅ | ✅ | — | — | 声明↔证据✅；目标达成不校验=设计边界 |
| Crash recovery | ⚠️ | ✅ | ✅ | — | — | recover✅；副作用窗口不一致 ❌（§3.12） |
| Concurrent run isolation | ⚠️ | ✅ | ✅ | — | — | run-keyed✅；容器级并行未隔离（§3.6） |
| Tool timeout | ✅ | ✅ | ✅ | — | — | run_python 强杀✅；库内工具无超时(记录) |
| Trust boundary | ⚠️ | ✅ | ✅ | — | — | 文本标记✅/结构性边界❌（§3.4） |
| RAG 生命周期信号 | ⚠️ | ✅ | — | — | — | ready 前静默无信号（§3.13） |
| Frontend event 集成 | ⚠️ | ✅ | ✅ | ✅(LIVE UI 事件) | ✅ | 死监听/死状态见 §5 |

---

## 5. 真实 State Machine（复核版）

唯一状态源 TaskState（runtime/task.py:18-26）：`SUBMITTED/RUNNING/WAITING_USER/WAITING_APPROVAL/PAUSED/COMPLETED/FAILED/CANCELLED`（**无 STALLED**；NO_PROGRESS 为其有界等价）。唯一改状态入口 `TaskManager.transition`（task_manager.py:746-782，写 events、终态关闭 pending、记 started/completed_at）。

| 状态 | 进入 | 退出 | 谁写 | 谁消费 | 前端 | 测试 | 生产可达 | 标 |
|---|---|---|---|---|---|---|---|---|
| SUBMITTED | create_task | →RUNNING(resume/新建) | runner/webapp | run_turn/recover | 处理中 | ✅ | ✅ | LIVE |
| RUNNING | resume/新建 | →8 态 | runner | Gate/recover/断连收口 | 处理中 | ✅ | ✅ | LIVE |
| WAITING_APPROVAL | runner.py:862(真实 pending 为事实源) | →RUNNING/CANCELLED/FAILED | runner | decide→resume | 等你确认 | ✅ | ✅(重启保留) | LIVE |
| WAITING_USER | **无任何写入方** | — | — | — | 文案 | can_transition | ❌ | DEAD |
| PAUSED | 用户 pause(webapp 端点) | →RUNNING | webapp | UI/resume | 已暂停 | ✅ | ✅(仅 DB 语义，不中断在飞协程) | LIVE(半) |
| COMPLETED | mark_success(先过 Gate/degraded) | 终态 | runner | UI | 已完成 | ✅ | ✅ | LIVE |
| FAILED | mark_failure/denied/stale | 终态 | runner/recover | UI | 遇到问题 | ✅ | ✅ | LIVE |
| CANCELLED | 断连收口/cancel 端点 | 终态 | webapp | UI | 已停止 | ⚠️(无直接单测，状态机断言覆盖) | ✅ | LIVE(语义缺陷 §3.2) |

前端事件对账：前端监听但 SSE 永不发送：`task.paused/task.cancelled/run.paused/run.cancelled/artifact.created/checkpoint.created`（cancelled/paused 只落 task_events 表，SSE 不推）→ **前后端不一致(死监听)**；前端 eventReducer 的 eventKey 幂等去重链实际未接线。

---

## 6. Tool Security Matrix（41 个真实注册工具，复核版）

列：能力（R读/W写/E子进程/N网络/O模型/M记忆/D删除）；Gate=默认审批门内；FS=FileScope(strict project 模式)校验；Tag=trust 文本出口；TO=超时。

| 工具 | 能力 | Gate | FS | Tag | 说明 |
|---|---|---|---|---|---|
| save_note | W | n | n | n | notes/ 写入，保护名单外 |
| read_note | R | n | n | y | |
| list_notes | R | n | n | n | |
| web_search | N | n | n | y | 外部内容边界 |
| get_current_datetime | — | n | n | n | |
| read_workspace_file | R | n | y | y | |
| list_workspace_files | R | n | y | n | |
| calculate | — | n | n | n | AST 白名单 |
| remember | M | n | n | n | 密钥闸+200 上限 |
| recall_memory | R/M | n | n | n | 作用域绑定 |
| forget_memory | D | **y** | n | n | |
| index_workspace | R/W | n | y | n | 显式目录否则拒 |
| search_documents | R | n | y | y | |
| schedule_add | W | n | n | n | 定时 |
| schedule_list | R | n | n | n | |
| schedule_remove | D | **y** | n | n | |
| schedule_set_enabled | W | n | n | n | |
| ask_image | R+O | n | y | y | 90s |
| deep_research | N/W/O | n | n | y | 3×120s 子调用 |
| fetch_github_repo | N/W | n | strict 拒 | y | |
| write_code_file | W | n | n(沙箱自约束) | n | code_sandbox 内 |
| read_code_file | R | n | n(自约束) | y | |
| list_code_files | R | n | n | n | |
| run_python | E | **y** | n | y | 40/120s 强杀+NetPolicy |
| code_loop | E+O | **y** | n | y | 修复≤3 |
| sandbox_snapshot | W | n | n | n | |
| sandbox_rollback | W/D | **y** | n | n | |
| list_sandbox_snapshots | R | n | n | n | |
| read_office_file | R | n | y | y | |
| read_spreadsheet | R | n | y | y | |
| save_word_doc/excel/ppt_deck | W | n | n | n | exports/ |
| write_project_file | W | all 时 y | **y** | n | 写根校验+备份 |
| edit_project_file | W | all 时 y | **y** | n | 同左 |
| search_sources | R | n | n(项目自限) | y | |
| gorden_ppt_templates/intro | R | n | n | n | slug 白名单 |
| gorden_ppt_build | R/E/W | n | n | n | 300s 子进程 |
| gorden_ppt_apply_custom | R/E/W | n | **y**(自调 authorize) | n | 模板路径越界拒 |
| scan_dependencies | R | n | n | n | WORKSPACE_ROOT 自限 |

**默认策略结论**：新增零配置工具 → 审批名单外放行 + FileScope 未登记 ALLOW + 授权异常 fail-open ⇒ **默认 ALLOW（非 fail closed）**；兜底仅靠工具自身硬边界。11 个工具带外部副作用面中最危险的 5 个在默认审批门内，真实文件写默认不在门内（`APPROVAL_GATED_TOOLS=all` 才纳入）——**记录为产品决策（FileScope+工具层旧校验兜底）**。

---

## 7. Context / Trust Model（真实信任层级）

注入顺序（后注入靠尾，全部同层在 system instructions 内）：默认人设/纪律（System policy）→ Project Context（项目说明/记忆≤10/来源清单）→ 附件清单 → **Sources 自动检索块（最尾）**。

| 来源 | 进入位置 | 层级 | 边界机制 | 结构性隔离 |
|---|---|---|---|---|
| System/Runtime policy | agent.py instructions 静态段 | 最高 | — | — |
| User instruction | 每轮 user 消息 | 用户 | input guardrail(仅最后一条) | 无 |
| Memory(项目记忆) | instructions 注入 ≤10 条 | 与 project 块同层 | 作用域强制 | 无 |
| Source(RAG 自动) | **instructions 尾部**(system) | **与 policy 同层** | trust 文本标记 | **无(纯文本)** |
| Tool output | SDK tool result | 模型观察 | 工具层 trust 文本 | 无(文本) |
| External web/github/office | 工具返回 | 同上 | trust 文本 | 无(文本) |

**结论**：信任层级只存在于提示词文本纪律层；Source 与 System policy 同层同权，无程序化剥离/拒收；`_DANGEROUS_PATTERNS` 死代码。防御 = agent.py:125 条文 + 文本包裹（§3.4）。

---

## 8. Provider Matrix

| 维度 | Non-streaming(sync/async 单轮) | Streaming(Web/CLI 主链) |
|---|---|---|
| 入口 | model.get_response(ResilientModel 包装) | model.stream_response(**直接委托 inner**) |
| 错误分类 | ✅ provider_errors kind 表 | ❌ 不分类（openai 原始异常上抛） |
| 有限重试 | ✅ kind 驱动(429/5xx=2,no_channel/timeout/net=1) | ❌ 无（仅 openai 客户端 max_retries=2 裸重试） |
| Retry-After | ✅(429,cap 60s) | ❌ |
| Fallback | ✅(FORGE_FALLBACK_*；401 需独立凭据；A→B 不回头) | ❌ |
| Attempt 审计 | ✅ record_attempt→provider.model_attempts | 计 0(预期语义,已记录) |
| 超时 | openai 600s/connect 5s；ModelSettings.timeout=None | 同左 + **无 idle/first-token 独立超时** |
| Usage 记账 | SDK raw_responses→model_calls | 同左 |
| 分层放大 | 单次调用有界(≤~3×3 主+fallback) | 客户端 ≤3 HTTP |
| 降级路径 | exhausted→provider.failure 事件→_fail(不进 repair) | 同左(无 attempt 详情) |
| 实测 | 离线注入 11 用例 PASS；Live 正常路径多次成功 | E2E Live 正常路径✅；**故障注入 NOT VERIFIED** |

辅助直连模型点（compact/research/multimodal/codex/repair）：无统一包装，各带裸 OpenAI client timeout(60-120s)——与主链 Provider 语义隔离，记录不改。

---

## 9. Crash / Recovery Matrix

| Crash 点 | 恢复后 Run 状态 | Messages | SDK 历史 | Tool 执行记录 | 副作用一致性 |
|---|---|---|---|---|---|
| 建 Run 后、RUNNING 前 | SUBMITTED→recover→failed | user 消息有 | 无 | 无 | ✅无副作用 |
| RUNNING 中(模型轮内) | failed(recover 900s) | user 有 | 部分 | 该 attempt ingest 缺失 | 工具若已执行=**无记录窗口** ⚠️ |
| 写文件工具成功后、ledger/ingest 前 | failed | user 有 | 视时点 | **无行** | **文件已改、永久无记录 → 重跑可重放副作用** ❌ |
| tool_calls/model_calls ingest 后、收口前 | failed | user 有 | 有 | 有(attempt 级) | ⚠️部分一致 |
| assistant 消息写后、终态前 | failed | 多 assistant 无 | 有 | 有 | ⚠️ |
| 终态写后 | 终态 | 完整 | 有 | 有 | ✅ |
| WAITING_APPROVAL 期 | **保留** pending(不在 recover states) | user 有 | 有 | 审批行先于副作用 | ✅ 最可靠(先记后做) |
| 双库 reset/删除中途 | 一库删一库未删 | — | — | — | ⚠️ 无跨库事务(协议=顺序执行) |
| 恢复机制 | recover_stale_tasks(默认 RUNNING/SUBMITTED>900s→failed，幂等；启动钩子 main.py:965-973/webapp.py:1686-1693) | checkpoint 仅终态 | — | — | 无 turn 级恢复点 |

---

## 10. Concurrency Matrix

| 场景 | 隔离机制 | 测试 | 结论 |
|---|---|---|---|
| 不同容器并发 | runctx + per-run 一切 | 离线记忆/审批交错 ✅ | ✅ 隔离成立 |
| 同容器并发(内存态) | run-keyed(ledger/approval/file_scope) | test_p1_reliability 4-run 交错 ✅ | ✅ 成立 |
| 同容器并发(SDK 会话/历史) | **无(共享 sessions.sqlite+session_id)** | 无 | ❌ 可能互相读到对方 turn |
| 同容器并发 compact | per-session asyncio.Lock | test_context_guard:397 ✅ | ✅ 只压一次 |
| 同容器并发写同文件 | **无文件锁** | 仅边界矩阵(非写竞争) | ❌ 丢更新风险 |
| run_index 分配 | MAX+1 读后插 | 无 | ⚠️ TOCTOU 双同 index |
| Approval 并发 | 行 run-keyed | 隔离测试 ✅ | ✅ 行级不串；decide TOCTOU ⚠️ |
| 多进程(web+daemon 同 DB) | 无跨进程互斥 | 无 | ⚠️ 记录 |
| 产品语义 | **当前=B(允许并行,无互斥)** | — | 建议收敛为 A(同容器单 Active Run) |

---

## 11. 修订后的完成度（按子域，替代单一百分比）

| 子域 | 评级 | 一句话依据 |
|---|---|---|
| Runtime Core(主链/生命周期) | ⚠️ HIGH 但有 P0 收口项 | 单链稳定(LIVE 21/21)；断连取消语义缺陷(§3.2) |
| Provider | ⚠️ PARTIAL→MEDIUM | 非流式完整；**流式无网关级重试/fallback/审计**(§3.3) |
| Context | ⚠️ MEDIUM-HIGH | 有界性✅；窗口基线缺失/溢出无兜底(§3.10) |
| Tool Runtime | HIGH | 41 工具单一暴露层+统一包装+ledger |
| Permission | ⚠️ MEDIUM-HIGH | FileScope 9/41+未知默认 ALLOW+fail-open 异常(§3.11) |
| Approval | ⚠️ MEDIUM-HIGH | 绑定精确(run+tool+args)；TOCTOU+重放语义(§3.7) |
| Safety | ⚠️ MEDIUM | 注入=文本标记、与 policy 同层(§3.4)；高危工具在门内✅ |
| Verification | MEDIUM-HIGH | 结果语义✅/自动策略 PARTIAL(§3.8) |
| Completion | ⚠️ MEDIUM-HIGH | 声明↔证据✅；目标达成不校验(设计边界,需明示) |
| Persistence | HIGH | 双库+联动协议+快照；无跨库事务(记录) |
| Recovery | ⚠️ MEDIUM | stale recover✅；副作用窗口丢失/重放(§3.12) |
| RAG(Sources) | ⚠️ MEDIUM | 检索核心✅；生命周期状态/信号/竞态缺口(§3.13) |
| Memory | HIGH | 双层+作用域强制+并发测试✅ |
| Frontend Event 集成 | MEDIUM-HIGH | SSE 单生成器✅；死监听/死状态/无 replay(§5) |

---

## 12. P0 / P1 / P2（严格按定义）

### P0
1. **断连/取消 = 只改 DB 状态、不中断在飞执行**（webapp.py:412-424 与 265/345 未 cancel run_task）→ 用户取消后副作用继续、尾部执行无 assistant 消息、状态机终态再转换异常路径。生产任务不可恢复/重复副作用。（§3.2；E2E T8 只证明 DB 层正确）
2. **写文件副作用 ↔ 记录窗口非原子**（工具执行后内存 ledger，attempt 结束才 ingest）→ 崩溃丢事实、重放副作用、无 exactly-once（§3.12）。判定依据含"会造成数据损坏/重复副作用/生产任务不可恢复"，列为 P0 最小修复。

### P1
1. **Provider streaming 无统一重试/fallback/attempt**（生产主链形态）；墙钟默认关、流式无 idle 超时（§3.3/§3.5）。
2. **Approval decide TOCTOU**（UPDATE 无 WHERE pending）+ resume 重放语义的重复副作用面（§3.7）。
3. **同容器并发无互斥**：共享 SDK 会话历史/文件写竞争/run_index TOCTOU——收敛为"同容器单 Active Run"串行策略（§3.6/§10）。
4. **Sources 生命周期**：ready 前静默无信号、删除/索引竞态孤儿、崩溃卡 parsing/indexing、rollback 状态不一致（§3.13）。
5. **RAG 注入结构性边界缺失**：Source 与 System policy 同层、DANGEROUS_PATTERNS 死代码、input guardrail 零覆盖（§3.4）。
6. **FileScope 默认 fail-open + 未知工具默认放行**（§3.11）；**context overflow 无压缩兜底**（§3.10）；**GET 带副作用建 Run + 无幂等键（Live 证实重复）**（§3.1）。

### P2
- 前端死事件监听/WAITING_USER 死状态清理；registry/broker/spec 展示层收敛；run_python 网络 OS 级 enforcement（外部任务）；41 工具全量 schema 字节基线测量；trust 文本标记升级评估；工具参数 provenance 轻量闸（仅高危删除/覆盖类）；Tool Readiness 文档化；README 模板数 17→19 口径、工具数同步；流式 attempt=0 语义注释。

---

## 13. Final Freeze Decision

**Runtime 是否可以进入 Final Integration & Architecture Freeze？**

> **有条件进入（YES WITH CONDITIONS）。**

必须冻结、不再大改的模块（冻结区）：
- run_turn 单门面 + execute_turn 唯一执行器 + 状态机(8 态) + TaskManager 持久层 + Completion Gate + FileScope/Approval 包装层 + 双库协议 + SSE api_stream 单生成器 + AgentRuntime.runctx 隔离 —— **这些结构本阶段验证为真实单链，不再做架构级拆改/重建/更名**。

进入冻结前必须先完成的 3–5 个条件（按序）：
1. **P0-1**：断连/取消语义修复（在飞 run_task 取消 + 断开≠取消区分 + 尾部结果落库），随附一个"断连后副作用终止"E2E。
2. **P0-2**：写副作用 write-ahead（执行前落 pending 行）最小协议，随附崩溃注入测试（进程 kill 后状态一致性断言）。
3. **P1-3**：同容器"单 Active Run"串行策略（409/排队）+ Provider streaming 统一重试/超时（墙钟默认开）。
4. **P1-5**：Sources ready 信号 + RAG 注入块内容闸/降权 + Approval decide 原子化 + GET→POST 幂等收敛（四小项可并行，均为行级改动）。
5. 前端死监听与 PAUSED 语义前后端对齐（P2 中最小集，随 1-4 一并交付）。

完成以上后：**冻结 Runtime**，进入产品功能与用户体验阶段；本轮不再执行任何下一轮"架构收口/重构"。

---

## 附 A：测试证据清单（本次实测）

| 项 | 结果 | 环境 |
|---|---|---|
| tests/run_tests.py 全量离线回归 | **453 OK / 106s**（含本次新增 2 条路由回归） | 离线 |
| tests/e2e_tmr.py（TMR E2E：3 Run/审批 resume/断连取消/legacy） | **PASS 21 / FAIL 0** | Live 后端+真实网关 |
| 重复 URL GET 探针（identical URL ×2） | **2 个独立 Run 均 completed（证实无幂等）** | Live |
| test_provider_errors（故障注入 401/403/404/429/503/no-channel/timeout） | 11/11 PASS | 离线 stub |
| test_completion_gate / test_verification / test_context_guard / test_approval / test_p1_reliability / test_guardrails / test_tool_router(含新增) | 全部 PASS | 离线 |
| provider Live 正常路径 | E2E 全程多次模型调用成功（reply_delta 流式正常） | Live |
| Live 故障注入（mid-stream 断流/墙钟/进程 kill 恢复） | **NOT VERIFIED**（不做破坏性真实任务注入；以代码取证+离线注入替代，如实记录） | — |

## 附 B：审计残留说明
- 本复核在 agent.db 产生的探针容器（audit-probe-*）与 E2E 容器已清理（tasks/runs/messages/events/tool_calls/model_calls/approvals/artifacts/checkpoints + sessions.sqlite 对应行，left_tasks=0 复核）。
- 与基线差异说明：基线 446 项→本次 453 项（含上轮 PPT 修复新增 1 个测试方法 + 跨文件 3 条断言并入既有用例后的净增）；"2 skipped 主题 CDP"本次未出现（后端在线的环境下 CDP 用例被执行）。

## 附 C：评级修订对照（相对 2026-09-06 基线）
下调：Provider retry/fallback（COMPLETE→PARTIAL，仅非流式）、Cancellation/Run lifecycle（COMPLETE→P0 收口项）、并发隔离（COMPLETE→PARTIAL）、Trust Boundary（COMPLETE→PARTIAL）、Wall timeout（COMPLETE→默认关 PARTIAL）、Token 预算（PARTIAL 保持并明确缺口）、Sources 生命周期（COMPLETE→PARTIAL，检索核心除外）、Approval（COMPLETE→PARTIAL 边界）、Crash Recovery（COMPLETE→PARTIAL 窗口）。
保持：主链单链性、Memory、Completion 证据语义、Repair 有界、双库持久化协议、SSE 单生成器。
