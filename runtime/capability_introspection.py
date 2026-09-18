# -*- coding: utf-8 -*-
"""Capability Introspection：Agent 自身能力的“事实查询”层（轻量、只读）。

目标：用户问“你能做什么 / 有哪些 MCP / 有哪些技能 / 能用哪些工具”时，
答案必须来自 Runtime 真实状态，而不是模型凭工具名/历史对话推断。

实现原则：
- 不新建第二套 Tool Registry；直接读主 Agent 当前真实挂载的 tools。
- 来源（origin）来自真实注册标记：_mcp_source="mcp" / _tool_origin="plugin"，
  其余为核心内置（builtin）；不按工具名猜。
- 不返回 API Key / 内部 URL / 路径 / 配置原文等敏感信息。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from typing import Any

# ---------------------------------------------------------------------------
# 展示名（用户友好）与 MCP 服务器展示名：按真实 tool_id / server_id 登记
# ---------------------------------------------------------------------------

DISPLAY_NAMES: dict[str, str] = {
    "web_search": "联网搜索",
    "deep_research": "深度调研",
    "get_current_datetime": "获取当前时间",
    "calculate": "数学计算",
    "read_note": "查看笔记",
    "list_notes": "列出笔记",
    "save_note": "保存笔记",
    "read_workspace_file": "查看项目文件",
    "list_workspace_files": "浏览项目文件",
    "read_code_file": "查看代码",
    "list_code_files": "浏览代码",
    "write_code_file": "编写代码",
    "run_python": "运行代码",
    "code_loop": "自动修复并验证",
    "remember": "记住偏好",
    "recall_memory": "回忆背景",
    "forget_memory": "忘记记忆",
    "index_workspace": "整理文件索引",
    "search_documents": "搜索资料",
    "ask_image": "看图问答",
    "read_office_file": "读取文档",
    "read_spreadsheet": "读取表格",
    "save_word_doc": "生成 Word",
    "save_excel_workbook": "生成 Excel",
    "save_ppt_deck": "生成 PPT",
    "fetch_github_repo": "抓取 GitHub 仓库",
    "schedule_add": "新建定时任务",
    "schedule_list": "查看定时任务",
    "schedule_remove": "删除定时任务",
    "schedule_set_enabled": "启停定时任务",
    "sandbox_snapshot": "保存检查点",
    "sandbox_rollback": "恢复检查点",
    "list_sandbox_snapshots": "查看检查点",
    "write_project_file": "修改项目文件",
    "edit_project_file": "编辑项目文件",
    "scan_dependencies": "依赖体检",
    # 本地技能（plugin）
    "gorden_ppt_templates": "PPT 模板",
    "gorden_ppt_template_intro": "PPT 模板说明",
    "gorden_ppt_build": "PPT 制作",
    "gorden_ppt_apply_custom": "自定义 PPT 模板",
    # 常见 MCP 工具（真实 tool_id 登记，非猜名）
    "fetch_fetch": "网页内容获取",
    "youtube_get-transcript": "YouTube 字幕读取",
    "youtube_get-transcript-languages": "YouTube 字幕语言",
    "obsidian_obsidian_list_vaults": "笔记库概览",
    "obsidian_obsidian_read_note": "读取笔记",
    "obsidian_obsidian_create_note": "新建笔记",
    "obsidian_obsidian_search_vault": "搜索笔记",
    "sqlite_list_tables": "查看数据表",
    "sqlite_read_query": "查询数据",
    "sqlite_write_query": "写入数据",
    "gitee_list_user_repos": "查看 Gitee 仓库",
    "gitee_create_issue": "创建 Gitee Issue",
    "playwright_browser_navigate": "打开网页",
    "playwright_browser_snapshot": "读取网页内容",
    "chrome_navigate_page": "打开网页（Chrome）",
    "chrome_take_snapshot": "读取网页快照",
}

SERVER_DISPLAYS: dict[str, str] = {
    "gitee": "Gitee 代码托管",
    "playwright": "浏览器自动化",
    "obsidian": "Obsidian 笔记",
    "sqlite": "本地数据库",
    "chrome": "Chrome 浏览器",
    "fetch": "网页抓取",
    "youtube": "YouTube 字幕",
}

SERVER_DESCRIPTIONS: dict[str, str] = {
    "gitee": "读取与维护 Gitee 仓库/Issue/PR",
    "playwright": "打开网页、读取页面、模拟浏览器操作",
    "obsidian": "读取/搜索/整理本地 Obsidian 笔记",
    "sqlite": "查询与写入本地 SQLite 数据库",
    "chrome": "用 Chrome 内核查看网页与调试页面",
    "fetch": "抓取并阅读网页内容",
    "youtube": "读取公开 YouTube 视频字幕",
}

ORIGIN_LABELS = {
    "native": "builtin",
    "mcp": "mcp",
    "skill": "plugin",
    "agent": "local",
}


def origin_of(fn_tool: Any) -> str:
    """真实来源：只看注册标记，不看工具名/类型名。"""
    if getattr(fn_tool, "_mcp_source", None) == "mcp":
        return "mcp"
    if getattr(fn_tool, "_tool_origin", None) == "plugin":
        return "plugin"
    return "builtin"


def display_for(tool_id: str, fallback: str = "") -> str:
    return DISPLAY_NAMES.get(tool_id) or (fallback or tool_id)


def server_display(server_id: str) -> str:
    return SERVER_DISPLAYS.get(server_id, server_id)


# ---------------------------------------------------------------------------
# 能力查询意图（最小检测，不做 Intent Engine）
# ---------------------------------------------------------------------------

_CAPABILITY_RE = re.compile(
    r"mcp|技能|能力|你能做什么|你会(?:什么|哪些)|能用|可用|可以帮你|"
    r"有哪些(?:工具|能力|功能|技能)|(?:工具|能力|插件).{0,4}(?:列表|清单|名称|名字|技术)|"
    r"工具(?:名称|名字|列表|清单|id|ID)|技术名称|把.{0,20}(?:列出来|展示)|"
    r"能(?:联网|搜索|上网|生成|制作|读|处理|修改|操作|做|调用|连)|"
    r"插件|capabil|integration|connected servers?",
    re.IGNORECASE,
)

_HISTORY_RE = re.compile(
    r"最近成功|实际成功(?:用过|调用)?|历史(?:成功|使用|记录)|成功(?:使用|调用)过|用过哪些|"
    r"previously|recent success|ever succeeded",
    re.IGNORECASE,
)


def looks_like_capability_query(text: str) -> bool:
    return bool(text) and bool(_CAPABILITY_RE.search(text))


def looks_like_history_query(text: str) -> bool:
    return bool(text) and bool(_HISTORY_RE.search(text))


# ---------------------------------------------------------------------------
# 真实状态读取
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def collect_tool_entries(agent: Any | None = None) -> list[dict]:
    """读取主 Agent 当前真实挂载的工具（只含 enabled 的已挂载工具）。"""
    if agent is None:
        try:
            from agent import assistant_agent

            agent = assistant_agent
        except Exception:
            return []
    entries: list[dict] = []
    for fn_tool in getattr(agent, "tools", []) or []:
        tool_id = str(getattr(fn_tool, "name", "") or "")
        if not tool_id:
            continue
        origin = origin_of(fn_tool)
        enabled = bool(getattr(fn_tool, "_enabled", True))
        entry: dict[str, Any] = {
            "tool_id": tool_id,
            "display_name": display_for(tool_id),
            "description": (getattr(fn_tool, "description", "") or "")[:300],
            "origin": origin,
            "enabled": enabled,   # 配置允许参与运行
            "available": enabled,  # 默认当前可调用
        }
        if origin == "mcp":
            entry["server_id"] = getattr(fn_tool, "_mcp_server", None) or ""
            entry["server_name"] = server_display(
                entry["server_id"] or tool_id.split("_", 1)[0]
            )
            entry["connected"] = _server_connected(entry["server_id"])
            entry["policy"] = getattr(fn_tool, "_mcp_policy", None) or "allow"
            if not entry["connected"]:
                entry["available"] = False
        entries.append(entry)
    return entries


def _server_connected(server_id: str) -> bool:
    if not server_id:
        return False
    try:
        import mcp_bridge

        return server_id in set(mcp_bridge._connected_names)
    except Exception:
        return False


def connected_mcp_servers(entries: list[dict] | None = None) -> list[dict]:
    if entries is None:
        entries = collect_tool_entries()
    seen: dict[str, dict] = {}
    for e in entries:
        if e.get("origin") != "mcp":
            continue
        sid = e.get("server_id") or ""
        if not sid:
            continue
        seen.setdefault(sid, {
            "server_id": sid,
            "server_name": server_display(sid),
            "description": SERVER_DESCRIPTIONS.get(sid, ""),
            "connected": bool(e.get("connected")),
            "tool_count": 0,
        })
        seen[sid]["tool_count"] += 1
    return list(seen.values())


def history_success_summary(limit: int = 15) -> list[dict]:
    """历史真实成功记录（仅查询成功状态，不读聊天内容）。"""
    try:
        from runtime.task_manager import DEFAULT_DB_PATH

        path = DEFAULT_DB_PATH
        if not path.exists():
            return []
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                """
                SELECT tool_name, COUNT(*) AS n, MAX(created_at) AS last_success
                FROM tool_calls
                WHERE status = 'succeeded'
                GROUP BY tool_name
                ORDER BY last_success DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            con.close()
        return [
            {
                "tool_id": r[0],
                "display_name": display_for(r[0]),
                "success_count": r[1],
                "last_success": r[2],
            }
            for r in rows
        ]
    except Exception:
        return []


def capability_snapshot(
    agent: Any | None = None,
    *,
    origin: str | None = None,
    available_only: bool = True,
    include_history: bool = False,
) -> dict:
    """轻量结构化能力快照（真实状态；不暴露密钥/路径）。"""
    entries = collect_tool_entries(agent)
    if origin:
        entries = [e for e in entries if e.get("origin") == origin]
    if available_only:
        entries = [e for e in entries if e.get("enabled") and e.get("available")]
    snapshot: dict[str, Any] = {
        "tools": entries,
        "mcp_servers": connected_mcp_servers(entries),
        "generated_at": _now_iso(),
        "runtime_state": "ok",
    }
    if include_history:
        snapshot["previously_succeeded"] = history_success_summary()
    return snapshot


# ---------------------------------------------------------------------------
# 注入给模型的只读事实文本（无密钥、无内部 URL/路径）
# ---------------------------------------------------------------------------


def capability_context_block(message: str = "", agent: Any | None = None) -> str:
    """按问题类型生成最小能力事实块；只含普通用户可见信息。"""
    if not (looks_like_capability_query(message)
            or looks_like_history_query(message)):
        return ""  # 非能力问题不注入，保持普通执行零差异
    try:
        snapshot = capability_snapshot(
            agent, include_history=looks_like_history_query(message)
        )
    except Exception:
        return (
            "\n【当前能力状态】我暂时无法读取 Runtime 能力状态，"
            "因此不能确认当前有哪些工具/扩展可用（无法查询 ≠ 没有）。\n"
        )
    if not snapshot.get("tools") and not snapshot.get("mcp_servers"):
        return (
            "\n【当前能力状态】当前没有可报告的已挂载能力；"
            "如状态可恢复请重试，不能把‘未知’说成‘没有’。\n"
        )

    lines: list[str] = ["\n【当前能力状态（来自 Runtime，非历史推断）】"]
    mcp = snapshot.get("mcp_servers") or []
    if mcp:
        lines.append("当前已连接的 MCP 扩展：")
        for s in mcp:
            state = "已连接" if s.get("connected") else "未连接"
            lines.append(f"- {s.get('server_name')}（{state}）"
                         f"{('：' + s.get('description', '')) if s.get('description') else ''}")
    else:
        lines.append("当前没有已连接的 MCP 扩展（不要声称有）。")

    by_origin: dict[str, list[dict]] = {}
    for e in snapshot.get("tools", []):
        by_origin.setdefault(e.get("origin", "builtin"), []).append(e)
    label = {"builtin": "内置能力", "plugin": "本地技能/插件", "local": "本地工具",
             "mcp": "MCP 工具"}.get
    want_ids = bool(re.search(r"技术名称|工具名|tool_id|内部(?:名称|id)|把.*列出来", message or ""))
    for origin_key in ("builtin", "plugin", "local", "mcp"):
        group = by_origin.get(origin_key)
        if not group:
            continue
        if origin_key == "mcp" and not want_ids:
            continue  # MCP 以服务器摘要展示，不逐工具展开
        lines.append(f"{label(origin_key)}：")
        for e in group:
            note = f"（{e.get('server_name')}）" if e.get("server_name") else ""
            shown = e.get("display_name")
            if want_ids:
                shown = f"{shown} [{e.get('tool_id')}]"
            lines.append(f"- {shown}{note}")

    if snapshot.get("previously_succeeded"):
        lines.append("历史真实成功记录（仅查询到你要求时展示）：")
        for h in snapshot["previously_succeeded"]:
            lines.append(f"- {h.get('display_name')} [{h.get('tool_id')}]"
                         f" 成功 {h.get('success_count')} 次")

    lines.append(
        "回答约束：只依据上面的当前状态回答；不要根据旧对话/工具名推断可用性；"
        "默认展示左侧友好名称，用户要技术名时才同时给方括号内的 tool_id；"
        "未连接/未知不能写成可用；"
        "描述能力时用“可以/支持”等现在式，不要使用“已生成/已保存/已产出/已完成”"
        "这类过去式完成措辞（本轮没有执行任何操作）；"
        "为回答本问题不要调用任何工具（含回忆/时间/搜索/列文件）——直接用上面的清单作答。"
    )
    return "\n".join(lines)


def snapshot_json() -> str:
    """给内部工具/API 用的 JSON（同样无敏感字段）。"""
    return json.dumps(capability_snapshot(), ensure_ascii=False)
