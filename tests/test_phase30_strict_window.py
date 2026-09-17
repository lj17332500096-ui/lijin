"""Phase 30: Strict Window Batch / Side-Effect / Concurrency tests.

Proves that with K=3, a batch of 5 tool calls in one assistant response
executes only the first 3; tools 4-5 never enter their bodies.
"""
import asyncio
import sys
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents import Agent, Runner, RunConfig
from agents.items import ModelResponse, ResponseFunctionToolCall, ResponseOutputMessage
from agents.models.interface import Model
from agents.usage import Usage
from agents.tool import function_tool

from benchmark.bounded_window import (
    ExecutionQuota,
    WindowClosed,
    WindowHooks,
    wrap_tools_with_quota,
)

# Global side-effect marker list
SIDE_EFFECTS: list[str] = []


def make_marker_tool():
    @function_tool
    def marker_tool(tag: str) -> str:
        """Append a marker to the global side-effect list."""
        SIDE_EFFECTS.append(tag)
        return f"marked {tag}"
    return marker_tool


class BatchModel(Model):
    """Returns N tool calls in one response, then a final text on the 2nd call."""

    def __init__(self, n_calls: int = 5) -> None:
        self.n_calls = n_calls
        self.call_count = 0

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, **kwargs) -> ModelResponse:
        self.call_count += 1
        if self.call_count == 1:
            output = [
                ResponseFunctionToolCall(
                    type="function_call", name="marker_tool",
                    arguments=f'{{"tag": "tool_{i+1}"}}', call_id=f"c{i+1}")
                for i in range(self.n_calls)
            ]
        else:
            output = [ResponseOutputMessage(
                id="m1", type="message", role="assistant", status="completed",
                content=[{"type": "output_text", "text": "done"}])]
        return ModelResponse(output=output, usage=Usage(), response_id="r1")

    async def stream_response(self, *a, **k):
        yield await self.get_response(*a, **k)


async def _run(n_calls: int, k: int):
    SIDE_EFFECTS.clear()
    tool = make_marker_tool()  # fresh tool each run (avoid nested wrapping)
    agent = Agent(name="batch", instructions="test", model=BatchModel(n_calls),
                  tools=[tool])
    quota = ExecutionQuota(k)
    wrap_tools_with_quota(agent, quota)
    hooks = WindowHooks(max_executions=k)
    status = "RUNNING"
    try:
        await Runner.run(agent, input="go", hooks=hooks,
                         run_config=RunConfig(tracing_disabled=True), max_turns=5)
        status = hooks.window_status
    except WindowClosed:
        status = "WINDOW_CLOSED"
    except Exception as e:
        status = f"ERR:{type(e).__name__}:{str(e)[:120]}"
    return status, list(SIDE_EFFECTS), hooks, quota


def test_batch_5_calls_k3():
    """5 tool calls, K=3 -> only tool_1..tool_3 have side effects."""
    status, effects, hooks, quota = asyncio.run(_run(5, 3))
    print(f"  status={status} effects={effects} non_blocked={hooks.non_blocked_executions}")
    assert effects == ["tool_1", "tool_2", "tool_3"], f"unexpected effects: {effects}"
    assert hooks.non_blocked_executions <= 3, f"overshoot: {hooks.non_blocked_executions}"
    assert "tool_4" not in effects and "tool_5" not in effects
    print("  PASS: batch 5 calls -> only 3 executed")


def test_batch_3_calls_k3():
    """Exactly 3 tool calls, K=3 -> all 3 execute."""
    status, effects, hooks, quota = asyncio.run(_run(3, 3))
    print(f"  status={status} effects={effects}")
    assert effects == ["tool_1", "tool_2", "tool_3"], f"effects={effects}"
    print("  PASS: batch 3 calls -> all 3 executed")


def test_batch_1_call_k3():
    """1 tool call, K=3 -> 1 executes."""
    status, effects, hooks, quota = asyncio.run(_run(1, 3))
    print(f"  status={status} effects={effects}")
    assert effects == ["tool_1"]
    print("  PASS: batch 1 call -> 1 executed")


def test_concurrency_repeat():
    """Repeat batch-5 K=3 test 20 times, assert no overshoot ever."""
    for i in range(20):
        status, effects, hooks, quota = asyncio.run(_run(5, 3))
        assert hooks.non_blocked_executions <= 3, f"run {i}: overshoot {hooks.non_blocked_executions}"
        assert effects == ["tool_1", "tool_2", "tool_3"], f"run {i}: effects={effects}"
    print("  PASS: 20 repeats, no overshoot")


if __name__ == "__main__":
    print("Strict Window Batch Tests:")
    test_batch_5_calls_k3()
    test_batch_3_calls_k3()
    test_batch_1_call_k3()
    test_concurrency_repeat()
    print("\nALL STRICT WINDOW BATCH TESTS PASS")
