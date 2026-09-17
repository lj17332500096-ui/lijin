# 《FORGE-P0-P1 整改验收矩阵》

> 日期：2026-09-06（第二次复审）· 依据：当前真实代码 + agent.db/sessions.sqlite + 全量离线测试（371/371）+ 真网关 E2E
> 状态标记：FIXED / PARTIAL / STILL_PRESENT / REGRESSED / NEW_DESIGN / N/A

| # | 原问题（Baseline Audit） | 旧等级 | 当前状态 | 代码证据（当前） | E2E/测试证据（当前） | 结论 |
|---|---|---|---|---|---|---|
| 1 | Completion 无验证，0 tool 假完成可 completed | P0 | FIXED | `runtime/completion.py` Gate；runner 收口在 mark_success 前（PASS/CLAIM_UNSUPPORTED/APPROVAL_INCONSISTENT + ≤1 repair） | 真网关对抗消息（0 工具+口头完成）→ failed“已阻止按完成收尾”；离线 20 项 | 已修复（残余语义缺口见 #47） |
| 2 | Web/Project 长历史无限增长 | P0 | FIXED | `runtime/context.py prepare_session_context` 在 run_turn execute 前执行；`compact_history` 分块摘要；Hard 窗口 | 真网关 12/16 轮历史→context.compaction.completed(32→7)；60 轮曲线锯齿有界；E2E 连续两轮不再重复 compact | 已修复 |
| 3 | 口头 Approval 无 pending 却 completed | P0 | FIXED | Gate APPROVAL_INCONSISTENT；pending 事实源=ApprovalStore（runner 先判 pending_for/denied_for） | 真网关对抗消息→failed（“未登记待审批操作”）；离线 TEST7 | 已修复 |
| 4 | 真实执行完成却因空输出/guardrail 判 failed（结果被抹） | P1-coupled | FIXED | `FinalResponseFailed` + runner 降级路径（executed 证据→completed+系统摘要+审计补写） | 集成测试（fake FinalResponseFailed+真实账本）→completed、无内部英文；离线 TEST10/12 | 已修复（降级在成功路径可证；真实模型触发该路径不可控，以离线+此前真网关 F3B 记录佐证） |
| 5 | code_loop/sandbox_rollback Approval 旁路 | P1 | FIXED | approval.py 风险分类默认进门（EXECUTION∪DESTRUCTIVE）；包装层调用即门 | 真网关 code_loop 全链 pending→approve→同 run 执行 exit0；换参重新 pending；wrapper 单测（含拒绝不执行） | 已修复 |
| 6 | WorkLocation 假边界 | P1 | FIXED | `runctx.file_scope` + `filescope.authorize_tool` 工具执行前判定；project_edit/tools 运行期根感知 | 真网关：wl 内读执行成功、wl 外读被“运行时文件边界”拒且无泄漏；离线隔离矩阵 | 已修复（RAG/github 未纳入清单，见 #52） |
| 7 | Failed/rejected/disconnected Audit 黑洞 | P1 | FIXED | runner `_backfill_failure_audit`（收口统一：账本→tool_calls、attempt_log→model_calls attempted） | 真网关失败 run（MaxTurns）四表齐全可复盘；离线 AuditBackfillTests | 已修复（断连路径未做真实客户端测试，机制同收口；标注 PARTIAL 于断连子项） |
| 8 | 并发 Run 共享进程级 mutable state | P1 | FIXED | runctx(ContextVar)；ApprovalGate run-keyed；ledger 按 run_id；tools 记忆绑定 ctx 优先 | 离线：4 路审批交错、10 轮 memory 交错零串扰；真网关双 run 并发各自完成、run 独立 | 已修复 |
| 9 | Web 两条 Agent 执行链 | P1 | FIXED | legacy /api/stream 已改为 run_turn adapter（webapp.py 内联 Runner 删除） | 真网关 legacy：reply 正常且带 context.metrics+completion.*（同一 Runtime 证据） | 已修复 |
| 10 | model_calls.model 为空 | P1 | FIXED | audit 记录 provider model；缺失回退 requested model；失败路径 attempted 标记 | 当前 DB model_calls.model 全部=agnes-2.5-flash；失败 run 亦有 attempted 行 | 已修复 |
| 11 | 跨 Project 文件读取（read 工具无项目归属校验） | P2 | FIXED（Project 语境） | FileScope 白名单根；legacy personal 语境保留原语义（有意的设计差异） | 离线隔离矩阵 + 真网关 wl 外拦截 | 已修复（legacy 语境的整工作区读仍是设计语义，见 #53） |
| 12 | memory.json.migrated.bak 明文可读 | P2 | STILL_PRESENT | tools 读保护名单仍不含 memory.json* | 复测：读成功（本会话再次实测） | 未修复（P2） |
| 13 | 程序输出（run_python/code_loop stdout）无 trust 边界 | P2 | STILL_PRESENT | code_exec/codex_loop 输出未过 trust.tag；web/file/office/rag/image 已过 | 静态扫描确认未加 | 未修复（P2） |
| 14 | 子进程网络不隔离 | P2 | STILL_PRESENT（记录事实） | 子进程环境白名单 16 键；无网络 ACL；本机执行 | 复测环境白名单/输出截断/超时仍生效 | 未修复（设计限制，如实记录） |
| 15 | tool_calls/goal 明文留存 | P2 | PARTIAL | tool 参数/摘要与 approvals 入库前脱敏（audit.redact_value） | RedactionTests 实证无明文密钥 | 部分修复（runs.goal 等仍是用户原文，无脱敏） |
| 16 | 死配置/死状态/死端点 | P2 | STILL_PRESENT（部分随收敛消失） | WAITING_USER、api_tasks_list/api_task_detail/touch_work_location/count_checkpoints(生产)/container_running_state_any、TASK_MAX_WALL_SECONDS env 仍无读取 | 静态扫描 + 本日 DB | 未修复（本阶段只报告） |
| 17 | Sources index_status 无真实索引 | P2 | STILL_PRESENT | index_status 默认 none，无深度 pipeline | DB 1 条 source index_status=none | 未修复 |
| 18 | sessions.sqlite vs agent.db.messages 双源 | P2 | PARTIAL（收敛减少） | 双源仍在；模型只读 SDK 库；compact 只动 SDK 库；delete_container 不清 SDK 行 | 静态 + DB（39 sessions/2913 messages vs agent.db 81 messages） | 部分修复（未做一致性协议） |
| 19 | Approval all/* 语义问题 | P2 | FIXED | all/*→ALL_SEMANTICS（执行+破坏/恢复+文件编辑） | 单测覆盖（含 SAFE 放行） | 已修复 |
| 20 | 其他旧假功能（ToolBroker/registry 不参与执行、spec risk 无消费者等） | P2 | STILL_PRESENT | broker/registry/spec 仍只盘点展示；approval 改用自己的风险分类（不再声称读 spec） | 静态扫描 | 未修复（报告性） |
| 21 | 完成语义残余缺口：声称“验证通过”但仅执行过（未查退出码） | （新发现） | — | Gate 只查“验证工具执行存在”，不查 exit=0 | 代码路径确认（证据=executed 工具） | NEW P1（Verification 语义待深化） |
| 22 | 阻塞式摘要调用占用事件循环（compact 同步 OpenAI 调用） | P2 | STILL_PRESENT | compact.summarize_transcript 同步 | 代码确认 | P1（并发 Web 体验） |
| 23 | 失败 run 的 pending approval 不自动关闭；stale SUBMITTED 不回收 | P2 | STILL_PRESENT | recover_stale_tasks 只处理 RUNNING；approvals 无终态联动 | DB 现存 1 条 failed-run pending；重启探针 SUBMITTED 不受理 | P2（状态一致性） |
| 24 | tool 被拦但成功路径审计标为 succeeded | P2 | STILL_PRESENT | audit.ingest 配对即标 succeeded；仅失败路径 backfill 用 blocked | DB blocked 行来自 backfill；成功 run 内 blocked 条目状态=succeeded（误标） | P2（Audit 语义） |
| 25 | 固定 input 基线 ~13k token | （测量） | 基本不变 | — | 真网关 E1：input_tokens=13,200（0 tool） | 与首审 13,204 持平 |
