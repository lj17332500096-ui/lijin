"""Tool Router：按查询给模型只配相关工具（默认开启；TOOL_ROUTER=off 关闭）。

选择规则（优先级从高到低）：
1. 查询里出现工具名（save_note、run_python…）→ 必选；
2. 关键词分组命中（按 中文/英文别名）→ 入选；
3. 常驻基础集（通用能力，避免误伤日常请求）；
4. 超过 max_tools 时按相关性排序裁掉最不相关的，基础集永不裁。

本模块是纯函数 + 静态关键词表：无模型调用、无网络，测试友好。

C1 可观测：select_tool_names 每次调用都会写一行 JSON 到 var/logs/tool_router.jsonl
（TOOL_ROUTER_LOG=off 可关；默认 on）。线上出问题时一眼定位是哪条查询被误裁。
"""

import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from runtime_paths import LOG_DIR

def infer_capability_plan(query: str, available: list[str] | None = None) -> dict:
    """Deprecated compatibility shim; task-plan inference lives in Runtime."""
    from runtime.task_plan import infer_task_plan

    return infer_task_plan(query, available)

# C1：可观测日志路径（var/logs/tool_router.jsonl）
_TOOL_ROUTER_LOG_PATH = LOG_DIR / "tool_router.jsonl"
# C1 指标：router 指标快照（var/logs/router_metrics.jsonl，每 60s 写一次，可被 dashboard 拉取）
_ROUTER_METRICS_PATH = LOG_DIR / "router_metrics.jsonl"

# 指标累加器（进程内，按时间窗口聚合）
_metrics_state: dict = {
    "total": 0,          # 总调用次数
    "hit_zero": 0,       # 命中 0 目标工具次数（仅 tool_needed/ambiguous 计入，修复 C）
    "total_intent_tool": 0,  # 新增：tool_needed + ambiguous 调用次数（hit_zero 真实分母）
    "escalated": 0,      # C2 熔断升级次数
    "fast_mode": 0,      # 逃生门命中次数
    "empty": 0,         # direct_text 路径（返回 0 工具）
    "latencies": [],     # 最近 1000 次延迟样本（ms）
    "last_flush": time.time(),
}


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    """Return a nearest-rank percentile (p95 index = ceil(.95 * N) - 1)."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    p = min(100.0, max(0.0, float(percentile)))
    index = max(0, math.ceil((p / 100.0) * len(ordered)) - 1)
    return ordered[index]

def _tool_router_log_enabled() -> bool:
    return os.getenv("TOOL_ROUTER_LOG", "on").strip().lower() not in ("off", "false", "0")


def _maybe_flush_metrics() -> None:
    """每 60s 把指标累加器写到 var/logs/router_metrics.jsonl（失败不阻塞）。"""
    now = time.time()
    if now - _metrics_state["last_flush"] < 60.0:
        return
    _metrics_state["last_flush"] = now
    try:
        _ROUTER_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        lat = _metrics_state["latencies"]
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "total_calls": _metrics_state["total"],
            # 修复 C：hit_zero_rate 分母改为"有工具意图"的调用次数（total_intent_tool），
            # 不再混入 direct_text / concept_qa / arith 这些本就无需工具的查询。
            "hit_zero_rate": round(
                _metrics_state["hit_zero"] / max(_metrics_state["total_intent_tool"], 1), 4
            ),
            "total_intent_tool": _metrics_state["total_intent_tool"],
            "escalated_total": _metrics_state["escalated"],
            "fast_mode_total": _metrics_state["fast_mode"],
            "empty_rate": round(_metrics_state["empty"] / max(_metrics_state["total"], 1), 4),
            "p95_latency_ms": round(_nearest_rank_percentile(lat, 95), 2),
            "avg_latency_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "window_samples": len(lat),
        }
        with _ROUTER_METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 指标写入失败不阻塞主流程


def record_router_call(
    latency_ms: float,
    hit_zero: bool,
    escalated: bool = False,
    fast_mode: bool = False,
    empty: bool = False,
    intent_class: str = "",
) -> None:
    """外部（C2 熔断/fast 模式）调用的指标记录入口；由 select_tool_names 内部调用。"""
    _metrics_state["total"] += 1
    if hit_zero:
        _metrics_state["hit_zero"] += 1
    if escalated:
        _metrics_state["escalated"] += 1
    if fast_mode:
        _metrics_state["fast_mode"] += 1
    if empty:
        _metrics_state["empty"] += 1
    # 修复 C：hit_zero 分母只算"有工具意图"的调用（tool_needed + ambiguous）
    if intent_class in ("tool_needed", "ambiguous"):
        _metrics_state["total_intent_tool"] += 1
    _metrics_state["latencies"].append(latency_ms)
    if len(_metrics_state["latencies"]) > 1000:
        _metrics_state["latencies"] = _metrics_state["latencies"][-1000:]
    _maybe_flush_metrics()

# PII 脱敏正则（C1 合规）：身份证、手机号、邮箱、银行卡、API key
# 顺序重要：
# 1. 身份证(18位)必须先于手机号匹配，否则手机号正则会匹配身份证前 11 位（如 11010119900）
# 2. 身份证必须先于银行卡匹配，否则银行卡正则会把身份证破坏
# 3. 银行卡用前后非数字边界锚定，避免匹配到其他 PII 的内部子串
_PII_PATTERNS = [
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "*****************"),   # 身份证(18位，含X)
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***********"),          # 中国大陆手机号(11位)
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***@***"),              # 邮箱
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "****"),                   # 银行卡(16-19位纯数字)
    (re.compile(r"(?i)\b(sk|cpk|tvly|ghp|gho|api[_-]?key)[-_]\w+\b"), "***MASKED***"),  # API key
]

def _mask_pii(text: str) -> str:
    """PII 脱敏：手机号/邮箱/身份证/银行卡/API key 替换为掩码。"""
    for pattern, repl in _PII_PATTERNS:
        text = pattern.sub(repl, text)
    return text

def _write_tool_router_log(
    query: str,
    selected: list[str],
    total_available: int,
    selected_count: int,
    hit_zero: bool,
    reasons: list[str] | None = None,
    intent_class: str = "",
) -> None:
    """C1：把 router 决策写一行 JSON 到 var/logs/tool_router.jsonl（失败不阻塞）。"""
    if not _tool_router_log_enabled():
        return
    try:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "schema_version": 2,  # 修复 C：hit_zero 口径收敛后的 schema 版本
            "query": _mask_pii((query or "")[:500]),  # PII 脱敏 + 截断
            "selected": selected,
            "selected_count": selected_count,
            "total_available": total_available,
            "hit_zero": hit_zero,  # 仅 tool_needed/ambiguous 的 0 工具计 True（修复 C）
            "intent_class": intent_class or "unclassified",
            "reasons": reasons or [],
        }
        from runtime.rotating_jsonl import append_jsonl

        append_jsonl(_TOOL_ROUTER_LOG_PATH, entry, env_prefix="FORGE_ROUTER_LOG")
    except Exception:
        # 日志失败不阻塞 router 主流程
        pass

# PPT 触发词要放宽：技能指令已把 gorden_ppt_* 写进人设，只要用户话里有
# ppt/模板/汇报/幻灯片/演示文稿 等词就必须把这组工具配给模型，否则会出现
# “技能已就绪但工具不在本轮工具列表 → Tool not found in agent” 的调用失败。
# “汇报/总结”单独出现也视为 PPT 族意图（汇报材料最常见的交付形态就是 PPT）。
_GORDEN_PPT_COMMON = ("ppt", "pptx", "汇报", "总结汇报", "汇报PPT", "演示文稿",
                      "幻灯片", "模板", "套模板", "按模板", "用模板", "总结")
#: PPT/汇报意图族（比 _GORDEN_PPT_COMMON 宽：覆盖“做PPT/生成PPT”等动词短语与“汇报/总结”语境）
_GORDEN_PPT_INTENT = re.compile(
    r"(ppt|幻灯片|演示文稿|课件|汇报|总结|模板|template|deck|slides)", re.IGNORECASE)

# 工具名 → 中文/英文别名关键词（不区分大小写；name 本身不必写，规则 1 已覆盖）
TOOL_TERMS: dict[str, tuple[str, ...]] = {
    "extension_manager": ("插件清单", "插件状态", "扩展状态", "可用技能", "已安装技能",
                          "加载技能", "启用技能", "extension manager", "load skill",
                          "刷新技能", "重新扫描技能", "refresh skills",
                          "品牌规范", "品牌风格", "品牌色", "小说创作", "小说",
                          "短篇拆解", "长篇拆解", "周报", "周总结", "依赖体检",
                          "依赖检查", "requirements", "文章", "文案", "创作",
                          "网页设计", "网页改版", "日计划"),
    "deep_research": ("调研", "研究报告", "全面了解", "对比", "报告", "深度", "研究", "综述", "research"),
    "get_current_datetime": ("今天", "明天", "日期", "几点", "现在时间", "星期", "时间", "date"),
    "get_weather": ("天气", "气温", "温度", "下雨", "下雪", "穿什么", "冷不冷", "热不热", "雾霾", "紫外线", "humidity", "weather"),
    "calculate": ("计算", "等于", "多少钱", "数学", "加减乘除", "乘", "百分比", "算一下", "换算", "calc"),
    "save_note": ("保存", "写成文件", "备忘", "记录下来", "存成", "note", "文件保存",
                  "周报", "月报", "日报", "写个记录", "存一下", "帮我存", "帮我记",
                  "写份周报", "写篇日记", "写成记录", "写成备忘", "帮我写个记录", "写一份周报", "写一份日报", "写一份月报"),
    "read_note": ("回看备忘", "读备忘", "查看备忘"),
    "list_notes": ("列出备忘", "有哪些备忘", "笔记列表", "notes"),
    "remember": ("记住", "记得我的", "偏好", "背景", "习惯", "喜欢", "remember"),
    "recall_memory": ("回忆", "记得关于我", "查一下记忆", "翻记忆", "查看记忆",
                      "我的记忆", "我的记录", "翻记录", "找笔记", "翻笔记",
                      "回忆一下", "回想", "recall", "查记忆"),
    "forget_memory": ("删除记忆", "忘掉", "清除记忆", "忘记", "别记了"),
    "index_workspace": ("建索引", "索引一下", "刷新索引", "index"),
    "search_documents": ("检索文档", "文档里", "在笔记里查", "本地资料", "rag", "搜文档",
                         "找文档", "找资料"),
    "read_workspace_file": ("读文件", "读取", "看文件", "源码", "文件内容", "打开文件", "read"),
    "list_workspace_files": ("目录", "工作区", "文件列表", "有哪些文件", "ls", "list"),
    "ask_image": ("图片", "截图", "照片", "看图", "图像", "image", "screenshot", "png", "jpg"),
    "read_office_file": ("word", "excel", "ppt", "文档", "docx", "xlsx", "pptx",
                         "表格文件", "读表格", "读文档", "pdf", "转成文字",
                         "提取文字", "ocr", "pdf文字"),
    "read_spreadsheet": ("csv", "工作表", "表头", "列", "电子表格", "多行", "sheet"),
    "save_word_doc": ("生成word", "word文档", "做成word", "docx", "写word"),
    "save_excel_workbook": ("excel", "工作簿", "xlsx", "表格文件保存", "做表",
                            "生成excel", "做excel", "excel表格"),
    "save_ppt_deck": ("ppt", "pptx", "幻灯片", "演示文稿", "课件", "做ppt", "生成ppt"),
    "fetch_github_repo": ("github", "仓库", "repo", "开源项目", "抓取仓库", "克隆", "clone",
                          "star数", "star 数", "github 仓库"),
    "write_code_file": ("写代码", "写脚本", "代码文件", "新建代码", "function",
                        "日志中间件", "加个中间件", "写中间件", "写个中间件",
                        "爬虫", "写爬虫", "抓网页", "正则表达式", "lru缓存",
                        "排序算法", "算法实现", "用户登录接口"),
    "read_code_file": ("读代码", "看代码", "回看代码"),
    "list_code_files": ("代码列表", "沙箱文件", "code_sandbox"),
    "run_python": ("运行", "执行", "跑一下", "python", "脚本", "print", "代码", "程序",
                   "测试", "报错", "调试", "sandbox", "跑测试", "跑脚本", "空指针"),
    "run_tests": ("测试", "跑测试", "运行测试", "执行测试", "验证", "校验",
                  "pytest", "unittest", "测试通过", "test", "单元测试"),
    "write_project_file": ("改项目", "项目文件", "新增文件", "加个文件", "新文件",
                           "新建文件", "project", "日志中间件", "加个中间件"),
    "edit_project_file": ("修改", "编辑", "重构", "改动", "修复", "替换", "edit",
                         "改成", "改为", "调成", "大小限制", "上传限制", "限制",
                         "样式", "按钮", "报错", "刷新", "页面", "提交",
                         "500", "token", "接口", "重构", "重构函数", "可读性"),
    "sandbox_snapshot": ("快照", "备份沙箱", "savepoint"),
    "sandbox_rollback": ("回滚", "恢复沙箱", "撤销改动", "rollback"),
    "list_sandbox_snapshots": ("查看快照", "快照列表"),
    "schedule_add": ("定时", "提醒我", "每天早上", "每周", "每天", "定时任务",
                     "schedule", "闹钟", "明天提醒", "N点提醒", "N 点提醒",
                     "明天9点", "设置提醒", "加个提醒"),
    "schedule_list": ("定时任务列表", "有哪些定时", "看任务", "查任务",
                      "定时任务", "我的任务"),
    "schedule_remove": ("删除任务", "取消定时", "移除任务", "删掉任务", "删任务"),
    "schedule_set_enabled": ("停用任务", "启用任务", "暂停任务", "停任务"),
    "scan_dependencies": ("依赖体检", "依赖检查", "requirements", "pyproject", "依赖问题"),
    "search_sources": ("参考资料", "检索资料", "查资料", "资料里", "报告里", "文档里写过",
                       "source", "参考文档", "查一下资料"),
    "gorden_ppt_templates": _GORDEN_PPT_COMMON + ("PPT模板", "模板做", "选模板",
                             "做一份PPT", "做一个PPT", "生成PPT", "做PPT", "ppt模板",
                             "模板清单", "template", "deck", "slides"),
    "gorden_ppt_template_intro": _GORDEN_PPT_COMMON + ("PPT模板", "模板做", "选模板",
                                  "模板介绍", "模板结构", "template"),
    "gorden_ppt_build": _GORDEN_PPT_COMMON + ("PPT模板", "生成PPT", "做一份PPT",
                         "做一个PPT", "做PPT", "模板生成", "build ppt", "deck"),
    "gorden_ppt_apply_custom": _GORDEN_PPT_COMMON + ("自己的PPT模板", "自定义模板",
                                "按我的PPT", "我的模板文件", "这个模板",
                                "custom template"),
}

# 常驻基础集（只放“几乎任何任务都可能需要、且无副作用”的通用能力）。
# web_search 不再常驻：它只应在“实时/检索意图”命中时暴露，否则 coding/只读任务
# 会被塞入无关联网工具，成为模型决策漂移与 tool wandering 的主要放大器。
BASE_TOOLS = {
    "get_current_datetime",
    "calculate",
    "read_workspace_file",
    "list_workspace_files",
}

#: 意图族 → 该族内"补齐"工具（替代原来的无条件 rest 填充）。
#: 多轮维修回归修复（B1）：原表只有"修复/修改/编辑/重构/实现/新增/添加/补充/补测试/
#: 测试/报错/调试/bug/代码/脚本/运行/执行/部署/字段/函数/接口/auth/calculate"，
#: 漏了"写"这个最高频动词，导致"帮我写一个排序算法""帮我写个函数"等典型
#: coding 意图不进 coding 族，模型拿不到 write_code_file / run_python。
_CODING_INTENT = re.compile(
    r"修复|修改|编辑|重构|实现|新增|添加|补充|补测试|测试|报错|调试|bug|代码|脚本|"
    r"运行|执行|部署|字段|函数|接口|auth|calculate|"
    r"写(?:代码|脚本|函数|模块|程序|算法|排序|逻辑|页面|样式|测试|组件|类|方法|文件|实现功能)|"
    r"算法|排序|实现功能|写代码|写脚本|写函数|写模块|写程序|编程|开发|程序",
    re.IGNORECASE,
)

#: B-fix：memory/note 意图族——查询带记忆/笔记/记录语义时补齐 memory/note 工具。
#: 原来 family 只有 coding/web/ppt，memory/note 类意图（"帮我写一份周报""查看我的记忆"）
#: 不进任何 family，save_note/recall_memory 门开了也补不进来，被 BASE_TOOLS 截断。
#: 注意：不要放宽泛的"写"单字（"帮我写个 PPT"会被误命中），要用"写一份周报/写篇日记"
#: 这种精确短语；"查看/翻/找"也不要单独出现（"查看"是高频通用词，会污染 PPT 类查询）。
#: 同时排除 PPT/汇报类查询（"帮我写个 PPT" 是 PPT 族意图，不是 memory 族）。
_MEMORY_NOTE_INTENT = re.compile(
    r"保存|存成|存为|记一下|记住|备忘|笔记|记录|周(?:报|记)|月(?:报|记)|日(?:报|记)|"
    r"我的(?:记忆|记录|笔记|备忘)|回忆|回想|recall|remember|forget|备忘|"
    r"写(?:份|篇|个|一下|一个)(?:周报|日报|月报|记录|备忘|笔记)|"
    r"查(?:我的|一下)(?:记忆|记录|笔记|备忘)|翻(?:我的)?(?:记忆|记录|笔记|备忘)|"
    r"找(?:我的)?(?:记忆|记录|笔记|备忘)",
    re.IGNORECASE,
)
# PPT/汇报类查询：命中时不进 memory/note 族（"帮我写个 PPT" 不是"写一份周报"）。
_MEMORY_NOTE_EXCLUDE = re.compile(
    r"ppt|pptx|幻灯片|演示文稿|课件|汇报|总结汇报|模板|deck|slides",
    re.IGNORECASE,
)
_MEMORY_NOTE_SUPPORT = {
    "save_note", "read_note", "list_notes",
    "remember", "recall_memory", "forget_memory",
}
_CODING_SUPPORT = {
    "read_workspace_file", "list_workspace_files", "read_code_file",
    "list_code_files", "search_documents", "index_workspace",
    "edit_project_file", "write_project_file", "write_code_file",
    "run_python", "run_tests", "code_loop",
}
_WEB_INTENT = re.compile(
    r"天气|气温|温度|下雨|下雪|雾霾|紫外线|风力|湿度|穿什么|冷不冷|热不热|"
    r"新闻|最新|实时|股价|航班|搜索|搜一下|查一下|查询|联网|网页|"
    r"资讯|what|news|weather|current|today|forecast|"
    r"读一下|读文件|读代码|看文件|查看文件|看下文件|帮我读|帮我看看.*\.(py|md|json|txt|toml|yaml|yml|cfg|ini|csv)|"
    r"打开.*\.(py|md|json|txt|toml|yaml|yml|cfg|ini|csv)|内容.*\.(py|md|json|txt|toml|yaml|yml|cfg|ini|csv)",
    re.IGNORECASE,
)
#: B-fix（2026-09-19 工具清单对账）：`search_sources` 已移出联网补齐族。
#: 它是**本地参考资料检索**，与"联网查实时信息"是两件事 —— 留在族里会让
#: "北京天气/航班/股价"这类纯联网查询凭空多带一个本地资料检索工具，
#: 与 BASE_TOOLS 注释里"web_search 不常驻"的清理理由完全同类。
#: 实测：50 道评测题里有 11 题被这样捎带，而它的采纳数是 1/11，
#: 且 `source_chunks` 当前为 0 行（库空，调用必然返回空）。
#: 用户明确提"参考资料/查资料/资料里"时仍由 TOOL_TERMS 词条派发，功能不丢。
_WEB_SUPPORT = {
    "get_current_datetime", "deep_research",
    "search_documents",
}

#: C-fix：「项目阅读 / 启动说明」意图族——"看 README 怎么启动""告诉我怎么跑起来"
#: 这类查询的目标是读项目文档/源码，应命中 read/list/search_documents，
#: 否则只落到 BASE 4 件套 → hit_zero。用精确短语（README/readme/怎么启动/跑起来/运行方式）
#: 触发，避免裸"启动"误伤"启动定时任务"等其它族。
_DOC_READ_INTENT = re.compile(
    r"readme|README|怎么启动|怎么跑(?:起来|起来)?|如何启动|如何跑起|"
    r"运行方式|使用方式|使用说明|项目说明|怎么运行|怎么使用",
    re.IGNORECASE,
)
_DOC_READ_SUPPORT = {
    "read_workspace_file", "list_workspace_files", "search_documents",
    "read_code_file", "list_code_files",
}

#: C-fix：「纯概念问答」——"Agent 和普通聊天模型有什么区别？""什么是 RAG？"
#: 这类纯知识性问题不需要任何工具，直接文本回答即可。识别后走 direct_text 路径，
#: 不再因"没命中目标工具"被计 hit_zero。必须叠加 _TOOL_NEEDED 排除（含"文件/代码/
#: 查询/搜索"等强信号的仍走工具路径，避免"什么是这个代码里的函数"被误吞）。
_CONCEPT_QA_INTENT = re.compile(
    r"什么是|是什么|有什么区别|有啥区别|有何区别|区别是|定义|概念|原理|"
    r"和.{0,8}(?:有)?(?:什么|啥)区别|普通.{0,6}(?:区别|不同)",
    re.IGNORECASE,
)

#: C-fix：「泛分析/解读」——"分析这个""帮我看下这段代码"，需要读文档/代码/数据类
#: 工具支撑。用精确短语（"分析"+"这个/一下"、"解读"、"看下这段"）触发，避免裸
#: "分析"误伤"分析报告生成PPT"（后者由 PPT 族接住）。
_ANALYSIS_INTENT = re.compile(
    r"分析(?:一下|下|这个|这段|这部分|这份|这页|这个项目)|解读|"
    r"帮我看下.{0,6}(?:代码|文档|数据|内容|情况)|看看.{0,4}(?:数据|结果|原因)",
    re.IGNORECASE,
)
_ANALYSIS_SUPPORT = {
    "read_workspace_file", "list_workspace_files", "search_documents",
    "read_code_file", "list_code_files", "calculate",
}
#: calculate 在 BASE 里但 hit_zero 统计的是「非 base 工具命中 0」，裸算式没进任何族
#: 就被判 hit_zero。识别算术算式（数字+运算符+数字，或"算/计算/等于"语境）后让
#: calculate 通过 family 命中，消除误判。用「数字运算符数字」精确模式，避免误伤
#: "1 楼停车场""第 2 章"等含数字的非算式文本。
_ARITH_INTENT = re.compile(
    r"\d\s*[\+\-\*/x×÷]\s*\d|\d\s*(?:等于|乘以|除以|加上|减去)\s*\d|"
    r"算(?:一下|下|一算)|计算(?:一下|下)?|等于(?:多少|几)|百分比",
    re.IGNORECASE,
)
_ARITH_SUPPORT = {"calculate", "read_spreadsheet"}

#: C-fix：「纯问候/闲聊」——"你好""介绍你自己""讲个笑话""谢谢""再见""hi"。
#: 这类无任务意图的寒暄不该配工具、更不该记 hit_zero（制造"新问题"假阳性）。
#: 与 _TOOL_NEEDED 反向排除：命中工具强信号（如"文件/代码/查询"）的仍走工具路径，
#: 避免"帮我读一下文件，谢谢"被问候词误吞。
_GREETING_INTENT = re.compile(
    r"^你好|^您好|^嗨|^哈喽|^hi\b|^hello\b|^hey\b|hi$|嗨$"
    r"|^(?:请|麻烦)?你?(?:用一?句话)?(?:介绍|说说)(?:你|您|自己|一下)*(?:你|您)?(?:自己)?$"
    r"|^(?:你|您)?是?(?:谁|什么模型|什么人|什么|哪个)(?:呀|啊|呢|哦)?$"
    r"|讲个?(?:笑话|段子|故事|冷笑话)"
    r"|^(?:谢谢|多谢|感谢|thanks|thank ?you)\b"
    r"|^(?:再见|拜拜|bye|goodbye)\b"
    r"|^(?:早上好|下午好|晚上好|早安|晚安)\b"
    r"|^在吗\b|^在不在\b|^在不在家\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Phase 22：MCP / Plugin candidate catalog + domain approval
# ---------------------------------------------------------------------------

#: MCP server → domain（server-level metadata；不硬编码具体工具）
_MCP_SERVER_DOMAIN: dict[str, str] = {
    "gitee": "source_control",
    "github": "source_control",
    "gitlab": "source_control",
    "playwright": "browser",
    "chrome": "browser",
    "youtube": "media",
    "fetch": "web",
    "anysearch": "web",
    "obsidian": "notes",
    "sqlite": "database",
}

#: provider identity（同一 domain 内不同 provider 不得互相替换）
_PROVIDER_TERMS: dict[str, tuple[str, ...]] = {
    "gitee": ("gitee", "码云"),
    "github": ("github",),
    "gitlab": ("gitlab",),
}

#: domain intent
_DOMAIN_INTENT: dict[str, "re.Pattern[str]"] = {
    "source_control": re.compile(
        r"仓库|repo|issue|pull|分支|release|代码托管|gitee|github|gitlab|码云|源码仓库",
        re.IGNORECASE),
    "browser": re.compile(
        r"浏览器|网页|打开网站|网页截图|页面自动化|browser|playwright|chrome|devtools|控制台",
        re.IGNORECASE),
    "media": re.compile(r"视频|字幕|transcript|youtube|油管", re.IGNORECASE),
    "database": re.compile(r"数据库|sqlite|\bsql\b|表结构|查询表", re.IGNORECASE),
    "notes": re.compile(r"obsidian|笔记库|vault", re.IGNORECASE),
    "web": re.compile(
        r"搜索|联网|网页|抓取网页|网页抓取|查找资料|anysearch|fetch|search|browse",
        re.IGNORECASE),
}

#: MCP 写/操作语义（只有明确 mutation 意图或点名时才暴露 mutation MCP 工具）
_MCP_MUTATION_TERMS = re.compile(
    r"创建|新建|修改|更新|删除|合并|提交|发起|上传|写入|发布|评论|"
    r"create|update|delete|remove|merge|write|fork|edit|comment|upload|"
    r"click|type|fill|press|drag|drop|evaluate|navigate",
    re.IGNORECASE,
)

_MCP_MAX_ADD = 6

#: 工具名 token → 中英文同义触发词（用于 MCP 工具与查询的相关性排序）
_MCP_TOKEN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "repo": ("仓库", "repo", "repository", "项目"),
    "repos": ("仓库", "repos", "repositories", "项目"),
    "repository": ("仓库", "repo", "repository", "项目"),
    "repositories": ("仓库", "repos", "repositories", "项目"),
    "list": ("列出", "列表", "有哪些", "list"),
    "issue": ("issue", "问题", "工单"),
    "issues": ("issue", "问题", "工单"),
    "pull": ("pull", "合并请求", "pr"),
    "pulls": ("pull", "合并请求", "pr"),
    "release": ("release", "发布", "版本"),
    "releases": ("release", "发布", "版本"),
    "user": ("用户", "user", "我的", "账号"),
    "info": ("信息", "info", "详情"),
    "detail": ("详情", "detail"),
    "file": ("文件", "file"),
    "content": ("内容", "content"),
    "branch": ("分支", "branch"),
    "tags": ("标签", "tag", "版本"),
    "comment": ("评论", "comment"),
    "comments": ("评论", "comment"),
    "notification": ("通知", "notification"),
    "notifications": ("通知", "notification"),
    "search": ("搜索", "search", "查找"),
    "transcript": ("字幕", "transcript", "视频"),
    "languages": ("字幕语言", "languages"),
}


def _mcp_relevance(name: str, text: str) -> int:
    score = 0
    for tok in name.lower().replace("-", "_").split("_"):
        for syn in _MCP_TOKEN_SYNONYMS.get(tok, ()):
            if syn in text:
                score += 1
                break
    return score



def build_tool_catalog(tools: list) -> dict[str, dict]:
    """从真实工具对象构建 provenance catalog（source 由注册路径属性决定，不靠名字猜）。"""
    from runtime.spec import capability_of, spec_for

    catalog: dict[str, dict] = {}
    for t in tools or []:
        name = getattr(t, "name", "")
        if not name:
            continue
        source = "CORE"
        server = ""
        if getattr(t, "_mcp_source", None) == "mcp":
            source = "MCP"
            server = getattr(t, "_mcp_server", "") or name.split("_", 1)[0]
        elif getattr(t, "_tool_origin", None) == "plugin":
            source = "PLUGIN"
        spec = spec_for(name)
        catalog[name] = {
            "name": name,
            "source": source,
            "server": server,
            "domain": _MCP_SERVER_DOMAIN.get(server, "") if source == "MCP" else spec.category,
            "capability": capability_of(name),
            "side_effect": spec.side_effect,
            "idempotent": bool(spec.idempotent or getattr(t, "_mcp_idempotent", False)),
            "risk": spec.risk,
            "description": (getattr(t, "description", "") or "")[:200],
        }
    return catalog


def _detected_providers(text: str) -> set[str]:
    return {p for p, terms in _PROVIDER_TERMS.items() if any(t in text for t in terms)}


def _detected_domains(text: str) -> set[str]:
    return {d for d, pat in _DOMAIN_INTENT.items() if pat.search(text)}


def _select_mcp_tools(text: str, available: list[str], catalog: dict[str, dict] | None,
                      mentioned: set[str]) -> list[str]:
    """MCP domain approval：只有 domain/provider 命中才批准相关 MCP 工具。"""
    if not catalog:
        return []
    providers = _detected_providers(text)
    domains = _detected_domains(text)
    if not providers and not domains:
        return []
    from runtime.spec import capability_of

    out: list[str] = []
    for name in available:
        entry = catalog.get(name) or {}
        if entry.get("source") != "MCP":
            continue
        server = entry.get("server") or name.split("_", 1)[0]
        domain = entry.get("domain") or _MCP_SERVER_DOMAIN.get(server, "")
        provider_match = server in providers
        # provider identity：命中 source_control provider 时，排除同 domain 的其它 provider
        if providers and domain == "source_control" and not provider_match:
            continue
        if not provider_match and domain not in domains:
            continue
        cap = entry.get("capability") or capability_of(name)
        if cap == "MUTATION" and not (_MCP_MUTATION_TERMS.search(text) or name in mentioned):
            continue
        out.append(name)
    seen: set[str] = set()
    res: list[str] = []
    for n in out:
        if n not in seen:
            seen.add(n)
            res.append(n)
    # 相关性排序：与查询 token 命中越多越靠前（同分保持原顺序）
    res.sort(key=lambda n: -_mcp_relevance(n, text))
    return res[:_MCP_MAX_ADD]


def _intent_family_tools(text: str) -> set[str]:
    """按查询意图返回应补齐的工具族（替代无条件 rest，避免无关工具扩散）。"""
    family: set[str] = set()
    if _CODING_INTENT.search(text):
        family |= _CODING_SUPPORT
    if _WEB_INTENT.search(text):
        family |= _WEB_SUPPORT
    # C-fix：项目阅读/启动说明意图族（"看 README 怎么启动"）与纯算术算式族
    # （"帮我算一下 12*34"），命中即把对应工具进 family，消除误判 hit_zero。
    if _DOC_READ_INTENT.search(text):
        family |= _DOC_READ_SUPPORT
    if _ARITH_INTENT.search(text):
        family |= _ARITH_SUPPORT
    if _ANALYSIS_INTENT.search(text):
        family |= _ANALYSIS_SUPPORT
    # C-fix：裸 "write"（英文）+ 英文名词 → coding 族（"帮我 write 一个 quicksort"）
    if re.search(r"\bwrite\b.{0,16}(?:quicksort|sort|function|code|script|file)\b", text, re.IGNORECASE):
        family |= _CODING_SUPPORT
    # B-fix：memory/note 意图族（"帮我写一份周报""查看我的记忆"等）
    # 排除 PPT/汇报类查询（"帮我写个 PPT" 是 PPT 族意图，不是 memory 族）。
    if _MEMORY_NOTE_INTENT.search(text) and not _MEMORY_NOTE_EXCLUDE.search(text):
        family |= _MEMORY_NOTE_SUPPORT
    # PPT/汇报意图族：汇报材料常见交付形态是 PPT，gorden_ppt 族工具应随查询意图补齐，
    # 否则技能已就绪但工具不在本轮列表 → "Tool not found in agent" 调用失败。
    if _GORDEN_PPT_INTENT.search(text):
        family |= {
            "gorden_ppt_build", "gorden_ppt_templates",
            "gorden_ppt_apply_custom", "gorden_ppt_template_intro",
            "save_ppt_deck",
        }
    return family

DEFAULT_MAX_TOOLS = 16

# 记忆 / 笔记类工具：默认不常驻，只在明确语义下才暴露。
# save_note 尤其危险（持久写入），无保存意图时绝不暴露，避免模型空转发起无关写入。
_MEMORY_NOTE_TOOLS = {
    "save_note",
    "read_note",
    "list_notes",
    "remember",
    "recall_memory",
    "forget_memory",
}

# 高门槛工具：只在查询明确命中对应语义(TOOL_TERMS/mentioned/scored)时才允许补进，
# 避免普通查询因“补全”被塞进 fetch_github_repo / save_ppt_deck / deep_research 等重工具。
_EXPLICIT_ONLY_TOOLS = {
    "fetch_github_repo",
    "save_ppt_deck",
    "save_word_doc",
    "save_excel_workbook",
    "deep_research",
    "scan_dependencies",
    "code_loop",
    "sandbox_rollback",
    "sandbox_snapshot",
    "list_sandbox_snapshots",
}
# ⚠️ 刻意**不含**技能工具 gorden_ppt_*（四个），且 `_HIGH_GATE_TERMS` 里也不该有它们的词条。
# 它们靠下方 `_GORDEN_PPT_INTENT` 意图族主动补齐 —— 有真实故障背景：
# 任务 goal 没写"套模板/做PPT"这类精确词时，工具会被裁出 16 窗口，
# 模型随后按技能指令调用即报 "Tool ... not found in agent"。
# 把它们塞进本集合（或补回词条）会掐掉这条补齐路径，丢掉
# "简约商务总结汇报"这类自然口语 —— 见
# tests/test_tool_router.py::test_natural_ppt_phrasing_keeps_gorden_tools。
# 代价（已接受）：不含 PPT 字面词的纯文字请求（"帮我写个季度总结"）也会带上
# 这 4 个工具，实测工具数 4→8；换来的确定性更值钱。
_HIGH_GATE_TERMS = (
    ("fetch_github_repo", ("github", "仓库", "repo", "开源项目", "抓取", "克隆", "clone")),
    ("save_ppt_deck", ("ppt", "pptx", "幻灯片", "演示文稿", "课件")),
    ("save_word_doc", ("word", "docx", "word文档")),
    ("save_excel_workbook", ("excel", "工作簿", "xlsx", "表格文件保存")),
    ("deep_research", ("调研", "调研报告", "深度", "研究", "全面了解", "综述")),
    ("scan_dependencies", ("依赖", "requirements", "pyproject", "依赖体检", "依赖检查")),
    ("sandbox_rollback", ("回滚", "恢复沙箱", "撤销", "rollback")),
    ("sandbox_snapshot", ("快照", "备份沙箱", "savepoint")),
    ("list_sandbox_snapshots", ("查看快照", "快照列表")),
    ("code_loop", ("修复", "代码修复", "code_loop")),
    # 注：gorden_ppt_* 四个技能工具**刻意没有词条** —— 它们不走高门槛，只走
    # `_GORDEN_PPT_INTENT` 意图族补齐（理由见 `_EXPLICIT_ONLY_TOOLS` 后的注释）。
    # 别在此处"补全"：词条只在工具名同时出现在 `_EXPLICIT_ONLY_TOOLS` 里才会被求值，
    # 补在这里只是给一个永不求值的位置再添一条死数据（2026-09-19 踩过）。
)


def _high_gate_hit(tool: str, text: str) -> bool:
    for name, terms in _HIGH_GATE_TERMS:
        if name == tool:
            return any(t in text for t in terms)
    return True

# 记忆 / 笔记语义：命中才允许上面这些工具进入本轮（否则强制排除）。
# 注意：与第 168 行的 _MEMORY_NOTE_INTENT（"意图族补齐"用）不同，这里是"语义门"，
# 决定 _MEMORY_NOTE_TOOLS 是否被放行。重命名为 _MEMORY_NOTE_SEMANTIC 避免覆盖冲突。
_MEMORY_NOTE_SEMANTIC = re.compile(
    r"记住|记一下|保存|存成|存为|记录下来|写(?:一份|个|篇|到)|备忘|笔记|"
    r"偏好|习惯|长期记忆|别忘|我(?:之前|以前|上次).{0,6}(?:保存|写|记)|"
    r"回看(?:备忘|笔记)|有哪些备忘|我的(?:备忘|笔记|偏好|记忆)|"
    r"recall|remember|save.*note|list.*note|memory|note", re.IGNORECASE,
)

# 细分 memory intent：不同意图只暴露对应工具，避免"记住"→ 同时开放 recall/forget。
_SEARCH_SAVE_INTENT = re.compile(
    r"记住|记一下|以后(?:都|要|请)用|长期记住|别忘|保存|存成|存为|记录下来|"
    r"写(?:一份|个|篇|到|一个|一份|一个|一篇)|remember|save", re.IGNORECASE,
)
# 回忆/忘记语境：即使含"记住"也不是保存意图。
# B2 修复：原表把"写"也当成非保存语境（导致"帮我写一份周报"被拦），已移除。
_NON_SAVE_CTX = re.compile(
    r"我之前|以前|上次|前一天|忘掉|忘记|忘了|把.{0,6}忘了|别忘|回看|查一下|查查|"
    r"有哪些|回忆|回想|记得.{0,6}(?:让|叫)我|你不是|记得我|recall|list", re.IGNORECASE,
)


def _save_intent(text: str) -> bool:
    return bool(_SEARCH_SAVE_INTENT.search(text)) and not bool(_NON_SAVE_CTX.search(text))


#: B3 修复：原表漏了"查看/记忆/我的记忆/看记忆/翻记忆/找记忆/有没有记/有没记"等高频词，
#: 导致"查看我的记忆""看看我的记忆"不进 recall 族。
_RECALL_INTENT = re.compile(
    r"我之前|记得(?:我|你)?(?:让我|告诉|保存的)?|回忆|回想|查(?:一下|查)?(?:记忆|记录|笔记|备忘)|"
    r"看(?:下)?(?:我的)?(?:记忆|记录|笔记|备忘)|翻(?:我的)?(?:记忆|记录|笔记|备忘)|"
    r"找(?:我的)?(?:记忆|记录|笔记|备忘)|有没有(?:记|记录)|有没记|我的(?:记忆|记录|笔记|备忘)|"
    r"回看(?:备忘|笔记)|有哪些备忘|我之前让你记住|recall|之前.{0,6}(?:保存|记)",
    re.IGNORECASE,
)
_FORGET_INTENT = re.compile(
    r"忘掉|删除记忆|清除记忆|不要再记|移除记忆|取消记住|forget", re.IGNORECASE,
)
_NOTE_QUERY_INTENT = re.compile(
    r"有哪些备忘|笔记列表|列出|看(?:下)?(?:备忘|笔记)|list.*note|我的备忘",
    re.IGNORECASE,
)


def _memory_tool_allowed(tool: str, text: str) -> bool:
    """某 memory/note 工具是否因对应语义意图而允许进入本轮。"""
    if tool in {"remember", "recall_memory", "forget_memory"}:
        from runtime.memory_policy import memory_enabled

        if not memory_enabled():
            return False
    if tool == "remember":
        return _save_intent(text)
    if tool == "save_note":
        return _save_intent(text)
    if tool == "recall_memory":
        return bool(_RECALL_INTENT.search(text))
    if tool == "read_note":
        return bool(_RECALL_INTENT.search(text) or _NOTE_QUERY_INTENT.search(text))
    if tool == "list_notes":
        return bool(_NOTE_QUERY_INTENT.search(text))
    if tool == "forget_memory":
        return bool(_FORGET_INTENT.search(text))
    return True

# 用户直接问“能用什么/有哪些能力/MCP/技能”这类能力盘点问题时，
# 默认探索类工具（列目录/翻笔记/搜索文档）只会诱导模型去翻文件找“能力清单”。
_CAPABILITY_INTENT = re.compile(
    r"mcp|技能|能力|功能|工具(?:列表|清单)?|能用|会用|可用|你会|能做|能做什么|"
    r"有哪些|capabil|tools?",
    re.IGNORECASE,
)

# 能力盘点时默认不派发的“探索类”工具：会诱导模型翻文件/翻笔记去找能力清单。
_CAPABILITY_EXPLORE_TOOLS = {
    "list_workspace_files",
    "read_workspace_file",
    "list_notes",
    "read_note",
    "search_documents",
    "index_workspace",
}

# 纯文本处理（改写/翻译/润色/重写）——不需要任何工具，直接回答。
_DIRECT_TEXT_INTENT = re.compile(
    r"改写|润色|重写|改得.{0,6}正式|换个说法|措辞|翻译成|翻译(?:一下|下|这段|这句|成|这份|成文字)|翻成|"
    r"polish|rephrase|rewrite|translate",
    re.IGNORECASE,
)
_SKILL_ACTION_INTENT = re.compile(
    r"加载.{0,12}(?:技能|skill)|刷新.{0,12}(?:技能|skill)|重新扫描.{0,12}(?:技能|skill)|"
    r"使用.{0,12}(?:技能|skill)|"
    r"(?:技能|skill).{0,16}(?:完成|处理|撰写|生成|制作|翻译)|"
    r"(?:品牌规范|品牌风格|品牌色|小说创作|小说|短篇拆解|长篇拆解|"
    r"周报|周总结|依赖体检|依赖检查|requirements|文章|文案|创作|"
    r"网页设计|网页改版|日计划)",
    re.IGNORECASE,
)
# 需要工具的强信号词：命中则不进入纯文本直答路径。
# B4 修复：原表把"查"单独当成强信号（"帮我翻译这段话"里"查"被误命中 → 误判需工具），
# 改成"查一下/查查/查询"等动词短语；同时"文件/项目/代码/仓库/目录/文档/表格"等
# 强信号保留（这些词确实需要工具）。
_TOOL_NEEDED = re.compile(
    r"项目|代码|仓库|目录|文档|表格|"
    r"查一下|查查|查询|搜索|搜一下|读一下|读|打开|保存|存成|导出|生成|运行|测试|修复|"
    r"部署|发送|邮件|github|pdf|excel|word|ppt|网页|联网|新闻|天气|图片|截图|记忆|笔记|定时|提醒",
    re.IGNORECASE,
)
# 只读意图（不要修改/只看/只分析）——不暴露写入类工具。
# C-fix：「只读模式」用短语级匹配（"只读模式"/"只读状态下"），避免裸"只读"误吞
# "删除只读文件""修改只读配置"等含只读属性的写意图；裸"只读"保留（"只读"单独出现
# 通常就是只读语境）。
_READONLY_INTENT = re.compile(
    r"不要修改|先不要改|别改|先别改|不要动|只看|只分析|只检查|只读(?!文件|配置|属性)|"
    r"仅分析|仅检查|read.?only",
    re.IGNORECASE,
)
_WRITE_TOOLS_SET = {
    "write_project_file", "edit_project_file", "write_code_file", "save_note",
    "save_ppt_deck", "save_word_doc", "save_excel_workbook", "sandbox_rollback",
    # 只读意图下执行类工具同样不暴露（避免“只看不改”任务去跑代码/循环）。
    "run_python", "code_loop",
}


def is_direct_text_task(query: str) -> bool:
    """纯文本改写/翻译/润色类请求 → 无需工具。

    扩展：纯概念问答（"Agent 和普通聊天模型有什么区别？""什么是 RAG"）也走
    direct_text，不配工具。必须叠加 _TOOL_NEEDED 排除（含强工具信号的不算）。
    """
    t = (query or "")
    if bool(_DIRECT_TEXT_INTENT.search(t)) and not bool(_TOOL_NEEDED.search(t)):
        return True
    # 纯概念问答：命中概念问句且不含任何工具强信号
    if _CONCEPT_QA_INTENT.search(t) and not _TOOL_NEEDED.search(t):
        return True
    return False


def _classify_intent(text: str, raw_query: str) -> str:
    """把查询归入 6 类意图，用于 hit_zero 口径收敛（修复 C）。

    返回值：
      'tool_needed'  命中 _TOOL_NEEDED 强信号 → 有工具意图，0 工具 = 真 hit_zero
      'greeting'     命中纯问候/闲聊且无强信号 → 不计 hit_zero
      'concept_qa'   命中概念问句且无强信号 → 直接回答，不计 hit_zero
      'arith'        命中算术算式 → calculate/read_spreadsheet 族，不计 hit_zero
      'direct_text'  命中改写/翻译/润色 或 is_direct_text_task → 不计 hit_zero
      'ambiguous'    无任何信号 → 保守计 hit_zero（保持旧口径兜底）

    设计原则：hit_zero 只在"有工具意图却 0 工具"时才是真风险；
    纯文本/问候/概念问答被记 hit_zero 是假阳性（制造"新问题"假象）。
    """
    t = (text or "").lower()
    if _TOOL_NEEDED.search(t):
        return "tool_needed"
    # 问候/闲聊优先于概念问答（"介绍你自己"既可能命中 concept_qa 也可能是 greeting，
    # 问候族更精确，放在前面）。
    if _GREETING_INTENT.search(t) and not _TOOL_NEEDED.search(t):
        return "greeting"
    if _CONCEPT_QA_INTENT.search(t):
        return "concept_qa"
    if _ARITH_INTENT.search(t):
        return "arith"
    if _DIRECT_TEXT_INTENT.search(t) or is_direct_text_task(raw_query):
        return "direct_text"
    return "ambiguous"


def laya_screen_skip(query: str) -> bool:
    """跳过 Laya 提示推理的性能护栏：True = 直接走关键词 Router。

    背景（2026-09-26）：训练侧做过 arith→text 口径翻转（"帮我算 12*34" 判纯文本、
    不需要工具，这是对的），但模型把该口径**外推**到了混合句 ——
    "帮我算 123*456 然后保存成备忘录" 只看到"算"就被判 text，导致 Layer 提示
    与实际工具需求不一致。

    护栏语义：明确工具意图时跳过不必要的本地分类调用。即使不跳过，Laya 结果也
    只是 trace 中的 hint，工具筛选仍由 select_tool_names 决定。
    """
    if not query:
        return False
    return bool(_WEB_INTENT.search(query) or _TOOL_NEEDED.search(query))


def router_enabled() -> bool:
    """Router 是否启用（TOOL_ROUTER=off 关闭）。

    逃生门：TOOL_ROUTER_FAST=on 命中时，select_tool_names 直接返回全量工具，
    跳过意图识别/裁剪——用于 Router 改坏后快速全量恢复，或复杂跨族任务。
    """
    fast = os.getenv("TOOL_ROUTER_FAST", "").strip().lower() in ("on", "true", "1")
    if fast:
        return False  # fast 模式 = 全量工具，等效关闭 router 选择
    return os.getenv("TOOL_ROUTER", "").strip().lower() not in ("off", "false", "0")


def router_fast_mode() -> bool:
    """逃生门状态查询：TOOL_ROUTER_FAST=on 时返回 True。"""
    return os.getenv("TOOL_ROUTER_FAST", "").strip().lower() in ("on", "true", "1")


def select_tool_names(
    query: str,
    available: list[str],
    max_tools: int = DEFAULT_MAX_TOOLS,
    external: list[str] | None = None,
    catalog: dict[str, dict] | None = None,
    plan_required_tools: list[str] | None = None,
) -> list[str]:
    """按查询从可用工具里选出子集；顺序稳定（基础集优先，其次按命中数降序）。

    ``plan_required_tools`` 由 Runtime Plan Controller 提供。Router 只负责验证
    候选是否已注册、是否符合当前意图策略，并在数量上限内保留它们；不识别任务阶段。
    逃生门：TOOL_ROUTER_FAST=on 时直接返回全量（不裁剪），日志里标 fast_mode。
    """
    _t0 = time.time()
    if not router_enabled():
        # fast 模式下仍写日志，标记 fast_mode=True 以便区分"正常全量"和"逃生门全量"。
        if router_fast_mode():
            _write_tool_router_log(
                query, list(available), len(available), len(available),
                hit_zero=False, reasons=["fast_mode"],
            )
            record_router_call((time.time() - _t0) * 1000.0,
                               hit_zero=False, fast_mode=True)
        return list(available)
    available_set = set(available)
    base = [name for name in available if name in BASE_TOOLS]
    text = (query or "").lower()
    skill_manager_ready = False
    if "extension_manager" in available_set:
        try:
            from skills_loader import enabled_names

            skills_root = Path(__file__).resolve().parents[1] / "skills"
            skill_manager_ready = any(
                ((skills_root / name / "skill.md").is_file()
                 and not (skills_root / name / "skill.md").is_symlink())
                or ((skills_root / name / "SKILL.md").is_file()
                    and not (skills_root / name / "SKILL.md").is_symlink())
                for name in enabled_names()
            )
        except Exception:
            skill_manager_ready = False
    # 纯文本改写/翻译仍不带业务工具；启用了本地 Skill 时，只额外提供只读的
    # Skill 按需加载入口，让模型在确有匹配技能时读取完整指引。
    if is_direct_text_task(query) and not plan_required_tools:
        selected = ["extension_manager"] if (
            skill_manager_ready and _DIRECT_TEXT_INTENT.search(query or "")
        ) else []
        record_router_call((time.time() - _t0) * 1000.0,
                           hit_zero=not selected, empty=not selected)
        return selected
    capability = bool(_CAPABILITY_INTENT.search(text))

    # 能力盘点问题：每台外部（MCP）服务器取一个代表工具，让模型直接回答“接入哪些服务”。
    reps: list[str] = []
    ext = set(external or [])
    if capability and ext:
        seen: set[str] = set()
        for name in available:
            if name not in ext or name not in available_set:
                continue
            prefix = name.split("_", 1)[0]
            if prefix and prefix not in seen:
                seen.add(prefix)
                reps.append(name)

    mentioned: set[str] = set()
    for name in available:
        if re.search(r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])", text):
            mentioned.add(name)

    # Eligibility：无记忆/笔记语义时，绝不暴露记忆/笔记类工具；有语义时按
    # 意图分工具允许（如“记住”→ 仅 remember/save，不开放 recall/forget）。
    def _allowed(name: str) -> bool:
        if name in _MEMORY_NOTE_TOOLS:
            return _memory_tool_allowed(name, text)
        if name in _EXPLICIT_ONLY_TOOLS:
            # 高门槛重工具只在语义命中时允许（rest 补全不盲目带进来）
            return name in mentioned or _high_gate_hit(name, text)
        return True

    available_set = {n for n in available_set if _allowed(n)}
    mentioned = {n for n in mentioned if _allowed(n)}

    # Runtime Plan Controller supplies explicit stage requirements. Router owns
    # only candidate exposure and applies its normal availability/intent policy.
    required_action_tools = [
        name for name in dict.fromkeys(plan_required_tools or [])
        if name in available_set and _allowed(name)
    ]
    # Only route the loader for recognizable Skill domains or an explicit Skill
    # request; routine web/math/file tasks should not pay for unrelated metadata.
    skill_relevant_request = bool(_SKILL_ACTION_INTENT.search(text))
    if (skill_manager_ready and not capability and skill_relevant_request
            and "extension_manager" not in required_action_tools):
        required_action_tools.insert(0, "extension_manager")
    # Skill-owned trigger descriptions supplement the hand-maintained tool alias
    # table. Only add tools from enabled Skills whose name or trigger text matches
    # this Skill-relevant request; unrelated Skill tools remain out of the turn.
    if skill_manager_ready and not capability and skill_relevant_request:
        try:
            from skills_loader import skill_catalog

            skill_stop_cjk = {
                "技能", "用户", "使用", "需要", "可以", "帮助", "任务", "处理",
                "生成", "完成", "进行", "相关", "内容", "提供", "支持", "当前",
                "通过", "要求", "触发", "说明", "工具", "本技", "技能",
            }
            query_cjk = {
                text[i:i + 2] for i in range(max(0, len(text) - 1))
                if "\u4e00" <= text[i] <= "\u9fff"
                and "\u4e00" <= text[i + 1] <= "\u9fff"
                and text[i:i + 2] not in skill_stop_cjk
            }
            skill_tools: list[str] = []
            for skill in skill_catalog():
                if not skill.get("enabled"):
                    continue
                skill_name = str(skill.get("name", "")).casefold()
                name_terms = {part for part in re.split(r"[_-]+", skill_name) if len(part) >= 3}
                explicit_name = bool(skill_name and skill_name in text) or any(
                    term in text for term in name_terms
                )
                description = str(skill.get("description", "")).casefold()
                description_cjk = {
                    description[i:i + 2]
                    for i in range(max(0, len(description) - 1))
                    if "\u4e00" <= description[i] <= "\u9fff"
                    and "\u4e00" <= description[i + 1] <= "\u9fff"
                    and description[i:i + 2] not in skill_stop_cjk
                }
                description_words = set(re.findall(r"[a-z][a-z0-9_+-]{2,}", description))
                query_words = set(re.findall(r"[a-z][a-z0-9_+-]{2,}", text))
                if explicit_name or len(query_cjk & description_cjk) >= 1 or query_words & description_words:
                    skill_tools.extend(
                        name for name in skill.get("tool_names", [])
                        if name in available_set and name not in skill_tools
                    )
            for name in skill_tools:
                if name not in required_action_tools:
                    required_action_tools.append(name)
        except Exception:
            pass

    scored: dict[str, int] = {}
    for name, terms in TOOL_TERMS.items():
        if name not in available_set:
            continue
        hits = sum(1 for term in terms if term.lower() in text)
        if hits:
            scored[name] = hits

    # Recall guard for tools added after the hand-written alias table was last
    # updated. Match explicit tool IDs/display labels, then require multiple
    # informative description overlaps before adding an otherwise unknown tool.
    if catalog:
        try:
            from runtime.capability_introspection import display_for
            _stops = {"请问", "帮我", "一下", "这个", "那个", "可以", "能够", "工具", "用于", "功能", "进行", "通过", "当前"}
            _query_words = set(re.findall(r"[a-z][a-z0-9_+-]{2,}", text))
            _query_words.update(
                text[i:i + 2] for i in range(max(0, len(text) - 1))
                if "\u4e00" <= text[i] <= "\u9fff" and "\u4e00" <= text[i + 1] <= "\u9fff"
                and text[i:i + 2] not in _stops
            )
            for name in available_set:
                if name in scored or not _allowed(name):
                    continue
                label = display_for(name).lower()
                if re.search(r"(?<![a-z0-9_])" + re.escape(name.lower()) + r"(?![a-z0-9_])", text) or (label and label in text):
                    scored[name] = 1
                    continue
                entry = catalog.get(name) or {}
                desc = (str(entry.get("description", "")) + " " + str(entry.get("domain", ""))).lower()
                _desc_words = set(re.findall(r"[a-z][a-z0-9_+-]{2,}", desc))
                _desc_words.update(desc[i:i + 2] for i in range(max(0, len(desc) - 1))
                                   if "\u4e00" <= desc[i] <= "\u9fff" and "\u4e00" <= desc[i + 1] <= "\u9fff"
                                   and desc[i:i + 2] not in _stops)
                overlap = len(_query_words & _desc_words)
                # CJK descriptions need two distinct matches to avoid broad,
                # generic tool summaries inflating unrelated routes.
                if overlap >= 2:
                    scored[name] = overlap
        except Exception:
            pass

    ordered = list(available)
    priority = {name: idx for idx, name in enumerate(ordered)}
    matched = sorted(
        (mentioned | set(scored)),
        key=lambda name: (-scored.get(name, 0), priority.get(name, 10**9)),
    )
    result = []
    if not capability:
        # 普通请求：常驻基础集（通用能力）
        base_pool = base
    else:
        # 能力盘点：避免列目录/翻笔记/搜文档等探索工具诱导循环，只保留被点名/命中者
        base_pool = [n for n in base if n in mentioned or n in scored]
    for name in base_pool + required_action_tools + reps + matched:
        if name in available_set and name not in result:
            result.append(name)
    # 补充不在任何匹配里的工具名（点名过的已含）
    if len(result) < len(available_set):
        if capability:
            rest = [
                name for name in ordered
                if name not in result
                and (name not in _CAPABILITY_EXPLORE_TOOLS
                     or name in mentioned or name in scored)
                and _allowed(name)
            ]
        else:
            # 只补“查询意图族”内的工具；不再无条件把剩余工具塞满 16 个窗口。
            family = _intent_family_tools(text)
            rest = [name for name in ordered
                    if name not in result and _allowed(name) and name in family]
        result.extend(rest)
    # Phase 22：MCP/Plugin domain approval（仅在提供 catalog 时；能力盘点走上方 reps 路径）。
    if catalog is not None and not capability:
        providers = _detected_providers(text)
        # provider identity：命中非 github 的 source_control provider 时，不得替换为 github 工具
        if providers and "github" not in providers:
            result = [n for n in result if n != "fetch_github_repo"]
        for n in _select_mcp_tools(text, ordered, catalog, mentioned):
            if n in available_set and n not in result:
                result.append(n)
        # AnySearch is the sole ordinary web-search provider. Keep only the
        # endpoint selected by the Runtime task plan in this turn's tool set.
        anysearch_tools = [
            name for name in ordered
            if (catalog.get(name, {}).get("source") == "MCP"
                and str(catalog.get(name, {}).get("server") or "").lower() == "anysearch"
                and "search" in name.lower()
                and "extract" not in name.lower())
        ]
        is_weather_lookup = bool(re.search(
            r"天气|气温|温度|下雨|下雪|雾霾|紫外线|风力|湿度|穿什么|冷不冷|热不热|weather|forecast",
            query or "", re.IGNORECASE,
        ))
        if anysearch_tools and _WEB_INTENT.search(query or ""):
            result = [name for name in result if name not in anysearch_tools]
            # Weather has a dedicated provider. Only route weather questions to
            # AnySearch when the user explicitly asks to search the web.
            if not is_weather_lookup or re.search(r"联网|网上|网页|搜索|search", query or "", re.I):
                planned_search = [
                    name for name in required_action_tools if name in anysearch_tools
                ]
                # Keep one concrete provider endpoint in the model's tool set;
                # otherwise search and batch_search can compete with each other
                # even though the Runtime plan already selected one.
                chosen_search = planned_search[:1] or [
                    next((name for name in anysearch_tools
                          if name.lower().endswith("_search")
                          and not name.lower().endswith("_batch_search")),
                         anysearch_tools[0])
                ]
                result.extend(name for name in chosen_search if name not in result)
    # 只读意图：从结果中剔除写入类工具（第二层由 intent gate / constraint 兜底）。
    if _READONLY_INTENT.search(text):
        result = [n for n in result if n not in _WRITE_TOOLS_SET]
    # Required stages are atomic for routing: preserve a later write/reminder
    # stage even when the normal relevance window is full.
    limit = max(len(base), len(required_action_tools), min(int(max_tools), len(available_set)))
    result = result[:limit]
    # 修复 C：hit_zero 口径收敛 —— 仅当"有工具意图"（tool_needed / ambiguous）
    # 且 0 个非基础工具时才计 hit_zero；纯文本/概念/算式/问候不计（消除假阳性）。
    _intent_class = _classify_intent(text, query)
    _non_base_hits = [n for n in result if n not in BASE_TOOLS]
    _hit_zero = (_intent_class in ("tool_needed", "ambiguous")) and (len(_non_base_hits) == 0)
    # C1 可观测：写一行 JSON 到 var/logs/tool_router.jsonl（含 intent_class 便于分桶）
    _write_tool_router_log(
        query=query,
        selected=result,
        total_available=len(available),
        selected_count=len(result),
        hit_zero=_hit_zero,
        intent_class=_intent_class,
    )
    # C1 指标：记录本次调用延迟；hit_zero 口径同上
    record_router_call(
        (time.time() - _t0) * 1000.0,
        hit_zero=_hit_zero,
        intent_class=_intent_class,
    )
    return result
