import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import mcp_bridge


class ConfigParseTests(unittest.TestCase):
    def test_empty_config_ok(self) -> None:
        self.assertEqual(mcp_bridge.parse_specs(""), ([], []))
        self.assertEqual(mcp_bridge.parse_specs("   "), ([], []))

    def test_valid_json(self) -> None:
        text = (
            '[{"name": "github", "command": "npx", "args": ["-y", "@github/mcp-server"], '
            '"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}}]'
        )
        specs, errors = mcp_bridge.parse_specs(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["name"], "github")
        self.assertEqual(specs[0]["command"], "npx")
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", specs[0]["env"])

    def test_invalid_json_reports_error(self) -> None:
        specs, errors = mcp_bridge.parse_specs("{not json")
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_missing_fields_and_duplicate_names(self) -> None:
        text = '[{"name": "a", "command": "x"}, {"name": "a", "command": "y"}, {"args": ["z"]}]'
        specs, errors = mcp_bridge.parse_specs(text)
        self.assertEqual(len(specs), 1)
        self.assertGreaterEqual(len(errors), 1)


class EnsureTests(unittest.TestCase):
    def test_no_config_is_noop(self) -> None:
        old = mcp_bridge._connected
        mcp_bridge._connected = False
        import os

        prev = os.environ.get("MCP_SERVERS")
        os.environ["MCP_SERVERS"] = ""
        import asyncio

        ok = asyncio.run(mcp_bridge.ensure_connected())
        self.assertTrue(ok)
        self.assertEqual(mcp_bridge._connected_names, [])
        if prev is None:
            os.environ.pop("MCP_SERVERS", None)
        else:
            os.environ["MCP_SERVERS"] = prev
        mcp_bridge._connected = old


class _FakeServer:
    """只提供 call_tool 的替身，用来驱动 _make_mcp_invoke 的重试路径。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def call_tool(self, remote_name, arguments):
        self.calls += 1
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, BaseException):
            raise item
        return item


def _invoke(server, args_json='{"q": "x"}'):
    import asyncio

    fn = mcp_bridge._make_mcp_invoke(server, "remote_tool", "srv_remote_tool")
    return asyncio.run(fn(None, args_json))


def _fast_retry(attempts: int):
    """把退避压到 0，避免测试真的等几秒。"""
    import os

    prev = {k: os.environ.get(k) for k in
            ("FORGE_MCP_ATTEMPTS", "FORGE_MCP_BASE_DELAY", "FORGE_MCP_RETRY_CAP")}
    os.environ["FORGE_MCP_ATTEMPTS"] = str(attempts)
    os.environ["FORGE_MCP_BASE_DELAY"] = "0"
    os.environ["FORGE_MCP_RETRY_CAP"] = "0"
    return prev


def _restore(prev):
    import os

    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class McpRetryTests(unittest.TestCase):
    """MCP 工具的网络重试现已统一走 resilience.run_with_retries。

    此前 mcp_bridge 自带一份等价实现（同样的指数退避 + full jitter + 同样的
    异常集合），两份策略会各自漂移 —— 本组测试锁住"仍会重试、且不重复重试非网络错误"。
    """

    def test_retries_network_error_then_succeeds(self) -> None:
        prev = _fast_retry(3)
        try:
            srv = _FakeServer([ConnectionError("boom"), OSError("again"), "payload"])
            out = _invoke(srv)
            self.assertEqual(srv.calls, 3, "应当在两次网络失败后第三次成功")
            self.assertIn("payload", out)
        finally:
            _restore(prev)

    def test_exhausts_attempts_and_reports_count(self) -> None:
        prev = _fast_retry(3)
        try:
            srv = _FakeServer([TimeoutError("t1"), TimeoutError("t2"), TimeoutError("t3")])
            out = _invoke(srv)
            self.assertEqual(srv.calls, 3, "应当恰好尝试 attempts 次")
            self.assertIn("已重试 3 次", out)
            self.assertIn("TimeoutError", out)
        finally:
            _restore(prev)

    def test_non_network_error_is_not_retried(self) -> None:
        prev = _fast_retry(3)
        try:
            srv = _FakeServer([ValueError("bad args")])
            out = _invoke(srv)
            self.assertEqual(srv.calls, 1, "非网络类错误不该重试")
            self.assertIn("ValueError", out)
            self.assertNotIn("已重试", out)
        finally:
            _restore(prev)

    def test_invalid_json_is_rejected_before_calling(self) -> None:
        srv = _FakeServer(["ok"])
        out = _invoke(srv, args_json="{not json")
        self.assertEqual(srv.calls, 0, "参数非法时不该发起调用")
        self.assertIn("不是合法 JSON", out)

    def test_retry_policy_comes_from_single_source(self) -> None:
        """重试异常集合必须来自 resilience 的唯一一份定义，不得在 mcp_bridge 内另抄。"""
        import inspect

        import resilience

        self.assertTrue(hasattr(resilience, "NETWORK_RETRYABLE_EXCEPTIONS"))
        self.assertIs(resilience.NETWORK_RETRYABLE_EXCEPTIONS,
                      resilience._RETRYABLE_EXCEPTIONS)
        src = inspect.getsource(mcp_bridge._make_mcp_invoke)
        self.assertIn("NETWORK_RETRYABLE_EXCEPTIONS", src)
        self.assertIn("run_with_retries", src)
        # 旧实现的两个特征：本地拼退避、本地 random
        self.assertNotIn("_random.uniform", src)
        self.assertNotIn("backoff = min(cap", src)
        for exc_name in ("ConnectionError", "TimeoutError", "OSError"):
            self.assertNotIn(exc_name, src,
                             f"mcp_bridge 里又出现本地异常元组成员 {exc_name}")


if __name__ == "__main__":
    unittest.main()
