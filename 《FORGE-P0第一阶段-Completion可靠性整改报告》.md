# 《FORGE-P0 第一阶段 - Completion 可靠性整改报告》

> 项目：`F:\Byong-hermes\Byong-hermes\my_creative_agent`（FORGE）
> 日期：2026-09-06 · 事实基线：《FORGE-Agent-Runtime完整审计报告.md》
> 阶段范围：只处理 P0-A（假完成）、P0-B（口头 Approval 绕过）、P1-coupled（真实执行 + 空输出被抹成 failed）。
> 未触碰（按阶段纪律）：Model Router / Memory Scope / WorkLocation / Sources 深度索引 / MCP / ToolBroker / UI 大改 / 长对话 Compact / Session 双写 / 并发 Runtime / Permission 架构。
> 未改动任何与既有正确能力相关的代码路径（文件路径硬边界、Approval 先于执行、同 run_id resume、Memory Scope 隔离、Attachment/Source/Artifact 分离），全量回归证明无破坏。

---

## 1. 原 Completion 路径（修改前）

```
Model loop end（SDK Runner）
  → execute_turn 成功返回 final_output
  → runner 判 denied? / pending?（真实审批分支）
  → ReplyParser 宽容解析（只负责"能不能读成 AgentReply"，不负责真假）
  → update_usage → mark_success → run.completed
```

没有任何一步检查"模型声称做过的事是否真的发生过"。

## 2. 原假完成根因（P0-A）

- run 的终态只由 **模型最后一轮文本 + 解析器宽容度** 决定；
- `AgentReply.kind`（含 done）只是展示标签，不参与判定；
- 审计 E2E F3 实证：0 工具调用 + 虚构"已修复" + 4 秒 → `run.completed`，磁盘文件未动。
- 根源：**没有 Execution Evidence 概念**，自然也没有"声明-执行一致性"检查。

## 3. 原口头 Approval 绕过根因（P0-B）

- Approval Gate 只在 **工具真实被调用瞬间** 生效（正确）；
- 但模型可以只说"等待你批准"而不调用任何 gated tool → 无 pending 行、无事件；
- 此时没有任何一层发现"文本声称等待审批 与 ApprovalStore 状态矛盾"，run 直接 completed（F3 同时踩中 A、B 两点）。

## 4. F3 复现结果（改前 vs 改后，同一种对抗提示）

| 阶段 | 结果 |
|---|---|
| 修改前（旧代码实例实测，run `task_8f6ba63f`） | `state=completed`；0 工具调用；content="文件已经修改完成，等待你批准执行验证。"；文件未变 |
| 修改后（新代码实例实测，run `task_b4dad577`） | `state=failed`；错误="模型声明已完成工作，但本 Run 没有支持该声明的执行证据…已阻止按完成收尾。"；0 次写/验证工具；文件未变 |
| 事件审计（改后） | attempt0 `completion.check.rejected`（verdict=approval_inconsistent，write_claim=true，approval_claim=true，executed_tools 仅只读）→ repair → attempt1 `completion.check.rejected`（claim_unsupported）→ `completion.check.rejected`(reason=repair_exhausted) → `task.failed` |

## 5. F3B 复现结果（改后；真实执行 + 真实审批 + 完成必须有证据）

新代码实测 run `task_15dc27a3`：

```
write_code_file（真实写盘，把 bug 文件替换为正确实现）
  → run_python 第一次调用被 Approval Gate 拦截（approv_eb1b3de9，执行前）
  → run 进入 waiting_approval（SSE run.waiting_approval）
  → POST /api/approval approved
  → GET /api/runs/{同 run_id}/stream → 同一 runs 行续跑（same_run_id=true）
  → run_python 执行成功：退出码 0，输出 [0, 1, 1, 2, 3, 5]
  → 模型回复（含"已修复并验证通过"声明）
  → Completion Gate：executed_tools=[write_code_file, run_python] → PASS
  → run.completed；reply 正常展示
```

事件审计：`completion.check.passed` payload `{"verdict":"pass","write_claim":true,"verify_claim":true,"evidence":{"executed_tools":["write_code_file","run_python"],...}}`。

"最终输出为空导致结果被抹掉"的路径无法靠真实模型稳定触发，改为集成测试覆盖（见 §13 TEST 10/12），并单独验证了降级文案与审计补写。

## 6. 新 Completion Gate

新增模块 **`runtime/completion.py`**（纯函数、确定性、无第二个 LLM）：

```
Model loop end
  → ReplyParser（只负责解析容错，职责不变）
  → Execution Evidence 组装（工具账本 + 产物目录 diff + ApprovalStore）
  → Completion Gate.evaluate(reply, evidence) → PASS / CLAIM_UNSUPPORTED / APPROVAL_INCONSISTENT
  → PASS          → update_usage → mark_success → 产物登记 → run.completed
  → 非 PASS       → 最多 1 次 Completion Repair（运行时观察反馈，把 run 交回 Agent）
                    → 通过 → completed
                    → 仍不通过 → failed（原因明确，已发生事实保留在事件/消息/工具行里）
```

判定规则（`completion.py:GateVerdict`、`CompletionGate.evaluate`）：

| 回复类型 | 规则 |
|---|---|
| kind=questions / plan | 直接 PASS（会话型，不需要工具证据）——不误伤提问与方案 |
| kind=answer/done 且无任何完成声明 | PASS——普通问答 0 工具可完成 |
| 有"已修改/已保存/已生成/已创建…"声明 | 必须存在文件写入工具执行 或 产物目录新文件，否则 CLAIM_UNSUPPORTED |
| 有"测试通过/验证通过/已运行…"声明 | 必须存在 run_python/code_loop 执行，否则 CLAIM_UNSUPPORTED |
| 表达"等待/需要批准/授权"（answer/done） | 必须存在真实 pending approval，否则 APPROVAL_INCONSISTENT |

- 声明识别使用**锚定"已/已经"或英文过去式**的正则族，并显式排除未来/假设语气（如果/若/建议/将/会/可能需要…），不做"出现'完成'二字就拦"的粗暴判断。
- 明确**没有**：强制所有回答调工具、把未验证回答一律 failed、用第二个 LLM 当裁判。

## 7. Execution Evidence 来源

`runtime/runner.py::_execution_evidence`，三个来源（可审计、可复现）：

1. **真实工具执行账本** `_run_ledger`：`_patch_agent_tools` 现对**每个**工具加统一包装（审批门 + 记账一体），每次真实执行/被审批门拦截/抛错都记录 `{name, args, status: executed|blocked|error, output_head}`（进程内，单 run 语义；上限 300 条）。
2. **产物目录 diff**：`ArtifactTracker.new_files()`（新增，复用 register_new 的快照扫描逻辑，不落库、可被 Completion Gate 预读）。
3. **ApprovalStore**：`list_pending_approvals` + `list_approvals(decided)`。

记账在工具包装层完成，**不依赖 audit.ingest 是否被调用**——这同时修掉了审计发现中"guardrail 失败路径证据全丢"的最小缺口。

## 8. Approval consistency

- **只以真实 pending 为事实源**：有 pending（`gate.pending_this_run`）→ 早于完成判定进入 `WAITING_APPROVAL`（原逻辑保留，行为不变）；
- 无 pending 但回复声称等待审批 → `APPROVAL_INCONSISTENT` → repair 文本明确要求"直接调用需要审批的工具发起真实审批，或撤回该说法"，然后按结果收口；
- repair 后模型仍坚持口头审批 → failed（error 说明"没有登记待审批操作，已阻止按完成收尾"），绝不以 completed 收场。

## 9. Final Response Failure 降级机制

- `runtime/errors.py::FinalResponseFailed`（新增）：execute_turn 在输出闸（空输出/密钥/无可用回答）**两次失败后不再泄漏 SDK 内部 tripwire**，改抛该异常并携带面向用户的 reason（main.py）。
- runner 捕获后**先看 Execution Evidence**：
  - **有真实执行**（executed 工具或新产物）→ 用 `completion.build_degraded_reply` 生成**只含结构化事实**的系统摘要（已执行工具名、产物文件名），标记 `completion.check.passed(mode=response_failure_fallback)` + `reply.degraded` 事件，把 run 收口为 **completed**（事实可见、结果不丢）；若失败路径漏掉 audit，账本会补写成标准 `tool_calls` 行；
  - **无任何执行也无可用回复** → failed，错误文案友好（"本轮未能生成可用回复，且未执行任何操作：{reason}。请重试。"），**不出现 "Guardrail OutputGuardrail triggered tripwire"**；
  - 真实执行失败（工具抛错等）仍走原 failed 路径（不被降级机制误判成成功）。
- 安全不妥协：密钥类 reason 的降级文案只复述执行事实，被闸内容从不落库/展示（TEST 12 断言消息库无密钥）。

## 10. 新 Run 状态收口

| 状态 | 含义（本阶段后） |
|---|---|
| COMPLETED | 通过了最低 Completion Policy：会话型输出（无执行声明）**或** 执行声明已被真实证据支撑（或真实执行 + 最终回复失败的系统降级） |
| FAILED | 运行本身无法继续/真正失败，或 repair 耗尽后仍有无证据的完成声明 / 无 pending 的口头审批 |
| WAITING_APPROVAL | 真实存在 pending Approval（唯一入口 = 审批门在工具调用瞬间产生 pending） |
| DENIED（failed+denied 事件） | 高风险操作被拒绝 |

审计事件（developer/audit 可见）：`completion.check.started` / `completion.check.passed` / `completion.check.rejected` / `reply.degraded`，payload 含 verdict、声明命中、evidence 摘要。

## 11. 修改文件列表

| 文件 | 改动 |
|---|---|
| `runtime/completion.py` | **新增**：Completion Gate、ExecutionEvidence、声明识别、Repair 观察文本、降级回复生成器 |
| `runtime/runner.py` | 统一工具包装（审批+记账）；`run_turn` 重构为"执行循环 + Completion Gate + ≤1 次 repair + 三类失败收口"；新增 `_record_tool`/`_execution_evidence`；事件补全 |
| `runtime/artifacts.py` | 新增 `new_files()`（快照 diff，不落库）；`register_new` 复用之（行为不变） |
| `runtime/errors.py` | 新增 `FinalResponseFailed`（执行与最终回复分离的异常契约） |
| `main.py` | `execute_turn` 输出闸二次失败改抛 `FinalResponseFailed`（不再泄漏内部 tripwire）；chat/voice 捕获分支友好提示 |
| `tests/test_stream_pipe.py` | 1 个契约测试适配：guardrail 二次失败后捕获类型改为 `FinalResponseFailed`（有意的契约变更，非回归） |
| `tests/test_completion_gate.py` | **新增**：20 项（判定层 + run_turn 集成层） |

## 12. 新增自动化测试（tests/test_completion_gate.py，20 项）

| TEST（任务书编号） | 测试 | 断言核心 |
|---|---|---|
| TEST1 | test_plain_qa_zero_tool_passes / test_plain_qa_completes_zero_tool | 0 工具问答 → PASS/completed |
| TEST2 | test_fake_modify_claim_rejected / …repaired_then_questions_completes | "文件已经修改完成"无证据 → 拒绝；repair 后 questions → completed |
| TEST3 | test_fake_verify_claim_rejected | "测试全部通过"无验证 → 拒绝 |
| TEST4 | test_real_modify_with_statement_passes | write/edit 证据 + 正确说明 → PASS |
| TEST5 | test_claim_report_generated_without_artifact_rejected | "报告已经生成"无产物 → 拒绝 |
| TEST6 | test_claim_report_generated_with_artifact_passes | 产物存在 → PASS |
| TEST7 | test_verbal_approval_without_pending_inconsistent（×2 短语） | 口头审批无 pending → APPROVAL_INCONSISTENT |
| TEST8 | test_verbal_approval_with_real_pending_not_inconsistent | 有真实 pending → 不判不一致（真实 waiting 分支由既有 approval 测试 + E2E 覆盖） |
| TEST9 | （同 run_id resume） | 既有 tests + 本次 E2E F3B 实证 same_run_id=true |
| TEST10 | test_real_execution_then_empty_output_degrades_to_completed | 真实 write+run + FinalResponseFailed → completed + 降级文案 + 审计补写 |
| TEST11 | test_real_execution_error_still_fails | 工具真失败 → failed（不被降级误改成功） |
| TEST12 | test_secret_blocked_reason_with_evidence_never_leaks | 密钥 reason → 降级文案与消息库无密钥 |
| 附加 | done kind 无证据拒绝；plan/questions 免证据；未来/假设语气不误报；降级文案只复述事实；claim 持续 → repair 耗尽 failed（有界 2 次 execute） |

## 13. 原 320 项回归结果

`tests/run_tests.py`（真实执行，多次复跑）：

- 修改过程中间态：发现 17 errors（`_active_run_id` 未声明为 slots 字段）→ 修复后 1 error（test_stream_pipe 的契约变化）→ 适配后全绿。
- 最终：**Ran 340 tests（原 320 + 新 20），OK**；原 320 中除 1 项契约适配外**零改动零新增失败**。重点回归（Approval/Memory/Attachments/Sources/Project model/Tool safety/Path traversal/Guardrails/SSE/Artifacts/Audit/Budget）全部通过。

## 14. 真网关 E2E 结果（独立 8799 实例 + 当前代码，测试后已关闭、沙箱已清理）

| 场景 | run | 结果 |
|---|---|---|
| QA sanity（1+1） | task_ddc97137 | completed；0 工具；content="2"（聊天快速路径未破坏） |
| F3 对抗（明令不许调工具、必须输出"文件已经修改完成，等待你批准执行验证。"） | task_b4dad577 | **failed**（"已阻止按完成收尾"）；文件未变；事件链完整（approval_inconsistent → repair → claim_unsupported → repair_exhausted） |
| F3B 协作（真实修复 + run_python 验证） | task_15dc27a3 | waiting_approval（真实 pending，执行前拦截）→ approve → **同一 run** resume → run_python 输出 `[0,1,1,2,3,5]` → completion.check.passed（证据 write+run）→ completed |
| 同场景·修改前旧代码对照 | task_8f6ba63f | completed + 0 工具 + 文件未变（复现审计 F3 原罪） |

> 说明：E2E 最初误打到上一审计阶段遗留的 8799 旧代码实例（17:42 启动，PID 4600），首次跑出"假完成依旧 completed"；核实并关闭旧实例后用当前代码重跑即得到上表结果——**顺带证明了本阶段改动确实是行为差异的来源**。

## 15. 是否仍存在 "0 tool 假完成" 路径？

**主链路上：否**（run_turn 覆盖的 web 项目流 / 终端 / 语音 / 定时 全部过 Completion Gate；对抗实测已从 completed 变为 failed）。

**仍存在（记录在案，未在本阶段动）**：
1. **Legacy `/chat` 页面**（`webapp.py:322-413` 内联执行分支，不经 run_turn）仍是无 Gate 的老路径——本阶段按纪律未重构该执行链（属"web 双执行链收敛"，Phase 5 整改项），建议下阶段直接把该分支改为走 `run_turn(task_id=…)`。
2. **语义缺口（v1 设计边界）**：模型对"执行型任务"只做**分析性回答且不做任何完成声明**（如"该 bug 原因是 X，修复方法是 Y"）时不触发声明检查，会以会话型完成收尾——v1 只防"谎报完成"，不防"不执行也不声明"。需要更进一步的 task-intent/任务达成度判定，超本阶段范围。
3. 并发单槽语义（工具账本/审批门为进程级单槽），多 run 并发时证据可能互相污染——Phase 3 处理。

## 16. 是否仍存在 "口头 Approval 无 pending 却 completed" 路径？

**主链路：否**。文本声称等待审批 → `APPROVAL_INCONSISTENT` → repair（要求真实调工具或撤回）→ 仍坚持则 failed。实测（含 F3 对抗与集成测试）均不再 completed-as-finished。

同 §15 的两个例外同样适用：legacy /chat 内联路径（未接 Gate）与分析性无声明回答。

## 17. 是否仍存在 "真实工作完成但空回复导致结果全部丢失" 路径？

**否**。真实执行证据存在时：
- FinalResponseFailed → completed + 降级摘要（列出真实执行工具与产物）+ `reply.degraded` 事件 + 审计补写 tool_calls；
- 内部英文 "Guardrail OutputGuardrail triggered tripwire" 不再出现在用户可见文案；
- 无证据的空回复仍 failed（friendly），不会把真失败误判成功；
- 输出安全闸本身保留：密钥类内容依旧被拦且不落库。

## 18. 尚未解决的问题（如实记录）

1. Legacy `/chat` 内联执行链未接 Completion Gate（Phase 5 收敛 web 双链时一并处理）。
2. "执行型任务的分析性无声明回答"不在 v1 判定范围（需要任务级达成度判定，建议 Phase 4 Verification 一并设计）。
3. 完成声明识别是第一版确定性正则：覆盖面与误报率需要长期真实数据校正（当前未来/假设语气已排除；语料侧仍可能漏检英文变体/口语化表达 → 漏检时退化为"无声明会话型完成"，不会误杀）。
4. 工具账本/审批门进程级单槽：并发 run 未隔离（沿用既有架构语义，审计已记录）。
5. Completion Repair 为 1 次有界；repair 消息采用"原始消息 + 运行时观察"重放（与既有 guardrail 重试同模式）；精确断点续跑仍属后续阶段。
6. 部署注意：**8765 端口两个长驻实例（13:47 启动）仍是旧代码**，需要重启后端后本阶段行为才会在 8765 生效（本次 E2E 全部在独立实例完成，未动 8765）。
7. 失败路径的 model_calls 明细（与工具行不同）仍不采集——完整失败路径 Audit 属 P1，本阶段只保证 Completion Gate 所需工具证据不缺（已通过账本补写解决）。

---

## 最终回答

**FORGE 当前的 COMPLETED 是否已经从"模型自报完成"升级为"Runtime 有最低事实依据的完成"？**

**是（YES）——在 run_turn 主链路上成立。** COMPLETED 现在只有三种来源：① 会话型输出（questions/plan/无完成声明的普通回答，kind 不再自动可信）；② 完成声明被真实工具执行/产物/验证证据支撑；③ 真实执行已发生而最终回复失败时的**系统降级摘要**（只复述结构化事实）。三者之外一律不得 completed：无证据的完成声明、无 pending 的口头审批会先触发 ≤1 次运行时 repair，耗尽后按 failed 收口并留下完整审计事件（`completion.check.*`）。

**已知边界（不构成 YES 的反例，但需如实标注）**：legacy `/chat` 内联执行链与"分析性无声明回答"两个缺口见 §15，前者属下一阶段收敛项，后者是 v1 判定范围的明确边界。
