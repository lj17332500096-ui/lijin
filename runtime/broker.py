"""ToolBroker：工具统一执行入口（骨架版）。

目标：未来的 Runner 只通过 Broker 调工具；Broker 负责
schema 校验 → Policy 检查 → 执行 → 统一结果。
闭环 A 只落地"统一执行 + 统一错误"，行为与直接调工具完全一致，
Policy/Approval/Trace 以可插拔钩子预留（默认不启用）。
"""

import asyncio
import json
from typing import Any, Awaitable, Callable

from agents.tool_context import ToolContext

from runtime.errors import ToolError
from runtime.registry import ToolRegistry

PreHook = Callable[[str, dict], Awaitable[None] | None]


class ToolBroker:
    def __init__(self, registry: ToolRegistry, pre_hook: PreHook | None = None) -> None:
        self.registry = registry
        self.pre_hook = pre_hook

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """执行一个工具并返回文本结果（与 Agent 内直接调用等价）。"""
        if not self.registry.contains(name):
            raise ToolError(f"tool not found: {name}（registry 未登记）")
        binding = self.registry.get(name)
        if self.pre_hook is not None:
            hook_result = self.pre_hook(name, arguments or {})
            if asyncio.iscoroutine(hook_result):
                await hook_result

        arguments = arguments or {}
        input_json = json.dumps(arguments, ensure_ascii=False)
        ctx = ToolContext(
            context=None,
            tool_name=name,
            tool_call_id="broker",
            tool_arguments=input_json,
        )
        result = binding.invoke.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    def execute_sync(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """同步便捷入口（无运行中事件循环时使用）。"""
        return asyncio.run(self.execute(name, arguments))
