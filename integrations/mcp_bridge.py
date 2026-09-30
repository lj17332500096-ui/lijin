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
import logging
import os
import re
import time
from typing import Any

from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from agents.tool import FunctionTool

from agent import assistant_agent

_logger = logging.getLogger(__name__)

ENV_KEY = "MCP_SERVERS"
ALLOWLIST_ENV = "FORGE_MCP_ALLOWLIST"

POLICY_VALUES = {"allow", "approval", "deny"}
DEFAULT_POLICY = "deny"

_connected = False
_config_errors: list[str] = []
_connected_names: list[str] = []
_failed_at: dict[str, float] = {}
_skip_reasons: list[str] = []
_servers: list[Any] = []
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
        if not isinstance(item, dict) or not item.get("name"):
            errors.append(f"{where}缺少 name")
            continue
        transport = str(item.get("transport") or item.get("type") or "stdio").strip().lower()
        if transport in ("http", "streamable_http", "streamable-http", "remote"):
            transport = "streamable-http"
            if not str(item.get("url") or "").strip():
                errors.append(f"{where}缺少 Streamable HTTP url")
                continue
            raw_headers = item.get("headers") or {}
            if not isinstance(raw_headers, dict):
                errors.append(f"{where} headers 应为对象")
                continue
        elif transport == "stdio":
            if not item.get("command"):
                errors.append(f"{where}缺少 stdio command")
                continue
            raw_headers = {}
        else:
            errors.append(f"{where} transport 不支持（{transport}）")
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
        raw_idempotent = item.get("idempotent_tools") or []
        if not isinstance(raw_idempotent, list):
            errors.append(f"{where} idempotent_tools 应为字符串数组，已忽略")
            raw_idempotent = []
        specs.append(
            {
                "name": name,
                "transport": transport,
                "url": str(item.get("url") or "").strip(),
                "headers": {str(k): str(v) for k, v in raw_headers.items()},
                "command": str(item.get("command") or ""),
                "args": [str(a) for a in (item.get("args") or [])],
                "env": {str(k): str(v) for k, v in (item.get("env") or {}).items()},
                "tool_policy": tool_policy,
                "default_tool_policy": default,
                # Retrying a remote call is safe only when the operator explicitly
                # declares the remote operation idempotent. Authorization is not
                # an idempotency guarantee.
                "idempotent_tools": [str(n) for n in raw_idempotent if str(n).strip()],
            }
        )
    return specs, errors


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_templates(value: str) -> str:
    """Resolve ${NAME} in trusted local MCP config without printing secret values."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.getenv(name)
        if resolved is None:
            missing.append(name)
            return ""
        return resolved

    result = _ENV_TEMPLATE_RE.sub(replace, value)
    if missing:
        raise ValueError("MCP headers reference unset environment variable(s): "
                         + ", ".join(sorted(set(missing))))
    return result


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


def _make_mcp_invoke(server: Any, remote_name: str, display_name: str,
                     *, idempotent: bool = False):
    import json as _json

    async def invoke(_ctx: Any, args_json: str) -> str:
        try:
            arguments = _json.loads(args_json or "{}")
            if not isinstance(arguments, dict):
                arguments = {}
        except _json.JSONDecodeError:
            return f"{display_name} 参数不是合法 JSON，已拒绝调用。"

        import logging as _logging
        import os as _os

        from runtime import resilience

        attempts = int(_os.getenv("FORGE_MCP_ATTEMPTS", "3") or 3) if idempotent else 1
        base_delay = float(_os.getenv("FORGE_MCP_BASE_DELAY", "0.5") or 0.5)
        cap = float(_os.getenv("FORGE_MCP_RETRY_CAP", "8.0") or 8.0)

        def _on_attempt(n: int, exc: BaseException) -> None:
            _logging.getLogger("mcp_bridge").info(
                "%s attempt %d/%d failed (%s)，退避后重试",
                display_name, n, attempts, type(exc).__name__,
            )

        try:
            if idempotent:
                # Only explicitly declared idempotent remote tools may be replayed.
                result = await resilience.run_with_retries(
                    lambda: server.call_tool(remote_name, arguments),
                    attempts=attempts,
                    base_delay=base_delay,
                    cap=cap,
                    retryable=resilience.NETWORK_RETRYABLE_EXCEPTIONS,
                    on_attempt=_on_attempt,
                )
            else:
                result = await server.call_tool(remote_name, arguments)
        except resilience.NETWORK_RETRYABLE_EXCEPTIONS as exc:
            retry_note = (f"幂等调用已尝试 {attempts} 次" if idempotent
                          else "未自动重试，以避免重复产生副作用")
            return ("MCP_OUTCOME_UNKNOWN: "
                    f"{display_name} 网络调用失败；{retry_note}。"
                    f"无法确认远端是否已执行，请先核对远端状态。"
                    f"({type(exc).__name__}: {str(exc)[:240]})")
        except Exception as exc:  # 非网络类（参数/认证等）：不重试，原样报错
            return f"{display_name} 执行出错：{type(exc).__name__}: {str(exc)[:300]}"

        return format_mcp_result(display_name, result)

    return invoke


def _build_tool(server: Any, spec: dict, remote_tool: Any, policy: str) -> FunctionTool:
    remote_name = getattr(remote_tool, "name", "")
    display_name = tool_full_name(spec["name"], remote_name)
    idempotent = remote_name in set(spec.get("idempotent_tools") or [])
    tool = FunctionTool(
        name=display_name,
        description=getattr(remote_tool, "description", "") or "",
        params_json_schema=getattr(remote_tool, "inputSchema", None) or {},
        on_invoke_tool=_make_mcp_invoke(server, remote_name, display_name,
                                        idempotent=idempotent),
        strict_json_schema=False,
    )
    setattr(tool, "_mcp_source", "mcp")
    setattr(tool, "_mcp_server", spec["name"])
    setattr(tool, "_mcp_remote", remote_name)
    setattr(tool, "_mcp_policy", policy)
    setattr(tool, "_mcp_idempotent", idempotent)
    return tool


async def attach_server_tools(server: Any, spec: dict) -> tuple[list[FunctionTool], list[str]]:
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
    targets = [assistant_agent]
    try:
        from agent import gateway_assistant_agent

        current = gateway_assistant_agent()
        if all(current is not item for item in targets):
            targets.append(current)
    except Exception:
        pass
    for target in targets:
        existing = [
            t for t in list(getattr(target, "tools", []) or [])
            if getattr(t, "name", "") not in _mounted_names
        ]
        target.tools = existing + mounted
        target.mcp_servers = []
    _mounted_names.extend(getattr(t, "name", "") for t in mounted)


async def ensure_connected() -> bool:
    """连接 allowlist 内服务器；失败服务器按冷却时间重试，成功项不重复挂载。"""
    global _connected
    if _connected and not _failed_at:
        return True
    specs, errors = parse_specs(os.getenv(ENV_KEY, ""))
    _config_errors.extend(errors)
    specs, allow_reasons = filter_allowlist(specs)
    _skip_reasons.extend(allow_reasons)
    if not specs:
        _connected = True  # 没有配置，视为“已处理”，避免反复解析
        return True

    mounted_total: list[FunctionTool] = []
    attempted = False
    try:
        retry_interval = max(
            0.0, float(os.getenv("FORGE_MCP_RETRY_INTERVAL_SECONDS", "30") or 30)
        )
    except ValueError:
        retry_interval = 30.0
    now = time.monotonic()
    # npx 冷启动（拉包）可能 > 5s（库默认 client_session_timeout_seconds=5），
    # 提到 60s 避免 gitee/playwright 冷启动时超时被跳过；FORGE_MCP_CONNECT_TIMEOUT 可覆盖。
    _conn_timeout_s: float = float(os.getenv("FORGE_MCP_CONNECT_TIMEOUT", "60") or 60)
    for spec in specs:
        server_name = spec["name"]
        if server_name in _connected_names:
            continue
        failed_at = _failed_at.get(server_name)
        if failed_at is not None and now - failed_at < retry_interval:
            continue
        attempted = True
        if spec["transport"] == "streamable-http":
            try:
                headers = {
                    key: _resolve_env_templates(value)
                    for key, value in spec["headers"].items()
                }
            except ValueError as exc:
                _failed_at[server_name] = time.monotonic()
                _skip_reasons.append(f"{server_name} 连接配置不完整（{exc}）")
                continue
            server = MCPServerStreamableHttp(
                name=spec["name"],
                params={"url": spec["url"], "headers": headers},
                # Remote HTTP handshakes are not cold-starting a local process;
                # keep the session timeout modest instead of inheriting the 60s
                # stdio/npx startup allowance.
                client_session_timeout_seconds=min(_conn_timeout_s, 10.0),
                cache_tools_list=True,
            )
        else:
            env = dict(os.environ)
            env.update(spec["env"])
            server = MCPServerStdio(
                name=spec["name"],
                params={"command": spec["command"], "args": spec["args"], "env": env},
                client_session_timeout_seconds=_conn_timeout_s,
            )
        try:
            await server.connect()
        except Exception as exc:
            _failed_at[server_name] = time.monotonic()
            _skip_reasons.append(
                f"{server_name} 连接失败（{type(exc).__name__}: {str(exc)[:160]}），"
                f"将在 {retry_interval:g} 秒冷却后重试"
            )
            try:
                await server.cleanup()
            except Exception:
                pass
            continue
        _servers.append(server)
        _connected_names.append(server_name)
        _failed_at.pop(server_name, None)
        _skip_reasons[:] = [
            reason for reason in _skip_reasons
            if not reason.startswith(f"{server_name} 连接失败（")
        ]
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
    if detail and (attempted or errors or allow_reasons):
        try:
            print(detail, flush=True)
        except UnicodeEncodeError:
            print("MCP 接入完成（终端编码限制，详情见启动日志）", flush=True)
    return bool(mounted_total)


async def close_servers() -> list[str]:
    """在创建 MCP 连接的同一事件循环中有序关闭并清理模块状态。

    MCP stdio transport 持有 AnyIO task group，不能等到 asyncio.run() 已退出后
    再依赖解释器回收。调用方应在应用的 async 生命周期 finally 中调用此方法。
    """
    global _connected
    errors: list[str] = []
    servers = list(reversed(_servers))
    _servers.clear()
    for server in servers:
        try:
            await server.cleanup()
        except Exception as exc:
            message = (
                f"{getattr(server, 'name', 'MCP')} cleanup: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
            errors.append(message)
            _logger.warning("MCP server cleanup failed: %s", message, exc_info=True)

    if _mounted_names:
        names = set(_mounted_names)
        targets = [assistant_agent]
        try:
            from agent import gateway_assistant_agent

            current = gateway_assistant_agent()
            if all(current is not item for item in targets):
                targets.append(current)
        except Exception:
            pass
        for target in targets:
            target.tools = [
                tool for tool in list(getattr(target, "tools", []) or [])
                if getattr(tool, "name", "") not in names
            ]
            target.mcp_servers = []
        _mounted_names.clear()
    _connected_names.clear()
    _failed_at.clear()
    _config_errors.clear()
    _skip_reasons.clear()
    _policy_summary.update(allow=0, approval=0, deny=0)
    _connected = False
    try:
        from runtime.runner import AgentRuntime

        AgentRuntime.get_default().refresh_tool_wrappers()
    except Exception:
        pass
    return errors


def status_text() -> str:
    """给启动横幅/Runtime 状态用的 MCP 摘要（含策略统计，不含密钥）。"""
    lines: list[str] = []
    if _config_errors:
        lines.append("[MCP] 配置问题：" + "；".join(_config_errors))
    if _connected_names:
        lines.append("[MCP] 已接入服务器：" + "、".join(_connected_names))
    if sum(_policy_summary.values()):
        allow, approval, deny = _policy_summary["allow"], _policy_summary["approval"], _policy_summary["deny"]
        approval_enabled = os.getenv("APPROVAL", "").strip().lower() not in ("off", "false", "0")
        effective_allow = allow + (0 if approval_enabled else approval)
        effective_approval = approval if approval_enabled else 0
        lines.append(
            f"[MCP] 工具权限映射：allow={allow} / approval={approval} / deny={deny}"
            f"；当前有效：allow={effective_allow} / approval={effective_approval} / deny={deny}"
        )
        if approval and not approval_enabled:
            lines.append(
                f"[MCP] 全局 APPROVAL=off：{approval} 个配置为 approval 的工具当前按 allow 执行"
            )
    if _skip_reasons:
        lines.append("[MCP] " + "；".join(_skip_reasons[-12:]))
    return "\n".join(lines)
