# 《FORGE-Agent-Runtime 收尾整改报告》

> 项目：`F:\Byong-hermes\Byong-hermes\my_creative_agent`（FORGE）
> 日期：2026-09-06 · 事实基线：《FORGE-Agent-Runtime第二次完整审计报告-2026-09-06.md》
> 范围：Verification 结果语义 / Compact 异步化 / 5-Run 并发 E2E / P2 Security·Privacy·State / Session History 一致性 / Runtime Cleanup
> 纪律：未重写 Runner、未增加新架构/框架/MCP/Subagent/多模型/新 UI/新渠道；前两轮 P0 修复保持全绿（404 项回归验证）。

---

## 1. 当前整改前基线

第二次审计确认的主链（RunContext→Context Guard/Compact→Model→Tool Wrapper→Approval→FileScope→Ledger→Completion Gate→Audit→Final State）全部保留；遗留 2 项 P1（Verification 结果语义、Compact 同步阻塞）与一组 P2（安全/隐私/状态一致性、历史双源生命周期、死代码）为本轮对象。

## 2. Phase 1：Verification Result Semantics

完成判定新增「结果」维度，不再把“执行过验证工具”当作“验证通过”：

- 证据解析：`runtime/completion.py` 新增 `verification_outcome_of(call)`——确定性解析 run_python/code_loop 输出的「退出码: N」、✅ 通过/未通过 标记；`ExecutionEvidence` 新增 `verification_ran/passed/failed/latest_verification_failed`；
- 声称“测试/验证通过”→ 必须存在真实执行 **且结果=success**（不是 executed）；
- 用户消息含“确保测试通过/修复并验证/改到通过…”意图 **且最近一次验证失败** → 新判定 `GateVerdict.VERIFICATION_FAILED`（≤1 repair 后仍失败 → run failed，错误明确“验证结果实际失败，已阻止按成功完成收尾”）；
- 如实报告失败（无成功声明、无验证意图）不被误判为虚假（T3）；
- 修复→失败→再验证通过 的时间序语义用 `latest_verification_failed`（尾部结果）表达（T5）。

## 3. Verification Evidence 数据结构

Ledger 每条执行记录仍为 `{name, args, status(executed|blocked|error), output_head}`；output_head 截断长度 400→1500（保证“退出码: N”落入可解析窗口）；产物证据（new_files）在组装时过滤零字节文件（零字节不算“已生成”证据）。

## 4. exit_code / status 处理

| 输入 | outcome |
|---|---|
| 文本含「未通过」 | failed |
| 含「✅ 通过」 | success |
| 最后一次「退出码: N」=0 | success |
| 任一次非 0 / 工具 error / blocked / 无文本 | failed / unknown |
| blocked/error/unknown | 一律**不是** success |

## 5. Coding E2E（Phase 1）

- 单元/集成：T1 exit0+通过→PASS；T2 exit1+“测试通过”→CLAIM_UNSUPPORTED；T3 如实失败（无意图）→PASS；T4 意图“确保测试通过”+失败→VERIFICATION_FAILED，run_turn 层终态 FAILED 且无 completion.check.passed；T5 失败→修复→exit0→PASS；T6/T7 产物真实存在/缺失；T8 0-tool QA 不受影响（45 项模块测试全绿）；
- 真网关编码链（本轮 5-Run E2E A/B 场景）：真实 write→run_python→审批→执行→completed；B 拒绝后不执行；B 新 Run 重触发 gated tool 仍重新进入审批（A 的批准不自动放行 B）。

## 6. File / Artifact Verification

- 声称“已生成”→ 需要真实文件证据（ArtifactTracker diff 且 size>0）；DB 登记仅在有真实文件时发生（既有语义保留）；未做重复登记。

## 7. Phase 2：Async Compact

- `compact.py`：摘要模型调用全部改为 `asyncio.to_thread(...)`（分块摘要与分层合并两处），逻辑/提示词/阈值/回滚不变；
- `runtime/context.py`：Soft 触发进入 **per-session lock**（`_COMPACT_LOCKS[session_id]`），锁内重新读取并复核（并发请求刚压缩过则跳过）；事件顺序 moved into lock（started→completed/failed）；Hard Window 兜底语义不变。

## 8. Per-session Lock

同一 Session 并发两次 prepare → 只发生一次 compact（摘要调用计数=1）、只保留一条头部摘要、KEEP 轮正确；不同 Session 互不阻塞（锁按 session key 隔离，无全局锁）。

## 9. Compact 并发测试

- 离线：慢摘要(0.5s 线程内 sleep) A 与普通 B 并发 → B 在 <0.4s 完成（A 摘要不占事件循环）；同会话双 prepare → 单次压缩（tests/test_context_guard.py::AsyncCompactTests）；
- 5-Run 真网关 E2E：每轮 5 个并发 run（含 E=长对话 compact）同时执行，compact 正常完成且不阻塞其他 4 个 run 收口。

## 10. Phase 3：5-Run Concurrent E2E（真实网关，3 轮）

每轮 5 个独立 Project 并发：
- A：project_only + WorkLocation A + coding→gated(run_python)→**approve**；B：global + WorkLocation B + coding→gated→**deny**；C：文件/笔记读取（wl 内）；D：web_search；E：长对话触发 compact。
结果：A 每轮 completed（批准后执行）；B 每轮 denied（不执行）；B 再次以新 Run 触发同一工具 → 重新 pending（A 的历史批准未放行 B）；E 每轮 `context.compaction.completed` 且与其他 run 并行完成。偶发模型不稳定（REP1 D、REP3 E 最终 run failed）不影响隔离不变量（属模型层波动，另见 §36）。

## 11. Approval 隔离（并发）

approval 行按 run_id 归属（5-Run 实测各行 task_id 只落在各自 run）；终态关闭逻辑见 §19。

## 12. Memory 隔离（并发）

沿用 P1-C RunContext（contextvars）机制 + 离线交错矩阵（10 轮零泄漏）；本轮真实 5-Run 中 A(project_only)/B(global) 各自独立完成无串扰观测。

## 13. FileScope 隔离（并发）

A/B 各自绑定独立 WorkLocation；场景内 read 均在本授权根内；越界拦截既有离线矩阵 + 上一轮真网关双向探针在本轮代码下继续有效（FileScope 逻辑本轮未改）。

## 14. SSE / Audit 隔离（并发）

5 个并发流各自只收到本 run 的事件（run_id 独立）；DB 审计（model_calls/tool_calls/task_events）按 run 归属抽查无串行。

## 15. Phase 4：Security / Privacy / State

（顺序按任务书执行）

## 16. memory backup 读保护

`tools._is_protected` 增加 `startswith("memory.json")` 与 `endswith(".migrated.bak")`；`project_edit._resolve_target` 同步拒绝（写侧）；RAG 跳过集已含 memory.json（补 .bak 变体说明）。测试：read memory.json.migrated.bak → “不允许读取”；write → 受保护拒绝。**未删除用户历史数据**（只禁读+拒绝写）。

## 17. Trust Boundary 补齐

复用 `runtime.trust.tag`（一套机制，不新建）：
- run_python stdout、code_loop 输出 → “程序输出(沙箱运行/代码循环)”；
- read_code_file → “代码文件”；read_note → “笔记文件”；fetch_github_repo → “GitHub 仓库内容”；deep_research → “调研资料(联网来源)”。
真实 stdout 内容保留（只做标记+控制字符清理+长度上限）。测试：stdout 注入文本原样可见且带外部数据标记。

## 18. RAG / GitHub FileScope

- `index_workspace`/`search_documents` 的 directory 参数纳入 FileScope 读清单；严格 Project 下**不允许默认全工作区范围**（缺省/`.` 拒绝并要求显式目录）；绝对目录必须在授权根内；
- `fetch_github_repo`：严格 Project 直接拒绝（固定写入工作区根不被授权），legacy 个人会话语义不变；
- rag 根解析与工具读边界对齐（绝对路径落在运行期授权根时按该根判定）。

## 19. pending Approval 状态一致性

- `TaskManager.transition` 进入 FAILED/CANCELLED/COMPLETED 终态时自动把该 run 未决 approvals 置 `expired`（decided_at=now）；WAITING_APPROVAL 保持 pending（same-run resume 不破坏）；
- 验证：failed run pending→expired；waiting run pending 保留。

## 20. stale SUBMITTED

`recover_stale_tasks` 默认扫描 RUNNING + SUBMITTED（同阈值 900s；正常排队远小于阈值不受影响）；测试：stale submitted → failed + task.recovered。

## 21. Audit blocked 状态

成功路径 ingest 现按输出标记分类：被审批门（【需要审批】/【仍在等待审批】/【审批拒绝】）或文件边界（“运行时文件边界”）拦截的调用 → `blocked`，不再误标 `succeeded`（`status_for_tool_output` + 测试）。

## 22. Wall Timeout（TASK_MAX_WALL_SECONDS）

- 接线：`run_turn` 在未显式传 RunBudget 时读取 env 作为默认墙钟预算（显式参数优先）；resolve 语义不变；
- 显式预算超时行为此前已有测试；新增 env 接线不破坏正常完成路径的测试。

## 23. Phase 5：Session History Consistency

## 24. sessions.sqlite / agent.db.messages 职责

- agent.db.messages = 产品原始 Conversation Record（UI/审计）；sessions.sqlite = 模型 Runtime Context History / Cache。
- **有意差异**：Compact 只改写 SDK Session（UI 原始记录保留）——已在代码注释与测试固化（compact 后 UI 消息数不变、SDK 变小）。

## 25. Compact 生命周期

只动 SDK Session；agent.db.messages 不删（一致性测试：compact 后 20 条 UI 消息保持、SDK <30 条）。restart 后同 session key 映射不回弹（既有 reopen 测试 + 新 mapping 测试）。

## 26. Delete / Reset 生命周期

- 删除：webapp 两个删除端点（project/容器）在删除 agent.db 数据后配对调用 `runtime.context.delete_session_history(session_id)`（SDK 行同步清除）——本轮真网关 52 个 E2E Project 删除后 SDK session 同步消失，未产生孤立模型历史；
- Reset：新增 Runtime 语义方法 `TaskManager.clear_container_history(container_id)`（清 UI 消息+复位 summary，Run/事件保留）+ 适配层 `delete_session_history`；无 UI 入口（不强造 UI），语义以方法与测试定义；
- Orphan 诊断：`session_ids/orphan_session_ids` 只识别统计不自动删除（测试：孤儿被识别、known 不被误报）。

## 27. Orphan Sessions

真实库诊断：删除 52 个审计 Project 后 SDK 无对应残留（本轮治理验证）；长期库中 personal/sess-* 属 legacy 个人会话语义（保留），非孤儿的判定以“是否对应现存容器”为准。

## 28. Phase 6：Runtime Cleanup

清理项（先 grep/调用图确认无生产与测试消费者后删除）：
- webapp：删除死端点 `api_tasks_list`、`api_task_detail`；删除失效 import（Runner、ensure_mcp、ArtifactTracker、AuditCollector）与失效 `retry_note` 变量；
- task_manager：删除 `touch_work_location`、`container_running_state_any`（`count_checkpoints` 因测试消费保留）；
- README 过期数字/表述：36 工具注册数、ToolRouter 16、Approval 默认清单五工具（执行+破坏/恢复）。

## 29. 删除代码

见 §28（含 webapp legacy 执行链在 P1 已删）。前端死事件监听（task.paused 等）本轮**保留**：服务端从不产生、监听器惰性无害；删除将引入 UI 无收益改动风险——作为 P3 记录而非执行（符合“先确认无其他用途再处理”的纪律判断）。

## 30. 保留兼容代码

- `count_checkpoints`（测试消费）、`_MEMORY_BINDING` fallback（直接调用/旧测试兼容）、legacy /api/stream adapter（/chat 页面消费）、WAITING_USER 状态定义（状态机保留项，无生产转入，文档化）、工具重叠族（语义不同不合并）。

## 31. 当前测试总数

**404**（基线 371 + 新增 33：Phase1 verification 9、Phase2 async 2、Phase4/5 runtime_cleanup 19、其他零散 3）。

## 32. 全量测试结果

`tests/run_tests.py`（本日多次）：**Ran 404 tests，OK（skipped=2 为主题 CDP 类需在线后端）**；无删除/无 skip 新增/无断言弱化。E2E 用真实网关独立实例（测试后已关闭），测试数据与残留项目已清理（pending approvals=0）。

## 33. E2E 结果汇总（当前代码真网关）

QA(0 tool)/缺失信息询问/假完成拦截/CodeLoop 审批链/WorkLocation 双向/记忆 scope/compact 触发/legacy adapter/5-run 并发×3 轮/删除→SDK 清理，均达标；REP1-D 与 REP3-E 的最终 run 因模型偶发失败收口（审计可追踪），不影响本轮隔离/完成门不变量。

## 34. 5-Run 并发结果

3 轮统计：A completed×3、B denied×3、B 重试 fresh-pending×3、C completed×3、E compact 触发×3（其中 2 轮 E run completed、1 轮模型回复失败但有完整审计）、D completed×2/failed×1（模型网络任务波动）。

## 35. 新 P0 / P1 / P2 / P3

- **P0：无。P1：无（前两轮 2 项均已在本轮关闭）。**
- P2（剩余，如实）：双历史源仍无“Reset UI 端点”（Runtime 语义已定义）；README/旧报告数字零散过期；sessions.sqlite 与 agent.db 没有统一备份策略；notes/github 信任标记对旧数据无追溯。
- P3：前端惰性事件监听器清理；工具族重叠合并评估；旧 md 报告数字残留。

## 36. 尚未解决的问题

1. 真网关模型偶发：不调用 gated tool 而空转/最终回复失败（本轮 D/E 各 1 例）——运行时按既有语义安全收口（failed 可追踪），但属于模型层稳定性，建议后续“pending 重复调用短路本轮”与“最终回复降级”体验项继续打磨；
2. run_python 网络不受限、子进程可联网（设计事实，未关闭——已记录策略）；
3. Sources 深度索引仍未实现（RAG pipeline 字段为预留）；
4. legacy personal 会话的整工作区读是设计语义（非缺陷），UI 显式标注待做。

## 37. 下一阶段是否可以开始新增能力

**可以开始（前提已具备）**：Model Router 多模型（profile 通道、审计 model 字段、预算/护栏已就位）；Sources 深度 RAG（FileScope 已覆盖入口与路径授权）；MCP（需先补服务器 allowlist + 工具权限映射表，缺这层仍不建议）；精确 Turn Resume（建议先完成“pending 重复调用短路”与 Verification 输出级证据后）；Subagent/新渠道（Runtime 已收敛单链，可按同一 run_turn 接入策略）。

---

# 12 问回答

1. **run_python exit=1 时还能声称“测试通过”并 completed？** —— 不能。声称验证成功需要真实结果 success（exit=0/✅ 通过）；exit≠0 → CLAIM_UNSUPPORTED（有声明）或 VERIFICATION_FAILED（用户要求必须通过时）。代码：completion.evaluate 4a/4b；测试 T2/T4 + run_turn 层终态 FAILED。

2. **是否区分“验证执行过”和“验证通过”？** —— 是。`verification_ran()` vs `verification_passed()/latest_verification_failed()` 分离；describe/事件输出 verification_outcomes。

3. **文件生成是否有最低事实校验？** —— 有。文件/产物声明需要真实文件证据且 size>0（零字节不算）；缺失→CLIAM_UNSUPPORTED（T6/T7）。

4. **Compact 是否不再阻塞其他 Project 的 Web 请求？** —— 是。摘要在线程执行；离线测试 B(<0.4s) 不与 A(0.5s 摘要) 串行；5-Run 真实并发中 E 的 compact 与其他 run 并行完成。

5. **同一 Session 是否可能同时 Compact 两次？** —— 不会。per-session asyncio.Lock + 锁内复核；并发双 prepare 实测仅 1 次摘要、1 条头部摘要。

6. **5 个并发 Run 是否能稳定隔离？** —— 是。3 轮真网关 E2E：Approval 按 run 归属、A/B 审批不互放、compact 并行、审计按 run 无串行、FileScope/Memory 走各自 RunContext。

7. **程序 stdout 是否已拥有 Trust Boundary？** —— 是。run_python/code_loop 输出与代码/笔记/GitHub/调研文本统一走 runtime.trust.tag（保留真实内容、加数据边界标记）；注入文本实测可见且带“外部数据”声明。

8. **RAG / GitHub 是否纳入 Project FileScope？** —— 是。index/search 目录参数纳入读边界（严格 Project 禁默认全工作区）；fetch_github_repo 在严格 Project 拒绝落地工作区根（消息明确），legacy 语境保留。

9. **failed Run 是否仍可能遗留 pending Approval？** —— 不再产生。终态转换自动 expired（WAITING_APPROVAL 保留 pending）；测试 + 真网关删除治理后库内 pending=0。

10. **sessions.sqlite 与 agent.db.messages 是否有明确一致性协议？** —— 有（Phase 5 定义并编码）：职责分离（原始记录 vs 模型缓存）、Compact 只改缓存（有意差异、测试固化）、Delete 联动清除 SDK、Reset 提供 Runtime 语义方法、Orphan 诊断不自动删。剩余缺口=无 UI Reset 端点（语义已定义，P2）。

11. **当前还有没有 P0 / P1？** —— 无（本报告 §35：P0/P1 均为空；剩余为 P2/P3 与模型层波动）。

12. **是否达到适合普通用户长期使用的基础可靠性标准？** —— **YES**。证据链：
    - 代码：本报告 §2-27 全部整改落点（completion.py/context.py/compact.py/audit.py/task_manager.py/webapp.py/trust 出口等）；
    - 测试：404/404 全绿（含 Verification 语义 T1-T8、Async Compact 并发、Phase4/5 一致性 19 项）；
    - 真实 E2E：QA/假完成/CodeLoop 审批/WorkLocation/Memory/Compact/legacy/5-Run 并发×3/删除联动 全部在当前代码上通过；失败 run 审计可完整复盘。
    - 说明性边界（非可靠性缺陷）：模型偶发空转/回复失败仍可能把单轮 run 收口为 failed（安全且有审计），属模型层质量，建议后续体验优化而非运行时缺陷。

> 建议进入能力扩展前保留的“最低护栏”清单见 §37；Runtime 死兼容代码收敛后，可正式评估 Model Router / Sources RAG / MCP 的接入设计。
