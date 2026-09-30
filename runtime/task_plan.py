"""Runtime-owned task plan inference for explicit multi-action requests.

This module defines ordered phases and required tool candidates. It does not
select the model's visible tool set and does not authorize tool execution;
Tool Router applies candidate and intent policy, while Runner/RunContext enforce
phase order and execution gates.
"""

from __future__ import annotations

import re
from typing import Any


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
    ), ("web_search",)),
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
    an installed tool. The returned tools are requirements for routing, not
    permissions. Runtime execution gates remain authoritative.
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
                "schema_version": 1,
                "required_tools": [],
                "required_capabilities": [],
                "phases": [],
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

    return {
        "schema_version": 1,
        "required_tools": required,
        "required_capabilities": list(dict.fromkeys(
            capability_of(name) for name in required
        )),
        "phases": phases,
    }


def plan_required_tools(plan: dict[str, Any] | None) -> list[str]:
    """Return a stable, unique required-tool list from a Runtime plan."""
    if not isinstance(plan, dict):
        return []
    tools = plan.get("required_tools")
    if not isinstance(tools, list):
        return []
    return list(dict.fromkeys(str(name) for name in tools if str(name)))
