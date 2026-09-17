# AgentReply 格式校验修复报告

> 项目：FORGE / my_creative_agent · 日期：2026-09-06
> 目标：把「Model → AgentReply JSON → 严格完整性校验 → 不符合即失败」重构为
> 「Model → ReplyParser → ReplyNormalizer → Minimal Schema Validator → Canonical AgentReply → Semantic Handler」。
> 结论：**格式问题不再构成任务失败**；离线回归 268/268 全绿；真实网关三种输出形态（正常 JSON / 纯文本 / fence+说明）零格式失败。

---

## 1. 原因

- `schemas.py::AgentReply` 用 Pydantic 强校验，`kind/summary/content` 必填、`questions/saved_file/ui` 有默认值但整体必须严格合 JSON；
- `guardrails.py::_raw_check_output` 把「任何格式瑕疵」（缺字段、纯文本、数组、空正文、note 缺 saved_file、saved_file 不可信、ui 畸形/超限）一律返回拦截原因 → `reply_integrity_guardrail` tripwire → `execute_turn` 重试/最终 raise → `run_turn` `mark_failure` → **run.failed**；
- 网关模型（chat/completions 中转）输出不稳定（偶发 fence、前后说明、别名字段、尾逗号），把「格式题」当成「任务错误」是频繁失败的根源；
- 重试会重跑工具，叠加触发 max_turns/预算墙，形成「回答重复 + 熔断」的次生故障（v18 流式可视化后暴露）。

## 2. 原 Schema（schemas.py 摘要）

- 必填：`kind`(answer|plan|note|questions|done)、`summary`、`content`
- 可选默认：`questions[]`、`saved_file?`、`next_step?`、`ui[]`（6 类白名单卡片，discriminator=type）
- 输出闸另加业务检查：空正文、note 必须真实 saved_file、saved_file 路径可信（notes/*.md、exports/*office）、ui 数量/一致性上限——**任何一项不过即整体失败**。

## 3. 新 Schema（协议稳定化）

核心必填仅保留两项：**`kind` + `content`**。其余全部按 kind 按需、缺失填默认：

| 字段 | 新规则 |
|---|---|
| kind | 白名单五值；非法/未知 → 降级 `answer` + warning |
| content | 必填；缺失时用 summary 兜底；再缺 → fallback |
| summary | 默认 `""` |
| questions | 默认 `[]`；questions 类允许只带问题列表（无正文也合法） |
| saved_file | 默认 null；**不可信路径 → 丢弃 + warning**（不再失败） |
| next_step | 默认 null |
| ui | 默认 `[]`；**结构/业务不合规 → 整组丢弃 + warning**（不再失败） |
| 别名 | type→kind、message/answer/text/reply/result→content、question→questions、path/files→saved_file |

## 4. Normalizer（runtime/reply_parser.py）

- `extract`：剥离 ```json fence（多行）、从任意位置 `{` 尝试 `raw_decode` 取第一个合法对象、容忍前后说明文字与多余换行；
- `normalize`：别名映射 + 安全默认值 + 长度上限（content≤200k、questions≤20、summary≤2k）；
- `forgive_validate`：逐级宽恕（ui 丢弃 → saved_file 丢弃 → next_step 丢弃 → 最小 answer 体），每次宽恕记录 `reply.validation_warning`；
- `_saved_file_trusted` / `_ui_within_limits`：沿用原 guardrail 的业务上限常量（`guardrails.UI_MAX_*`），语义从「拒绝」改为「丢弃+警告」。

## 5. Repair（格式修复，绝不重做任务）

- 本地确定性修复（默认、零成本）：智能引号归一、去尾逗号、`{…}` 区间二次截取重解；
- 可选模型修复：`FORGE_REPLY_REPAIR=model` 时用网关低温度单次调用“只整理 JSON、不重做任务”（默认关闭，避免引入不稳定与成本）；
- 修复后仍进入 normalize/validate 同一管线。

## 6. Fallback（最终兜底）

- 仍无法形成合法 AgentReply 且**存在可读文本** → 降级 `{"kind":"answer","content":"<原始可读回答>"}`，记 `reply_validation_warning`，**Run 正常 completed**；
- 仅当模型**完全没有可用输出**（空串）才进入错误流程（安全闸兜底拦截）。

## 7. 格式错误与任务错误分离

| 错误 | 处理 | Run 状态 |
|---|---|---|
| 格式瑕疵（缺字段/别名/fence/前后说明/尾逗号/纯文本） | normalize / repair / fallback | **completed**（+warning 事件） |
| saved_file 不可信 / ui 不合规 | 丢弃该字段 + warning | **completed** |
| 密钥/API Key 出现在输出 | 安全闸 tripwire（唯一保留的拦截） | failed（安全） |
| 模型完全无输出 | 安全闸兜底 | failed |
| tool execution failed / verification failed / model unavailable | 原有异常路径 | failed（真实错误） |

> 落地：`guardrails.check_output` 只做安全审查（`runtime.reply_parser.parse` + `_has_secret`）；`runner.run_turn` 成功分支先 canonical 化（`final_output = canonical_json`）并写 `reply.validation_warning` 事件（≤3 条）；CLI/Web/审计下游拿到的都是稳定 canonical。

## 8. Provider Structured Output（第七项）

- 探测函数 `reply_parser.structured_output_supported()`：当前第三方网关（`OPENAI_BASE_URL` + chat/completions，openai-agents 0.22）**不支持** `response_format/json_schema`，SDK 无 text_format 钩子 → **未启用**，由 ReplyParser 全权兜底；
- 预留启用路径：直连 OpenAI responses（无 BASE_URL）时返回 True，可在 Agent 层配置原生 JSON 输出后关掉部分容错层（报告建议保留 parser 作为最后防线）。

## 9. 测试结果

- 新增 `tests/test_reply_parser.py`（20 用例）：正常 JSON / 缺 optional / 字段乱序 / fence / 前说明 / 后说明 / type 代 kind / message 代 content / questions 缺失 / ui 缺失 / saved_file 缺失 / 非法 JSON 本地修复 / 修复失败纯文本降级 / 未知 kind 降级 / dict 与 AgentReply 实例形态 / questions 仅问题列表 / 空输出不 ok / 密钥不拦但可解析 / coerce 永不抛 / 结构化输出探测；
- 更新旧契约用例（`test_guardrails` / `test_ui_schema` / `test_office_docs` / `test_main_helpers` / `test_task_runtime` / `test_budget_router`）：格式类从“必拒”改为“宽恕+warning”，密钥类仍必拦；
- 全量离线：**268/268 通过**；真实网关冒烟：正常 JSON、纯文本、fence+说明三种形态全部 `ok`，final_output 均为合法 canonical。

## 10. 是否仍存在“格式问题直接导致 run.failed”的路径

**已不存在**：
1. 输出闸（`reply_integrity_guardrail`）不再因格式 tripwire——只有密钥/完全无输出才拦；
2. `execute_turn` 的 OutputGuardrailTripwireTriggered 重试路径现在只会被安全类触发；
3. `run_turn` 成功分支的 canonical 化保证下游永不因非 JSON 崩溃；
4. 保留的唯一“因输出而失败”路径：密钥泄露（安全，必须失败）、模型零输出（真实无答案，属任务错误范畴）。

## 附：关键文件

- `runtime/reply_parser.py`（新，解析/归一化/最小校验/修复/降级/探测）
- `guardrails.py`（输出侧改为纯安全闸）
- `main.py`（`coerce_reply` 走 parser）
- `runtime/runner.py`（成功分支 canonical 化 + warning 事件）
- `tests/test_reply_parser.py`（新 20 用例）+ 6 个旧用例契约更新
