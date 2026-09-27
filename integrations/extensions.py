import os
from typing import Literal

from agents import function_tool

from skills_loader import load_skill_text


@function_tool
def extension_manager(action: Literal["list", "load_skill"], name: str = "") -> str:
    """查看已安装 Skill/MCP 扩展状态，或加载某个已启用 Skill 的完整指引。

    action=list 返回真实 Runtime 已挂载工具、Skill 启用情况及 MCP 连接状态；
    action=load_skill 且提供 name 时，读取该 Skill 的指令正文供当前任务使用。
    此工具不安装扩展、不修改配置、不执行 Skill 脚本。
    """
    import json

    if action == "load_skill":
        if not name.strip():
            return "请提供要加载的 Skill 名称。可先用 action='list' 查看清单。"
        return load_skill_text(name.strip())

    from skills_loader import skill_catalog
    from runtime.capability_introspection import capability_snapshot

    skills = skill_catalog()
    try:
        snapshot = capability_snapshot(available_only=False)
        entries = snapshot.get("tools", [])
        plugin_entries = [entry for entry in entries if entry.get("origin") == "plugin"]
        mcp_entries = [entry for entry in entries if entry.get("origin") == "mcp"]
        mcp_servers = snapshot.get("mcp_servers", [])
    except Exception as exc:
        plugin_entries, mcp_entries, mcp_servers = [], [], []
        runtime_state = f"unavailable:{type(exc).__name__}"
    else:
        runtime_state = "ok"

    known_skill_tools = {
        tool_name for skill in skills for tool_name in skill["tool_names"]
    }
    registered_plugin_tools = {entry["tool_id"] for entry in plugin_entries}
    for skill in skills:
        skill["registered_tools"] = [
            tool_name for tool_name in skill["tool_names"]
            if tool_name in registered_plugin_tools
        ]
        if not skill["enabled"]:
            skill["status"] = "disabled"
        elif skill["has_tools"] and len(skill["registered_tools"]) < len(skill["tool_names"]):
            skill["status"] = "enabled_tools_not_all_registered"
        else:
            skill["status"] = "enabled"
    # Include configured servers that failed to connect or have no exposed tools.
    # parse_specs also returns command/env values; this view intentionally discards them.
    try:
        from integrations import mcp_bridge

        configured, _errors = mcp_bridge.parse_specs(os.getenv(mcp_bridge.ENV_KEY, ""))
        allowed, _allowlist_reasons = mcp_bridge.filter_allowlist(configured)
        allowed_names = {spec["name"] for spec in allowed}
        connected_names = set(getattr(mcp_bridge, "_connected_names", ()))
        tool_counts = {}
        for entry in mcp_entries:
            sid = entry.get("server_id") or ""
            tool_counts[sid] = tool_counts.get(sid, 0) + 1
        mcp_servers = [
            {"server_id": spec["name"],
             "connected": spec["name"] in connected_names,
             "status": ("connected" if spec["name"] in connected_names
                        else "excluded_by_allowlist" if spec["name"] not in allowed_names
                        else "configured_not_connected"),
             "tool_count": tool_counts.get(spec["name"], 0),
             "default_tool_policy": spec["default_tool_policy"]}
            for spec in configured
        ]
    except Exception:
        pass
    return json.dumps({
        "runtime_state": runtime_state,
        "skills": skills,
        "mcp_servers": mcp_servers,
        "mcp_tools": [
            {"tool_id": entry.get("tool_id"), "server_id": entry.get("server_id"),
             "connected": entry.get("connected", False),
             "policy": entry.get("policy", "deny"),
             "available": entry.get("available", False)}
            for entry in mcp_entries
        ],
        "other_plugin_tools": [
            entry.get("tool_id") for entry in plugin_entries
            if entry.get("tool_id") not in known_skill_tools
        ],
        "notes": [
            "Skill 清单描述是元数据，仅用于识别；只启用的 Skill 可按需加载。",
            "MCP 状态来自当前 Runtime 实际挂载；清单不包含密钥、命令或环境变量。",
            "本工具只读，不安装扩展、不更改配置。",
        ],
    }, ensure_ascii=False)
