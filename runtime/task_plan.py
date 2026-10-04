"""Runtime-owned task plan **hints** for explicit multi-action requests.

This module defines ordered phases and candidate tool names for the
LangGraph workflow. It does not authorize tool execution; Runner/RunContext
enforce phase order and all execution gates.

P1-7（2026-10-04）：本模块从"工具选择器"降级为"结构化提示"。

# 为什么要降级

`infer_task_plan` 原先是一个**用正则实现的工具选择器**：13 条中文关键词规则
（`天气|气温|温度` → `get_weather` 等）决定工具集，且其结果会被
`runner._workflow_select_tools` **并入** LLM 选择器已选中的名字。

两个问题：
1. **违反分工宪法**（`AGENTS.md`：面向业务语义的判断绝不能用关键字/正则）。
   "用户是否在说天气"是业务语义，正是宪法禁止用正则做的判断。
2. **LLM 的选择被规则悄悄否决**：LLM 选了什么不重要，正则命中就会把工具
   加回来 —— 这让"语义判断从规则迁到 LLM"在授权链最外层失效。

# 降级后的契约（权威性变了，结构没变）

| 字段         | 角色                                   | 是否参与授权 |
|--------------|----------------------------------------|--------------|
| `phases`     | 阶段顺序建议 + 命中规则标注（可观测）   | ❌ 不参与     |
| `required_tools` | **恒为空**（保留字段以兼容消费方）   | ❌ 不参与     |
| `match_kind` | 决策来源：llm / rule_hint / none       | 仅观测       |

Runtime 独占工具授权：LLM 选择器（`runtime/api_workflow.py`）出建议，
Runtime 做交集与兜底，`runner.py` 不再把本模块的输出并入授权集。
开关 `FORGE_TASK_PLAN_RULE_HINT=off` 可让 phases 也一并置空（完全交给 LLM），
实现见 `02-design/P1_7_TASK_PLAN_BOUNDARY_20261004.md`。
"""

from __future__ import annotations

import os
import re
from typing import Any

#: P1-7 回退开关。缺省 on = 保留规则作为**提示**（可观测、可对比）；
#: 设为 off 时 phases 也置空 = 完全交给 LLM 选择器。
RULE_HINT_ENV = "FORGE_TASK_PLAN_RULE_HINT"
_TRUTHY = ("on", "true", "1", "yes")


def rule_hint_enabled() -> bool:
    """规则提示是否启用（只影响 `phases`，永不影响授权）。"""
    return (os.getenv(RULE_HINT_ENV) or "on").strip().lower() in _TRUTHY


#: 动作 → 能力 的**关键词提示表**（P1-7 起仅用于生成 `phases` 提示，不参与授权）。
#:
#: 它是"违规被降级"而不是"违规被删除"：删掉会丢失阶段顺序提示与可观测性，
#: 保留它则让 LLM 选择器多一个参考维度。权威性已由 `required_tools` 恒空收回。
#: 设计说明见 02-design/P1_7_TASK_PLAN_BOUNDARY_20261004.md。
_ACTION_CAPABILITY_RULES: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("external_fact", re.compile(
        r"天气|气温|温度|天气预报|几点了|现在时间|当前时间|今天日期|当前日期", re.I,
    ), ("get_weather", "get_current_datetime")),
    ("local_search", re.compile(
        r"(?:项目|本地|工作区|指定)(?:文件夹|目录|资料|文档|文件).{0,16}(?:搜索|搜一下|查找|检索)|"
        r"(?:搜索|搜一下|查找|检索).{0,16}(?:项目|本地|工作区)(?:文件夹|目录|资料|文档|文件)", re.I,
    ), ("search_documents",)),
    ("external_research", re.compile(
        r"搜索|搜一下|联网查|检索最新|查找资料|查资料|最新(?:资料|信息|数据)|web.?search", re.I,
    ), ("anysearch_search", "anysearch_batch_search")),
    ("read_input", re.compile(
        r"读取|读入|打开|分析(?:这个|这份|一下)?(?:文件|表格|csv|excel)|"
        r"(?:csv|excel|表格).{0,12}(?:分析|读取|计算|求平均)|"
        r"从.{0,12}(?:文件|表格|csv).{0,8}(?:读取|读出|计算)", re.I,
    ), ("read_spreadsheet", "read_office_file", "read_workspace_file")),
    ("compute", re.compile(
        r"计算|算(?:一下|平均|总和|合计|最大值|最小值)|求(?:平均|总和|合计|最大值|最小值)|"
        r"平均值|加总|统计(?:一下|结果)?|温差|差值|相差", re.I,
    ), ("calculate",)),
    ("write_output", re.compile(
        r"写入(?:到|进)?(?:文件|项目|工作区)|保存(?:结果|文件|到文件)|导出|写到文件|生成文件", re.I,
    ), ("write_project_file",)),
    ("save_note", re.compile(
        r"(?:结果.{0,4})?保存(?:结果)?(?:为|成|到)?(?:备忘录|笔记)|存成(?:备忘录|笔记)|记入备忘", re.I,
    ), ("save_note",)),
    ("remember", re.compile(
        r"记住|记一下|以后(?:都|要|请)用|保存到记忆|长期记住", re.I,
    ), ("remember", "save_note")),
    ("recall", re.compile(
        r"回忆|回想|查(?:一下)?(?:我的)?记忆|查看(?:我的)?记忆|记得我(?:平时|喜欢|偏好)?", re.I,
    ), ("recall_memory", "read_note")),
    ("schedule", re.compile(
        r"提醒我|设置提醒|加个提醒|明天.{0,8}提醒|每天.{0,8}提醒|每周.{0,8}提醒|定时提醒", re.I,
    ), ("schedule_add",)),
)


def infer_task_plan(query: str, available: list[str] | None = None) -> dict[str, Any]:
    """Build a deterministic ordered plan for explicit actions in the request.

    ``available`` is the Runtime registry snapshot used to bind each phase to
    an installed tool. The returned tools are **hints for the LLM selector and
    for observability, not permissions** — Runtime execution gates remain
    authoritative (P1-7).

    P1-7：`required_tools` 恒为空列表（保留字段仅为兼容既有消费方），
    规则命中只体现在 `phases[].tools` 与 `phases[].matched_rule` 上。
    """
    text = query or ""
    # Broad questions about whether the Agent can access local/workspace files
    # are capability inquiries, not requests to read a particular file. Keep
    # the planner's keyword rules from turning those questions into a required
    # read_input phase.
    try:
        from runtime.capability_introspection import looks_like_capability_query
        if looks_like_capability_query(text):
            return {
                "schema_version": 2,
                "required_tools": [],
                "required_capabilities": [],
                "phases": [],
                "match_kind": "none",
            }
    except Exception:
        # Preserve ordinary planning if capability-introspection is unavailable.
        pass

    candidates = set(available) if available is not None else None
    phases: list[dict[str, Any]] = []
    required: list[str] = []
    try:
        from runtime.spec import capability_of
    except Exception:
        capability_of = lambda name: "OTHER"

    matches: list[tuple[int, int, str, tuple[str, ...]]] = []
    local_search = any(
        phase == "local_search"
        for phase, pattern, _names in _ACTION_CAPABILITY_RULES
        if pattern.search(text)
    )
    explicit_online_search = bool(re.search(
        r"联网|网上|网络|网页|在线|web\s*search|internet|online", text, re.I,
    ))
    for order, (phase, pattern, names) in enumerate(_ACTION_CAPABILITY_RULES):
        match = pattern.search(text)
        if match:
            # A project/workspace directory makes an otherwise generic "搜索"
            # request local. Only add web search when the user explicitly asks
            # for an online source as well.
            if phase == "external_research" and local_search and not explicit_online_search:
                continue
            matches.append((match.start(), order, phase, names))

    # Preserve the user's action order; stable rule order resolves ties.
    for _position, _order, phase, names in sorted(matches):
        if phase == "read_input":
            preferred = (
                ("read_spreadsheet", "read_office_file", "read_workspace_file")
                if re.search(r"csv|xlsx|excel|表格|工作表|sheet", text, re.I)
                else ("read_office_file", "read_workspace_file", "read_spreadsheet")
            )
            selected = [name for name in preferred
                        if candidates is None or name in candidates][:1]
        elif phase == "external_fact":
            preferred = ("get_weather",) if re.search(
                r"天气|气温|温度|天气预报", text, re.I
            ) else ("get_current_datetime",)
            selected = [name for name in preferred
                        if candidates is None or name in candidates]
        elif phase == "external_research" and candidates is not None:
            # AnySearch MCP is the only exposed provider for ordinary web search.
            if re.search(r"anysearch_batch_search", text, re.I):
                preferred = ("anysearch_batch_search", "anysearch_search")
            elif re.search(r"anysearch_search", text, re.I):
                preferred = ("anysearch_search", "anysearch_batch_search")
            else:
                preferred = ("anysearch_search", "anysearch_batch_search")
            selected = [name for name in preferred if name in candidates][:1]
        else:
            selected = [name for name in names
                        if candidates is None or name in candidates]

        if not selected:
            continue
        required.extend(name for name in selected if name not in required)
        phases.append({
            "phase": phase,
            "tools": selected,
            "capabilities": list(dict.fromkeys(capability_of(name) for name in selected)),
            "depends_on": [phases[-1]["phase"]] if phases else [],
        })

    # Current-information lists often need page content after search snippets.
    # Keep extraction as a later phase so it cannot run before discovery.
    has_research = any(phase.get("phase") == "external_research" for phase in phases)
    wants_current_listing = bool(re.search(
        r"今天|今日|目前|现在|最新|清单|榜单|热门(?:项目|仓库)?|排名|star|stars|趋势", text, re.I,
    ))
    if has_research and wants_current_listing and candidates is not None:
        extract_tools = [name for name in ("anysearch_extract", "fetch_fetch")
                         if name in candidates]
        if extract_tools:
            phases.append({
                "phase": "source_extract",
                "tools": extract_tools[:1],
                "capabilities": list(dict.fromkeys(
                    capability_of(name) for name in extract_tools[:1]
                )),
                "depends_on": [phases[-1]["phase"]],
            })
            required.extend(name for name in extract_tools[:1] if name not in required)

    # P1-7：规则命中**不再**产出授权级工具集。
    # - `required` 在整个函数里照常累积，仅用于生成 phases 的工具与
    #   required_capabilities（可观测 / 给 LLM 参考），到此处**刻意丢弃**；
    # - `required_tools` 恒为空：调用方（runner._workflow_select_tools）即使
    #   仍调用 plan_required_tools()，拿到的也是空列表 —— 用"数据为空"而不是
    #   "改调用点"来收回授权，能同时防住其它潜在消费方。
    # - `match_kind` 让决策来源可观测、可回归。
    if not rule_hint_enabled():
        # 回退档：连提示也不给，phases 置空 = 完全交给 LLM 选择器。
        phases = []

    return {
        "schema_version": 2,
        # P1-7：恒空。保留字段仅为兼容既有消费方（runner 仍在读它）。
        "required_tools": [],
        "required_capabilities": list(dict.fromkeys(
            capability_of(name) for phase in phases for name in phase.get("tools", [])
        )),
        "phases": phases,
        "match_kind": "rule_hint" if phases else "none",
    }


def plan_required_tools(plan: dict[str, Any] | None) -> list[str]:
    """Return a stable, unique required-tool list from a Runtime plan.

    P1-7：**本函数不再是授权来源。** 自 P1-7 起 `infer_task_plan` 产出的
    `required_tools` 恒为空（规则降级为 `phases` 提示），因此本函数对
    `infer_task_plan` 的结果恒返回空列表。

    保留本函数是为了**不破坏调用方契约**（runner 仍会调用它），
    并让"规则不得回流成授权"这条性质由**数据**保证而非仅靠约定。
    工具授权的唯一来源是 LLM 选择器 + Runtime 交集/兜底。
    """
    if not isinstance(plan, dict):
        return []
    tools = plan.get("required_tools")
    if not isinstance(tools, list):
        return []
    return list(dict.fromkeys(str(name) for name in tools if str(name)))
