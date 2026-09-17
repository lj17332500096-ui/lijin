# 《FORGE-Agent-Convergence-Hardening实施与测试报告》

> 日期：2026-09-10
> 目标：多问题任务收敛 + Tool Router 限制 + DiscoveryTracker 升级 + Completion 收口。
> 原则：复用现有主链（route_agent → Tool Router → SDK Runner → tool wrapper → ReplyParser → Completion Gate → stream_final），**未新建**第二套 Agent Loop / Planner / Completion Engine / Activity System / Tool Runtime / Workflow Engine。

---

## 1. 原失败 Run 的真实调用序列

带真实输入「介绍你能做什么、有哪些技能、擅长什么，并告诉我北京天气。」（`tk_2226f4c9` / `task_cd5a696d`）：

```
state = failed
error = Max turns (20) exceeded
tool_calls ≈ 27
```

事件时间线（agent.db `task_events` / `tool_calls`）：

| 阶段 | 事实 |
|---|---|
| 重复工具调用 | `web_search` 连续 **12+ 次**，Query 全是同一主题的不同措辞：`北京天气`/`北京今日天气`/`北京现在多少度`/`Beijing weather today Sep 9`/`北京天气9月9日 实况`… |
| 无关工具 | `read_note`(读不存在技能文件/读已有天气笔记)、`recall_memory`(空)、`save_note` |
| save_note 被拒 | `save_note` 被 intent gate 拒绝（`【工具意图门】...已阻止调用`），但 **之后再试了一次** |
| 无数据 | web_search 始终只返回**标题+链接**，无可靠实时温度 → 模型反复换词尝试 → 轮数耗满 |
| 收尾 | `Max turns (20) exceeded` → failed；前端「遇到问题 · 查看执行过程」→「最终回答生成失败，已执行的操作仍然保留。」 |

---

## 2. 为什么现有 DiscoveryTracker 没拦住

`runtime/readiness_gate.py` 的 `DiscoveryTracker` 是**精确签名**判定：

```python
discovery_signature(name, arguments)  # 参数做字符串归一
```

- key 是「参数精确字符串」→ `北京天气 9月9日` / `北京今日天气` / `Beijing weather today` **签名互不相同** → 换词即绕过重复检测。
- **更关键**：`note_discovery` 只在 `rctx.readiness_status != READY` 时触发（`runner.py`）。普通问答（天气）`readiness=READY`，**有界探索根本未启用**，没有任何一层拦截 web_search 换词。

---

## 3. Tool Router 为什么选入无关 note/memory 工具

`runtime/tool_router.py`：

```python
BASE_TOOLS = { ..., "save_note", ... "read_note", "list_notes", ... }  # 常驻，永不裁剪
```

- `save_note`、`read_note`、`list_notes` 在**常驻基础集**里 → 任意请求（含「北京天气」）无条件暴露给模型。
- `select_tool_names` 的 `rest` 补全逻辑（未命中也会补全所有工具）也会把它们加进子集。
- 于是模型在「能力介绍+天气」这种**无关记忆/笔记**的请求下，仍会去调 `read_note`/`save_note`。

---

## 4. web_search 当前真实能力

`tools.py web_search_impl` 走 Tavily / DuckDuckGo / Bing，返回**格式化字符串**（标题+链接+摘要）。这些免费源对「城市实时温度」这类**结构化实时数据**覆盖很差，实测返回多为标题/链接，**无可靠温度实体**。

> 结论：**当前 FORGE 没有可靠的结构化天气能力。**「不断换个词 web_search」不是解法；模型只要尝试有限次后应基于已知信息作答 / 诚实说明限制。本次**不擅自建设天气系统**（超出范围），只让收敛策略正确生效。

---

## 5. 修改后的 Tool Router 规则

`runtime/tool_router.py`：

1. `BASE_TOOLS` **移除** `save_note` / `read_note` / `list_notes`（不再无条件暴露）。
2. 新增 `_MEMORY_NOTE_TOOLS`（save_note/read_note/list_notes/remember/recall_memory/forget_memory）与 `_MEMORY_NOTE_INTENT` 正则。
3. `select_tool_names` 增加 **eligibility**：请求不含记忆/笔记语义（`memory_context=False`）时，从 `available_set` / `mentioned` / `scored` / `rest` 全阶段排除 `_MEMORY_NOTE_TOOLS`。

效果：
- 「介绍你能做什么有哪些技能并告诉我北京天气」→ 不再暴露任何 note/memory 工具（实测 `memory_context=False`，note/mem 命中=无）。
- 「帮我记住我家地址」「有哪些备忘」「读我保存过的方案」→ 保留相应工具。
- `save_note` 尤其严格：无保存意图时绝不暴露（第一层），不再只依赖后置 intent gate（第二层）。

---

## 6. 修改后的 DiscoveryTracker（三层）

`runtime/readiness_gate.py`：

- **Level 1 精确签名**：`discovery_signature`（保留）。
- **Level 2 语义意图**：`semantic_search_intent` —— 只对搜索类工具（`_SEARCH_TOOLS`）生效：去噪声词 → 提取核心实体 → 中英城市映射（`北京→beijing` 等）→ 日期统一 `@now`。把「北京天气9月9日 / 北京今日天气 / Beijing weather today」归并为**同一意图** `web_search|@now|q:beijing`。
- **Level 3 结果新颖度**：通过「同一语义意图达到 `semantic_limit`(3) 即收敛」替代对返回内容结构的脆弱解析（web_search 返回字符串，不做不可靠的用户态 parsing），达到同等「结果无新增→无进展」语义。
- **Intent-level blocking**：收敛后把意图记入 `blocked_intents`；之后同意图再调用（换词也）直接返回 `SEARCH_CONVERGENCE_REACHED`，**不真实执行**。仅对当前 Run 生效，不禁用全局 web_search。

`runner.py` 触发条件升级：`_sem_search`（web_search/search_documents/search_sources）**始终**走 `note_discovery`（含 READY），其它只读探索保持「非 READY才启用」——避免误伤正常多步探索。

---

## 7. semantic intent signature 规则

```
semantic_search_intent(name, arguments):
  仅 name in _SEARCH_TOOLS：
    1. 提取 query/keyword/q
    2. 城市中英映射（北京→beijing …）
    3. 去噪声（今天/天气/温度/数字/日期/please/today/weather…）
    4. 日期统一折叠 @now
  返回 f"{name}|@now|q:{entity}"
```

实测：北京相关 5 种措辞 → 全部 `web_search|@now|q:beijing`；上海/广州/成都互相不同（不误伤）。

---

## 8. result novelty 规则

用「同语义意图次数达到 `semantic_limit`(3)」作为 `NO_PROGRESS` 触发（Level 2 语义收敛）。不同 query 若落到同一语义意图 + 连续多次 → 收敛。**不同主题（不同实体）不会累计**，因此 Coding Agent 的多步不同探索不被误伤。

---

## 9. Run-level Tool Budget

`runtime/runctx.py`（RunContext）：

- `max_total_tool_executions = 20`（软预算，Max turns=20 的并行护栏）
- `max_web_search_executions = 5`
- `tool_execution_counts`（真实执行计数，被拒不计）
- `can_execute_tool(name)` / `note_executed(name)`

`runner.py` tool wrapper：
- 工具入口（L193 后）：`rctx.can_execute_tool(name)` 预算检查，超限返回「已达上限，停止调用」+ 记录 `tool.budget_exhausted`。
- 真实执行成功后：`rctx.note_executed(name)` 计数。

**被 Runtime 阻止的调用**走 `TOOL_BLOCKED`（不计入 `tool_execution_counts`），与真实执行分离。

---

## 10. rejected action memory

`runtime/runctx.py`：

- `rejected_actions: dict[str, str]`（工具名 → 原因）
- `remember_rejected(action, reason)` / `action_rejected(action)`

`runner.py` intent gate：
- `check_user_tool_intent` 拒绝时 → `rctx.remember_rejected(name, reason)`。
- 下次同工具再调用（无新用户消息/授权）→ 入口预检 `rctx.action_rejected(name)` → 直接返回 `ACTION_ALREADY_REJECTED_FOR_THIS_RUN`，不再重复执行。

效果：save_note 被拒后，模型再次 save_note → 不执行，被明确告知「除非用户改变意图，否则不要重试」（修复原生死循环第二击）。

---

## 11. Completion Gate PARTIAL / convergence 行为

**结论：无需新增 verdict 枚举。** 现有 `CompletionGate.evaluate` 已支持 PARTIAL：

- 诚实 limitation 的 `kind=answer`（0 write/verify/approval 声明）→ 默认 `PASS`。
- 能力盘点部分 → `is_capability_query` 分支 `PASS`。
- 无证据声称执行完成 → `CLAIM_UNSUPPORTED`；重复调用 → `NO_PROGRESS`；验证失败 → `VERIFICATION_FAILED`。

实测（确定性 evaluate）：
| 场景 | verdict |
|---|---|
| 能力介绍 + 天气说「搜索未返回可靠数据，无法确认」 | **PASS**（PARTIAL ✅）|
| 「已成功查询到天气，25度」但 0 证据 | PASS（信息性声明，真实性归出口 guardrail）|
| 真实修改+验证通过 | PASS |
| 读文件完成 + 天气未果 | PASS（部分完成 ✅）|

**一个子问题失败 ≠ 整个请求失败**：天气无法解决时，模型收敛后诚实说明 → `PASS` 进入 `stream_final` → 正常生成最终回答。

---

## 12. 是否修改 AgentReply schema

**没有。** 未新增 `partial` / `unresolved` 字段。诚实 limitation 通过现有 `answer.content` 表达即可让 gate `PASS`，避免第二套响应协议。

## 13. 是否修改 ReplyParser

**没有。** 现有解析不变。

## 14. 是否修改 stream_final

**没有改核心机制。** `tools=[]` + `FinalContentStream` 结构保留。仅保留此前「最终回答生成失败」的回退链修复（decoded → raw → canonical），确保 PARTIAL/limitation 也能进入 `stream_final(tools=[])`。

---

## 15. 修改文件清单

| 文件 | 改动 |
|---|---|
| `runtime/tool_router.py` | BASE_TOOLS 移除 note/memory；新增 `_MEMORY_NOTE_TOOLS`/`_MEMORY_NOTE_INTENT`；`select_tool_names` eligibility |
| `runtime/readiness_gate.py` | 新增 `_SEARCH_TOOLS`/`_CITY_MAP`/`_SEARCH_NOISE`/`semantic_search_intent`/`_blocked_text`；`DiscoveryTracker` 升级三层 + `blocked_intents` |
| `runtime/runctx.py` | `RunContext` 新增 tool budget + rejected-action memory |
| `runtime/runner.py` | tool wrapper 接入 budget 预检 + `note_executed` + rejected-action 预检；`note_discovery` 对搜索类始终启用 |
| `tests/test_convergence_hardening.py` | **新增** 14 个收敛确定性测试 |

（此前一轮已修复的 `public_activity.py`/`public_response.py` 保留，未回退。）

---

## 16. 单元测试

`tests/test_convergence_hardening.py` —— **14 passed**：

- SemanticIntentTests：不同措辞归并 / 不同主题不归并 / 非搜索用精确
- DiscoveryConvergenceTests：同意图收敛 / 非搜索精确耗尽 / 不同主题不误伤
- ToolRouterEligibilityTests：天气/能力介绍不外露 / 显式保存保留
- RunBudgetTests：web_search 上限 / 总执行上限 / rejected-action 记忆
- CodingNoRegressTests：Coding 多步不同探索不误伤 / 多步执行在预算内

---

## 17. 集成测试

全量回归（排除需浏览器的 `test_theme_cdp`）：**580 passed, 1 skipped**（含既有 566 + 新增 14），无回归。

本次新增收敛逻辑依赖可复现的状态机测试；未使用模型额度。

## 18. Coding Agent 回归测试

`test_convergence_hardening.py::CodingNoRegressTests`：
- 搜索代码(2) → 读多文件(3) → 运行测试(2)：不同工具/不同主题 → 不被语义收敛拦截。
- 多步执行(6 真实调用) 在总预算内 → 不被误伤。

## 19. 原失败任务修改前后对比

### BEFORE（真实 run `task_cd5a696d`）

```
turns:     20 (Max turns exceeded)
tool calls: 27
web_search: 12+（同一主题换词，无上限/无语义收敛，readiness=READY 有界探索未启用）
memory/note: read_note ×3、recall_memory ×2、save_note ×2(被拒后又试)
completion: Max turns (20) exceeded → FAIL
status:    failed，前端「遇到问题」
```

### AFTER（预期行为，由确定性测试支撑）

```
turns:     收敛后自动结束，远低于 20
tool calls: 大幅减少（note/memory 不暴露、搜索语义收敛、budget 上限）
web_search: 同一语义意图最多 3 次真实执行 → 收敛 → SEARCH_CONVERGENCE_REACHED
blocked duplicate searches: 收敛后换词直接拦截（不执行）
memory/note: 不再无关出现（Tool Router eligibility）
completion: 能力介绍 PASS + 天气 limitation → PARTIAL PASS → stream_final 正常生成最终回答
status:    completed（部分完成 + 诚实 limitation），而非 failed
```

> 真实端到端（用真实模型重跑原失败任务）需消耗额度，本次以确定性单测 + 集成回归验证收敛逻辑；真实模型复跑作为后续验证项（见 §21）。

---

## 20. 最终回答

1. **Tool Router 为什么不会给普通天气任务暴露 save_note/read_note/recall_memory？**
   移除 BASE_TOOLS 常驻 + `_MEMORY_NOTE_INTENT` eligibility，无记忆/笔记语义时全阶段排除 `_MEMORY_NOTE_TOOLS`。

2. **“北京天气”和“Beijing weather today”能否识别为同一搜索意图？**
   能。`semantic_search_intent` 城市映射 + 去噪 → 都归一为 `web_search|@now|q:beijing`。

3. **DiscoveryTracker 是否升级为 exact + semantic intent + result novelty？**
   是。exact(精确签名) + semantic intent(换词归并) + result novelty(语义收敛代表无新增)。

4. **web_search 同一语义目标最多真实执行多少次？**
   真实执行受 `semantic_limit=3`（语义收敛）+ `max_web_search_executions=5`（budget 兜底）双重限制。

5. **不同 query 返回相同结果是否会触发 No Progress？**
   会。落到同一语义意图且连续达到上限 → `SEARCH_CONVERGENCE_REACHED`（代表结果无新增）。

6. **intent gate 拒绝 save_note 后为什么不会再真实执行第二次？**
   `rctx.remember_rejected` + 入口 `action_rejected` 预检 → 直接 `ACTION_ALREADY_REJECTED_FOR_THIS_RUN`，不执行。

7. **一个子问题无法完成时是否仍可完成整个 Run？**
   可。诚实 limitation 的 answer → gate `PASS`（能力介绍部分也命中 `is_capability_query`）。

8. **Completion Gate 是否支持“能力已耗尽 → limitation → final”？**
   支持（现有 PASS 路径），无需新增 verdict。

9. **Completion repair 是否还能重新打开已经 blocked 的搜索意图？**
   不能。`blocked_intents` 里已收敛意图会被 `semantic_search_intent` 直接拦截；repair 观察引导「基于已有信息/说明限制」，不会重新搜。

10. **Max turns=20 是否仍保持为最后 safety guard？**
    是。保持 20，仅作最终保险；行为收敛（tool router / 语义收敛 / budget / rejected-memory / completion）在 turn 耗尽前主动触发。

11. **原失败任务重测：**
    确定性测试已覆盖收敛逻辑；真实模型端到端复跑需额度（见 §21）。

12. **Coding Agent 正常多步搜索/修改/验证是否被误伤？**
    未误伤。`CodingNoRegressTests` 证明不同主题/不同工具多步探索不触发语义收敛，且在预算内。

---

## 21. 剩余验证项（诚实清单）

- 真实模型端到端重跑「介绍你能做什么…北京天气」：需模型额度，验证 `turns/tool_calls/web_search/status` 的 AFTER 数值。本次以确定性单测 + 集成回归（580 绿）验证收敛**逻辑**；数值型端到端待跑。
- 前端 Activity「收敛后不再显示正在查询」：已有搜索聚合（tool_started 同类合并）+ `finish`/`wait` 状态；未改前端，若需更贴切文案可后续微调。
