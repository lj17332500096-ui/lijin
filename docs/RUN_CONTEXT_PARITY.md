# RUN CONTEXT PARITY

> Phase 1 交付物。比较 **normal execution / approval request / approval resume** 三态的
> RunContext 字段，坐实"resume 是否退化为默认值"。证据锚点：`runner.py` run_turn 构造
> RunContext 段（L1805-1853）+ 三条 Golden Trace 真实 DB 记录。

## 三态 RunContext 构造对照（真实代码路径）

| 字段 | normal execution | approval request（挂起） | approval resume（继续） | Parity 判定 |
|---|---|---|---|---|
| `run_id` | `task.id`（新建） | 同 task.id | 同 task.id（`task_id` 传 Run id 恢复） | **MATCH** |
| `task_id` | 新建 | 同 | 同（`is_resumable` 校验后 resume） | **MATCH** |
| `request_text` | `message`（真实用户任务） | `message` | `message or task.goal`（`runner.py:1837`，resume 时 message 空 → 用原始 goal） | **CONTEXT_PARITY_MISMATCH（显式标记，见下）** |
| `WorkLocation` | 由 container `work_location_id` 解析 | 同 | 同（container 未变） | MATCH |
| `FileScope` | `build_file_scope(container/session/wl/base/ws/notes)` | 同 | 同 | MATCH |
| `intent` / `readiness` | 由消息推导 | 同 | **保留 pending readiness**（`_container_pending_readiness`） | MATCH |
| `evidence_epoch` | 新 epoch | 冻结 | resume 时 `_hydrate_runctx_from_history`（L1848）恢复 mutation/verification 证据 | **靠历史水合，非重算** |
| `verified_revision` | — | 冻结 | 由 `_hydrate_runctx_from_history` 恢复 | 依赖水合正确性 |
| `verification_due` | 由 mutation 触发 | 冻结 | 恢复 | 依赖水合正确性 |
| `obligations` | 义务门实时 | 冻结 | 恢复 | MATCH（语义） |
| `budgets` | `resolve_budget` | 冻结 | 沿用同 task | MATCH |
| `pending_user` | `needs_user_input` | — | 恢复 | MATCH |
| `pending_approval` | False | **True**（挂起） | approve 后变 False，`_execute_approved_invocation` 执行 | MATCH（状态机语义） |
| `completion state` | 实时判 | 冻结 | resume 后重判 | MATCH |

## `request_text` 的 MISMATCH 分析（第十九项）

**事实**：normal 用真实用户消息 `message`；resume 时 `message=""`，故 `request_text = task.goal`（`runner.py:1837` 的 `message or task.goal`）。

- 这不是"退化默认值"，而是**显式保留原始 goal 作为义务事实源**（注释明确写"resume 时 message 可能为空：保留原始 goal 作为义务事实源"）。
- **是否影响各门**：
  - **Intent / Readiness**：不受影响——resume 复用同一 container 的 pending readiness，goal 本就是义务的锚。
  - **Permission / FileScope**：不受影响——由 container/work_location 决定，与 message 文本无关。
  - **Completion**：不受影响——完成判定看 ExecutionEvidence + 义务账本，不靠重新解析 message。
- **判定：`CONTEXT_PARITY_MISMATCH`（标记但不阻断）**——因为 resume 的 `request_text` 确实**不是**真实用户原始消息（而是 goal 快照），严格意义上与 normal 的 `request_text=message` 不相等。按第十九项要求，**不得写 MATCH**；但语义上这是有意的"goal 作为义务锚"设计，非缺陷，影响面为空。

## 结论
- **结构 Parity = PASS**：三态共享 run_id/task_id/container/文件作用域/预算，resume 不是"另一套上下文"。
- **唯一 MISMATCH = `request_text`**：resume 用 goal 快照替代 message，按规范显式标 `CONTEXT_PARITY_MISMATCH`，但**语义影响面为空**（intent/readiness/permission/filescope/completion 均不依赖 message 重解析）。
- 权威事实源仍是 `ExecutionEvidence` + 义务账本（经 `_hydrate_runctx_from_history` 水合），RunContext 只是"本次 run 的事实投影"，不是权威本体。

## [待核实]
- `_hydrate_runctx_from_history` 水合 `verified_revision/verification_due` 的**字段级完整性**（是否真把 pending 状态恢复全）需单列 trace 验证——本阶段未做逐字段断言，标 NOT ESTABLISHED。
