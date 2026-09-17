# FORGE Agent Verification Tool Abstraction — Phase 16 报告

> 日期：2026-09-11
> 主题：Pure Verification Capability + Tool Abstraction Proof
> 基线：《FORGE-Agent-Tool-Calling-Compatibility-Phase15》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**

---

## 1. Executive Summary

本轮验证核心假设：**现有 `run_python` 抽象距离“验证修改”太远，模型会 function call，
但不能可靠地把 verification intent 映射成正确的 Python verification code。**

**结论：假设成立（Tool Abstraction Mismatch 已确认）。**

新增一个**非常薄的纯验证工具 `run_tests`**（Python/pytest，结构化 argv，无 shell=True），
并与 `run_python` 做同 prompt A/B：

| 变体（Raw API，N=5，同 prompt） | Tool Call Generation | Valid Args | **Semantic Verification** |
|---|---:|---:|---:|
| `run_python` | 4/5 | 4/5 | **2/5 = 40%** |
| **`run_tests`（新）** | **5/5** | **5/5** | **5/5 = 100%** |

Minimal SDK（N=3）：`run_tests` selection **3/3**、valid args **3/3 = 100%**。

Direct 执行探针：`run_tests(project='micro_fixture')` → **2 passed（PASS）**；
非法 target / 不安全 extra_args 被拒绝（安全边界有效）。

Full Agent（N=1）：**M4 在 mutation 后第一个动作即为 `run_tests`（VERIFICATION），
latest revision covered，behavior pass**；M1 漂移。N≥3 因本地模型单步过慢未完成。

**最终答案：FORGE 需要一个直接表达“运行测试/验证修改”的纯 verification capability = 是。**

---

## 2. Phase 15 Causal Finding

- `run_python` 是 Python 文件/内联代码运行器；`args="pytest"` 报错；真正跑 pytest 需模型自己写
  `import pytest; pytest.main(...)`。
- Raw API：Tool Call Generation 100%，但 **Semantic Verification 仅 20%**（其余为 os.listdir/os.walk）。
- 因此新假设：**Tool Abstraction Mismatch**。

## 3. Existing Verification Capability Audit

| tool | 声明 | 真实行为 | 纯验证? |
|---|---|---|---|
| `run_python` | VERIFICATION | 运行 Python 文件/内联代码；验证需自己写 pytest code | 否（indirect） |
| `code_loop` | VERIFICATION | write/fix/verify compound，**会自动 mutation** | 否（compound） |
| 其他（test/verify/check/lint/build/command/shell/pytest/unittest） | — | **不存在** | — |

→ **不存在符合要求的纯 verification capability** → 允许实现最小新工具。

## 4. code_loop Capability Correction

`capability_of("code_loop")`：`VERIFICATION` → **`MUTATION_VERIFICATION_LOOP`**。
`capability_of("run_python")`：`VERIFICATION` → **`CODE_EXECUTION`**。
`capability_of("run_tests")`：**`VERIFICATION`**。
（`spec.TOOL_CATALOG` 同步：`run_tests` side_effect=False。）

## 5. Pure Verification Tool Decision

新增 **`run_tests`**（`code_exec.py`）——薄 adapter，只读执行 pytest，不修改源码。
非 shell tool、非任意 command execution、非 Planner。

## 6. Pure Tool Contract

```
run_tests(project: str, target: str = "", extra_args: str = "", timeout: int = 120)
required: ["project"]      （strict_mode=False → truthful）
```
- `project`：项目名（必填）。
- `target`：可选，单个测试文件/用例。
- `extra_args`：可选 pytest 参数（如 `-q -x`）。
- `timeout`：可选，默认 120，最大 300。
**不要求模型填写 filename/code，也不要求它写 `import pytest`。**

## 7. Security Boundary

- **禁止 `shell=True`**：结构化 argv `[sys.executable, "-m", "pytest", <target>, <extra_args>]`。
- `target` 必须落在项目根内（`_canonical_under`），否则拒绝。
- `extra_args` 经字符白名单校验，拒绝 `; | & > < $ \`` 等。
- cwd = 受信根 / 沙箱 project；env = `_sanitized_env()`（剔除密钥）。
- 继续遵守 FileScope / trusted root / Permission / Approval / 现有 sandbox。

## 8. Direct Execution Probe

| 输入 | 结果 |
|---|---|
| `run_tests(project='micro_fixture')` | 退出码 0，`2 passed`，**PASS** |
| `target='../evil.py'` | **拒绝**：“测试目标必须在项目根内” |
| `extra_args='; rm -rf /'` | **拒绝**：“含不安全的字符” |

## 9. ExecutionEvidence Integration

`run_tests` 加入 `_P9_VERIFICATION_TOOLS`；其结果（`退出码: 0` / `passed`）产生
`verification_passed`、`verified_revision = current mutation revision`。
测试 FAIL → verification attempted 但 obligation 未满足。仅真实 PASS 才 satisfied。

## 10. Raw API run_python Baseline

N=5，同 prompt：gen 4/5、valid 4/5、**semantic 2/5（40%）**。
（[0] 写了 `subprocess.run([python,-m,pytest,...])`；[1]/[3]/[4] 为 os.walk/listdir；[2] 无 tool_call。）

## 11. Raw API Pure Verification

N=5：`run_tests` gen **5/5**、valid **5/5**、semantic **5/5（100%）**。
参数示例：`{"project":"micro_fixture"}` / `{"project":"micro_fixture","extra_args":"-v"}`。

## 12. Raw API A/B

| | run_python | run_tests |
|---|---:|---:|
| generation | 4/5 | **5/5** |
| valid args | 4/5 | **5/5** |
| semantic verification | 2/5 (40%) | **5/5 (100%)** |

→ **Tool Abstraction Mismatch 确认**：更直接的抽象把 semantic verification 从 40% 提升到 100%。

## 13. Minimal SDK run_python

Phase 15：selection 2/3、valid 2/3（生成多为探索代码）。

## 14. Minimal SDK Pure Verification

`run_tests`（N=3）：selection **3/3**、valid args **3/3 = 100%**。

## 15. SDK A/B

| | run_python | run_tests |
|---|---:|---:|
| selection | 2/3 | **3/3** |
| valid args | 2/3 | **3/3** |

## 16. Full Agent Availability

`run_tests` 加入 Tool Router `_CODING_SUPPORT` + `TOOL_TERMS`；
coding 任务的 exposed tools 包含 `run_tests`（verification capability 可用）。

## 17. Full Agent M1

N=1：first action after mutation = DISCOVERY（`list_notes`）；VERIFICATION 0；failed。
→ M1 在 full loop 仍漂移（single-tool 100%，full-loop 未达）。

## 18. Full Agent M4

N=1：first action after mutation = **VERIFICATION（`run_tests`）**；
verification_pass=True、latest_revision_covered=True、behavior_pass=True。
（终态 failed 由后续 finalization/repair 路径导致，非 verification 缺失。）

## 19. Verification Attempt

| 层 | Attempt |
|---|---|
| Raw API run_tests | 5/5 (100%) |
| Minimal SDK run_tests | 3/3 (100%) |
| Full Agent M4 | 1/1 (100%) |
| Full Agent M1 | 0/1 (0%) |

Full Agent N≥3 未完成（本地模型单步过慢）。

## 20. Latest Revision Coverage

Full Agent M4：**covered=True**（run_tests 在最新 revision 执行并通过）。

## 21. M3 Failure Recovery

未进入（需先确认 Full Agent M1/M4 attempt ≥90%）。

## 22. Controls

M6/M7/M8：`run_tests` 为纯验证 capability，不影响 read-only / 无测试修改 / 关键词误判用例
（obligation detection 未变）。全量测试 733 passed 覆盖相关回归。

## 23. Unnecessary Verification

`run_tests` 只在 obligation=required 且模型选择时执行；新增工具本身不强制调用。
Control（M6/M7/M8）未出现强制验证。

## 24. Structured Commitment Decision

**L1 暂不需要**：single-tool（raw/SDK）已 100%，且 Full Agent M4 自然选择了 `run_tests`。
剩余 full-loop 漂移（M1）属 **情况 B（full-loop action selection）**，需先以 N≥3 确认后再决定 L1。

## 25. Production Decision

**Pure Verification Tool = ADOPT（实验→候选生产）**：
- 直接执行探针全通过、安全边界有效；
- Raw API / Minimal SDK semantic verification 100%；
- Full Agent M4 达到 verification attempt + latest revision coverage。
- **但**：Full Agent M1/M4 N≥3 未完成、M3 闭环未验证 → 暂标 **EXPERIMENTAL**，待 N≥3 达标后进入生产 Registry。
- 保留 Completion Obligation Gate（不得放宽）；Guard B 继续 OFF。

## 26. Final Causal Diagnosis

**Primary cause = Tool Abstraction Mismatch（已确认）。**
`run_python` 要求模型自己把 verification intent 转成 `pytest.main(...)` 代码；
`run_tests` 直接表达“运行测试/验证修改”，semantic verification 从 40% → 100%。
残余（M1 full-loop 漂移）属 **full-loop action selection / tool competition**，
需 N≥3 与（必要时）L1 验证。

## 27. Next Direction

在**本地模型**下完成 Full Agent **M1/M4 N≥3**：
- 若 Verification Attempt ≥90% 且 Latest Revision Coverage ≥80% → 进入 **M3 闭环**（FAIL→REPAIR→PASS→COMPLETE，N=3，≥2/3）。
- 若 Full Agent 仍低而 single-tool 高 → 进入 **L1 Structured Action Commitment**（不新增 Planner）。

---

## 最终输出

```text
run_python semantic verification rate =
40%（Raw API，N=5）

pure verification semantic rate =
100%（Raw API，run_tests，N=5）

Minimal SDK pure verification rate =
100%（run_tests，N=3）

Full Agent verification attempt =
M4 1/1（100%）、M1 0/1（0%）——N≥3 未完成（本地模型过慢）

Latest Revision Coverage =
M4 True（full agent N=1）

M3 closed-loop success =
NOT RUN

Primary cause =
tool abstraction mismatch（已确认；残余为 full-loop action selection）

Pure Verification Tool =
EXPERIMENTAL（单工具/直执/安全全通过；Full Agent N≥3 待完成）

L1 Structured Commitment needed =
NO（当前证据下不需要；若 Full Agent N≥3 仍低则为 YES）

Production fix =
新增 run_tests（纯验证，结构化 argv，无 shell）+ 修正 capability metadata
（run_python→CODE_EXECUTION、code_loop→MUTATION_VERIFICATION_LOOP、run_tests→VERIFICATION）
+ Router 暴露 run_tests

Next component to modify =
Full Agent verification attempt 的 N≥3 确认（M1/M4），随后 M3 闭环
```

## 附：修改文件

| 文件 | 改动 |
|---|---|
| `code_exec.py` | 新增 `run_tests`（纯验证，结构化 argv，target/extra_args 校验，无 shell） |
| `agent.py` | 注册 `run_tests` 工具 |
| `runtime/spec.py` | capability 校正：run_tests→VERIFICATION、run_python→CODE_EXECUTION、code_loop→MUTATION_VERIFICATION_LOOP；catalog 增加 run_tests |
| `runtime/readiness_gate.py` | `_P9_VERIFICATION_TOOLS` 加入 run_tests |
| `runtime/tool_router.py` | `_CODING_SUPPORT`/`TOOL_TERMS` 加入 run_tests |
| `benchmark/tool_probe.py` | 支持 run_tests |
| `tests/test_phase11_completion.py` | capability 断言更新 |
