"""Phase 1 — 非侵入式 Provider Payload 捕获。

只观测、不改 Runtime 行为：
1) monkey-patch `httpx.AsyncClient.send`，发往 /chat/completions 时把请求体脱敏后
   落盘到 `_audit/provider_payload_<run_id>.json`；
2) 走 production 入口 `AgentRuntime.get_default().run_turn`，抓完整路径。

产出 5 份 Phase 1 观测/判定文档：
  _audit/case1_summary.json            (CASE1 元信息 + Router 侧工具名)
  _audit/provider_payload_<run>.json   (真实 /chat/completions 请求体，脱敏)
  _audit/run_flags_<run>.json          (真实 env / runtime flag resolved 值)
  _audit/active_db_probe.json         (active DB 身份确认)
  _audit/mcp_sqlite_probe.json        (MCP sqlite server 启动结果)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv

load_dotenv(BASE / ".env")


def _sha256(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _redact(v):
    """脱敏 + 把 OpenAI SDK 的 Omit 占位符压成字符串，保证 JSON 可序列化。"""
    tname = type(v).__name__
    if isinstance(v, dict):
        return {k: ("***" if any(x in k.lower() for x in ("key", "token", "secret", "password")) else _redact(x))
                for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_redact(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    # OpenAI SDK 的 Omit 单例（表示"不下发该字段"）/ 其他不可序列化对象 → 字符串
    return f"<{tname}>"


class _Captured:
    def __init__(self):
        self.items: list[dict] = []


def install_observers(run_id: str, cap: _Captured) -> None:
    """在 OpenAI Python SDK 的 `AsyncCompletions.create` 上挂观测。

    这是真实 HTTP 发出前的最后一站——能拿到 SDK 构造好的完整 kwargs
    （含 tools / messages / model / temperature / max_tokens / tool_choice）。
    不修改任何参数，只把脱敏副本写进 `cap.items`，再委托给原始 create。
    """
    out_dir = BASE / "_audit"
    out_dir.mkdir(exist_ok=True)

    _patched_clients: set[int] = set()

    def _install_on_client(client) -> None:
        completions = getattr(client, "chat", None)
        completions = getattr(completions, "completions", None)
        if completions is None:
            return
        key = id(client)
        if key in _patched_clients:
            return
        _patched_clients.add(key)
        orig = completions.create

        async def _obs(*args, **kwargs):
            try:
                body = _redact(kwargs)
                tool_objs = body.get("tools") or []
                red = body
                red["__audit__"] = {
                    "run_id": run_id,
                    "capture_ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "endpoint": "chat.completions.create",
                }
                red["__schema_sha256__"] = {
                    "tools_blob_sha256": _sha256(kwargs.get("tools")),
                    "tool_count": len(tool_objs),
                    "tool_names": sorted(t.get("name") for t in tool_objs
                                          if isinstance(t, dict) and t.get("name")),
                    "message_count": len(kwargs.get("messages") or []),
                    "model": _redact(kwargs.get("model")),
                    "tool_choice": _redact(kwargs.get("tool_choice")),
                    "temperature": _redact(kwargs.get("temperature")),
                    "top_p": _redact(kwargs.get("top_p")),
                    "max_tokens": _redact(kwargs.get("max_tokens")),
                    "stream": _redact(kwargs.get("stream")),
                    "extra_params_present": sorted(
                        k for k in kwargs.keys()
                        if k not in ("model", "messages", "tools", "temperature",
                                     "top_p", "max_tokens", "stream", "tool_choice")
                    ),
                }
                red["tools"] = [
                    {"name": t.get("name"),
                     "desc_sha256": _sha256(t.get("description", "")),
                     "params_sha256": _sha256(t.get("parameters") or {})}
                    for t in tool_objs if isinstance(t, dict)
                ]
                red["messages"] = [
                    {k: _redact(v) for k, v in m.items()
                     if k in ("role", "content", "tool_calls", "tool_call_id")}
                    for m in (kwargs.get("messages") or [])
                ]
                cap.items.append(red)
            except Exception:
                pass
            return await orig(*args, **kwargs)

        completions.create = _obs

    # 挂在 SDK 内部用的 AsyncOpenAI 实例上。
    # agents SDK 里每个 OpenAIProvider 持有一个 AsyncOpenAI client；我们拦截
    # `OpenAIChatCompletionsModel._get_client` 返回的实例（它即该 client 本身）。
    from agents.models.openai_provider import OpenAIChatCompletionsModel

    _orig_get_client = OpenAIChatCompletionsModel._get_client

    def _patched_get_client(self):
        c = _orig_get_client(self)
        try:
            _install_on_client(c)
        except Exception:
            pass
        return c

    OpenAIChatCompletionsModel._get_client = _patched_get_client


def _env_truth() -> dict:
    keys = [
        "FORGE_MODEL_PREF", "AGENT_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY",
        "OPENAI_USE_RESPONSES", "FORGE_FALLBACK_MODEL",
        "FORGE_LOCAL_MODEL_NAME", "FORGE_LOCAL_MODEL_BASE_URL", "FORGE_LOCAL_MODEL_API_KEY",
        "FORGE_COMPLETION_READY", "FORGE_REDUNDANT_GUARD", "FORGE_DECISION_HINT",
        "FORGE_OBLIGATION_GATE", "FORGE_OBLIGATION_FEEDBACK", "FORGE_VERIFICATION_FOCUS",
        "FORGE_REPEAT_GUARD", "FORGE_APPROVAL_TTL_SECONDS",
        "FORGE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "FORGE_STREAM_IDLE_TIMEOUT_SECONDS",
        "FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS", "FORGE_PROVIDER_ATTEMPTS_PERSIST",
        "FORGE_DATA_DIR",
    ]
    out = {}
    for k in keys:
        v = os.getenv(k, "")
        if "KEY" in k.upper() and v:
            v = "***" + (v[-4:] if len(v) > 4 else "")
        out[k] = v
    return out


def _agent_tool_names(agent) -> list[str]:
    return sorted(t.name for t in (getattr(agent, "tools", None) or []))


async def run_case1() -> dict:
    from runtime.runner import AgentRuntime
    cap = _Captured()
    run_id = f"phase1-case1-{int(time.time())}"
    install_observers(run_id, cap)

    rt = AgentRuntime.get_default()
    case_text = ("读取当前工作区中名为 'README.md' 的文件，"
                 "只报告它的文件名和第一行内容，不要修改它。")

    agent = rt.route_agent(case_text, channel="chat")
    router_tools = _agent_tool_names(agent)

    result = await rt.run_turn(case_text, session_id=f"phase1-{run_id}")
    terminal = getattr(result, "state", None) or getattr(result, "terminal_state", None)

    out_dir = BASE / "_audit"
    (out_dir / f"provider_payload_{run_id}.json").write_text(
        json.dumps(cap.items, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"run_flags_{run_id}.json").write_text(
        json.dumps(_env_truth(), indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "run_id": run_id,
        "case": "CASE1 READ-only",
        "user_request": case_text,
        "terminal_state": str(terminal),
        "router_selected_tool_names": router_tools,
        "router_selected_count": len(router_tools),
        "active_agent_tool_names": router_tools,  # route_agent 返回的就是 active agent
        "provider_payload_files": [f"provider_payload_{run_id}.json"],
        "env_truth": _env_truth(),
    }
    (out_dir / "case1_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


async def main():
    s = await run_case1()
    print("CASE1 done:", json.dumps({k: v for k, v in s.items() if k != "env_truth"},
                                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
