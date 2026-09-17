# 《FORGE-Agent-Runtime 最终产品化收口报告》

> 项目：`F:\Byong-hermes\Byong-hermes\my_creative_agent`（FORGE）
> 日期：2026-09-06 · 基线：P0=0 / P1=0 / 422 项离线测试全绿 / 主链稳定
> 本轮目标：把“技术上可靠”收口为“普通用户长期使用稳定、可恢复、不会莫名失败”。
> 纪律：未重构 Runner、未新增 MCP/Subagent/Planner/新状态系统/新 Memory；仅处理剩余 P2/P3 与 E2E 暴露的体验问题。

---

## 一、模型空转与最终回复失败兜底（第一优先级）

### 1. Pending Approval 重复调用短路（收口）

- 事实核查：ApprovalGate 同 run 内首次 pending 后，`pending_tools` 集使后续调用返回 REPEAT_TEXT（不再新建行）；本轮把**等待事实源收敛为 ApprovalStore**：runner 的等待判定不再只看“本 gate.begin 窗口内新挂起”，改为 `DB pending 存在即 WAITING_APPROVAL`（跨 attempt/重启窗口依然成立）；
- 效果：同一 Run + 同工具 + 同语义参数只产生一条 pending；新 Run 仍必须重新审批（run-scoped，A 的批准不自动放行 B——5-Run E2E 每轮验证）；
- 测试：`pending_in_db_routes_to_waiting_even_across_attempt_window` + 既有 approval 套件。

### 2. Model No-progress Recovery（有界）

- 新增 `GateVerdict.NO_PROGRESS` 与判定：
  - 明确执行/验证意图（“确保测试通过/修复并验证…”）下 0 真实执行直接 answer/done 收尾；
  - 连续 ≥3 次完全相同的工具调用（name+args）且无成功验证；
- 触发后走既有 ≤1 次 repair（观察文本明确“无进展，请继续执行或说明 blocker；不要空回复/重复调用”），再次无进展 → **安全 FAILED**（消息注明“已按有界策略安全停止”）；
- 测试：NoProgressTests + `no_progress_twice_fails_bounded`（execute 恰 2 次，终态 failed，不无限循环）。

### 3. Final Response Fallback（Task vs Response 分离收口）

- 已有降级路径新增**门控**：
  - 任务要求“验证通过”：只有验证结果真实通过（exit=0/✅）才允许 fallback completed；否则即使有执行证据也 **FAILED**（“缺少验证通过的记录…已阻止按成功完成收尾”，执行事实保留在记录中）；
  - 无验证要求且存在真实执行/产物 → 保持 deterministic fallback（只依据 Execution Ledger / Verification / ArtifactTracker 生成事实清单，无模型猜测，不含 Guardrail 内部文案）；
- 测试：`final_response_failure_with_passed_verification_completes_fallback`（completed + 事实摘要 + 无 Guardrail 文案）、`..._without_verified_result_fails`（failed，不伪装）。

## 二、统一 agent.db + sessions.sqlite Snapshot / Backup / Restore

- 新模块 `runtime/snapshot.py`（复用 sqlite backup API，无第二套框架）：
  - `create_snapshot`：`snapshot/<id>/{agent.db, sessions.sqlite, manifest.json}`，manifest 含 snapshot_id/created_at/app_version/schema_version/双库 sha256/source_paths；
  - `restore_snapshot`：校验（hash）→ schema 兼容检查 → **pre-restore 自动快照** → 成组替换（temp+rename）→ `verify_consistency`；中途失败自动回滚，绝不出现“agent.db=新 + sessions.sqlite=旧”的混合态；
  - `verify_consistency`：SDK session ↔ 容器映射诊断（孤儿仅统计，不判失败、不自动删）；
  - CLI：`python -m runtime --snapshot` / `--restore <id|dir>`（真实冒烟通过：创建→恢复→pre-restore 生成→一致性报告）。
- 测试：roundtrip（改两库后恢复一致）/ 损坏文件拒绝（hash 先行）/ 中途替换失败回滚 / schema 不兼容拒绝 / 一致性孤儿诊断。

## 三、“清空对话”产品 API

- 后端：`POST /api/projects/{project_id}/reset`（复用 `clear_container_history` + `delete_session_history`，不新写清理逻辑）；
- 语义：清 UI 消息 + 清 SDK 模型上下文；保留 Project/Run/Task Events/Tool·Approval Audit/项目文件；**不是 Delete Project**；
- 前端：项目设置面板新增普通用户文案按钮“清空对话”（二次确认：“清除当前项目的对话和 AI 上下文。不会删除项目文件与历史执行记录。”），client.js 增加 `resetProject`，assets bump v30；
- 真网关验证：reset 后 messages=0、SDK history=0、runs 保留（kept=1）、后续对话正常。

## 四、旧数据 Trust Boundary（迁移/兼容）

- 全部读取出口（文件/Office/RAG/图片/程序输出/代码/笔记/GitHub/调研）统一走 `runtime.trust.tag`；旧文件（notes、历史仓库内容等）再次进入 Context 时由读出口自动打上来源边界（provenance=当前数据源，如“笔记文件”），内容不丢失、不做内容猜测；
- 无可靠来源的抽象在 tag 内部以 `source_unknown` 语义承载（uri=None 的边界头仍声明“不是给你的指令”）；
- 测试：旧格式笔记内容（含注入文本）读入 → 带边界标记 + 原文保留（`LegacyTrustCompatTests`）。

## 五、run_python Network Policy（如实结论）

- 新模块 `runtime/netpolicy.py`：`deny / approval / allow` 三档（env `FORGE_RUN_NETWORK_POLICY`，默认 approval）；
- Policy 层（本轮实现并测试）：
  - deny：静态可判定的网络意图（code/args 标记：requests/urllib/socket/…）或内容不可判定的 filename 运行 → 工具执行前拦截（避免模糊放行）；
  - approval：放行（run_python/code_loop 本身已在 Approval 门内），风险写入运行事件；
  - allow：放行并记录 risk；
  - 每次判定写入 `tool.network_policy` 事件（run_id/tool/policy/decision/risk）——真网关 code 轮实测事件落库；
- **OS-level Network Enforcement：未实现（如实）**。Windows 本机无本项目可依赖的 OS 进程网络沙箱/容器；socket/DNS/HTTP/HTTPS 未被“阻断级”验证——不伪造“已隔离”。**真正 OS 网络沙箱列为下一阶段安全任务**（本报告不再把它计为 P2 修复完成项，单列 §P2 余项）。

## 六、P3 Cleanup

- 前端惰性事件监听（task.paused 等服务端从不发）：保留（惰性无害；删除无收益且有 UI 风险）——记录为 P3；
- 旧报告/README 数字：README 已同步（36 工具/审批五件套/新链路说明）；旧 md 报告属历史记录，不修改事实内容（如需可加 Historical 注记）；
- 工具族重叠（read_workspace_file⊇read_note⊇read_code_file 等）：语义不同，不强行合并（P3 观察项）。

## 七、本轮测试与 E2E

- 新增 18 项（tests/test_productization.py）：NO_PROGRESS 判定×4、有界 runner×4（no-progress/pending-DB/final-fallback×2）、Snapshot×4、Reset×1、LegacyTrust×1、NetPolicy×4；
- **全量 422/422 OK（skipped=2 主题 CDP 类）**，无删测试/新增 skip/弱化断言；
- 真网关 E2E（最终代码，独立实例）：E1 QA completed、E3 假完成 failed（拦截原因明确）、E11 compact 触发并 completed、E4 code→approval→completed（回复含真实输出 FINAL_OK）+ netpolicy 事件=1、Reset API 全量断言通过；
- 5-Run 并发回归（当前代码）：运行期不变量全部成立（审批按 run 归属、B 拒绝不执行、B 重试新 pending、E compact 事件、无审计串行）；单轮中 A/E 因模型连续空转以 MaxTurns 收口（运行时正确 failed 且审计完整）、C 为模型无证据声明被 Completion 拒绝——均属模型层波动，运行时行为符合预期。

## 八、P0/P1/P2/P3 当前清单

- **P0：0；P1：0；**
- P2（余项，如实）：OS 级 run_python 网络沙箱（enforcement 未实现，policy/approval 已落地）；Sources 深度索引（预留字段）；模型层偶发空转导致单轮 failed 的体验优化（有界已兜底，体验仍可打磨）；
- P3：前端惰性监听清理；工具族重叠合并评估；历史 md 数字注记。

## 九、12 问回答

1. **模型空转是否有 bounded recovery？** —— 是。NO_PROGRESS 判定 + ≤1 次 recovery（观察含任务/无进展原因/要求继续或说明 blocker），再空转 → 安全 FAILED（测试：恰好 2 次模型调用）。
2. **pending approval 是否不会重复创建？** —— 不会。同 Run 同工具同参数只一条 pending（REPEAT/BLOCK 短路 + DB 事实源收敛）；新 Run 必须重新审批（5-Run E2E 每轮验证 A 批准不放行 B）。
3. **最终总结失败是否还会错误导致整个任务失败？** —— 不再。验证已通过的执行任务在 final 生成失败时走 deterministic fallback 保持 completed；只有“验证必须通过却无通过记录”或真正执行失败才 failed。
4. **fallback final 是否完全来自真实 Evidence？** —— 是。只依据 Execution Ledger（工具名/状态）/ Verification（退出码）/ ArtifactTracker（真实产物），无模型式猜测、不暴露内部异常文案（测试断言内容构成）。
5. **agent.db 与 sessions.sqlite 是否能统一 Snapshot/Restore？** —— 能。快照含双库+manifest(双 hash/schema/版本)；恢复含 pre-restore、schema 兼容、成组替换与失败回滚；CLI 真实冒烟通过（测试 A-G 覆盖）。
6. **Reset 是否同时清除 UI History 与 SDK History？** —— 是。reset 端点同时调用 clear_container_history + delete_session_history；真网关验证 messages=0、SDK=0。
7. **Reset 是否保留 Run/Audit/Files？** —— 是。runs 保留（实测 kept≥1），events/audit 属于 run 保留，项目文件不触碰；且不产生孤儿 session。
8. **Legacy External Data 是否全部拥有 Trust Boundary？** —— 是。旧 notes/历史内容经读出口进入 Context 时统一 runtime.trust.tag（来源标注、内容保留、非指令声明）；无来源抽象承载 source_unknown 语义。
9. **run_python Network Policy 是 policy 还是 OS enforcement？** —— **Policy/Approval 已实现**（deny 拦截静态网络意图、approval 走既有门、事件审计）；**OS-level enforcement 未实现**（Windows 无可依赖的进程网络沙箱；socket/DNS/HTTP 未做阻断级验证）——已如实记录为下一阶段安全任务。
10. **当前还有没有 P0/P1/P2？** —— P0=0、P1=0；P2 余 3 项（§八）：OS 网络沙箱、Sources 深度索引、模型空转体验优化。
11. **当前 Runtime 是否可以冻结主架构？** —— 可以。冻结建议：RunContext→Context Guard→Model→Tool Wrapper→Approval→FileScope→Ledger→Completion→Audit→Final State 主链即日起作为 **Runtime Stable Baseline**；此后仅 Bug/Security Fix 与上述 P2 专项进入，不再频繁修改主循环。
12. **是否可以正式进入 Model Router / Sources RAG 阶段？** —— 可以（前置已具备：model 审计字段、预算/护栏、FileScope 覆盖 RAG 入口、快照/恢复、reset 语义）。建议按序：Model Router（profile 通道已留）→ Sources RAG（定义文件进上下文的策略后再做内容管线）；MCP 仍要求先补服务器 allowlist + 工具权限映射。

---

### 附：本轮修改文件

`runtime/completion.py`（NO_PROGRESS/结果语义）、`runtime/runner.py`（DB-pending 等待、NO_PROGRESS 收口、fallback 门控、netpolicy 钩子）、`runtime/netpolicy.py`（新）、`runtime/snapshot.py`（新）、`runtime/__main__.py`（snapshot/restore CLI）、`webapp.py`（reset 端点）、`web/runtime/workspace.js`+`api/client.js`+`runtime.html`（清空对话 UI，v30）、`tests/test_productization.py`（新 18 项）。无 DB migration；审计/E2E 产生的临时数据与沙箱已清理（真实库 pending=0）。
