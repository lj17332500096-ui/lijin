"""Phase 1 — CASE 2 (Mutation) + CASE 4 (Approval) Golden Trace 驱动。

只观测、不修改生产代码：走生产入口 `AgentRuntime.get_default().run_turn`，
配 non-invasive Provider payload 捕获（与 tools_phase1_provider_capture.py 同法），
把两条 trace 的观测字段落到 `_audit/caseN_summary.json`。

不修改任何 Runtime / Benchmark / Router / Approval / Completion / Verification 语义。
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
    tname = type(v).__name__
    if isinstance(v, dict):
        return {k: ("***" if any(x in k.lower() for x in ("key", "token", "secret", "password")) else _redact(x))
                for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_redact(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return f"<{tname}>"


class _Captured:
    def __init__(self):
        self.items: list[dict] = []


def install_observers(run_id: str, cap: _Captured) -> None:
    from agents.models.openai_provider import OpenAIChatCompletionsModel
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

    _orig_get_client = OpenAIChatCompletionsModel._get_client

    def _patched_get_client(self):
        c = _orig_get_client(self)
        try:
            _install_on_client(c)
        except Exception:
            pass
        return c

    OpenAIChatCompletionsModel._get_client = _patched_get_client


def _agent_tool_names(agent) -> list[str]:
    return sorted(t.name for t in (getattr(agent, "tools", None) or []))


def _dump(out_path: Path, obj: dict) -> None:
    out_path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")


async def run_case2() -> dict:
    """CASE 2：Mutation → verification → completion（隔离 fixture 中改一个已知 bug）。

    用隔离 fixture（`code_sandbox/phase1_fixture/`）确保不动真项目。
    """
    from runtime.runner import AgentRuntime
    rt = AgentRuntime.get_default()
    cap = _Captured()
    run_id = f"phase1-case2-{int(time.time())}"
    install_observers(run_id, cap)

    # 隔离 fixture：写一个已知错的 calc.py
    fixture = BASE / "code_sandbox" / "phase1_fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    calc = fixture / "calc.py"
    calc.write_text("def add(a, b):\n    return a - b  # BUG: 应该是 a + b\n",
                    encoding="utf-8")

    prompt = (
        f"在 {fixture.as_posix()}/calc.py 里修复 add 函数的 bug（应该返回 a + b），"
        "然后写一个最小测试验证 add(2,3)==5，并跑通它。"
        "修完报告 revision / 验证结果 / 完成判定。"
    )
    result = await rt.run_turn(prompt, session_id=f"phase1-{run_id}")
    terminal = getattr(result, "state", None) or getattr(result, "terminal_state", None)

    summary = {
        "case": "CASE2 Mutation → verification → completion",
        "run_id": run_id,
        "user_request": prompt,
        "fixture_file": calc.as_posix(),
        "terminal_state": str(terminal),
        "router_tool_count": len(cap.items[0]["__schema_sha256__"]["tool_names"]) if cap.items else None,
        "provider_model": cap.items[0]["__schema_sha256__"]["model"] if cap.items else None,
        "provider_tool_count": cap.items[0]["__schema_sha256__"]["tool_count"] if cap.items else None,
        "tools_blob_sha256": cap.items[0]["__schema_sha256__"]["tools_blob_sha256"] if cap.items else None,
        "provider_requests": len(cap.items),
    }
    _dump(BASE / "_audit" / f"provider_payload_{run_id}.json", cap.items)
    _dump(BASE / "_audit" / "case2_summary.json", summary)
    return summary


async def run_case4() -> dict:
    """CASE 4：Mutation → 受保护 run_tests → WAITING_APPROVAL → approve → resume same run →
    approved invocation → verification evidence。

    只观测，不修改 Approval 门或 Verification 语义；approve 由驱动脚本调用
    生产路径 `runtime.approval.ApprovalGate.approve`（与 CLI 用户侧同义）。
    """
    from runtime.runner import AgentRuntime
    from runtime.approval import ApprovalGate  # 如果 ApprovalGate 在 approval 模块导出
    rt = AgentRuntime.get_default()
    cap = _Captured()
    run_id = f"phase1-case4-{int(time.time())}"
    install_observers(run_id, cap)

    fixture = BASE / "code_sandbox" / "phase1_fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    target = fixture / "guarded.py"
    target.write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")

    prompt = (
        f"修改 {fixture.as_posix()}/guarded.py 给 mul 函数加 docstring，"
        "然后运行 run_tests 验证。若需要审批请按流程申请，不要绕过。"
    )
    result = await rt.run_turn(prompt, session_id=f"phase1-{run_id}")
    terminal = getattr(result, "state", None) or getattr(result, "terminal_state", None)
    task_id = getattr(result, "task_id", None) or getattr(result, "run_id", None)

    # 若停在 WAITING_APPROVAL，走生产路径 approve 一次再 resume
    resume_state = terminal
    resume_result = None
    if terminal in ("WAITING_APPROVAL", "waiting_approval"):
        # 用 tasks API 查 pending approvals
        try:
            pend = rt.tasks.get_pending_approvals(task_id) if hasattr(rt.tasks, "get_pending_approvals") else None
            if pend:
                ap = pend[0]
                ap_id = getattr(ap, "id", None) or getattr(ap, "approval_id", None)
                # 走 ApprovalGate 的 approve（生产语义，不绕过 Verification）
                if rt.approval is not None and hasattr(rt.approval, "approve"):
                    rt.approval.approve(ap_id)
                resume_result = await rt.run_turn(
                    "", task_id=task_id, mode="async", max_turns=10)
                resume_state = getattr(resume_result, "state", None) or getattr(
                    resume_result, "terminal_state", None)
        except Exception as e:
            resume_state = f"resume_failed:{type(e).__name__}:{str(e)[:200]}"

    summary = {
        "case": "CASE4 Mutation → protected run_tests → WAITING_APPROVAL → approve → resume",
        "run_id": run_id,
        "user_request": prompt,
        "initial_terminal": str(terminal),
        "resume_state": str(resume_state),
        "task_id": str(task_id),
        "provider_requests": len(cap.items),
        "provider_model": cap.items[0]["__schema_sha256__"]["model"] if cap.items else None,
        "provider_tool_count": cap.items[0]["__schema_sha256__"]["tool_count"] if cap.items else None,
    }
    _dump(BASE / "_audit" / f"provider_payload_{run_id}.json", cap.items)
    _dump(BASE / "_audit" / "case4_summary.json", summary)
    return summary


async def main() -> None:
    c2 = await run_case2()
    c4 = await run_case4()
    print("CASE2:", json.dumps({k: v for k, v in c2.items() if k != "user_request"},
                                ensure_ascii=False, indent=2))
    print("CASE4:", json.dumps({k: v for k, v in c4.items() if k != "user_request"},
                                ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
