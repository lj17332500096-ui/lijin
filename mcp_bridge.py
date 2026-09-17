"""MCP 服务器接入（可选）：服务器 allowlist + 工具权限映射后挂到主 Agent。

配置：.env 里 MCP_SERVERS 填 JSON 数组（不填或为空 = 不启用，一切行为不变）：
    MCP_SERVERS=[{
        "name": "github",
        "command": "npx",
        "args": ["-y", "@github/mcp-server"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."},
        "default_tool_policy": "deny",           # allow | approval | deny（缺省 deny）
        "tool_policy": {"get_issue": "allow"}    # 工具原始名 → 策略（可只列授权项）
    }]

策略语义（权限映射，fail-closed）：
- allow    = 用户声明信任，模型可直接调用（仍走 Runtime 执行记账与审计）；
- approval = 需要先经既有审批门（WAITING_APPROVAL → 用户批准后放行），并纳入
             Write-Ahead 副作用台账；不授予审批的工具绝不静默执行；
- deny（默认）= 工具不挂载，模型根本看不到。

可选 allowlist：FORGE_MCP_ALLOWLIST=server1,server2（逗号分隔）；设置了之后
只连接名单内的服务器，名单外直接跳过。未设置时允许 MCP_SERVERS 中显式配置的全部服务器
（显式配置本身即第一层授权，allowlist 用于额外收窄，例如区分开发/生产配置）。

挂载方式：不再把服务器塞给 agent.mcp_servers（SDK 内部执行会绕过 Runtime 审批/记账），
而是把每个已授权工具包成 FunctionTool 挂进 agent.tools，由 AgentRuntime 的统一包装链
（Approval → FileScope → 记账）接管；挂载后调用 refresh_tool_wrappers() 重新包装。
"""

import json
import os
from pathlib import Path
from typing import Any

from agents.mcp import MCPServerStdio
from agents.tool import FunctionTool

from agent import assistant_agent

BASE_DIR = Path(__file__).resolve().parent
ENV_KEY = "MCP_SERVERS"
ALLOWLIST_ENV = "FORGE_MCP_ALLOWLIST"

POLICY_VALUES = {"allow", "approval", "deny"}
DEFAULT_POLICY = "deny"

_connected = False
_config_errors: list[str] = []
_connected_names: list[str] = []
_skip_reasons: list[str] = []
_servers: list[MCPServerStdio] = []
_mounted_names: list[str] = []
_policy_summary: dict[str, int] = {"allow": 0, "approval": 0, "deny": 0}


# ---------------------------------------------------------------------------
# 配置解析与策略
# ---------------------------------------------------------------------------

def _normalize_policy(value: Any, errors: list[str], where: str) -> str:
    text = str(value or DEFAULT_POLICY).strip().lower()
    if text not in POLICY_VALUES:
        errors.append(f"{where} 非法策略 {value!r}（允许 allow/approval/deny），已按 deny 处理")
        return DEFAULT_POLICY
    return text


def parse_specs(text: str) -> tuple[list[dict], list[str]]:
    """解析 MCP_SERVERS 配置；返回 (条目列表, 错误列表)。条目含 tool_policy/default_tool_policy。"""
    text = (text or "").strip()
    if not text:
        return [], []
    errors: list[str] = []
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [f"MCP_SERVERS 不是合法 JSON：{exc}"]
    if not isinstance(raw, list):
        return [], ["MCP_SERVERS 应为 JSON 数组"]
    specs: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"MCP 服务器第 {i + 1} 项"
        if not isinstance(item, dict) or not item.get("name") or not item.get("command"):
            errors.append(f"{where}缺少 name/command")
            continue
        name = str(item["name"]).strip()
        if name in seen:
            errors.append(f"MCP 服务器名重复：{name}")
            continue
        seen.add(name)
        tool_policy: dict[str, str] = {}
        raw_policy = item.get("tool_policy") or {}
        if not isinstance(raw_policy, dict):
            errors.append(f"{where} tool_policy 应为对象，已忽略并按默认策略处理")
            raw_policy = {}
        for tool_name, value in raw_policy.items():
            tool_policy[str(tool_name)] = _normalize_policy(
                value, errors, f"{where} 工具 {tool_name} 的 tool_policy"
            )
        default = _normalize_policy(
            item.get("default_tool_policy", DEFAULT_POLICY),
            errors, f"{where} default_tool_policy",
        )
        specs.append(
            {
                "name": name,
                "command": str(item["command"]),
                "args": [str(a) for a in (item.get("args") or [])],
                "env": {str(k): str(v) for k, v in (item.get("env") or {}).items()},
                "tool_policy": tool_policy,
                "default_tool_policy": default,
            }
        )
    return specs, errors


def filter_allowlist(specs: list[dict], allowlist: str | None = None) -> tuple[list[dict], list[str]]:
    """FORGE_MCP_ALLOWLIST 收窄可连接的服务器；未设置时放行全部显式配置。"""
    raw = allowlist if allowlist is not None else os.getenv(ALLOWLIST_ENV, "").strip()
    if not raw:
        return specs, []
    allowed = {x.strip().lower() for x in raw.split(",") if x.strip()}
    keep: list[dict] = []
    reasons: list[str] = []
    for spec in specs:
        if spec["name"].lower() in allowed:
            keep.append(spec)
        else:
            reasons.append(
                f"{spec['name']} 不在 {ALLOWLIST_ENV} 名单内，已跳过（不连接、不挂载）"
            )
    return keep, reasons


def _policy_for(spec: dict, remote_tool_name: str) -> str:
    """返回某服务器工具的策略（显式映射优先，其次服务器默认，最后全局 deny）。"""
    mapped = (spec.get("tool_policy") or {}).get(remote_tool_name)
    if mapped is not None:
        return str(mapped)
    return str(spec.get("default_tool_policy") or DEFAULT_POLICY)


def tool_full_name(server_name: str, remote_tool_name: str) -> str:
    """模型可见的工具名：<server>_<tool>，保证服务器间不撞名且可追溯到来源。"""
    return f"{server_name}_{remote_tool_name}"


# ---------------------------------------------------------------------------
# 结果格式化与调用包装
# ---------------------------------------------------------------------------

def format_mcp_result(display_name: str, result: Any, max_len: int = 6000) -> str:
    """把 MCP CallToolResult 转成给模型/用户的文本（只取文本内容，截断有界；带外部数据信任边界）。"""
    parts: list[str] = []
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if isinstance(result, str):
        parts.append(result)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text is None:
                try:
                    text = json.dumps(block, ensure_ascii=False, default=str)
                except Exception:
                    text = str(block)
            parts.append(str(text))
    else:
        parts.append(str(result))
    joined = "\n".join(parts).strip() or "(MCP 工具未返回文本内容)"
    if bool(getattr(result, "isError", False)):
        from runtime.trust import tag

        return tag(
            f"MCP 工具输出({display_name} 执行失败)",
            None,
            f"{display_name} 执行失败：{joined[:800]}",
        )
    from runtime.trust import tag

    return tag(f"MCP 工具输出({display_name})", None, joined[:max_len])


def _make_mcp_invoke(server: MCPServerStdio, remote_name: str, display_name: str):
    async def invoke(_ctx: Any, args_json: str) -> str:
        try:
            arguments = json.loads(args_json or "{}")
            if not isinstance(arguments, dict):
                arguments = {}
        except json.JSONDecodeError:
            return f"{display_name} 参数不是合法 JSON，已拒绝调用。"
        try:
            result = await server.call_tool(remote_name, arguments)
        except Exception as exc:  # noqa: BLE001 - 工具错误以文本返回给模型，不崩 Run
            return f"{display_name} 执行出错：{type(exc).__name__}: {str(exc)[:300]}"
        return format_mcp_result(display_name, result)

    return invoke


def _build_tool(server: MCPServerStdio, spec: dict, remote_tool: Any, policy: str) -> FunctionTool:
    remote_name = getattr(remote_tool, "name", "")
    display_name = tool_full_name(spec["name"], remote_name)
    tool = FunctionTool(
        name=display_name,
        description=getattr(remote_tool, "description", "") or "",
        params_json_schema=getattr(remote_tool, "inputSchema", None) or {},
        on_invoke_tool=_make_mcp_invoke(server, remote_name, display_name),
        strict_json_schema=False,
    )
    setattr(tool, "_mcp_source", "mcp")
    setattr(tool, "_mcp_server", spec["name"])
    setattr(tool, "_mcp_remote", remote_name)
    setattr(tool, "_mcp_policy", policy)
    return tool


async def attach_server_tools(server: MCPServerStdio, spec: dict) -> tuple[list[FunctionTool], list[str]]:
    """列出一个已连接服务器的工具并按策略挂载；返回 (挂载的工具, 跳过原因)。"""
    try:
        remote_tools = await server.list_tools()
    except Exception as exc:
        return [], [f"{spec['name']} 列出工具失败（{type(exc).__name__}: {str(exc)[:160]}），已跳过"]
    mounted: list[FunctionTool] = []
    skipped: list[str] = []
    allow_names: list[str] = []
    approval_names: list[str] = []
    for remote_tool in remote_tools or []:
        remote_name = getattr(remote_tool, "name", "")
        if not remote_name:
            continue
        policy = _policy_for(spec, remote_name)
        display_name = tool_full_name(spec["name"], remote_name)
        if policy == "deny":
            _policy_summary["deny"] = _policy_summary.get("deny", 0) + 1
            skipped.append(f"{display_name}（未授权，策略 deny）")
            continue
        try:
            mounted.append(_build_tool(server, spec, remote_tool, policy))
        except Exception as exc:  # noqa: BLE001 - 单个工具构建失败不影响其它工具
            skipped.append(f"{display_name} 构建失败（{type(exc).__name__}: {str(exc)[:160]}）")
            continue
        _policy_summary[policy] = _policy_summary.get(policy, 0) + 1
        if policy == "approval":
            approval_names.append(display_name)
        else:
            allow_names.append(display_name)

    # 运行时策略登记（模块级集合，幂等）：
    # - 严格 Project 下 FileScope 需要显式登记，否则任何新工具都会 fail-closed 拒绝；
    # - approval 工具进入审批门 + Write-Ahead 副作用台账。
    if allow_names or approval_names:
        from runtime.filescope import register_non_file_tools

        register_non_file_tools(set(allow_names) | set(approval_names))
    if approval_names:
        from runtime.approval import register_gated_names
        from runtime.runner import SIDE_EFFECT_TOOLS

        register_gated_names(approval_names)
        SIDE_EFFECT_TOOLS.update(approval_names)
    return mounted, skipped


def _mount_tools(mounted: list[FunctionTool]) -> None:
    """把授权工具挂进 assistant_agent.tools，并清空 SDK 自动挂载通道（防重复执行）。"""
    existing = [
        t for t in list(getattr(assistant_agent, "tools", []) or [])
        if getattr(t, "name", "") not in _mounted_names
    ]
    assistant_agent.tools = existing + mounted
    _mounted_names.extend(getattr(t, "name", "") for t in mounted)
    assistant_agent.mcp_servers = []


async def ensure_connected() -> bool:
    """连接 allowlist 内的服务器，按工具权限映射挂载；无配置/全部拒绝时安全返回。"""
    global _connected
    if _connected:
        return True
    specs, errors = parse_specs(os.getenv(ENV_KEY, ""))
    _config_errors.extend(errors)
    specs, allow_reasons = filter_allowlist(specs)
    _skip_reasons.extend(allow_reasons)
    if not specs:
        _connected = True  # 没有配置，视为“已处理”，避免反复解析
        return True

    mounted_total: list[FunctionTool] = []
    for spec in specs:
        env = dict(os.environ)
        env.update(spec["env"])
        server = MCPServerStdio(
            name=spec["name"],
            params={"command": spec["command"], "args": spec["args"], "env": env},
        )
        try:
            await server.connect()
        except Exception as exc:
            _skip_reasons.append(
                f"{spec['name']} 连接失败（{type(exc).__name__}: {str(exc)[:160]}），已跳过"
            )
            continue
        _servers.append(server)
        _connected_names.append(spec["name"])
        mounted, skipped = await attach_server_tools(server, spec)
        _skip_reasons.extend(skipped)
        mounted_total.extend(mounted)

    if mounted_total:
        _mount_tools(mounted_total)
        try:
            from runtime.runner import AgentRuntime

            AgentRuntime.get_default().refresh_tool_wrappers()
        except Exception:
            pass  # Runtime 未初始化时稍后 _ensure 会连同 MCP 工具一起包装
    _connected = True
    detail = status_text()
    if detail:
        try:
            print(detail, flush=True)
        except UnicodeEncodeError:
            print("MCP 接入完成（终端编码限制，详情见启动日志）", flush=True)
    return bool(mounted_total)


def status_text() -> str:
    """给启动横幅/Runtime 状态用的 MCP 摘要（含策略统计，不含密钥）。"""
    lines: list[str] = []
    if _config_errors:
        lines.append("[MCP] 配置问题：" + "；".join(_config_errors))
    if _connected_names:
        lines.append("[MCP] 已接入服务器：" + "、".join(_connected_names))
    if sum(_policy_summary.values()):
        allow, approval, deny = _policy_summary["allow"], _policy_summary["approval"], _policy_summary["deny"]
        lines.append(f"[MCP] 工具权限映射：allow={allow} / approval={approval} / deny={deny}")
    if _skip_reasons:
        lines.append("[MCP] " + "；".join(_skip_reasons[-12:]))
    return "\n".join(lines)
