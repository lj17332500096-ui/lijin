"""ToolBroker：工具统一执行入口。

目标：Runner 只通过 Broker 调工具；Broker 负责
schema 校验 → Policy 检查 → 执行（含退避重试 + 并发限流）→ 统一结果。

2026-09-19 加固（③ 工具层）：
- ``execute()`` 增加 ``run_id`` 形参；非幂等默认仍保持「单次执行」语义（与历史一致）。
- 提供 ``execute_resilient()`` 入口：退避抖动重试 + 并发限流（复用 resilience.py），
  由调用方显式选择启用（不悄悄改既有行为）。
- 幂等键：``resilience.idempotency_key(run_id, name, args_json)``，供写-Ahead 台账
  去重（tool_calls.invocation_id 已存在，幂等键作为可选附加维度）。
"""

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

from agents.tool_context import ToolContext

from runtime.errors import ToolError
from runtime.registry import ToolRegistry

PreHook = Callable[[str, dict], Awaitable[None] | None]


class ToolBroker:
    def __init__(self, registry: ToolRegistry, pre_hook: PreHook | None = None,
                 run_id: str | None = None,
                 max_concurrent: int | None = None) -> None:
        self.registry = registry
        self.pre_hook = pre_hook
        self.run_id = run_id
        # 并发上限：显式参数 > env > 默认 3；0/None = 不限流
        if max_concurrent is None:
            max_concurrent = int(os.getenv("FORGE_TOOL_MAX_CONCURRENT", "3") or 3)
        self.max_concurrent = max_concurrent

    async def _execute_core(self, name: str, arguments: dict[str, Any],
                            tool_call_id: str, input_json: str) -> str:
        binding = self.registry.get(name)
        if self.pre_hook is not None:
            hook_result = self.pre_hook(name, arguments or {})
            if asyncio.iscoroutine(hook_result):
                await hook_result
        ctx = ToolContext(
            context=None,
            tool_name=name,
            tool_call_id=tool_call_id,
            tool_arguments=input_json,
        )
        result = binding.invoke.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    async def execute(self, name: str, arguments: dict[str, Any] | None = None,
                      *, tool_call_id: str = "broker",
                      max_concurrent: int | None = None) -> str:
        """单次执行（与历史行为一致；可选并发限流）。

        传 max_concurrent / self.max_concurrent 启用限流；缺省不限流。
        """
        if not self.registry.contains(name):
            raise ToolError(f"tool not found: {name}（registry 未登记）")
        arguments = arguments or {}
        input_json = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        cap = max_concurrent if max_concurrent is not None else self.max_concurrent
        if cap and cap > 0:
            import resilience

            async with resilience.tool_slot(self.run_id or "anon",
                                             max_concurrent=cap):
                return await self._execute_core(name, arguments,
                                                tool_call_id, input_json)
        return await self._execute_core(name, arguments, tool_call_id, input_json)

    async def execute_resilient(self, name: str,
                                arguments: dict[str, Any] | None = None,
                                *,
                                attempts: int = 3,
                                base_delay: float = 0.5,
                                cap_delay: float = 8.0,
                                max_concurrent: int | None = None,
                                retryable: tuple[type[BaseException], ...] | None = None,
                                ) -> str:
        """退避抖动重试 + 并发限流的执行入口（显式 opt-in）。

        - 每次尝试都会重新执行工具（适合幂等的网络类工具）；
        - 非网络类异常（ValueError/AgentError 等）直接透传，不重试；
        - attempts 耗尽后抛出最后一次网络类异常；
        - 幂等键：``resilience.idempotency_key(run_id, name, args_json)`` 可用作
          写-Ahead 台账去重维度（tool_calls.invocation_id 仍是唯一执行键）。
        """
        if not self.registry.contains(name):
            raise ToolError(f"tool not found: {name}（registry 未登记）")
        arguments = arguments or {}
        input_json = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        import resilience

        async def _once() -> str:
            cap = max_concurrent if max_concurrent is not None else self.max_concurrent
            if cap and cap > 0:
                async with resilience.tool_slot(self.run_id or "anon",
                                                 max_concurrent=cap):
                    return await self._execute_core(name, arguments,
                                                    "broker-resilient", input_json)
            return await self._execute_core(name, arguments,
                                            "broker-resilient", input_json)

        return await resilience.run_with_retries(
            _once, attempts=attempts, base_delay=base_delay, cap=cap_delay,
            retryable=retryable)

    def execute_sync(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """同步便捷入口（无运行中事件循环时使用）。"""
        return asyncio.run(self.execute(name, arguments))
