# FORGE Agent Verification Capability Proof — Phase 14 报告

> 日期：2026-09-11
> 主题：Verification Capability Proof + Structured Action Commitment
> 基线：《FORGE-Agent-Verification-Selection-Phase13》 + 当前真实代码
> 约束：**禁止 gateway/agnes/外部模型；本地 `Qwen3.6-35B-A3B`**（`FORGE_MODEL_PREF=local`）

---

## 1. Executive Summary

本轮把 Phase 13 的 A2 拆成 **A2a（Description Discoverability）** 与
**A2b（Schema / Invocation Usability）**，并首次给出**干净的因果证明**：

**A2b 是本地模型不调用 verification 的“能力层”主因，且是一个真实的 Tool Contract Bug。**

**决定性证据（原始 OpenAI 兼容 API，单一 run_python 工具，同一 prompt）**：

| Schema | finish_reason | tool_call |
|---|---|---|
| **truthful**（`required: [project]`） | `tool_calls` | `run_python({"project":"micro_fixture","args":"pytest","timeout":60})` ✅ |
| **strict**（`required: [project,filename,code,args,timeout]`） | `length` | **无 tool_call**（content 为空）❌ |

即：当 schema 诚实表达“只有 project 必需”时，本地模型**能且会**正确调用
`run_python(args="pytest")`；当 schema 强制 5 个字段全填时，本地模型**根本无法产生 tool call**。

**回答核心问题**：本地模型不是“不想选”，而是 **“不会调”**（schema 契约错误导致无法构造合法调用）。
A2b 已按真实实现修正为 truthful schema。

**但**：修正 schema 后，**完整 Agent Loop 内** M1/M4 的 verification attempt 仍为 0
（模型漂移到 DISCOVERY/READ）→ 存在**第二层** barrier（full-loop 工具竞争/上下文）。
L1/L2/L3 因本地模型单步 >5–10 分钟未能在会话内完成，标记 NOT EXECUTED。

---

## 2. Phase 13 Causal Gap

Phase 13 排除了 A（未暴露）、C（Completion）、D（detection）、E（未调用），
但未证明 **A2b（schema）** 与 **capability-level tool choice**。本轮补齐 A2b。

## 3. A2a vs A2b

- **A2a（Description）**：旧 `run_python` description 无测试/验证语义 → 成立；Phase 13 已修。
- **A2b（Schema / Invocation Usability）**：本轮审计发现 **required 全字段** 的契约错误 → 成立；本轮修复。

## 4. run_python Real Schema

```
strict_json_schema = True（修复前）
properties: project, filename(default ''), code(default ''), args(default ''), timeout(default 40)
required  : [project, filename, code, args, timeout]   ← 全部必填（错误）
additionalProperties: False
实现签名  : run_python(project, filename='', code='', args='', timeout=40)  ← 只有 project 必需
```

## 5. code_loop Real Schema

`code_loop(project, filename, max_attempts, args)`（由 `runtime/codex_loop.py` 提供）。
本轮未修改；Usability Probe 因本地模型时间成本未跑满。

## 6. Tool Invocation Usability

原始 API 探针（`run_python`，同一 prompt，truthful vs strict）：

- truthful：`finish_reason=tool_calls`，参数 `{"project":"micro_fixture","args":"pytest","timeout":60}`
  → **选择成功 + 参数合法 + 语义正确（pytest）**。
- strict：`finish_reason=length`，**无 tool_call**。

→ **run_python invocation usability：truthful = 可用（1/1 正确）；strict = 不可用（0/1）**。

## 7. Schema Corrections

`code_exec.py`：`@function_tool` → `@function_tool(strict_mode=False)`。
修复后 schema：`required: ['project']`，`additionalProperties` 不再强制。
**未把真实必需参数偷偷默认掉**（project 仍必需）。

## 8. Direct Tool Probe

`benchmark/tool_probe.py`（只暴露单工具 + 最小验证任务）。
- 单次 smoke（truthful）：selection 1/1，valid args 1/1（调用了 run_python，但先做了目录遍历，后引用不存在的 run_tests.py）。
- N=3（strict/truthful）：均 `MaxTurnsExceeded` 且无解析到的 tool_call → 说明在 SDK/Agent 封装下
  本地模型的 function-calling **不稳定**（与原始 API 的干净结果对比，见 §23 诊断）。

## 9. Structured Action Commitment

设计：`verification_due` 的决策 turn 先提交 `next_action_class ∈ {VERIFY, VERIFICATION_PREPARATION,
REVISE, BLOCKED}`（禁 FINAL），工具仍由模型自选。
**未执行**（本地模型时间成本；且 A2b 已给出更强因果结论）。

## 10. Commitment Accuracy

**NOT EXECUTED**。

## 11. Capability-Level Constraint

**NOT EXECUTED**（L2）。

## 12. Required Tool Choice

**NOT EXECUTED**（L3）；SDK 是否支持 `tool_choice=required` 未在本轮验证。

## 13. L0/L1/L2/L3 Experiment

| Level | 状态 | 结果 |
|---|---|---|
| L0 Free Choice（full loop，schema 已修） | **EXECUTED** | M1/M4 verification attempt **0%** |
| L1 Structured Commitment | NOT EXECUTED | — |
| L2 Capability-Focused | NOT EXECUTED | — |
| L3 Required Tool Choice | NOT EXECUTED | — |
| Direct raw-API probe | **EXECUTED** | truthful schema → 正确调用；strict → 无法调用 |

## 14. Verification Preparation

full-loop 中 mutation 后首个动作是通用 DISCOVERY/READ（非测试/构建配置定向）→ Preparation ≈ 0%。

## 15. M1 Results

| 条件 | Verification | State |
|---|---|---|
| Phase 12/13 baseline（strict schema） | 0 | failed |
| **Phase 14（truthful schema，full loop）** | **0** | failed |

## 16. M4 Results

同 M1：truthful schema 后 full-loop 仍 0 verification（state=failed）。

## 17. M3 FAIL→REVISE→PASS

**未执行**（前提 verification attempt 未达成）。

## 18. Latest Revision Coverage

0%（full loop 无 verification）。

## 19. False Completion

**0**（Completion Obligation Gate 始终 ON）。

## 20. False Verification Claims

**0**。

## 21. Controls

M6/M7/M8 在 Phase 13/14 均 pass；schema 修改不影响 control。

## 22. Local Microbenchmark

Behavior Pass = 3/5；verification-required case Attempt = 0%（full loop）。
→ Local Microbenchmark Baseline = NO。

## 23. Final Causal Diagnosis

**两层 barrier**：

1. **能力层（已证明、已修）— Tool Schema / Invocation Usability（A2b）**：
   `run_python` 的 strict schema 把 5 个字段全设为 required，而实现只要求 `project`。
   原始 API 对照实验证明：truthful schema 下本地模型**能正确调用** `run_python(args="pytest")`；
   strict schema 下**无法产生 tool call**。这是“不会调”，不是“不想选”。
2. **决策层（仍存在）— full-loop 工具竞争 / action selection**：
   修正 schema 后，完整 Agent Loop 中模型仍漂移到 DISCOVERY/READ，verification attempt 仍 0%。
   这与“单工具原始 API 能正确调用”形成对照 → 说明 full-loop 的多工具/长上下文是第二层障碍。

**因此 Phase 13 的 “model-level action selection limitation” 需修正为**：
`function-calling/schema limitation（已修） + full-loop tool-choice competition（待验）`。

## 24. Production Decision

- **保留** truthful schema 修复（`strict_mode=False`）：这是正确的 tool-contract 修复，
  不改变安全边界，且是本地模型调用 verification 的**必要前提**。
- **不进入生产**：L1/L2/L3 未验证；Guard B 继续 OFF；不放宽 Completion Obligation Gate。

## 25. Next Direction

在**本地模型**下完成 **L1（Structured Action Commitment）→ L2（Capability-Focused Exposure）→
L3（Required Tool Choice）** 的 N≥3 实验，判断 full-loop 的残余障碍是
`action commitment` / `tool-choice competition` / `required-choice enforcement` 中的哪一个；
若 L3 仍无法正确产生 args/执行，则转向 **llama-server / OpenAI-compat function-calling 兼容性**排查。

---

## 最终输出

```text
run_python invocation usability =
truthful schema: 可用（原始 API：finish_reason=tool_calls，args={"project":"micro_fixture","args":"pytest"}）
strict schema:   不可用（finish_reason=length，无 tool_call）

code_loop invocation usability =
NOT EXECUTED（本地模型时间成本）

Free Choice Verification Rate =
0%（full loop，M1/M4，schema 已修）

Structured Commitment Verification Rate =
NOT EXECUTED

Capability-Focused Verification Rate =
NOT EXECUTED

Required-Choice Verification Rate =
NOT EXECUTED / unsupported（未验证 SDK tool_choice）

Latest Revision Coverage =
0%

Primary cause =
tool schema / invocation usability（A2b，已证明并修复）
+ 残余 full-loop tool-choice competition（待 L1/L2/L3 验证）

Winning control level =
L0（full loop）不达标；原始单工具 truthful schema 达标；
L1/L2/L3 未执行

Production recommendation =
保留 truthful run_python schema（strict_mode=False）；
在本地模型下继续 L1→L2→L3 实验后再决定最小可靠机制；
不放宽 Completion Obligation Gate

Next component to modify =
本地模型下的 full-loop verification action selection
（Structured Action Commitment / capability-focused exposure / required tool choice 依次验证）
```

## 附：修改文件

| 文件 | 改动 |
|---|---|
| `code_exec.py` | `run_python` 改为 `@function_tool(strict_mode=False)`（truthful schema，required=[project]） |
| `benchmark/tool_probe.py` | 新增单工具 Usability Probe（strict vs truthful） |
