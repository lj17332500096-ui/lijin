"""ToolRegistry：登记系统内所有工具（只负责"有哪些工具"，权限属于 Policy）。

登记内容 = ToolSpec 元数据 + 可调用的 FunctionTool（SDK 装饰器产物）。
用法：从主 Agent 的 tools 列表自动发现并登记（discover_from_agent）。
"""

from dataclasses import dataclass
from typing import Any

from runtime.errors import ToolError
from runtime.spec import ToolSpec, spec_for


@dataclass(slots=True)
class ToolBinding:
    spec: ToolSpec
    invoke: Any  # FunctionTool：.on_invoke_tool(ctx, arguments_json)


class ToolRegistry:
    def __init__(self) -> None:
        self._bindings: dict[str, ToolBinding] = {}

    def register(self, binding: ToolBinding) -> None:
        if binding.spec.name in self._bindings:
            raise ToolError(f"duplicate tool: {binding.spec.name}")
        self._bindings[binding.spec.name] = binding

    def get(self, name: str) -> ToolBinding:
        try:
            return self._bindings[name]
        except KeyError:
            raise ToolError(f"tool not found: {name}") from None

    def contains(self, name: str) -> bool:
        return name in self._bindings

    def all(self) -> list[ToolBinding]:
        return list(self._bindings.values())

    def names(self) -> list[str]:
        return sorted(self._bindings)

    def __len__(self) -> int:
        return len(self._bindings)


def binding_from_function_tool(
    fn_tool: Any,
    source: str = "native",
) -> ToolBinding:
    """把一个 SDK FunctionTool 包装成 ToolBinding（自动带目录元数据）。"""
    name = getattr(fn_tool, "name", "")
    description = getattr(fn_tool, "description", "") or ""
    schema = getattr(fn_tool, "params_json_schema", None) or {}
    spec = spec_for(name, description=description, input_schema=schema, source=source)
    return ToolBinding(spec=spec, invoke=fn_tool)


def discover_from_agent(agent: Any) -> ToolRegistry:
    """从主 Agent 的 tools 列表发现并登记全部工具（行为零改动）。"""
    registry = ToolRegistry()
    for fn_tool in getattr(agent, "tools", []) or []:
        # 来源必须来自真实注册标记，不能按类型名/工具名猜：
        # - MCP bridge 挂载的 FunctionTool 带 _mcp_source="mcp"；
        # - skills_loader 收集的本地技能工具带 _tool_origin="plugin"；
        # - 其余核心工具为 native（builtin）。
        if getattr(fn_tool, "_mcp_source", None) == "mcp":
            source = "mcp"
        elif getattr(fn_tool, "_tool_origin", None) == "plugin":
            source = "skill"
        else:
            source = "native"
        registry.register(binding_from_function_tool(fn_tool, source=source))
    return registry
