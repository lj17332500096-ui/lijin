"""API 接口层审计 Phase D 验收（D3 结构化日志 + D4 重复护栏 Run 隔离）。

运行：PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/test_api_layer_phase_d.py -q
默认不修改任何全局日志配置；install_structured_logging 只在显式开启的环境变量下挂 handler。
"""
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import runtime.execution as runtime_execution

from runtime import structured_log as sl
from runtime.structured_log import (
    current_request_id,
    install_structured_logging,
    new_request_id,
    slog,
)
from runtime.runctx import RunContext


def _read_json_line(line: str) -> dict:
    return json.loads(line)


class StructuredLogBaseTests(unittest.TestCase):
    def setUp(self):
        # 确保干净：清除 request_id contextvar
        sl._request_id.set(None)

    def test_off_by_default_is_silent(self):
        """未开启 FORGE_STRUCTURED_LOG 时 install 返回 False，slog 无可见输出。"""
        os.environ.pop("FORGE_STRUCTURED_LOG", None)
        self.assertFalse(install_structured_logging())

    def test_request_id_generation_and_readback(self):
        rid = new_request_id("turn")
        self.assertTrue(rid.startswith("turn-"))
        self.assertEqual(rid, current_request_id())
        sl._request_id.set(None)
        self.assertIsNone(current_request_id())

    def test_json_line_shape_off_in_memory_stream(self):
        """强制挂到内存 stream（绕过 env 判定）→ 每行是合法 JSON，含 run_id/request_id 字段。"""
        # 临时挂 handler（不读 env）
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(sl._StructuredFormatter())
        target = logging.getLogger(sl.STRUCTURED_LOG_NAME)
        target.addHandler(handler)
        target.setLevel(logging.INFO)
        target.propagate = False
        try:
            new_request_id("turn")
            slog.info("测试事件", key="v1", count=3)
            # run_id：无 RunContext 时应为 None
            line = buf.getvalue().strip()
            self.assertTrue(line, "应至少输出一行 JSON")
            obj = _read_json_line(line)
            self.assertEqual(obj["msg"], "测试事件")
            self.assertEqual(obj["key"], "v1")
            self.assertEqual(obj["count"], 3)
            self.assertIsNone(obj["run_id"])
            self.assertEqual(obj["request_id"], current_request_id())
            self.assertIn("ts", obj)
        finally:
            target.removeHandler(handler)
            target.propagate = True

    def test_run_id_injected_when_in_run(self):
        """在 RunContext 内打日志，run_id 自动注入。"""
        from runtime import runctx
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(sl._StructuredFormatter())
        target = logging.getLogger(sl.STRUCTURED_LOG_NAME)
        target.addHandler(handler)
        target.setLevel(logging.INFO)
        target.propagate = False
        try:
            runctx.bind(RunContext(run_id="run-d3-test"))
            try:
                slog.info("run 内事件", task="t1")
            finally:
                runctx.bind(None)
            obj = _read_json_line(buf.getvalue().strip())
            self.assertEqual(obj["run_id"], "run-d3-test")
        finally:
            target.removeHandler(handler)
            target.propagate = True

    def test_install_idempotent(self):
        """重复挂 handler 不会累积。"""
        buf = io.StringIO()
        target = logging.getLogger(sl.STRUCTURED_LOG_NAME)
        # 手工挂一个带标记的 handler
        h = logging.StreamHandler(buf)
        h._forge_structured = True
        target.addHandler(h)
        try:
            # 即便 FORGE_STRUCTURED_LOG=1，也不应再挂第二个
            os.environ["FORGE_STRUCTURED_LOG"] = "1"
            self.assertTrue(install_structured_logging())
            self.assertEqual(
                [x for x in target.handlers if getattr(x, "_forge_structured", False)],
                [h],
            )
        finally:
            target.removeHandler(h)
            os.environ.pop("FORGE_STRUCTURED_LOG", None)


class StructuredLogWiringTests(unittest.TestCase):
    """各模块确实接了 slog 与 request_id 贯穿（记录层漂移守门）。"""

    def test_runner_imports_slog(self):
        import runtime.runner as runner
        self.assertTrue(hasattr(runner, "slog"))

    def test_task_manager_imports_slog(self):
        import runtime.task_manager as tm
        self.assertTrue(hasattr(tm, "slog"))

    def test_llama_bridge_binds_request_id(self):
        """llama_bridge._request_id 会绑定 context（D3 贯穿）。"""
        import llama_bridge

        # 构造一个最小 Request 替身（只读 headers）
        class _FakeReq:
            headers = {"x-request-id": "external-123"}

        before = current_request_id()
        rid = llama_bridge._request_id(_FakeReq())
        self.assertEqual(rid, "external-123")
        self.assertEqual(current_request_id(), "external-123")
        # 清理，避免污染其它用例
        sl._request_id.set(before)

    def test_main_turn_creates_request_id(self):
        """runtime_execution.execute_turn 入口会调 new_request_id（grep 确认）。"""
        execution_src = (BASE / "runtime" / "execution.py").read_text(encoding="utf-8")
        main_src = (BASE / "main.py").read_text(encoding="utf-8")
        self.assertIn("new_request_id", execution_src)
        self.assertIn("install_structured_logging", main_src)


class RepeatGuardRunIsolationTests(unittest.TestCase):
    """D4 的 Run 级隔离 + 回退路径（与 test_tools.py 里的同名测试互补，
    这里用 RunContext 直接验证 note_repeat_call 的语义）。"""

    def setUp(self):
        import tools
        self._tools = tools
        tools._last_repeat_calls.clear()

    def tearDown(self):
        self._tools._last_repeat_calls.clear()

    def test_run_context_repeat_call_isolated(self):
        from runtime.runctx import RunContext
        r1 = RunContext(run_id="a")
        self.assertFalse(r1.note_repeat_call("web_search", "q"))
        self.assertTrue(r1.note_repeat_call("web_search", "q"))
        r2 = RunContext(run_id="b")
        self.assertFalse(r2.note_repeat_call("web_search", "q"))

    def test_process_level_fallback_when_no_run(self):
        self.assertFalse(self._tools._too_repetitive("x", "k"))
        self.assertTrue(self._tools._too_repetitive("x", "k"))
        self.assertIn("x", self._tools._last_repeat_calls)


if __name__ == "__main__":
    unittest.main()
