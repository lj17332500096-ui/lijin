"""Readiness → Tool Capability Gate（最小收口，2026-09-07-v2）。

本轮不重新设计 Task Readiness，只把真实压测确认的 5 个缺口做确定性收口：

1. Readiness 状态必须真正约束 Tool Timing：
   READY        → 允许正常执行工具（走既有 Approval/FileScope 门）；
   DISCOVERABLE → 只允许登记为 DISCOVERY_SAFE 的只读探索工具；
   NEEDS_USER   → 禁止任何实质性执行工具（只读探索可用于消除歧义，有界）。
2. DISCOVERABLE/未知状态下的只读探索必须有界（同意图 + 同参数 + 连续无新信息）。

分类依据只来自 Registry metadata / MCP 策略 metadata / 显式配置，禁止按工具名字
字符串匹配（if "read" in name）判断。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 固定语义
# ---------------------------------------------------------------------------

STATUS_READY = "READY"
STATUS_DISCOVERABLE = "DISCOVERABLE"
STATUS_NEEDS_USER = "NEEDS_USER"
STATUS_UNKNOWN = "UNKNOWN"

VALID_STATUSES = {STATUS_READY, STATUS_DISCOVERABLE, STATUS_NEEDS_USER, STATUS_UNKNOWN}

DISCOVERY_SAFE = "DISCOVERY_SAFE"
SIDE_EFFECTING = "SIDE_EFFECTING"

DISCOVERY_EXHAUSTED = "DISCOVERY_EXHAUSTED"

# ---------------------------------------------------------------------------
# Convergence Levels（Phase 4）：NORMAL → CAUTION → CONVERGENCE → TERMINALIZE
# 不是“限制次数”，而是“何时继续 / 何时收口 / 何时终态”的语义分级。
# 绝对次数（budget）只是最后防线；主要依据 meaningful progress。
# ---------------------------------------------------------------------------

CONVERGENCE_NORMAL = "NORMAL"
CONVERGENCE_CAUTION = "CAUTION"
CONVERGENCE_CONVERGENCE = "CONVERGENCE"
CONVERGENCE_TERMINALIZE = "TERMINALIZE"

CONVERGENCE_ORDER = {
    CONVERGENCE_NORMAL: 0,
    CONVERGENCE_CAUTION: 1,
    CONVERGENCE_CONVERGENCE: 2,
    CONVERGENCE_TERMINALIZE: 3,
}

#: CAUTION 下的确定性提示（要求模型改变策略，而不是继续重复）。
CONVERGENCE_CAUTION_TEXT = (
    "【CONVERGENCE·CAUTION】检测到你正在重复没有带来新信息的动作。"
    "请换一个真正不同的做法，或直接基于已有信息收口；不要再用相同参数重复调用同一工具。"
)

#: CONVERGENCE 下的确定性提示（禁止继续 discovery 扩散，只允许验证/收口）。
CONVERGENCE_REACHED_TEXT = (
    "【CONVERGENCE_REACHED】连续多次没有产生新的有效信息，Runtime 已停止继续探索。"
    "现在只允许：完成必要验证、读取已有 evidence、给出最终回答。"
    "请不要再发起新的搜索/列目录/读文件，直接基于已掌握的事实作答；"
    "无法确认的部分如实说明，或用 questions 一次问清缺失项。"
)

#: TERMINALIZE：不再回到 Agent tool loop，强制进入终态决策。
CONVERGENCE_TERMINALIZE_TEXT = (
    "【TERMINALIZE】本轮探索已无进展，Runtime 不再执行任何工具。"
    "请立刻用一句话给出最终回答：已完成的部分如实说明，未完成/无法确认的部分明确说明。"
)

#: 被视为“discovery 扩散”的工具（收敛时禁止继续发起）。
_DISCOVERY_CLASS_TOOLS = frozenset({
    "web_search", "search_documents", "search_sources", "deep_research",
    "web_search_v2", "read_workspace_file", "list_workspace_files",
    "read_code_file", "list_code_files", "read_note", "list_notes",
    "index_workspace", "read_office_file", "read_spreadsheet",
    "list_sandbox_snapshots", "recall_memory", "search_sources",
})


#: Phase 9：coding 阶段分类（与 benchmark/evidence_analysis.py 对齐）
_P9_MUTATION_TOOLS = frozenset({
    "write_project_file", "edit_project_file", "write_code_file",
    "save_note", "save_word_doc", "save_excel_workbook", "save_ppt_deck",
})
_P9_VERIFICATION_TOOLS = frozenset({"run_tests", "run_python", "code_loop", "code_loop_tool"})
_P9_READ_TOOLS = frozenset({
    "read_workspace_file", "read_code_file", "read_note", "recall_memory",
    "read_office_file", "read_spreadsheet",
})
_P9_DISCOVERY_TOOLS = frozenset({
    "list_workspace_files", "list_code_files", "index_workspace",
    "search_documents", "search_sources", "web_search", "list_notes",
    "list_sandbox_snapshots",
})


def canonical_target_of(name: str, arguments: dict[str, Any] | None) -> str | None:
    """规范化目标身份；无法解析时返回 None（禁止静默空字符串）。"""
    args = dict(arguments or {})
    for k in ("path", "file", "filename", "directory", "project"):
        v = args.get(k)
        if v:
            return str(v).replace("\\", "/").strip().lower() or None
    for k in ("query", "keyword", "q"):
        v = args.get(k)
        if v:
            return ("query:" + str(v).strip().lower()) or None
    return None


def is_discovery_class_tool(name: str) -> bool:
    """该工具是否属于“探索/检索”类（收敛时优先禁止继续扩散）。"""
    return name in _DISCOVERY_CLASS_TOOLS


def _text_fingerprint(text: object) -> str:
    """对任意工具结果做确定性指纹（规范化空白后 sha1 前 16 位）。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass(slots=True)
class ProgressSignal:
    """一次动作的进展判定结果。"""

    novel: bool
    reason: str
    signature: str = ""
    fingerprint: str = ""
    error_class: str | None = None

#: 探索耗尽后返回给模型的确定性引导（不是“再试一次”，而是要求结束本轮）。
DISCOVERY_EXHAUSTED_TEXT = (
    "【DISCOVERY_EXHAUSTED · 运行时提示】你在同一意图下连续多次执行相同只读探索"
    "（列目录/读文件/检索），且没有获得新信息。Runtime 已阻止继续空转。"
    "请立即结束本轮：如果缺的信息必须由你确认，用 questions 一次问清并点名缺失项；"
    "如果当前环境/项目里没有可用内容，直接如实说明没找到；不要再重复调用本工具或换参数继续探索。"
)

_BLOCK_DISCOVERABLE = (
    "【Readiness 门】当前任务处于 DISCOVERABLE：只允许只读探索工具（列目录/读文件/检索/查时间等），"
    "不允许执行写入/修改/删除/发送/外部副作用类工具。请先基于只读结果回答或澄清；"
    "若确需执行修改，请先通过探索定位目标并向用户说明，在用户确认执行条件后重试。"
)

_BLOCK_NEEDS_USER = (
    "【Readiness 门】当前任务处于 NEEDS_USER：缺少必须由用户提供/确认的关键信息，"
    "禁止执行写入/修改/删除/发送/运行等实质性工具。请停止执行，用 questions 一次问清缺失项"
    "（必须点名具体字段，例如“从哪里出发？”，禁止只写“请补充更多信息”）。"
)

_TOOL_INTENT_BLOCK = (
    "【工具意图门】用户本轮没有要求执行这个写入操作，Runtime 已阻止调用。"
    "请直接回答当前问题；只有用户明确要求保存内容或记住信息时，才能调用对应工具。"
)

_SAVE_NOTE_INTENT = re.compile(
    r"保存|存成|存为|记录(?:下来|到)|写(?:一份|个|篇|到)|生成|整理(?:一份|成)|"
    r"总结|周报|报告|方案|清单|文章|邮件|文档|笔记|备忘|大纲|ppt",
    re.IGNORECASE,
)
_REMEMBER_INTENT = re.compile(
    r"记住|记一下|以后(?:都|要|请)|长期记忆|别忘|偏好|习惯|常驻|默认(?:用|为)",
    re.IGNORECASE,
)


def check_user_tool_intent(name: str, request_text: str | None) -> tuple[bool, str | None]:
    """限制模型主动发起、但用户没有要求的持久写入。

    这里只覆盖真实压测确认的两个高频越界面；其他副作用仍由 Readiness、Approval
    与 FileScope 处理。判断依据是 Runtime 保存的原始用户请求，不信任工具参数。
    """
    text = str(request_text or "").strip()
    if name == "save_note" and not _SAVE_NOTE_INTENT.search(text):
        return False, _TOOL_INTENT_BLOCK
    if name == "remember" and not _REMEMBER_INTENT.search(text):
        return False, _TOOL_INTENT_BLOCK
    return True, None


# ---------------------------------------------------------------------------
# Missing Required Information（轻量，覆盖 Benchmark 暴露的“猜关键参数”场景）
# 判断依据是 Runtime 原始请求 + 即将执行的工具，缺“执行必须且无法从上下文确定”
# 的字段时，在真正执行前拦截并引导询问。
# ---------------------------------------------------------------------------

_FROM_HINT = re.compile(r"(?:从|自)\s*([\u4e00-\u9fa5A-Za-z]{1,12})\s*(?:出发|起飞|走)")

#: 航班/行程：必须含“出发地 + 目的地 + 日期/时间”；只有是明确“某城市出发→上海”才充足
_FLIGHT_REQUIRED = ("出发", "从", "起飞", "首都机场", "机场", "航班")
_DEST_REQ = ("到", "去", "飞", "航班", "高铁", "车次")


def missing_required_fields(message: str | None, tool_name: str,
                            arguments: dict[str, Any] | None) -> tuple[bool, str | None]:
    """返回 (missing, 引导文本) —— 缺执行必需字段时拦截，防止模型猜参数空转。

    只针对 Benchmark 暴露的 4 类“必需信息”场景，普通探索（读文件/搜代码/修复）不拦。
    """
    text = str(message or "").strip()
    if not text:
        return False, None
    # 1) 航班/行程：缺出发地
    if re.search(r"航班|机票|飞往|去.+的机票|查.*航班", text):
        if not any(t in text for t in ("从", "自", "出发", "起飞")):
            return True, (
                "【缺少必要信息】查询航班的出发地未知。请先向用户确认从哪里出发（出发地），"
                "得到答复后再查。不要自己假设出发地或直接搜索。"
            )
    # 2) 提醒/定时：缺时间
    if re.search(r"提醒我|提醒一下|叫我|设定.*提醒", text):
        if not any(t in text for t in ("早上", "下午", "晚上", "点", "明天", "今天", "周", "分钟后",
                                       "上午", "下午", "整点", "日期", "上午")):
            return True, (
                "【缺少必要信息】提醒缺少“什么时候”提醒。请先向用户确认提醒时间，"
                "不要自己假设一个时间或直接创建定时任务。"
            )
    # 3) 删除“没用/没用的文件”：缺标准
    if re.search(r"没用的?文件|不用的?文件|清理.*文件|删掉.*文件", text):
        if re.search(r"删除|删掉|清理", text):
            return True, (
                "【需要用户确认】“没用”需要明确标准。请先读出候选文件清单让用户确认要删哪些，"
                "或先询问删除标准，不要自行定义“没用”并直接删除。"
            )
    # 4) 部署/上线：缺目标环境（Phase 7）
    if re.search(r"部署|上线|发布到|发布一下", text):
        if not any(t in text for t in (
                "本地", "服务器", "云端", "云服务", "docker", "vercel", "railway",
                "测试环境", "生产环境", "staging", "production", "k8s", "kubernetes",
                "nginx", "pm2", "宝塔", "阿里云", "腾讯云", "aws", "gcp")):
            return True, (
                "【缺少必要信息】部署缺少目标环境。请先向用户确认部署到哪里"
                "（本地 / 服务器 / Docker / 云平台），不要自行假设环境并执行。"
            )
    # 5) 破坏性批量删除：缺明确清单/确认（Phase 7）
    if re.search(r"删除|删掉|移除|清理", text) and re.search(r"文件|目录|迁移|migration", text):
        if re.search(r"所有|全部|批量|整个|项目里|目录下", text):
            return True, (
                "【需要用户确认】这是破坏性批量删除。请先列出候选清单并请用户确认要删哪些，"
                "在得到确认前不要执行删除。"
            )
    # 6) 发送/转发：缺收件人与内容确认（Phase 7）
    if re.search(r"发给|发送给|转发给|寄给", text):
        return True, (
            "【需要用户确认】发送/转发前必须确认收件人与要发送的具体内容。"
            "请先向用户确认，不要自行推断并发送。"
        )
    return False, None


def required_questions(message: str | None) -> list[str]:
    """返回本轮执行所必需、但缺失的关键信息对应的具体问题（用于确定性 needs_user_input 收尾）。
    与 missing_required_fields 同一套判定，只是输出面向用户的问句。"""
    text = str(message or "").strip()
    if not text:
        return []
    qs: list[str] = []
    if re.search(r"航班|机票|飞往|去.+的机票|查.*航班", text):
        if not any(t in text for t in ("从", "自", "出发", "起飞")):
            qs.append("请告诉我从哪里出发？")
    if re.search(r"提醒我|提醒一下|叫我|设定.*提醒", text):
        if not any(t in text for t in ("早上", "下午", "晚上", "点", "明天", "今天", "周", "分钟后",
                                       "上午", "整点", "日期")):
            qs.append("你希望什么时候提醒你？")
    if re.search(r"没用的?文件|不用的?文件|清理.*文件|删掉.*文件", text):
        if re.search(r"删除|删掉|清理", text):
            qs.append("哪些文件算“没用”的？请给出判断标准，或让我先列出候选清单由你确认。")
    if re.search(r"部署|上线|发布到|发布一下", text):
        if not any(t in text for t in (
                "本地", "服务器", "云端", "云服务", "docker", "vercel", "railway",
                "测试环境", "生产环境", "staging", "production", "k8s", "kubernetes",
                "nginx", "pm2", "宝塔", "阿里云", "腾讯云", "aws", "gcp")):
            qs.append("你希望部署到哪里（本地 / 服务器 / Docker / 云平台）？")
    if re.search(r"删除|删掉|移除|清理", text) and re.search(r"文件|目录|迁移|migration", text):
        if re.search(r"所有|全部|批量|整个|项目里|目录下", text):
            qs.append("请确认要删除的具体文件/目录清单（这是破坏性操作）。")
    if re.search(r"发给|发送给|转发给|寄给", text):
        qs.append("请确认收件人，以及要发送的具体内容。")
    return qs

#: 显式配置补充的只读探索工具（逗号分隔；仅当 metadata 无法表达“只读”时用于登记）。
def _configured_safe_tools() -> set[str]:
    raw = os.getenv("READINESS_DISCOVERY_SAFE_TOOLS", "").strip()
    return {n.strip() for n in raw.split(",") if n.strip()} if raw else set()


def classify_tool(
    name: str,
    spec: Any | None = None,
    mcp_policy: str | None = None,
) -> str:
    """返回 DISCOVERY_SAFE 或 SIDE_EFFECTING。

    优先级（全部来自元数据/显式配置，不依赖工具名猜测）：
    1. MCP 工具策略 metadata（allow=只读探索，approval=会改变外部状态）；
    2. 显式配置 READINESS_DISCOVERY_SAFE_TOOLS；
    3. ToolSpec.side_effect=False（Registry 目录）；
    4. 其余一律保守判为 SIDE_EFFECTING（fail closed）。
    """
    if mcp_policy == "allow":
        return DISCOVERY_SAFE
    if mcp_policy == "approval":
        return SIDE_EFFECTING
    if name in _configured_safe_tools():
        return DISCOVERY_SAFE
    if spec is not None and getattr(spec, "side_effect", True) is False:
        return DISCOVERY_SAFE
    return SIDE_EFFECTING


def check_status_tool(status: str | None, effect: str) -> tuple[bool, str | None]:
    """根据当前 readiness.status 决定某类工具是否放行。返回 (allowed, reason)。"""
    if status in (None, "", STATUS_UNKNOWN, STATUS_READY):
        return True, None
    if effect == DISCOVERY_SAFE:
        return True, None
    if status == STATUS_DISCOVERABLE:
        return False, _BLOCK_DISCOVERABLE
    if status == STATUS_NEEDS_USER:
        return False, _BLOCK_NEEDS_USER
    return True, None


# ---------------------------------------------------------------------------
# Bounded Discovery：同意图 + 同参数 + 连续无新信息 → DISCOVERY_EXHAUSTED
# ---------------------------------------------------------------------------

#: 输出容量类参数不计入“意图签名”（同一次探索加容量不算新意图）
_VOLATILE_ARGS = {
    "max_chars", "max_entries", "max_results", "limit", "timeout",
    "max_tokens", "top_k", "max_length",
}


def _normalize_value(value: Any) -> str:
    text = str(value).strip()
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    return text.lower()


def discovery_signature(name: str, arguments: dict[str, Any] | None) -> str:
    """同一发现意图的稳定签名：工具名 + 规范化关键参数（去掉输出容量类字段）。"""
    args = dict(arguments or {})
    for key in list(args):
        if key in _VOLATILE_ARGS:
            args.pop(key, None)
    normalized: dict[str, Any] = {}
    for key in sorted(args):
        value = args[key]
        if isinstance(value, (list, tuple)):
            normalized[key] = [_normalize_value(v) for v in value]
        elif isinstance(value, dict):
            normalized[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            normalized[key] = _normalize_value(value)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{name}|{payload}"


# ---------------------------------------------------------------------------
# Semantic search intent（轻量、确定性、无 embedding）——
# 解决“北京天气9月9日 / 北京今日天气 / Beijing weather today”换词绕过精确签名。
# 只对查询/搜索类工具做模糊规约；读文件/列目录仍用精确签名。
# ---------------------------------------------------------------------------

#: 会污染语义的关键词：今天/现在/实时/当前/最新/温度/天气/度/x月x日/周x…
# （完全不用反斜杠转义——字符类用字面字符，空格用 [ ]，避免写盘时二次转义破坏。）
_SEARCH_NOISE = re.compile(
    r"(今天|明天|昨天|现在|当前|实时|最新|今日|每日|前一天|后天|"
    r"天气|气温|温度|多少度|最高|最低|最[高低]温|晴|雨|阴|多云|雪|"
    r"预报|查询|搜索|查找|请问|帮我说|帮我|告诉我|关于|介绍|推荐|"
    r"[0-9]{1,4}[年/. -][0-9]{1,2}[月/. -]?[0-9]{0,2}日?|[0-9]{1,2}日|[0-9]+|"
    r"号|点|度|星期[一二三四五六日天]|周[一二三四五六日天]|"
    r"please|search|query|find|tell[ ]*me|about|weather|today|now|"
    r"current|temperature|degree|for|the|is|what|how|forecast)",
    re.IGNORECASE,
)

#: 中英城市/主题互译（只覆盖高频地点，用于跨语言归并；非 exhaustive）。
_CITY_MAP = {
    "北京": "beijing", "上海": "shanghai", "广州": "guangzhou", "深圳": "shenzhen",
    "成都": "chengdu", "杭州": "hangzhou", "武汉": "wuhan", "西安": "xian",
    "南京": "nanjing", "重庆": "chongqing", "苏州": "suzhou", "天津": "tianjin",
    "长沙": "changsha", "郑州": "zhengzhou", "青岛": "qingdao", "厦门": "xiamen",
    "香港": "hongkong", "台北": "taipei", "纽约": "newyork", "伦敦": "london",
    "东京": "tokyo", "巴黎": "paris", "新加坡": "singapore", "北京天气": "beijing",
}


#: 语义意图收敛只对搜索类工具生效（换措辞归并）；列目录/读文件仍走精确签名。
_SEARCH_TOOLS = {"web_search", "search_documents", "deep_research", "search_sources", "web_search_v2"}

#: 时间范围（轻量 canonical time scope；不建复杂时间 NLP）
_TIME_SCOPE_TOKENS = (
    ("tomorrow", ("明天", "明日", "后天")),
    ("yesterday", ("昨天", "昨日")),
    ("this_week", ("这周", "本周", "这个星期", "这礼拜", "近一周")),
    ("next_week", ("下周", "下星期")),
    ("today", ("今天", "今日", "现在", "当前", "实时", "today", "now", "current")),
)


def _time_scope(raw: str) -> str:
    lw = raw.lower()
    for scope, toks in _TIME_SCOPE_TOKENS:
        for tok in toks:
            if tok in lw:
                return scope
    return "unspecified"


#: 天气 / 事实类可请求字段（用于 requested_fields，避免把“温度”和“降水”过度归并）
_FIELD_TERMS = (
    ("temperature", ("温度", "气温", "多少度", "几度", "temperature", "temp", "degree", "预报温度")),
    ("precipitation", ("降水", "雨", "降雨", "雪", "precip", "rain", "snow")),
    ("air_quality", ("空气质量", "pm2.5", "aqi", "污染", "air quality")),
    ("wind", ("风力", "风速", "风", "wind")),
    ("conditions", ("天气状况", "天气情况", "天气如何", "晴", "阴", "多云", "小雨", "大雨", "状况", "conditions")),
)


def _requested_fields(raw: str) -> tuple[str, ...]:
    lw = raw.lower()
    fields: list[str] = []
    for field, toks in _FIELD_TERMS:
        if any(t in lw for t in toks):
            fields.append(field)
    return tuple(fields) or ("general",)


def semantic_search_intent(name: str, arguments: dict[str, Any] | None) -> str:
    """把搜索/查询归约为语义意图（保留实体 + 时间范围 + 请求字段）。

    只对搜索类工具生效。输出：tool|intent|entity|time_scope|fields。
    同主题不同措辞归并；但不同时间范围 / 不同请求字段不再过度归并。
    无法提取实体时回退到精确签名，避免不同工具被错误归并。
    """
    args = dict(arguments or {})
    if name not in _SEARCH_TOOLS:
        return discovery_signature(name, arguments)
    raw = str(args.get("query") or args.get("keyword") or args.get("q") or "").strip()
    if not raw:
        return discovery_signature(name, arguments)
    lowered = raw.lower()
    intent = "search"
    if any(w in lowered for w in ("天气", "气温", "温度", "多少度", "几度", "weather", "temp", "climate", "摄氏度")):
        intent = "weather"
    elif any(w in lowered for w in ("新闻", "news", "股价", "股票", "价格", "航班", "比赛", "汇率")):
        intent = "news_fact"
    entity = ""
    for city_cn, city_en in _CITY_MAP.items():
        if city_cn in lowered or city_en in lowered:
            entity = city_en
            break
    if not entity:
        cleaned = _SEARCH_NOISE.sub(" ", lowered)
        cleaned = re.sub(r"[，。、,.!?；;：:\"']", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        entity = cleaned[:24] if cleaned else "general"
    scope = _time_scope(raw)
    fields = ",".join(_requested_fields(raw))
    return f"{name}|{intent}|{entity}|{scope}|{fields}"


#: 从 web_search 格式化文本提取 {url,title} 指纹（保守、失败返回 None）。
def result_fingerprint(text: object) -> set[str] | None:
    """从搜索结果字符串提取 URL/域名集合；解析失败返回 None（调用方回退到 attempt 计数）。"""
    s = str(text or "")
    if not s or s.startswith("三个搜索源都失败了"):
        return None
    urls: set[str] = set()
    for m in re.finditer(r"链接:\s*(\S+)", s):
        url = m.group(1).strip()
        if not url or url in ("", "无标题"):
            continue
        urls.add(url.rstrip("，。,. "))
    if not urls:
        return None
    return urls


def result_overlap(prev: set[str] | None, new: set[str] | None) -> float | None:
    """结果重叠度 0..1；任一为 None（解析失败）返回 None（调用方回退 attempt 计数）。"""
    if not prev or not new:
        return None
    inter = len(prev & new)
    return inter / max(1, len(new))


@dataclass(slots=True)
class DiscoveryTracker:
    """单 Run 的只读探索计数（run-scoped，随 RunContext 隔离）。

    重复判定分两层，**语义搜索意图** 用「结果新颖度」判断而非简单计数：
    - exact：精确签名（同参数同工具）连续 3 次 → 耗尽；
    - semantic：同一语义意图下，若连续尝试的结果**重叠度过高（低新颖）**累计 2 次
      则收敛（blocked）；若某次结果有明显新增信息（低重叠）则视为有进展、允许继续。
      绝对语义次数 semantic_limit 只是 hard cap，不作为新颖度本身。
      语义层只对搜索类工具生效，绝不误伤“列目录 A / 列目录 B”这类不同主题探索。
    """

    semantic_limit: int = 3              # 绝对语义尝试 hard cap（兜底，非 novelty）
    low_novelty_required: int = 2        # 连续低新颖度达到此值 → 收敛
    counts: dict[str, int] = field(default_factory=dict)
    semantic_counts: dict[str, int] = field(default_factory=dict)
    _last_results: dict[str, set[str]] = field(default_factory=dict)   # sem -> result fingerprint
    _low_novelty: dict[str, int] = field(default_factory=dict)         # sem -> 连续低新颖次数
    blocked_intents: set[str] = field(default_factory=set)
    exhausted_sig: str | None = None
    strikes: int = 0
    force_stop: bool = False
    # ---- Run 级 Progress State（Phase 4 convergence semantics）----
    meaningful_progress_count: int = 0
    consecutive_no_progress: int = 0
    duplicate_action_count: int = 0
    repeated_error_count: int = 0
    blocked_repeat_count: int = 0
    last_progress_turn: int = 0
    turn: int = 0
    #: 语义动作签名 -> 最近一次结果指纹（用于“同动作同结果”判定）
    action_fingerprints: dict[str, str] = field(default_factory=dict)
    #: 语义动作签名 -> 连续同结果次数
    _same_result_streak: dict[str, int] = field(default_factory=dict)
    #: 错误类别 -> 次数（同类错误重复且无新上下文）
    error_classes: dict[str, int] = field(default_factory=dict)
    #: 语义动作签名 -> 是否已记录（首次出现 = 新动作 = progress）
    _seen_actions: set[str] = field(default_factory=set)
    #: 收敛阈值（可调；默认保守，避免误伤正常多步任务）
    caution_after: int = 1
    convergence_after: int = 2
    terminalize_after: int = 3
    # ---- Phase 9：Evidence / Saturation（复用本 tracker，不新增第二套）----
    unique_files_read: set = field(default_factory=set)
    unique_queries: set = field(default_factory=set)
    mutation_seen: bool = False
    verification_seen: bool = False
    verification_passed: bool = False
    low_novelty_streak: int = 0
    post_mutation_discovery: int = 0
    post_verification_discovery: int = 0
    # ---- Phase 10：Evidence Epoch + Exact Execution Identity（用于 redundant guard）----
    evidence_epoch: int = 0
    verified_revision: int | None = None
    verification_coverage: list = field(default_factory=list)
    _exact_seen: dict = field(default_factory=dict)
    redundant_guard_hits: int = 0
    suppressed_read: int = 0
    suppressed_search: int = 0
    suppressed_verification: int = 0
    completion_ready_announced: bool = False
    #: 一次性放行：completion_ready 后允许最后一次读操作（供模型撰写最终回复）
    completion_ready_one_shot_read: bool = False
    # ---- Phase 11：Per-Target 重复调用护栏（near-identical loop guard）----
    # 现有 exact/semantic 护栏按「冻结参数 + 结果新颖度 + evidence epoch」判定，
    # 因此模型把同一路径/目录/查询在参数微调下反复发起（list 同一目录 3 次、
    # 同一 query 改词再搜、list↔search 来回）时仍会通过。这里按「逻辑目标」计数，
    # 不受 bump_epoch 重置（目标重复是行为信号，不随 mutation 失效），达到 cap
    # 即强制重定向。刻意独立于 _exact_seen。
    repeat_targets: dict = field(default_factory=dict)  # target_key -> calls
    repeat_target_hits: int = 0
    repeat_target_cap: int = 3   # 第 3 次同目标调用时触发重定向

    def repeat_target_key(self, name: str, arguments: dict[str, Any] | None) -> str | None:
        """逻辑目标键（比 exact_identity 更宽松：不含 evidence epoch，容忍参数微调）。
        无目标可解析时返回 None（不拦截，避免误杀）。"""
        args = dict(arguments or {})
        if name in _P9_READ_TOOLS:
            tgt = str(args.get("path") or args.get("file") or args.get("filename")
                      or args.get("directory") or args.get("query") or "").strip().lower()
            return f"read|{tgt}" if tgt else None
        if name in _P9_DISCOVERY_TOOLS:
            q = str(args.get("query") or args.get("keyword") or args.get("q")
                    or args.get("directory") or "").strip().lower()
            return f"search|{q}" if q else None
        if name in _P9_VERIFICATION_TOOLS:
            cmd = str(args.get("filename") or args.get("file") or args.get("code")
                      or "").strip().lower()
            return f"verify|{cmd[:160]}" if cmd else None
        return None

    def note_repeat_target(self, name: str, arguments: dict[str, Any] | None) -> int:
        """执行前计数一次目标调用；返回该目标的累计次数（首调 = 1）。"""
        key = self.repeat_target_key(name, arguments)
        if key is None:
            return 0
        n = self.repeat_targets.get(key, 0) + 1
        self.repeat_targets[key] = n
        return n

    def target_saturated(self, name: str, arguments: dict[str, Any] | None) -> tuple[bool, int]:
        """该逻辑目标是否已达 cap（达到 = 应拦截重定向）。不计入。"""
        key = self.repeat_target_key(name, arguments)
        if key is None:
            return False, 0
        count = self.repeat_targets.get(key, 0)
        return count >= self.repeat_target_cap, count

    def mark_repeat_target_hit(self) -> None:
        self.repeat_target_hits += 1

    def bump_epoch(self) -> None:
        """mutation 成功后调用：epoch+1 并清空 read/search/verification 的 exact cache。"""
        self.evidence_epoch += 1
        self._exact_seen.clear()

    def exact_identity(self, name: str, arguments: dict[str, Any] | None):
        """精确执行身份（比 analysis fingerprint 严格；不含 result_excerpt）。"""
        args = dict(arguments or {})
        if name in _P9_READ_TOOLS:
            target = str(args.get("path") or args.get("file") or args.get("filename")
                         or args.get("directory") or "").strip().lower()
            # 目标不可解析 → 绝不 suppression（否则不同文件会被误判相同）
            return ("read", target, self.evidence_epoch) if target else None
        if name in _P9_DISCOVERY_TOOLS:
            q = str(args.get("query") or args.get("keyword") or args.get("q")
                    or args.get("directory") or "").strip().lower()
            return ("search", q, self.evidence_epoch) if q else None
        if name in _P9_VERIFICATION_TOOLS:
            cmd = str(args.get("filename") or args.get("file") or args.get("code")
                      or "").strip()
            # 验证命令不可解析 → 不 suppression（避免误杀不同验证）
            return ("verify", cmd[:400], self.evidence_epoch) if cmd else None
        return None  # mutation / 其它 → 绝不 suppression

    def redundant_check(self, name: str, arguments: dict[str, Any] | None) -> tuple[bool, str]:
        """只有 EXACT redundant（同身份同 epoch）才返回 True。"""
        key = self.exact_identity(name, arguments)
        if key is None:
            return False, ""
        if key in self._exact_seen:
            return True, (
                "【已有证据】该目标在当前 workspace state 下已经执行过且内容未变化，"
                "无需重复执行；请基于已有结果继续下一步（读其他文件 / 修改 / 验证 / 回答）。"
            )
        return False, ""

    def mark_exact_seen(self, name: str, arguments: dict[str, Any] | None) -> None:
        key = self.exact_identity(name, arguments)
        if key is not None:
            self._exact_seen[key] = True

    def completion_ready(self) -> bool:
        """已发生 mutation 且 verification 已通过 → 具备收口条件（不是新状态）。

        保守：只有“改过 + 验证通过”才认为完成，避免 read-only / 仅验证任务被误收口。
        """
        return bool(self.mutation_seen and self.verification_passed)

    def guard_summary(self) -> dict[str, Any]:
        return {
            "evidence_epoch": self.evidence_epoch,
            "redundant_guard_hits": self.redundant_guard_hits,
            "suppressed_read": self.suppressed_read,
            "suppressed_search": self.suppressed_search,
            "suppressed_verification": self.suppressed_verification,
            "completion_ready": self.completion_ready(),
            "repeat_target_hits": self.repeat_target_hits,
            "repeat_target_cap": self.repeat_target_cap,
        }

    # ---- Phase 4：动作签名 / progress 观测 ----

    def action_signature(self, name: str, arguments: dict[str, Any] | None) -> str:
        """统一语义动作签名：搜索类走语义意图，其它走精确规范化签名。"""
        if name in _SEARCH_TOOLS:
            return semantic_search_intent(name, arguments)
        return discovery_signature(name, arguments)

    def note_error(self, name: str, error_class: str | None) -> None:
        """记录一次工具错误；同类错误重复且无新上下文 → 计入无进展。"""
        cls = (error_class or "error").strip()[:60]
        self.error_classes[cls] = self.error_classes.get(cls, 0) + 1
        if self.error_classes[cls] >= 2:
            self.repeated_error_count += 1
            self.consecutive_no_progress += 1

    def observe(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        result_text: object | None = None,
        *,
        status: str = "executed",
        error_class: str | None = None,
        turn: int | None = None,
    ) -> ProgressSignal:
        """对一次动作做确定性进展判定，并更新 Run 级 progress 状态。

        进展（novel=True）的定义：
        - 首次出现的语义动作（新方向）；
        - 同一动作返回了与上次不同的结果（新 evidence）；
        - 真实 mutation（写/改/删）；
        - 验证类工具（run_python/code_loop）产生了新的结果。
        非进展（novel=False）：同签名同结果重复、重复被拦、同类错误重复。
        """
        if turn is not None:
            self.turn = turn
        else:
            self.turn += 1
        sig = self.action_signature(name, arguments)

        if status == "blocked":
            self.blocked_repeat_count += 1
            self.consecutive_no_progress += 1
            return ProgressSignal(False, "blocked_repeat", sig)
        if status == "error":
            self.note_error(name, error_class)
            return ProgressSignal(False, f"tool_error:{error_class or 'error'}", sig,
                                  error_class=error_class)

        fp = _text_fingerprint(result_text)
        prev = self.action_fingerprints.get(sig)
        first_action = sig not in self._seen_actions
        novel = False
        reason = ""
        if first_action:
            novel = True
            reason = "new_action"
            self._seen_actions.add(sig)
        elif fp and fp != prev:
            novel = True
            reason = "new_result"
        elif not fp:
            # 结果不可解析：只在动作本身是新的时候算进展（上面已处理）
            novel = False
            reason = "empty_result"
        else:
            novel = False
            reason = "same_result"

        if fp:
            self.action_fingerprints[sig] = fp
            if not novel:
                self._same_result_streak[sig] = self._same_result_streak.get(sig, 0) + 1
            else:
                self._same_result_streak[sig] = 0

        if novel:
            self.meaningful_progress_count += 1
            self.consecutive_no_progress = 0
            self.last_progress_turn = self.turn
            self.low_novelty_streak = 0
        else:
            self.consecutive_no_progress += 1
            self.low_novelty_streak += 1
            if reason in ("same_result",):
                self.duplicate_action_count += 1
        # ---- Phase 9：Evidence / Saturation（复用本 tracker）----
        try:
            args = arguments or {}
            if name in _P9_READ_TOOLS:
                tgt = str(args.get("path") or args.get("file") or args.get("filename")
                          or args.get("directory") or "").strip()
                if tgt:
                    self.unique_files_read.add(tgt)
            elif name in _P9_DISCOVERY_TOOLS:
                q = str(args.get("query") or args.get("keyword") or args.get("q")
                        or args.get("directory") or "").strip()
                if q:
                    self.unique_queries.add(q)
            if name in _P9_MUTATION_TOOLS and status != "error":
                # Phase 24：三态语义 — 只有 COMMITTED 才推进 mutation_seen。
                # FAILED / UNKNOWN 均不推进 revision。
                from runtime.runner import _mutation_result_ok as _mrok
                _outcome = _mrok(name, str(result_text or ""))
                if _outcome == "COMMITTED":
                    self.mutation_seen = True
                # FAILED / UNKNOWN → 不设置 mutation_seen，不 bump epoch
            elif name in _P9_VERIFICATION_TOOLS:
                self.verification_seen = True
                text = str(result_text or "")
                passed = ("退出码: 0" in text or "退出码：0" in text
                          or "✅ 通过" in text or "passed" in text.lower())
                if passed:
                    self.verification_passed = True
                    self.verified_revision = self.evidence_epoch
                try:
                    self.verification_coverage.append({
                        "tool": name, "passed": bool(passed),
                        "verified_revision": self.evidence_epoch,
                    })
                except Exception:
                    pass
            elif name in _P9_READ_TOOLS or name in _P9_DISCOVERY_TOOLS:
                if self.mutation_seen:
                    self.post_mutation_discovery += 1
                if self.verification_passed:
                    self.post_verification_discovery += 1
        except Exception:
            pass
        return ProgressSignal(novel, reason, sig, fp, error_class)

    def convergence_level(self) -> str:
        """按 Run 级 progress 历史返回收敛级别（NORMAL/CAUTION/CONVERGENCE/TERMINALIZE）。"""
        if self.force_stop:
            return CONVERGENCE_TERMINALIZE
        if (self.consecutive_no_progress >= self.terminalize_after
                or self.blocked_repeat_count >= self.terminalize_after + 1):
            return CONVERGENCE_TERMINALIZE
        if (self.consecutive_no_progress >= self.convergence_after
                or self.duplicate_action_count >= self.convergence_after
                or self.blocked_repeat_count >= self.convergence_after):
            return CONVERGENCE_CONVERGENCE
        if (self.consecutive_no_progress >= self.caution_after
                or self.repeated_error_count >= 1):
            return CONVERGENCE_CAUTION
        return CONVERGENCE_NORMAL

    def is_duplicate_action(self, name: str, arguments: dict[str, Any] | None) -> bool:
        """执行前判断：该语义动作是否已在“同结果”状态下重复过（用于 CAUTION 抑制）。"""
        sig = self.action_signature(name, arguments)
        return self._same_result_streak.get(sig, 0) >= 1

    def progress_summary(self) -> dict[str, Any]:
        return {
            "meaningful_progress_count": self.meaningful_progress_count,
            "consecutive_no_progress": self.consecutive_no_progress,
            "duplicate_action_count": self.duplicate_action_count,
            "repeated_error_count": self.repeated_error_count,
            "blocked_repeat_count": self.blocked_repeat_count,
            "last_progress_turn": self.last_progress_turn,
            "convergence_level": self.convergence_level(),
        }

    # ---- Phase 9：Exploration Saturation + Evidence-Driven Decision Hint ----

    def saturation_summary(self) -> dict[str, Any]:
        return {
            "unique_files_read": len(self.unique_files_read),
            "unique_queries": len(self.unique_queries),
            "meaningful_progress_count": self.meaningful_progress_count,
            "low_novelty_streak": self.low_novelty_streak,
            "duplicate_action_count": self.duplicate_action_count,
            "mutation_seen": self.mutation_seen,
            "verification_seen": self.verification_seen,
            "verification_passed": self.verification_passed,
            "post_mutation_discovery": self.post_mutation_discovery,
            "post_verification_discovery": self.post_verification_discovery,
        }

    def saturated(self) -> bool:
        """探索收益明显下降：连续低新颖，或 mutation/verification 后仍持续探索。"""
        return (self.low_novelty_streak >= 2
                or self.post_verification_discovery >= 2
                or self.post_mutation_discovery >= 3)

    def decision_hint(self) -> str | None:
        """极短的结构化运行时事实反馈（不是 Planner，不写成长 Prompt）。"""
        if not self.saturated():
            return None
        s = self.saturation_summary()
        verify = ("通过" if s["verification_passed"]
                  else ("已运行" if s["verification_seen"] else "否"))
        lines = [
            "【执行状态】",
            f"已读取相关文件 {s['unique_files_read']} 个；新增有效证据 "
            f"{s['meaningful_progress_count']} 条；连续低新颖探索 {s['low_novelty_streak']} 次。",
            f"已修改：{'是' if s['mutation_seen'] else '否'}；已验证：{verify}。",
        ]
        if s["verification_passed"]:
            lines.append("验证已通过：除非有明确未解决问题，否则直接收口给出最终回答，不要再探索。")
        elif s["mutation_seen"] and not s["verification_seen"]:
            lines.append("已修改但未验证：下一步优先运行验证，而不是继续探索。")
        else:
            lines.append("继续探索需要明确的未解决问题；否则转入修改/验证/回答。")
        return "\n".join(lines)

    def _blocked_text(self, intent: str) -> str:
        return (
            "【SEARCH_CONVERGENCE_REACHED · 运行时提示】这个搜索方向你已经尝试了多次，"
            "返回的结果没有带来新的有效信息，Runtime 已停止对该搜索意图的继续尝试。"
            "请不要再换措辞重试。若有其它可用能力（读已有笔记/文件/记忆）可继续；"
            "否则直接基于已掌握的信息作答，无法确认的部分如实说明限制。"
        )

    def note(self, name: str, arguments: dict[str, Any] | None,
             result_text: object | None = None) -> tuple[bool, str | None]:
        """记录一次只读探索调用（执行前判断：计数 + 次数兜底 + 已收敛拦截）。

        若传入 result_text，一并做结果新颖度更新（执行后调用可复用）。
        新颖度导致收敛时会写入 blocked_intents。
        """
        if self.force_stop:
            return False, DISCOVERY_EXHAUSTED_TEXT
        exact = discovery_signature(name, arguments)
        self.counts[exact] = self.counts.get(exact, 0) + 1
        sem = semantic_search_intent(name, arguments)
        sem_search = name in _SEARCH_TOOLS
        if sem_search:
            self.semantic_counts[sem] = self.semantic_counts.get(sem, 0) + 1
            if sem in self.blocked_intents:
                return False, self._blocked_text(sem)
            # 无结果可解析时：认可的新颖度无法判断，用 attempt 次数兜底（安全 cap）。
            if result_fingerprint(result_text) is None:
                if self.semantic_counts[sem] >= self.semantic_limit and sem not in self.blocked_intents:
                    self.blocked_intents.add(sem)
                    return False, self._blocked_text(sem)
        # 结果新颖度（若有 result）：低新颖累计达到阈值 → 收敛
        if result_text is not None and sem_search:
            if self._novelty_tick(sem, result_text):
                return False, self._blocked_text(sem)
        # 精确层：同参数连续 3 次 → 耗尽
        if self.counts[exact] >= 3:
            self.exhausted_sig = exact
            self.strikes += 1
            if self.strikes >= 2:
                self.force_stop = True
            return False, DISCOVERY_EXHAUSTED_TEXT
        return True, None

    def note_result(self, name: str, arguments: dict[str, Any] | None, result_text: object) -> None:
        """执行后单独调用：只更新结果新颖度（不重复计数/不触发精确耗尽）。"""
        if name not in _SEARCH_TOOLS:
            return
        sem = semantic_search_intent(name, arguments)
        self._novelty_tick(sem, result_text)

    def _novelty_tick(self, sem: str, result_text: object) -> bool:
        fp = result_fingerprint(result_text)
        if not fp or sem in self.blocked_intents:
            return False
        prev = self._last_results.get(sem)
        overlap = result_overlap(prev, fp) if prev else None
        self._last_results[sem] = fp
        if overlap is not None and overlap >= 0.8:
            self._low_novelty[sem] = self._low_novelty.get(sem, 0) + 1
        else:
            self._low_novelty[sem] = 0
        if self._low_novelty[sem] >= self.low_novelty_required:
            self.blocked_intents.add(sem)
            return True
        return False

    def is_blocked(self, name: str, arguments: dict[str, Any] | None) -> tuple[bool, str | None]:
        """执行前只读判断：某语义意图是否已被收敛/耗尽。不计数、不更新 result。"""
        if self.force_stop:
            return True, DISCOVERY_EXHAUSTED_TEXT
        if name not in _SEARCH_TOOLS:
            return False, None
        sem = semantic_search_intent(name, arguments)
        if sem in self.blocked_intents:
            return True, self._blocked_text(sem)
        return False, None

    def as_dict(self) -> dict[str, int]:
        d = dict(self.counts)
        for k, v in self.semantic_counts.items():
            d[f"sem:{k}"] = v
        for k, v in self._low_novelty.items():
            d[f"nov:{k}"] = v
        return d

    def hard_stopped(self) -> bool:
        return self.force_stop

    def blocked(self) -> list[str]:
        return sorted(self.blocked_intents)


# ---------------------------------------------------------------------------
# User Constraint（显式约束：不要修改/不要删除/只看/先别动…）
# 只在当前用户 turn 生效；后续 run 重新解析，不持久化到 Task。
# ---------------------------------------------------------------------------

_NO_WRITE = re.compile(
    r"(?:不要|别|先不要|请勿|避免|千万别|不)[^，。.]{0,10}(?:修改|改动|改文件|改代码|编辑|"
    r"写入|覆盖|重构|更换|变动|动它|动代码|动文件|改它)",
    re.IGNORECASE,
)
_NO_DELETE = re.compile(
    r"(?:不要|别|先不要|请勿|千万别|不)[^，。.]{0,10}(?:删除|删掉|删文件|删代码|移除|清理掉|删它)",
    re.IGNORECASE,
)
_READ_ONLY = re.compile(
    r"(?:只|仅)(?:看|读|分析|检查|说明|解释|评价|阅读)|只看(?:看)?|先.{0,4}(?:分析|找|检查).{0,6}(?:原因|问题)|"
    r"不要修改|先不要改|先改原因|别改", re.IGNORECASE,
)


def detect_user_constraints(message: str | None) -> dict[str, bool]:
    """提取当前用户 turn 的显式约束。默认全部 allow；命中则收窄对应动作。"""
    text = str(message or "").strip()
    c = {"allow_write": True, "allow_delete": True}
    if not text:
        return c
    if _NO_WRITE.search(text) or _READ_ONLY.search(text) or "不要修改" in text or "先不要改" in text:
        c["allow_write"] = False
    if _NO_DELETE.search(text):
        c["allow_delete"] = False
    return c


# ---------------------------------------------------------------------------
# Clarification Precision（只做最小确定性校验，供 Runner/Harness 复用）
# ---------------------------------------------------------------------------

#: 明确“空泛澄清”的标志词：这些词出现且无具体问句 → 精度不足
VAGUE_CLARIFY_MARKERS = (
    "补充更多信息", "补充关键信息", "补充更多", "请补充信息", "信息不足",
    "请提供更多信息", "需要更多信息", "说明一下任务", "需要你补充", "再详细一点",
)
_QUESTION_MARK = re.compile(r"[?？]")


def clarification_precision(questions: list[str] | None) -> str:
    """返回 'ok' | 'low' | 'empty'。

    ok    = 有具体问句（不带空泛标志词，或带问号）；
    low   = 只写“需要补充关键信息/信息不足”等空泛措辞；
    empty = questions 为空。
    """
    qs = [str(q).strip() for q in (questions or []) if str(q).strip()]
    if not qs:
        return "empty"
    vague = [
        q for q in qs
        if any(marker in q for marker in VAGUE_CLARIFY_MARKERS)
        and not _QUESTION_MARK.search(q)
    ]
    return "low" if vague else "ok"


def normalize_missing(value: Any) -> list[dict[str, str]]:
    """把模型 readiness.missing 规整为 [{name, reason}]（只透传受控小列表）。"""
    out: list[dict[str, str]] = []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return out
    for item in value[:16]:
        if isinstance(item, str):
            name = item.strip()
            if name and name not in (o.get("name") for o in out):
                out.append({"name": name[:40], "reason": ""})
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("field") or "").strip()[:40]
        if not name:
            continue
        if name in (o.get("name") for o in out):
            continue
        reason = str(item.get("reason") or "")[:160]
        out.append({"name": name, "reason": reason})
    return out[:8]
