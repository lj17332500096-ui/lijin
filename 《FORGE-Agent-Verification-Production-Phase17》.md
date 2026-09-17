# FORGE Agent Verification Production — Phase 17 报告

> 日期：2026-09-11
> 主题：Verification Production Qualification + Finalization Closure
> 基线：《FORGE-Agent-Verification-Tool-Abstraction-Phase16》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**

---

## 1. Executive Summary

本轮解决两件事：**`run_tests` 是否具备生产资格**，以及**验证成功后 Run 仍 failed 的根因**。

**结论**：

1. **发现并修复一个真实安全缺口**：`run_tests` 原为 `side_effect=False` 且**不在**
   `EXECUTION_TOOLS` / netpolicy / filescope 名单中 → 会**绕过 Approval、Readiness、Network Policy**。
   已修复：`side_effect=True`（执行测试代码 = 有执行副作用）、加入 `EXECUTION_TOOLS`、
   netpolicy 与 filescope 覆盖。
2. **Security Probe**：PASS/FAIL 正确区分、target traversal / 不安全 extra_args 被拒、
   env sanitization 生效、approval 覆盖 —— **Security qualification = PASS**；
   OS 级隔离**未提供**（与 `run_python` 同，属已记录限制）。
3. **M4 finalization 根因（F4）**：verification PASS + behavior pass，但终态
   `no_progress`。原因是 **Obligation Gate 的阻断占用了唯一的 completion repair 额度**：
   第一次 completion 因缺 verification 被 obligation gate 阻断（消耗 repair=1）→ 模型随后
   正确 `run_tests` 通过 → 但该轮 finalization 若未 gate-PASS，`_repair>=1` 直接
   `_fail(repair_exhausted)` → false failure。
4. **Full Agent M1/M4 N≥3 未完成**（本地模型单步过慢）；M4 N=1 自然选择 `run_tests`，
   M1 N=1 仍漂移。→ run_tests 暂标 **EXPERIMENTAL**，未进入 PRODUCTION。

---

## 2. Phase 16 Findings

- Tool Abstraction Mismatch 确认：Raw API `run_tests` semantic 100% vs `run_python` 40%；
  Minimal SDK `run_tests` 100%；Direct 执行 pytest 2 passed。
- Full Agent M4 N=1 首个 post-mutation 动作即 `run_tests`，latest revision covered。

## 3. run_tests Risk Classification

- 真实语义：**不修改源码（PURE SOURCE-MUTATION = false），但执行测试代码（CODE EXECUTION = true）**。
- 原分类 `side_effect=False` 会被 `classify_tool` 判为 `DISCOVERY_SAFE` →
  在 DISCOVERABLE/NEEDS_USER 下仍放行，且不被 Approval 门拦截 → **风险**。
- 已修正：`side_effect=True`、`risk=medium`、`capability=VERIFICATION`。

## 4. Permission / Sandbox Audit

| 边界 | 修复前 | 修复后 |
|---|---|---|
| Readiness gate（DISCOVERY_SAFE 判定） | run_tests=DISCOVERY_SAFE（错误） | SIDE_EFFECTING（正确） |
| Approval `EXECUTION_TOOLS` | 不含 run_tests | **含 run_tests** |
| Network policy（runner） | 只覆盖 run_python/code_loop | **覆盖 run_tests** |
| FileScope `NON_FILE_TOOLS` | 不含 run_tests | **含 run_tests**（自管路径） |
| env sanitization | 复用 `_sanitized_env` | 一致 |

## 5. Target Security

- `target` 经 `Path.resolve()` + `_canonical_under(base_dir)` 判定，**非字符串 startswith**。
- 实测：`target='../../evil.py'` → 拒绝（“测试目标必须在项目根内”）。

## 6. Extra Args Security

- 结构化 argv：`[sys.executable, "-m", "pytest", <target>, <extra_args...>]`；**无 shell=True**。
- `extra_args` 逐 token 白名单正则 `^[A-Za-z0-9_\-\.\/=:,]+$`，拒绝 `; | & > < $ \``。
- 实测：`extra_args='; whoami'` → 拒绝。
- 残余风险：pytest 参数可加载插件/改变 rootdir；本轮依赖 sandbox/受信根边界，未新增复杂 policy。

## 7. Security Probe

| 用例 | 结果 |
|---|---|
| 1) 正常 PASS | 退出码 0，PASS |
| 2) pytest FAIL | 退出码 1，FAIL（**非 tool failure**） |
| 3) target traversal | 拒绝 |
| 4) unsafe extra_args | 拒绝 |
| 5) 测试读敏感环境变量 | 无 KEY/TOKEN 泄漏（env sanitization 生效） |
| 6) 测试写 project root 外 | **可写入**（OS 级隔离未提供，与 run_python 同） |
| 7) capability metadata | VERIFICATION / side_effect=True / risk=medium |
| 8) approval 覆盖 | run_tests ∈ EXECUTION_TOOLS |

→ **Security qualification = PASS**（在既有 sandbox/policy 范围内；OS 级隔离为已记录限制）。

## 8. Capability Metadata

```
run_tests  → VERIFICATION（side_effect=True）
run_python → CODE_EXECUTION
code_loop  → MUTATION_VERIFICATION_LOOP
```
Completion Ledger 可识别 `run_tests` 的 PASS 为 verification evidence；
Permission/Readiness 不再把它当纯只读工具。

## 9. Full Agent M1 N>=3

**未完成**。N=1：first post-mutation action = DISCOVERY（`list_notes`），verification 0，failed。
（本地模型单步过慢，N≥3 未跑完。）

## 10. Full Agent M4 N>=3

**未完成**。N=1：first post-mutation action = **VERIFICATION（`run_tests`）**，
verification_pass=True、latest_revision_covered=True、behavior_pass=True，
终态 failed / `no_progress`。

## 11. Verification Attempt Stability

样本不足，**NOT ESTABLISHED**（M4 1/1、M1 0/1，N≥3 未完成）。

## 12. Latest Revision Coverage

M4 N=1：**covered=True**。N≥3 未完成。

## 13. M4 Finalization Failure Root Cause

M4 trace（N=1）：`mutation → completion attempt 1（gate PASS 但 obligation 缺 verification）
→ obligation gate 阻断（obligation_blocked=1，消耗 repair=1）→ 模型 run_tests PASS
→ completion attempt（`_repair>=1`）→ 未 gate-PASS → `_fail(repair_exhausted)`
→ terminal.kind = no_progress`。

**第一个把正确执行链转向失败的节点**：**Obligation Gate 与 completion repair 共用同一 repair 额度**。

## 14. Completion Eligibility Snapshot

verification PASS 后：mutation_satisfied=True、verification_satisfied=True、
verified_revision==current_revision、pending_user/approval=False、unresolved_blockers=False
→ **completion_eligible = true**（eligibility 本身正确）。

## 15. Finalization Failure Classification

| 类别 | 判定 |
|---|---|
| F1 verification PASS 但 eligible=false | 否（eligible=true） |
| F2 eligible=true 但继续 exploration | 部分可能 |
| F3 Gate PASS 但 final-response 失败 | 可能 |
| **F4 已 finalizable 但 repair/obligation 逻辑错误重开/消耗** | **主因** |

→ **M4 = F4**（obligation gate 消耗 completion repair 额度 → repair_exhausted false failure）。

## 16. Guard B Re-entry Decision

**NO**。未观察到“completion_eligible=true 且无新 blocker 仍持续探索”的稳定证据
（M4 是 repair 额度问题，不是 wandering）。Guard B 继续 OFF。

## 17. Guard B Experiment

**未执行**（前提未满足）。

## 18. M1 Drift Analysis

M1 N=1：run_tests available 且 verification_due，但 first action = DISCOVERY（`list_notes`）。
→ full-loop 仍存在 action selection 漂移（样本不足，需 N≥3 确认）。

## 19. L1 Entry Decision

**NO**：single-tool（Raw/SDK）已达 100%，且 M4 自然选择 `run_tests`；
M1 漂移需 N≥3 先确认，未达 L1 进入条件。

## 20. M3 FAIL→REPAIR→PASS

**未执行**（M1/M4 N≥3 未完成）。

## 21. Verification Scope

最小语义：`PROJECT`（无 target）/ `TARGET`（指定 target）/ `UNKNOWN`。
`run_tests(project)` 无 target → PROJECT scope；指定 target → TARGET scope。
本轮未新增 coverage engine；`verification_coverage` 记录 tool/result/verified_revision。

## 22. Verification Relevance

Completion Ledger 不默认“TARGET PASS == project verified”：
verification obligation 的满足取决于 scope；本轮记录 target，未做复杂 relevance 引擎。

## 23. Controls

M6/M7/M8：`run_tests` 新增不改 obligation detection（三态），
read-only / 无测试修改 / “修改测试说明” 不触发强制验证。全量测试 733 passed 覆盖回归。

## 24. Unnecessary Verification

新增工具不强制调用；obligation 仅在 required 时生效 → Unnecessary Verification Rate ≈ 0。

## 25. Runtime Regression

全量 **733 passed, 1 skipped**（含 approval/filescope/spec 变更）。

## 26. Production Qualification

`run_tests` 生产资格对照：

| 条件 | 状态 |
|---|---|
| Security Probe PASS | ✅ |
| Raw API semantic ≥90% | ✅ 100% |
| Minimal SDK semantic ≥90% | ✅ 100% |
| Full Agent attempt ≥90% | ❌ N≥3 未完成 |
| Latest Revision Coverage ≥80% | ❌ N≥3 未完成 |
| M3 closed loop ≥2/3 | ❌ 未执行 |
| False Completion = 0 | ✅ |
| False Verification Claim = 0 | ✅ |
| Control Regression = 0 | ✅ |
| Finalization correctness | ❌ M4 F4（repair 额度） |

→ **run_tests status = EXPERIMENTAL**（未达 PRODUCTION）。

## 27. Local Production Status

```
Runtime Semantic Baseline      = YES
Gateway-era Production E2E     = HISTORICAL ONLY
Local Microbenchmark Baseline  = NOT ESTABLISHED（Full Agent N≥3 未完成）
Local Coding Benchmark Baseline= NOT ESTABLISHED
Local Full Production E2E      = NOT ESTABLISHED
```

## 28. Next Direction

1. **修复 F4**：把 Obligation Gate 的阻断与 completion repair 额度分离
   （obligation block 不应消耗唯一的 completion repair；在同一 repair 循环内允许多次 obligation 反馈）。
   这是最小、必要的 Finalization 修复（不新增 Engine）。
2. 在本地模型下完成 **Full Agent M1/M4 N≥3**，确认 Verification Attempt ≥90% 与
   Latest Revision Coverage ≥80%。
3. 达标后进入 **M3 闭环**（FAIL→REPAIR→PASS→COMPLETE，N=3，≥2/3）。

---

## 最终输出

```text
run_tests security qualification =
PASS（target/args/env/approval/readiness 边界生效；OS 级隔离未提供，与 run_python 同）

run_tests Full Agent attempt =
M4 1/1（N≥3 未完成）、M1 0/1

Latest Revision Coverage =
M4 N=1 True（N≥3 未完成）

M4 finalization root cause =
F4 — Obligation Gate 阻断消耗了唯一的 completion repair 额度 → repair_exhausted
（verification 与 eligibility 均正确）

Completion Eligibility =
CORRECT（verification PASS 后 eligible=true）

Guard B needed =
NO

M3 closed-loop =
NOT RUN

False Completion =
0

False Verification Claim =
0

Unnecessary Verification Rate =
≈0

run_tests status =
EXPERIMENTAL

L1 Structured Commitment needed =
NO

Primary remaining bottleneck =
Obligation Gate 与 completion repair 额度耦合（F4）+ full-loop M1 action selection（待 N≥3）

Next component to modify =
现有 repair/completion 交界：将 obligation 阻断与 completion repair 额度分离（最小 Finalization 修复）
```

## 附：修改文件

| 文件 | 改动 |
|---|---|
| `runtime/approval.py` | `EXECUTION_TOOLS` 加入 run_tests |
| `runtime/spec.py` | `run_tests` side_effect=True（执行测试代码）；capability=VERIFICATION |
| `runtime/runner.py` | netpolicy 条件加入 run_tests |
| `runtime/filescope.py` | `NON_FILE_TOOLS` 加入 run_tests |
