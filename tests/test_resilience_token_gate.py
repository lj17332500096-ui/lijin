"""2026-09-19 新增：resilience（③ 工具层）+ token_gate（④ 成本闸门）单元测试。

覆盖 7 类核心行为：
1. 幂等键稳定（同参不同序 → 同 key）
2. 退避重试 3 次后抛 / 非 retryable 不重试
3. 并发限流 peak ≤ max_concurrent / 槽位等待超时
4. TokenBudgetGate 超预算即抛 / 0 关闭
5. broker.execute / execute_resilient 签名兼容
6. classify_exception：BudgetExceeded(reason="token_budget") → KIND_TOKEN_BUDGET
7. MCP bridge：_make_mcp_invoke 网络重试路径（模拟）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resilience import (  # noqa: E402
    idempotency_key,
    run_with_retries,
    stable_args_json,
    tool_slot,
    cleanup_run_slots,
)
from runtime.token_gate import (  # noqa: E402
    TokenBudgetGate,
    attach_to_task,
    resolve_token_budget,
)
from runtime.errors import BudgetExceeded  # noqa: E402
from runtime.task import RunBudget, Task, TaskState  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 幂等键
# ---------------------------------------------------------------------------

def test_idempotency_key_stable():
    k1 = idempotency_key("run-1", "write_code_file", '{"a":1,"b":2}')
    k2 = idempotency_key("run-1", "write_code_file", '{"b":2,"a":1}')
    assert k1 == k2
    assert len(k1) == 16

def test_idempotency_key_cross_run():
    k1 = idempotency_key("run-1", "write_code_file", '{"a":1}')
    k2 = idempotency_key("run-2", "write_code_file", '{"a":1}')
    assert k1 != k2

def test_stable_args_json_sorts_keys():
    s1 = stable_args_json({"a": 1, "b": 2})
    s2 = stable_args_json({"b": 2, "a": 1})
    assert s1 == s2


# ---------------------------------------------------------------------------
# 2. 退避重试（async 用例用 asyncio.run 包装，规避 pytest-asyncio 依赖）
# ---------------------------------------------------------------------------

def test_retry_exhausts_then_raises():
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ConnectionError("net down")

    async def driver():
        with pytest.raises(ConnectionError):
            await run_with_retries(lambda: boom(), attempts=3,
                                   base_delay=0.01, cap=0.05)

    asyncio.run(driver())
    assert calls["n"] == 3

def test_nonretryable_passthrough():
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ValueError("bad")

    async def driver():
        with pytest.raises(ValueError):
            await run_with_retries(lambda: boom(), attempts=5)

    asyncio.run(driver())
    assert calls["n"] == 1  # 非 retryable 不重试

def test_success_first_call():
    ok = {"n": 0}

    async def okcall():
        ok["n"] += 1
        return 42

    async def driver():
        return await run_with_retries(lambda: okcall(), attempts=5)

    r = asyncio.run(driver())
    assert r == 42
    assert ok["n"] == 1


# ---------------------------------------------------------------------------
# 3. 并发限流
# ---------------------------------------------------------------------------

def test_tool_slot_concurrency():
    state = {"v": 0, "peak": 0}

    async def job():
        async with tool_slot("conc-test", max_concurrent=2):
            state["v"] += 1
            state["peak"] = max(state["peak"], state["v"])
            await asyncio.sleep(0.02)
            state["v"] -= 1

    async def driver():
        await asyncio.gather(*[job() for _ in range(6)])

    asyncio.run(driver())
    assert state["peak"] <= 2
    cleanup_run_slots("conc-test")

def test_tool_slot_timeout():
    async def hold():
        async with tool_slot("to-test", max_concurrent=1):
            await asyncio.sleep(0.05)

    async def driver():
        h = asyncio.create_task(hold())
        await asyncio.sleep(0.01)
        with pytest.raises(asyncio.TimeoutError):
            async with tool_slot("to-test", max_concurrent=1, wait_timeout=0.005):
                pass
        await h

    asyncio.run(driver())
    cleanup_run_slots("to-test")


# ---------------------------------------------------------------------------
# 4. TokenBudgetGate
# ---------------------------------------------------------------------------

def test_token_gate_budget_off():
    t = Task(id="t1", session_id="s", goal="g")
    gate, usage = attach_to_task(t, budget=0)
    assert gate.is_open() is False
    # 关闭时 note 不抛
    gate.note(99999, 99999)

def test_token_gate_over_budget_raises():
    t = Task(id="t2", session_id="s", goal="g")
    gate, usage = attach_to_task(t, budget=100)
    # 累计 100 OK
    gate.note(60, 40)
    # 第 2 次 60+40=120 > 100 → 抛
    with pytest.raises(BudgetExceeded):
        gate.note(60, 40)

def test_resolve_token_budget_env():
    os.environ["FORGE_TOKEN_BUDGET"] = "1234"
    try:
        assert resolve_token_budget() == 1234
    finally:
        del os.environ["FORGE_TOKEN_BUDGET"]
    assert resolve_token_budget() == 50000


# ---------------------------------------------------------------------------
# 5. broker 签名兼容
# ---------------------------------------------------------------------------

def test_broker_signature_compat():
    import inspect
    from runtime.broker import ToolBroker
    from runtime.registry import ToolRegistry

    reg = ToolRegistry()
    b = ToolBroker(reg, run_id="r1")
    sig = inspect.signature(b.execute)
    assert "tool_call_id" in sig.parameters
    assert "max_concurrent" in sig.parameters
    sig2 = inspect.signature(b.execute_resilient)
    assert "attempts" in sig2.parameters
    assert "base_delay" in sig2.parameters
    assert "retryable" in sig2.parameters


# ---------------------------------------------------------------------------
# 6. classify_exception：BudgetExceeded(reason="token_budget") → KIND_TOKEN_BUDGET
# ---------------------------------------------------------------------------

def test_budget_exceeded_token_budget_kind():
    from runtime.terminalization import (
        classify_exception,
        KIND_TOKEN_BUDGET,
    )
    exc = BudgetExceeded("token_budget")
    tr = classify_exception(exc)
    assert tr.kind == KIND_TOKEN_BUDGET, f"got kind={tr.kind!r}"

def test_budget_exceeded_wallclock_kind():
    from runtime.terminalization import (
        classify_exception,
        KIND_TIMEOUT,
    )
    # 墙钟预算（reason 不是 token_budget）仍走 KIND_TIMEOUT
    exc = BudgetExceeded("wall_clock")
    tr = classify_exception(exc)
    assert tr.kind == KIND_TIMEOUT, f"got kind={tr.kind!r}"


# ---------------------------------------------------------------------------
# 7. MCP bridge：_make_mcp_invoke 网络重试（monkeypatch server.call_tool）
# ---------------------------------------------------------------------------

def test_mcp_invoke_retries_on_network_error():
    """MCP call_tool 前两次抛网络错，第三次成功 → invoke 重试后返回结果。"""
    import json
    from unittest.mock import MagicMock

    server = MagicMock()
    call_count = {"n": 0}
    # 用类（非实例）存储，避免 raise 实例索引错位
    _exceptions = [ConnectionError, ConnectionError]

    async def fake_call_tool(name, args):
        call_count["n"] += 1
        if call_count["n"] <= len(_exceptions):
            raise _exceptions[call_count["n"] - 1]("conn reset")
        return MagicMock(content=["ok"], isError=False)

    server.call_tool = fake_call_tool

    from mcp_bridge import _make_mcp_invoke

    invoke = _make_mcp_invoke(server, "some_tool", "fake_server_some_tool")

    old_attempts = os.environ.get("FORGE_MCP_ATTEMPTS")
    old_base = os.environ.get("FORGE_MCP_BASE_DELAY")
    old_cap = os.environ.get("FORGE_MCP_RETRY_CAP")
    os.environ["FORGE_MCP_ATTEMPTS"] = "3"
    os.environ["FORGE_MCP_BASE_DELAY"] = "0.01"
    os.environ["FORGE_MCP_RETRY_CAP"] = "0.05"
    try:
        ctx = MagicMock()
        result = asyncio.run(invoke(ctx, json.dumps({"q": "test"})))
    finally:
        for k, v in [("FORGE_MCP_ATTEMPTS", old_attempts),
                     ("FORGE_MCP_BASE_DELAY", old_base),
                     ("FORGE_MCP_RETRY_CAP", old_cap)]:
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 第三次成功 → result 包含 "ok"
    assert "ok" in result, f"result={result!r}"
    assert call_count["n"] == 3, f"expected 3 calls, got {call_count['n']}"


def test_mcp_invoke_non_retryable_no_retry():
    """MCP call_tool 抛 ValueError（非网络）→ invoke 返回错误文本，不重试。"""
    import json
    from unittest.mock import MagicMock

    server = MagicMock()
    call_count = {"n": 0}

    async def fake_call_tool(name, args):
        call_count["n"] += 1
        raise ValueError("bad param")

    server.call_tool = fake_call_tool

    from mcp_bridge import _make_mcp_invoke

    invoke = _make_mcp_invoke(server, "tool2", "fake_server_tool2")

    old_attempts = os.environ.get("FORGE_MCP_ATTEMPTS")
    os.environ["FORGE_MCP_ATTEMPTS"] = "3"
    try:
        ctx = MagicMock()
        result = asyncio.run(invoke(ctx, json.dumps({"x": 1})))
    finally:
        if old_attempts is None:
            os.environ.pop("FORGE_MCP_ATTEMPTS", None)
        else:
            os.environ["FORGE_MCP_ATTEMPTS"] = old_attempts

    # 非网络错误 → 立即返回错误文本，不重试
    assert "bad param" in result, f"result={result!r}"
    assert call_count["n"] == 1, f"expected 1 call, got {call_count['n']}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
