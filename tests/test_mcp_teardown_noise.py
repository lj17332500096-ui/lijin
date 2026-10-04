"""P2：MCP 关停不再把 ~43 行 anyio 噪音糊在用户终端上，且不掩盖真故障。

回归背景（真实现场）：程序退出时 6 个 stdio MCP server 各报一遍
`RuntimeError: Cancel scope is different ...`，约 50 行噪音直接印在终端上。
本文件锁住三件事：

1. **噪音定向静音**：只认 anyio 自己的「跨 task 退出 cancel scope」消息；
   其他 RuntimeError（一律 RuntimeError 吞掉 = 掩盖真 bug）必须照旧冒泡。
2. **不中断清理**：`CancelledError` 继承 `BaseException` 而非 `Exception`，
   修复前的 `except Exception` 抓不到它 ⇒ 一个 server 被取消会中断整个循环，
   后面所有 server 都不清理（子进程残留）。
3. **不打 traceback**：良性噪音降级为一行 debug，不再 `exc_info=True`
   每个 server 印一条完整 traceback（实测 6 个 server = 43 行）。

全部离线：不联网、不启动真实 MCP 子进程（用桩替代）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from integrations import mcp_bridge

#: anyio/_backends/_asyncio.py::CancelScope.__exit__ 的原话（勿手改，上游文案）
CROSS_TASK_MSG = ("Attempted to exit cancel scope in a different task "
                  "than it was entered in")
NOT_CURRENT_SCOPE_MSG = ("Attempted to exit a cancel scope that isn't the current "
                         "tasks's current cancel scope")


class _StubServer:
    """最小 MCP server 桩：cleanup 按预设抛错。"""

    def __init__(self, name: str, exc: BaseException | None = None) -> None:
        self.name = name
        self.cleanup_calls = 0
        self._exc = exc

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self._exc is not None:
            raise self._exc


class _CloseServersBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        mcp_bridge._servers.clear()
        self.addCleanup(mcp_bridge._servers.clear)

    def _install(self, *servers: _StubServer) -> None:
        mcp_bridge._servers.extend(servers)


class CrossTaskNoiseIsSilencedTests(_CloseServersBase):
    """良性跨 task 噪音：静音、且不打 traceback。"""

    async def test_benign_cross_task_runtime_error_is_not_reported(self) -> None:
        self._install(*[_StubServer(f"s{i}", RuntimeError(CROSS_TASK_MSG)) for i in range(6)])
        with self.assertLogs(mcp_bridge._logger, level="DEBUG"):
            errors = await mcp_bridge.close_servers()
        self.assertEqual(errors, [], "良性跨 task 噪音不该进 errors（它是已解释的关停竞态）")

    async def test_sibling_not_current_scope_message_also_silenced(self) -> None:
        """anyio 同一处抛出的另一条消息也应命中（同族，不是新 recognize）。"""
        self._install(_StubServer("s0", RuntimeError(NOT_CURRENT_SCOPE_MSG)))
        with self.assertLogs(mcp_bridge._logger, level="DEBUG"):
            errors = await mcp_bridge.close_servers()
        self.assertEqual(errors, [])

    async def test_cancelled_error_from_cancel_scope_is_silenced(self) -> None:
        self._install(*[_StubServer(f"s{i}", asyncio.CancelledError("Cancelled via cancel scope 1d09"))
                        for i in range(6)])
        with self.assertLogs(mcp_bridge._logger, level="DEBUG"):
            errors = await mcp_bridge.close_servers()
        self.assertEqual(errors, [])

    async def test_no_traceback_recorded_for_benign_noise(self) -> None:
        """核心诉求：不再每个 server 印一条完整 traceback。"""
        self._install(*[_StubServer(f"s{i}", RuntimeError(CROSS_TASK_MSG)) for i in range(6)])
        with self.assertLogs(mcp_bridge._logger, level="DEBUG") as cap:
            await mcp_bridge.close_servers()
        self.assertTrue(cap.records, "应留一行 debug 供追查")
        for rec in cap.records:
            self.assertIsNone(
                rec.exc_info,
                f"{rec.getMessage()}：良性噪音仍挂了 exc_info（这就是那 43 行）")

    async def test_one_debug_record_per_server_not_a_traceback(self) -> None:
        """计数断言：恰好 6 条（每 server 一行），而不是靠 assertIn 蒙混。"""
        with self.assertLogs(mcp_bridge._logger, level="DEBUG") as cap:
            self._install(*[_StubServer(f"s{i}", RuntimeError(CROSS_TASK_MSG)) for i in range(6)])
            await mcp_bridge.close_servers()
        self.assertEqual(len(cap.records), 6)


class RealFailuresStillSurfaceTests(_CloseServersBase):
    """绝不 blanket 吞：真 bug / 真取消必须照旧可见，且不中断后续 server 的清理。"""

    async def test_unrelated_runtime_error_is_reported(self) -> None:
        self._install(_StubServer("s0", RuntimeError("some genuine defect")))
        errors = await mcp_bridge.close_servers()
        self.assertEqual(len(errors), 1)
        self.assertIn("some genuine defect", errors[0])

    async def test_unrelated_exception_types_reported(self) -> None:
        for exc in (FileNotFoundError("server binary missing"),
                    ValueError("bad params"),
                    TimeoutError("teardown timed out")):
            with self.subTest(exc=type(exc).__name__):
                mcp_bridge._servers.clear()
                self._install(_StubServer("s0", exc))
                errors = await mcp_bridge.close_servers()
                self.assertEqual(len(errors), 1, f"{type(exc).__name__} 被误静音了")

    async def test_cancelled_error_does_not_abort_remaining_servers(self) -> None:
        """CancelledError 不再中断循环：6 个 server 必须全部尝试清理。"""
        servers = [_StubServer(f"s{i}", asyncio.CancelledError("user cancelled the run"))
                   for i in range(6)]
        self._install(*servers)
        errors = await mcp_bridge.close_servers()
        self.assertEqual(len(errors), 6, "取消必须逐个隔离后继续，而不是中断整个循环")
        for srv in servers:
            self.assertEqual(srv.cleanup_calls, 1,
                             f"{srv.name} 没被清理（修复前 CancelledError 会中断循环）")

    async def test_noise_does_not_stop_later_servers_from_being_cleaned(self) -> None:
        """一个 server 出良性噪音，其余仍必须被清理。"""
        servers = [_StubServer("noisy", RuntimeError(CROSS_TASK_MSG)),
                   _StubServer("ok0"), _StubServer("ok1")]
        self._install(*servers)
        errors = await mcp_bridge.close_servers()
        self.assertEqual(errors, [])
        for srv in servers:
            self.assertEqual(srv.cleanup_calls, 1, f"{srv.name} 未被清理")

    async def test_real_failure_does_not_stop_later_servers(self) -> None:
        servers = [_StubServer("boom", RuntimeError("real defect")),
                   _StubServer("ok0"), _StubServer("ok1")]
        self._install(*servers)
        errors = await mcp_bridge.close_servers()
        self.assertEqual(len(errors), 1)
        self.assertEqual([s.cleanup_calls for s in servers], [1, 1, 1])

    async def test_warning_for_real_failure_still_has_traceback(self) -> None:
        """真故障仍要带 traceback —— 降级只针对良性噪音。"""
        with self.assertLogs(mcp_bridge._logger, level="WARNING") as cap:
            self._install(_StubServer("s0", RuntimeError("real defect")))
            await mcp_bridge.close_servers()
        self.assertEqual(len(cap.records), 1)
        self.assertIsNotNone(cap.records[0].exc_info,
                             "真故障必须保留 traceback，否则无法追查")


class ModuleStateResetTests(_CloseServersBase):
    """关停后模块状态必须干净（回归：噪音分支不得跳过状态复位）。"""

    async def test_state_is_reset_even_with_benign_noise(self) -> None:
        mcp_bridge._connected_names.extend(["a", "b"])
        mcp_bridge._mounted_names.append("a")
        mcp_bridge._connected = True
        self.addCleanup(mcp_bridge._connected_names.clear)
        self.addCleanup(mcp_bridge._mounted_names.clear)
        self._install(_StubServer("s0", RuntimeError(CROSS_TASK_MSG)))
        await mcp_bridge.close_servers()
        self.assertFalse(mcp_bridge._connected)
        self.assertEqual(len(mcp_bridge._connected_names), 0)
        self.assertEqual(len(mcp_bridge._mounted_names), 0)


if __name__ == "__main__":
    unittest.main()
