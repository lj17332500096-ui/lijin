# P1-7 设计说明：`task_plan` 降级为"结构化提示"，不再用正则决定工具集

> 日期：2026-10-04 ｜ 阶段：P2 ｜ 风险：**高**（复审报告标注"最大架构债"）
> 依据：`MIGRATION_POST_REVIEW_2026-10-04.md` §4 P1-7 行

## 1. 问题

`runtime/task_plan.py` 的 `infer_task_plan` 是一个**用正则实现的工具选择器**：
13 条中文关键词规则（`天气|气温|温度` → `get_weather`、
`计算|算平均` → `calculate`…）加上 4 处句内二次 `re.search` 决定"首选哪个工具"。

它与迁移引入的 LangGraph LLM 选择器**职责重叠**，且两者在同一次 Run 里先后生效：
`runner.py:3670` 把 `plan_required_tools(...)` 出来的名字**并入**
LLM 已选中的工具集。后果有两层：

1. **违反分工宪法**（`AGENTS.md`：面向业务语义的判断绝不能用关键字/正则）
   ——"用户是否在说天气"是业务语义，正是宪法禁止用正则做的判断。
2. **LLM 选择被规则悄悄否决**：LLM 选了什么不重要，正则命中就会把工具加回来。
   这使 L2 声称的"语义判断从规则迁到 LLM"在最外层失效。

复审报告同时指出：该违规不是孤立的，`router.py:32-52` 的
`_CODING_SCENE_MARKERS` 是同一种模式（**不在本轮范围**，见 §6）。

## 2. 挂载点与契约

**不新增节点、不改图拓扑**。保留 `infer_task_plan` 这个入口与它的返回结构
（`runner.py:3660-3708` 已在消费），只改它的**权威性**：

| 字段 | 现状 | 改后 | 契约 |
|---|---|---|---|
| `phases` | 正则决定工具与顺序 | LLM 分析给出的阶段顺序；正则命中仍记 `matched_rule` | **仅供 LLM 与日志参考，不参与授权** |
| `required_tools` | 正则决定，并入 LLM 选择集 | **不再并入**（`_required_names` 不再叠加） | Runtime 独占授权 |
| `match_kind` | 无 | `llm` / `rule_hint` / `none` | 决策来源可观测、可回归 |

**输入输出签名不变**：`infer_task_plan(query, available) -> dict`。
`plan_required_tools()` 保留（返回 `required_tools`），但调用方不再把它并入授权集。

## 3. 为什么不整体 LLM 化

整体改造 `task_plan`（连阶段顺序也交给 LLM）是 P3 设计议题。理由：本轮是
**红灯清理**，在验收基线不稳时引入 LLM 依赖会让"修好了"与"新引入的不确定性"
无法区分。这里取中间态 —— 规则**降级为提示**而非删除，收益（阶段顺序/提示）
保留，授权权收回 Runtime。

## 4. 回退策略

- 开关 `FORGE_TASK_PLAN_RULE_HINT`：**缺省 `on`**（保留现有行为，纯正则路径，
  便于对比）；设为 `off` 时 `phases` 为空、`required_tools` 为空
  （= 完全交给 LLM），`runner` 侧无需任何改动即可正常退化。
- `plan_required_tools()` 的返回值在任何开关下都仍然可解析 —— 不改结构，
  只改"是否被并入授权集"，故回退不涉及数据迁移。

## 5. 对 `runtime/runner.py:3670` 的影响面

唯一改动：`_required_names` 由「`plan_required_tools(...)` ∩ 可用集」
改为「**空列表**（规则只作提示）」。`_langgraph_selected_tool_names` 的并集、
`_valid_names` 校验、`_unknown_names` 登记、三层 fail-safe
（筛选异常→全量 / 空选→全量 / clone 异常→全量）**全部不动**。

**影响面评估**：`runner.py` 是 4819 行主干，但这一处改动的语义是
"**只收窄、不放宽**"——LLM 选了什么就用什么，不会因为少了规则的补入而
多暴露工具。工具**变少**的场景只发生在"LLM 漏选 + 正则刚好补上"，
此时由已有的 `empty_selection_full_tools_fallback` 兜底（全量暴露）。
即：**最坏情况退化为"暴露全部工具"，不会退化为"无可用工具"**。

## 6. 明确不做（防范围蔓延）

- ❌ 不动 `runtime/router.py:32-52` 的 `_CODING_SCENE_MARKERS`
  （同类违规，但属模型档位选择、不涉工具授权，另案处理）
- ❌ 不删 13 条规则（降级为 `matched_rule` 提示，保留可观测性与回退能力）
- ❌ 不改 `infer_task_plan` 的签名与返回结构（`runner` 消费面不动）
- ❌ 不做 `task_plan` 整体 LLM 化（P3）

## 7. 验收

新增 `tests/test_task_plan_no_regex_authority.py`：

1. **核心断言**：`required_tools` 不得由 `re.search` 决定 ——
   `plan_required_tools()` 的结果在 `FORGE_TASK_PLAN_RULE_HINT=off` 时必须为空，
   且 `runner._workflow_select_tools` 源码中不得再把 `plan_required_tools`
   并入授权集（AST 断言，不靠运行时）。
2. **反向用例**：`required_tools` 为空时，LLM 选中的工具仍**完整可见**
   （这是收窄授权后必须成立的性质，否则会误伤正常任务）。
3. 既有 `tests/test_task_plan.py` 保持绿（规则降级但未删除，提示能力仍在）。
