"""runtime 包：Agent Runtime（errors/spec/registry/broker/runner + Task 体系）。"""

from runtime.broker import ToolBroker
from runtime.errors import (
    AgentError,
    ApprovalRequired,
    BudgetExceeded,
    ModelError,
    PolicyDenied,
    RuntimeError,
    TaskCancelled,
    ToolError,
    ToolNotFound,
    ToolTimeout,
)
from runtime.registry import (
    ToolBinding,
    ToolRegistry,
    binding_from_function_tool,
    discover_from_agent,
)
from runtime.runner import AgentRuntime, RunResult
from runtime.spec import TOOL_CATALOG, ToolSpec, spec_for
from runtime.state_machine import (
    ALLOWED_TRANSITIONS,
    RESUMABLE_FROM,
    TERMINAL_STATES,
    assert_transition,
    can_transition,
)
from runtime.task import (
    RunBudget,
    Task,
    TaskEvent,
    TaskState,
    TaskUsage,
    utcnow_iso,
)
from runtime.task_manager import DEFAULT_DB_PATH, TaskManager

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AgentError",
    "AgentRuntime",
    "ApprovalRequired",
    "BudgetExceeded",
    "DEFAULT_DB_PATH",
    "ModelError",
    "PolicyDenied",
    "RESUMABLE_FROM",
    "RESUMABLE_FROM_STR",
    "RunBudget",
    "RunResult",
    "RuntimeError",
    "TERMINAL_STATES",
    "TOOL_CATALOG",
    "Task",
    "TaskCancelled",
    "TaskEvent",
    "TaskManager",
    "TaskState",
    "TaskUsage",
    "ToolBinding",
    "ToolBroker",
    "ToolError",
    "ToolNotFound",
    "ToolRegistry",
    "ToolSpec",
    "ToolTimeout",
    "assert_transition",
    "binding_from_function_tool",
    "can_transition",
    "discover_from_agent",
    "spec_for",
    "utcnow_iso",
]

RESUMABLE_FROM_STR = sorted(s.value for s in RESUMABLE_FROM)
