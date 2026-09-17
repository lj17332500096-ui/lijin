import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import main as main_module
from runtime.errors import FinalResponseFailed


class _FakeResult:
    final_output = "ok"


class _CustomTrip(Exception):
    """模拟 OutputGuardrailTripwireTriggered（被 patch 进 main 模块命名空间）。"""


class StreamPipeTests(unittest.TestCase):
    """v18 流式导出：重试前必须发 stream_reset（清掉失败增量）+ 自动重试工具帧。"""

    def test_guardrail_retry_emits_stream_reset_then_success(self) -> None:
        events: list[tuple[str, dict]] = []
        attempts = {"n": 0}

        async def fake_run_attempt(mode, message, session=None, run_config=None, max_turns=20,
                                   debug=False, agent=None, stream_events_cb=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                if stream_events_cb:
                    stream_events_cb("reply_delta", {"text": "坏内容"})
                raise _CustomTrip("输出不是合法 AgentReply JSON")
            return _FakeResult()

        def cb(name, payload):
            events.append((name, payload))

        async def run():
            with patch.object(main_module, "_run_attempt", new=fake_run_attempt):
                with patch.object(main_module, "ensure_mcp", new=lambda: asyncio.sleep(0)):
                    with patch.object(main_module, "OutputGuardrailTripwireTriggered", new=_CustomTrip):
                        out = await main_module.execute_turn(
                            "stream", "帮我算 1+1", session=None, stream_events_cb=cb
                        )
            return out

        out = asyncio.run(run())
        self.assertEqual(out, "ok")
        names = [n for n, _ in events]
        self.assertEqual(attempts["n"], 2)
        self.assertIn("stream_reset", names)
        self.assertIn("tool", names)
        self.assertLess(names.index("stream_reset"), len(events) - 1)

    def test_no_callback_no_crash(self) -> None:
        """v28+：guardrail 两次失败后不再泄漏 SDK 内部 tripwire，
        改抛 FinalResponseFailed（执行与最终回复分离的契约）。"""

        async def fake_run_attempt(mode, message, session=None, run_config=None, max_turns=20,
                                   debug=False, agent=None, stream_events_cb=None):
            raise _CustomTrip("无可用输出")

        async def run():
            with patch.object(main_module, "_run_attempt", new=fake_run_attempt):
                with patch.object(main_module, "ensure_mcp", new=lambda: asyncio.sleep(0)):
                    with patch.object(main_module, "OutputGuardrailTripwireTriggered", new=_CustomTrip):
                        try:
                            await main_module.execute_turn("stream", "x", session=None)
                        except FinalResponseFailed:
                            return "raised-ok"
            return "no-raise"

        self.assertEqual(asyncio.run(run()), "raised-ok")


if __name__ == "__main__":
    unittest.main()
