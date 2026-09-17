# FORGE Agent Finalization Repair — Phase 18 报告

> 日期：2026-09-11
> 主题：Finalization Repair Semantics + Verification Production Acceptance
> 基线：《FORGE-Agent-Verification-Production-Phase17》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**

---

## 1. Executive Summary

本轮定位并修复 Phase 17 的 **F4 false failure**，并建立确定性回归套件。

**真正的 F4 根因（比 Phase 17 的判断更底层）**：

> `runtime/completion.py` 的 `VERIFY_TOOLS` **不包含 `run_tests`**。
> 因此 `run_tests` 的 PASS 不被 Completion Gate 的 `ExecutionEvidence` 识别为验证证据 →
> 模型在正确验证后给出“已验证通过”时被判 `CLAIM_UNSUPPORTED` → repair 耗尽 → false failure。

（Phase 17 猜测的“repair 额度共用”是**次因**，不是根因。）

**修复**：
1. `VERIFY_TOOLS` 加入 `run_tests`（真正的 F4 修复）。
2. **拆分 repair 语义**：obligation feedback 按 `obligation_deficit_signature` 独立计数，
   与 completion repair 分离（新 revision / 新证据产生新 signature，旧反馈不污染新 finalization）。

**验证**：
- 新增 `tests/test_phase18_repair_semantics.py`（5 项）全通过；
- 全量 **738 passed, 1 skipped**；
- Live M4 复测（N=1）本轮模型漂移未验证（0 verification），未观测到 F4 路径；
  M1/M4 N≥3 因本地模型单步过慢**未完成**。

---

## 2. Phase 17 F4 Reproduction

Phase 17 M4（N=1）：`mutation → early final → obligation block → run_tests PASS →
final → repair_exhausted → no_progress → failed`，而 verification/eligibility 均正确。

## 3. Completion Repair Semantics

`completion_repairs` 只处理：所有 enforceable obligations 已满足后，**final response 本身**
仍不合格（证据缺失、语义拒绝、与真实结果冲突）。上限 1 次，超过 → `repair_exhausted`。
missing verification / mutation / pending approval / waiting_user **不消耗** completion repair。

## 4. Obligation Repair Semantics

模型尝试 final 但存在 mutation/verification deficit → obligation feedback（结构化缺口），
继续正常 Agent Loop，**不消耗 completion repair**。

## 5. Deficit Signature

`RunContext.obligation_deficit_signature()`：
`rev=<epoch>|mut=<status>:<satisfied>|ver=<status>:<satisfied>:<verified_revision>|user=<bool>`。
用于判断“是否仍是同一次缺口”。

## 6. Evidence Progress

verification PASS（或新 mutation）→ signature 改变 → 允许新一轮反馈/收口，
旧 obligation feedback 不污染新 finalization。

## 7. Repair Counter Separation

| 计数 | 负责 | 上限 | 不消耗 |
|---|---|---|---|
| `obligation_feedback_signatures`（按 signature） | obligation deficit 反馈 | 每 signature 1 次 | completion repair |
| `completion_repairs` | final response 不合格 | 1 | — |

循环有界（`range(6)`），既有界又不会被单次 obligation block 提前耗尽。

## 8. M4 Deterministic Regression

`tests/test_phase18_repair_semantics.py`：

| 测试 | 断言 | 结果 |
|---|---|---|
| obligation block → run_tests PASS → completed | state=completed、有 obligation_blocked、无 repair_exhausted | ✅ |
| 重复同 deficit → bounded failure | failed、有 `missing_required_verification` reason | ✅ |
| verification FAIL → 不 completed | state≠completed | ✅ |
| 新 mutation → 新 deficit → 后 PASS → completed | state=completed | ✅ |
| control（无验证义务） | completed | ✅ |

**断言：obligation block 不消耗 completion repair；满足义务后 final state=completed。**

## 9. Missing Verification Bounded Failure

同一 deficit signature 第二次出现且无新证据 → `failed` + `event_reason=missing_required_verification`
（`terminal.kind=bounded_failure`）。不无限循环，也不允许无验证 completed。

## 10. Verification Failure Recovery

`run_tests` FAIL → verification obligation 仍 unsatisfied；FAIL 属 real new evidence，
允许继续 READ/DISCOVERY/MUTATION；新 mutation → revision 更新 → 新 deficit。

## 11. No-Progress Semantics

`no_progress` 仅用于“无新 evidence / state transition 却反复失败循环”。
verification PASS 是明确 progress，不应判 no_progress（F4 已消除该误判）。

## 12. M4 Live Retest

N=1：本轮模型漂移（first post-mutation 未选 `run_tests`，VERIFICATION=0，failed）。
**未观测到 F4 路径**（因为本轮未发生 verification）。需 N≥3 才能观测到修复后的收口。

## 13. M1 N>=3

**未完成**（本地模型单步过慢）。

## 14. M4 N>=3

**未完成**。

## 15. Verification Attempt Stability

**NOT ESTABLISHED**（N≥3 未完成）。

## 16. Latest Revision Coverage

未观测（本轮 live 未发生 verification）。

## 17. M1 Drift Decision

样本不足；M1 在 N=1 中仍漂移（DISCOVERY）。需 N≥3 判断。

## 18. L1 Entry Decision

**NO**：single-tool（Raw/SDK）已 100%，F4 已修；M1 漂移需 N≥3 先确认。

## 19. M3 Closed Loop

**未执行**（M1/M4 N≥3 未完成）。

## 20. Verification Scope

保持 PROJECT / TARGET / UNKNOWN 最小语义；未建 coverage engine。

## 21. Controls

M6/M7/M8 由三态 obligation detection 保证不误触发；全量 738 passed 覆盖回归。

## 22. Tool Contract Security

**PASS**：target traversal / 不安全 extra_args / env sanitization / approval 覆盖均有效
（Phase 17 Security Probe）。

## 23. Execution Boundary Security

**ACCEPTED LIMITATION**：测试代码与宿主 Python 进程同权限，可写 project root 外
（无 OS 级 filesystem confinement）。这是已知、已记录的限制，与 `run_python` 相同。

## 24. Production Security Decision

FORGE 当前产品**接受**：用户批准 CODE EXECUTION 后，执行的 Python/test code 拥有当前宿主
Python 进程可访问的 filesystem 权限 → `Execution Boundary = KNOWN ACCEPTED LIMITATION`。
（若未来要求强隔离，需单独 P0/P1 安全任务，本轮不重构 sandbox。）

## 25. Runtime Regression

全量 **738 passed, 1 skipped**（含 Phase 18 新增 5 项）。

## 26. run_tests Production Qualification

| 条件 | 状态 |
|---|---|
| Tool Contract Security | ✅ PASS |
| Execution Boundary | ✅ ACCEPTED LIMITATION |
| Raw API semantic ≥90% | ✅ 100% |
| Minimal SDK semantic ≥90% | ✅ 100% |
| Full Agent M1/M4 门槛 | ❌ N≥3 未完成 |
| Latest Revision Coverage 门槛 | ❌ 未观测 |
| M3 ≥2/3 | ❌ 未执行 |
| False Completion | ✅ 0 |
| False Failure after obligations satisfied | ✅ 0（确定性） |
| Control Regression | ✅ 0 |

→ **run_tests status = EXPERIMENTAL**（F4 已修 + 安全达标；仅差 live N≥3/M3 验证）。

## 27. Local Microbenchmark Status

NOT ESTABLISHED（Full Agent N≥3 未完成）。

## 28. Next Direction

在**本地模型**下完成 Full Agent **M1/M4 N≥3**（F4 已修，预期 M4 满足义务后 completed）；
达标后进入 **M3 闭环**。仅当 M1 在 N≥3 中仍明显漂移（run_tests available + verification_due
仍选 DISCOVERY），才进入 L1 Structured Action Commitment。

---

## 最终输出

```text
F4 repair bug =
FIXED（根因：VERIFY_TOOLS 缺 run_tests；并拆分 obligation/completion repair 计数）

Obligation repair semantics =
PASS（确定性 5/5）

Completion repair semantics =
PASS（确定性 5/5）

Observed M1 verification =
0/1（live N=1；Qualification status = INSUFFICIENT SAMPLE）

M4 verification =
0/1（live 复测漂移，未观测 F4 路径）

Latest Revision Coverage =
NOT OBSERVED

M3 closed loop =
NOT RUN（未执行，禁止写成 0/3）

False Completion =
0

False Failure after obligations satisfied =
0（确定性回归验证）

Tool Contract Security =
PASS

Execution Boundary Security =
ACCEPTED LIMITATION

run_tests status =
EXPERIMENTAL

L1 Structured Commitment needed =
NO

Primary remaining bottleneck =
Full Agent live verification selection 稳定性（N≥3 未测；本地模型单步过慢）

Next component to modify =
本地模型下 Full Agent M1/M4 N≥3 复测（F4 已修），随后 M3 闭环
```

## 附：修改文件

| 文件 | 改动 |
|---|---|
| `runtime/completion.py` | `VERIFY_TOOLS` 加入 `run_tests`（真正的 F4 修复） |
| `runtime/runctx.py` | 新增 `obligation_deficit_signature()` |
| `runtime/runner.py` | 拆分 obligation feedback（按 signature）与 completion repair 计数；循环有界 6 |
| `tests/test_phase18_repair_semantics.py` | 新增 5 项确定性回归 |
