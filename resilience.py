"""工具层韧性（Resilience）：退避抖动重试 + 并发限流 + 幂等键。

设计原则
--------
- **零改主链路**：纯函数 + 上下文管理器，Runner/ToolBroker 按需组合调用，
  不重写任何既有组件。
- 指数退避 + 完全随机抖动（full-jitter）：避免惊群效应（AWS 经典公式
  sleep = min(cap, base * 2 ** attempt) 的一半随机数）。
- 幂等键 = sha1(run_id, tool_name, args_json)[:16]，run_id 缺省时退化为
  仅 tool_name|args（同参数同工具天然去重）。
- 并发限流用 asyncio.Semaphore，单 Run 内默认 3。

用法
----
    import resilience

    # 重试：对幂等的网络类调用包装（attempt_factory 每次重新执行）
    result = await resilience.run_with_retries(
        lambda: call_tool(), attempts=3, base_delay=0.5, cap=8.0)

    # 并发限流（单 Run 内最多 3 个工具并行）
    async with resilience.tool_slot(run_id, max_concurrent=3):
        await execute_one_tool(...)

    # 幂等键
    key = resilience.idempotency_key(run_id, "write_code_file", args_json)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from contextlib import asynccontextmanager
from typing import Any, Callable

_logger = logging.getLogger("resilience")

_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)

#: 公开别名：网络类可重试异常。调用方（如 mcp_bridge）应当复用**这一份**定义，
#: 而不是各自再抄一遍元组 —— 抄第二遍就会漂移（2026-09-19：MCP 曾自带一套
#: 与 run_with_retries 完全相同的退避重试实现，属同类漂移）。
NETWORK_RETRYABLE_EXCEPTIONS = _RETRYABLE_EXCEPTIONS


async def run_with_retries(
    attempt_factory: Callable[[], Any],
    attempts: int = 3,
    base_delay: float = 0.5,
    cap: float = 8.0,
    retryable: tuple[type[BaseException], ...] | None = None,
    on_attempt: Callable[[int, BaseException], None] | None = None,
) -> Any:
    """指数退避 + 完全随机抖动的重试包装（普通 async 函数）。

    用法：``await resilience.run_with_retries(lambda: call_tool(), attempts=3)``

    Args:
        attempt_factory: 零参 callable，每次尝试都会重新调用；
            同步/异步皆可（异步返回 awaitable 会被 await）。
        attempts: 总尝试次数（含首次）。
        base_delay: 基础退避秒数（第 N 次失败后最长 base_delay * 2 ** N）。
        cap: 单次 sleep 上限（防止退避无限膨胀）。
        retryable: 可重试异常类型；缺省 = 网络类（Timeout / Connection / OSError）。
        on_attempt: 每次失败后的回调（attempt 编号从 1 开始；最后一次不触发）。

    非 retryable 异常（含 KeyboardInterrupt / SystemExit）直接透传，不重试。
    全部 attempts 耗尽后抛出最后一次 retryable 异常。
    """
    retryable = retryable or _RETRYABLE_EXCEPTIONS
    for attempt in range(1, max(1, attempts) + 1):
        try:
            r = attempt_factory()
            if asyncio.iscoroutine(r):
                r = await r
            return r
        except retryable as exc:
            if attempt >= attempts:
                raise
            backoff = min(cap, base_delay * (2 ** (attempt - 1)))
            sleep_s = random.uniform(0.0, backoff)
            if on_attempt is not None:
                try:
                    on_attempt(attempt, exc)
                except Exception:
                    pass
            _logger.debug("resilience: attempt %d/%d failed (%s), sleep %.2fs",
                          attempt, attempts, type(exc).__name__, sleep_s)
            await asyncio.sleep(sleep_s)
    raise RuntimeError("run_with_retries: unreachable (attempts exhausted path)")


def idempotency_key(run_id: str, tool_name: str, args_json: str) -> str:
    """生成幂等键：sha1(run_id|tool|args)[:16]。

    同一 Run 内同参数同工具 → 同一 key；跨 Run 即使参数相同也生成不同 key
    （隔离运行边界）。run_id 缺省为空串时退化为仅 tool_name|args（跨 Run
    同参数同工具也去重，适合纯幂等查询类场景）。args_json 应预先
    json.dumps 归一化（用 stable_args_json 保证字段序稳定）。
    """
    import json as _json
    # 幂等键稳定化：args_json 按 key 排序后再 hash，保证 {"a":1,"b":2} 与
    # {"b":2,"a":1} 生成同一 key。
    try:
        stable = _json.dumps(_json.loads(args_json or "{}"),
                             ensure_ascii=False, sort_keys=True)
    except Exception:
        stable = args_json or ""
    payload = f"{run_id}|{tool_name}|{stable}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


def stable_args_json(args: dict | None) -> str:
    """归一化 args 为稳定 JSON 字符串（sort_keys，ensure_ascii=False）。"""
    if not args:
        return "{}"
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


@asynccontextmanager
async def tool_slot(run_id: str, *, max_concurrent: int = 3,
                    wait_timeout: float = 30.0):
    """单 Run 工具并发限流（Semaphore）。

    超出 max_concurrent 时阻塞等待；等待超过 wait_timeout 秒 → 抛 TimeoutError
    （避免一个慢工具卡死整个 Run 的工具池）。
    """
    key = _slots.setdefault(run_id, asyncio.Semaphore(max_concurrent))
    try:
        await asyncio.wait_for(key.acquire(), timeout=wait_timeout)
    except asyncio.TimeoutError:
        _logger.warning("resilience: tool_slot %s timed out after %.1fs (cap=%d)",
                        run_id, wait_timeout, max_concurrent)
        raise
    try:
        yield
    finally:
        key.release()


# 进程级 _slots（按 run_id 隔离；Run 结束可调用 cleanup_run_slots 回收）
_slots: dict[str, asyncio.Semaphore] = {}


def cleanup_run_slots(run_id: str) -> None:
    _slots.pop(run_id, None)
