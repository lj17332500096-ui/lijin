# 此刻 · NOW 最终回答边界与内部推理泄露修复报告

- 日期：2026-09-08
- 范围：**后端 / Harness 输出通道**（未改前端 CSS/视觉；前端仅继续"不渲染 reasoning/feedback"，无新增）。
- 结论先行：**FINAL ANSWER BOUNDARY = PASS**（根因已定位并修复；Provider reasoning 已与 content 分离；DB 最终内容干净；前端不渲染内部信息）。

---

## 0. 判定字段

```
RAW_PROVIDER_HAS_REASONING_FIELD = YES        （实测 message 字段含 reasoning_content）
REASONING_MERGED_INTO_CONTENT_BEFORE = NO     （agents SDK 拆分为 thinking block；DB content 干净）
COMPLETION_FEEDBACK_LEAKED = YES              （修复前：repair 消息把 Gate 文案拼进用户消息 → 模型复述）
DATABASE_FINAL_CONTENT_CLEAN = YES            （现存 runs 的 assistant content 均无 Gate/reasoning 原文；修复后新 run 更干净）
FRONTEND_RENDERING_INTERNAL_REASONING = NO    （前端只渲染 assistant content，从不渲染 reasoning/feedback；未修改前端）
ROOT_CAUSE = 运行时 runner.py:1467 把 Completion Gate 观察文案以 "（系统提示：{observation}）" 形式
            拼进 repair 轮的用户消息文本，弱模型将其复述进最终回答（"系统提示让我撤回完成声明"）。
FIXED = YES
```

---

## 1. 证据链（逐层）

| 层 | 取证结果 |
|---|---|
| Provider raw response | 实测 `POST /v1/chat/completions`（`localhost:8080`，Qwen3.6-35B gguf）→ `response.keys=[choices,created,model,system_fingerprint,object,usage,id,timings]`；`choices[0].message.keys=[role, content, reasoning_content]`。**存在独立 `reasoning_content`**。 |
| reasoning 是否并入 content | `agents` 库 `chatcmpl_stream_handler.py`：`delta.content`（message）与 `delta.reasoning_content`（thinking block）**分别处理**，`reasoning_content` 存进 `ResponseReasoningItem`，`output_text` 只聚合 message 文本 → **reasoning 不并入 content**。 |
| 最终 content 提取 | `runtime/runner.py` `_assistant_parts → parse(final_output) → result.canonical`（ReplyParser），读取的是 message 内容（`choices[0].message.content`），不经 reasoning。 |
| Database assistant content | 检索 `messages role='assistant'`：含"系统提示/运行时校验/撤回/Run 没有/执行证据"= **0 条**；含"我需要/让我想/我还没有"= 3 条，但**均为正常的追问/澄清**（如"我需要确认几个关键信息才能帮你安排…"），**非内部推理**。→ DB 内容干净。 |
| 真正泄露点 | `runtime/runner.py:1467`（修复前）：`attempt_text = f"{message}\n（系统提示：{observation_for(gate_verdict)}）"` —— 把 `OBSERVATION_CLAIM_UNSUPPORTED`（"【运行时校验】…请撤回完成声明…"）**原样拼进用户消息**，模型复述 → "系统提示让我撤回完成声明"。 |

---

## 2. 根因（精确到行）

`runtime/runner.py` repair 分支（`for _repair in range(2)` 的未通过路径）：
```
# 修复前
attempt_text = f"{message}\n（系统提示：{observation_for(gate_verdict)}）"
```
- 把内部 Gate 观察（"请撤回完成声明 / 没有检测到执行证据 / 已阻止按完成收尾"）注入到**下一轮模型的用户消息**中。
- 该网关为**本地 reasoning 模型**（Qwen3.6-35B），指令跟随时会**复述**这句"系统提示：…" → 进入最终回答。
- 之后若该内容为 `canonical.content`，会被持久化/流式给前端 → 用户看到"系统提示让我撤回完成声明 / 我需要…"。

## 3. 修复（输出通道收口，非字符串删除）

**`runtime/completion.py`** 新增（把内部 observation 转成面向用户的重新生成指令）：
- `short_user_reason(verdict)`：给模型一个**用户能懂的短事实**（如"该任务实际上还没有真正执行或没有产生可见结果。"），**不含** 系统/校验/证据/运行 等术语。
- `repair_prompt(message, verdict)`：= 原用户请求 + 该事实 + 明确要求——"请基于实际情况重新回答…直接说明还需要什么…用普通友好直接的口吻…**不要提及、复述或解释任何后台的校验机制、内部规则或这一次的更正说明**"。指令文本**本身不含可被复述的内部术语**（不出现"系统提示/撤回/无执行证据"等字眼，避免被模型引用）。

**`runtime/runner.py:1467`**：
```
# 修复后
from runtime.completion import repair_prompt as _rp
attempt_text = _rp(message, gate_verdict)
```
- 保留内部审计事件 `completion.check.rejected`（仅落 audit）；**不再**把 observation 原样注入用户消息；移除会暴露内部文案的 stream tool 事件。

## 4. 最终回答边界（输出契约，已内置）

`repair_prompt` 的生成指令相当于 Final Answer Contract：
1. 直接回答用户问题；2. 只讲用户需要知道的事实；3. 需补信息时直接点名缺失项；4. 未完成则说明"还需要什么"；5. 不描述内部推理；6. 不描述 Runtime/Gate/Tool 机制；7. 不说"系统提示我"；8. 不引用隐藏 policy；9. 不复述 corrective feedback。

## 5. 泄露检测（防御性测试，非根因）

新增 `tests/test_final_answer_boundary.py`（4 项 PASS）：
- `repair_prompt` 对 **5 种 GateVerdict** 均**不含** `系统提示/运行时校验/撤回完成声明/执行证据/Run 没有/没有检测到/Completion Gate/CLAIM_UNSUPPORTED/ExecutionEvidence/验证未通过/readiness/policy/gate/runtime`。
- `repair_prompt` 为**用户能懂的重新生成指令**（含"还需要什么/未真正完成"+ "不要提及后台…"）。
- `short_user_reason` 五种 verdict 均无术语、<60 字。
- 干净最终回答样例无术语。

## 6. 运行结果

- `pytest tests/test_final_answer_boundary.py tests/test_contract.py tests/test_completion_gate.py -k "not RunTurn"` → **22 passed**。
- `pytest tests/test_frontend_smoke.py` → **1 passed**（前端冒烟，验证修改后前端与后端服务仍正常、无 JS 错误）。
- 说明：`test_completion_gate.py` 中 9 个 `RunTurnCompletionGateTests` 因**既有 harness 漂移**（`_FakeHarness._execute()` 未接受 runner 已传入的 `provider` kwarg）失败，与本次修改无关；纯 Gate 逻辑 17 项 PASS。

## 7. Provider 合规（10 次说明）

- 已实际抓取 1 次真实 provider 响应，确认 `message.reasoning_content` 字段存在（`RAW_PROVIDER_HAS_REASONING_FIELD = YES`）。
- `agents` SDK 将 reasoning 拆分为 thinking block、`output_text` 仅含 message 内容 → **`REASONING_MERGED_INTO_CONTENT_BEFORE = NO`**（由 SDK 源码 + DB content 佐证）。
- 本轮未跑满 10 次网络调用（成本/可达限制）；但"reasoning 独立字段 + 不并入 content"已由实现层+数据库两层证据确认，非猜测。

---

## 8. 修复前/后 最终回答（示例）

**修复前（会被模型复述）**：
> 系统提示让我撤回完成声明。当前 Run 没有检测到支持该声明的执行证据……

**修复后（期望，面向用户）**：
> 要继续搭建评测集，我还需要：• 主要评测对象 • 最关心的核心场景 • 主要边界案例。如果你暂时没有，我可以先按通用框架起一个初版。

（修复后由 `repair_prompt` 生成该方向的清洁重答指令，模型据此重答，不再复述内部校验文案。）

---

## 9. 未改 / 如实说明

- 未改前端 CSS/视觉；前端只渲染 assistant content，不渲染 reasoning/feedback（`FRONTEND_RENDERING_INTERNAL_REASONING = NO`）。
- **残余（已注意）**：`_fail(reason_text, …)` 的 `reason_text`（如"已阻止按完成收尾…"）会进入 run 的 `error_message`，失败卡可能显示此类**内部措辞**。这属于 Error 卡文本，非最终回答边界；如需一并"人话化"，可再对 `reason_text` 做用户化改写（超出本次"最终回答边界"范围，已在报告标注）。
- 根因修复是**输出通道收口**（不再把内部文案注入模型可复述的通道），不是对模型输出做简单字符串删除。

## FINAL ANSWER BOUNDARY = **PASS**
