"""API 接口层审计 Phase C 验收测试：C3 统一错误结构 + C6 写操作守卫。

背景（详见 FORGE-API-LAYER-AUDIT-2026-09-22.md §19.6–19.7）
-----------------------------------------------------------
`/api/*` 63 条归档后，Phase C 只剩两项，且都落在 `llama_bridge.py`（冻结层）：

- **C3 统一错误结构**：非 2xx 改为 OpenAI 标准信封
  `{"error": {message, type, code, retryable}, "request_id"}`。
  形状不是自定的，是前端取错函数 `kIe()` 的**首选**分支 `r?.error?.message` 决定的。
  关键约束：**200 响应不得使用该信封**——工具结果走 `plain_text_response`（文本），
  且 200 里的 `error` 必须是**字符串**（前端对它做 `String(a.error)`，对象会变 `[object Object]`）。
- **C6 写操作守卫**：会直接产生副作用的 POST 端点（`/tools`、`/v1/chat/completions`、
  `/v1/chat/completions/control`）要求 `Content-Type: application/json`，否则 415；
  再加 `webapp.main()` 的**绑定断言**（非回环监听需 `FORGE_ALLOW_NONLOCAL_UI=1`）。

本文件用 TestClient 断言「守卫生效」与「契约未被打破」两侧，后者同样重要：
过度应用守卫（例如把 415 加到无 body 的 GET/DELETE 上）会静默打断 llama-ui。

注意 TestClient 的 `request.client.host` 是 `"testclient"`（**不是回环**），
这正是本方案选择「绑定断言」而非「逐请求回环断言」的原因——后者会打死全部 UI 测试。
"""

from __future__ import annotations

import os
import sys
import asyncio
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import llama_bridge as lb  # noqa: E402


# ---------------------------------------------------------------------------
# 桩：避免测试真的实例化 AgentRuntime / 执行工具
# ---------------------------------------------------------------------------
class _StubGate:
    def __init__(self, gated=()):
        self._gated = set(gated)

    def should_gate(self, name: str) -> bool:
        return name in self._gated


class _StubBroker:
    def __init__(self, result="ok", raises=None):
        self.calls: list = []
        self._result = result
        self._raises = raises

    async def execute(self, tool, params):
        self.calls.append((tool, params))
        if self._raises is not None:
            raise self._raises
        return self._result


class _StubSpec:
    def __init__(self, name, side_effect=False):
        self.name = name
        self.description = f"{name} desc"
        self.input_schema = {"type": "object", "properties": {}}
        self.side_effect = side_effect


class _StubBinding:
    def __init__(self, name, side_effect=False):
        self.spec = _StubSpec(name, side_effect)


class _StubRegistry:
    def all(self):
        return [_StubBinding("think"), _StubBinding("write_code_file", True)]

    def get(self, name):
        for binding in self.all():
            if binding.spec.name == name:
                return binding
        raise KeyError(name)


class _StubRuntime:
    def __init__(self, gated=(), broker=None):
        self.approval = _StubGate(gated)
        self.broker = broker or _StubBroker()
        self.registry = _StubRegistry()

    def _ensure(self):
        pass

    async def execute_readonly_api_tool(self, tool, params, *, session_id, user_id=None):
        result = await self.broker.execute(tool, params)
        return {"run_id": "fixture-run", "result": result, "execution_status": "executed"}


def _make_app() -> Starlette:
    """只挂本阶段涉及的端点，不引入 webapp 全量路由。"""
    return Starlette(routes=[
        Route("/tools", lb.api_tools_list, methods=["GET"]),
        Route("/tools", lb.api_tools_execute, methods=["POST"]),
        Route("/v1/chat/completions", lb.api_chat_completions, methods=["POST"]),
        Route("/v1/chat/completions/control", lb.api_chat_control, methods=["POST"]),
        Route("/v1/streams/lookup", lb.api_stream_lookup, methods=["POST"]),
        Route("/v1/stream", lb.api_stream_resume, methods=["GET", "DELETE"]),
        Route("/models/load", lb.api_models_load, methods=["POST"]),
        Route("/models/unload", lb.api_models_unload, methods=["POST"]),
    ])


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(_make_app(), follow_redirects=False)

    def _patched_runtime(self, stub):
        """把 AgentRuntime.get_default 换成桩（llama_bridge 在函数内 import）。"""
        from runtime.runner import AgentRuntime
        return mock.patch.object(AgentRuntime, "get_default", return_value=stub)


# ---------------------------------------------------------------------------
# C6 —— 写操作守卫（Content-Type）
# ---------------------------------------------------------------------------
class JsonContentTypeGuardTests(_Base):
    def test_tools_post_without_content_type_is_415(self) -> None:
        r = self.client.post("/tools", content=b'{"tool":"think"}')
        self.assertEqual(r.status_code, 415, "无 Content-Type 的 POST /tools 必须被拒")
        body = r.json()
        self.assertEqual(body["error"]["code"], "unsupported_media_type")
        self.assertIn("request_id", body)

    def test_tools_post_with_text_plain_is_415(self) -> None:
        """跨站简单请求的典型形态：text/plain，浏览器不发预检即投递。"""
        r = self.client.post("/tools", content=b'{"tool":"think"}',
                             headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 415)

    def test_tools_post_with_json_passes_guard(self) -> None:
        stub = _StubRuntime(gated=set())
        with self._patched_runtime(stub):
            r = self.client.post("/tools", json={"tool": "think"})
        self.assertNotEqual(r.status_code, 415, "带 application/json 的请求不得被守卫拦下")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stub.broker.calls, [("think", {})])

    def test_control_post_without_json_is_415(self) -> None:
        r = self.client.post("/v1/chat/completions/control", content=b'{"run_id":"r1"}')
        self.assertEqual(r.status_code, 415)

    def test_get_stream_is_not_guarded(self) -> None:
        """前端 GET /v1/stream 走 A0()，本就不带 Content-Type —— 不得被 415 打断。"""
        r = self.client.get("/v1/stream?conv_id=nope&from=0")
        self.assertNotEqual(r.status_code, 415)
        self.assertEqual(r.status_code, 404)

    def test_delete_stream_is_not_guarded(self) -> None:
        r = self.client.delete("/v1/stream?conv_id=nope")
        self.assertNotEqual(r.status_code, 415)

    def test_readonly_post_is_not_guarded(self) -> None:
        """守卫只覆盖**写**端点；/v1/streams/lookup 是只读，按设计不拦。"""
        r = self.client.post("/v1/streams/lookup", content=b"{}")
        self.assertNotEqual(r.status_code, 415)

    def test_guard_runs_before_runtime_instantiation(self) -> None:
        """被拒的请求不得触碰 AgentRuntime（否则等于给了 DoS 一个副作用入口）。"""
        from runtime.runner import AgentRuntime
        with mock.patch.object(AgentRuntime, "get_default",
                              side_effect=AssertionError("不应实例化 Runtime")):
            r = self.client.post("/tools", content=b'{"tool":"think"}')
        self.assertEqual(r.status_code, 415)


# ---------------------------------------------------------------------------
# C3 —— 统一错误结构
# ---------------------------------------------------------------------------
class UnifiedErrorEnvelopeTests(_Base):
    def _assert_envelope(self, body: dict) -> None:
        self.assertIn("error", body)
        err = body["error"]
        self.assertIsInstance(err, dict, "信封里的 error 必须是对象（前端读 error.message）")
        for key in ("message", "type", "code", "retryable"):
            self.assertIn(key, err, f"信封缺字段 {key}")
        self.assertIsInstance(err["message"], str)
        self.assertIsInstance(err["code"], str)
        self.assertIsInstance(err["retryable"], bool)
        self.assertIn("request_id", body)


class NonStreamingChatRouteTests(_Base):
    """完整路由回归：忽略 Run 活动事件，直到消费到终态事件。"""

    def _patch_chat_turn(self, events):
        queue = asyncio.Queue()
        for item in events:
            queue.put_nowait(item)

        async def fake_run_chat_turn(*args, **kwargs):
            return "run-123", "session-123", queue, asyncio.sleep(0)

        return mock.patch.object(lb, "_run_chat_turn", side_effect=fake_run_chat_turn)

    def test_non_stream_waits_for_end_after_activity_events(self) -> None:
        events = [
            ("control", {"type": "run.started"}),
            ("assistant_delta", {"metadata": {"delta": "天气"}}),
            ("__end__", {"content": "今天晴。", "kind": "answer", "state": "completed", "ok": True}),
        ]
        with self._patch_chat_turn(events):
            response = self.client.post("/v1/chat/completions", json={
                "stream": False,
                "messages": [{"role": "user", "content": "今天天气"}],
            })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "今天晴。")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")

    def test_non_stream_returns_terminal_run_error_after_activity(self) -> None:
        events = [
            ("control", {"type": "run.started"}),
            ("__error__", {"error": "provider unavailable"}),
        ]
        with self._patch_chat_turn(events):
            response = self.client.post("/v1/chat/completions", json={
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            })
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "run_failed")

    def test_model_mismatch_is_rejected_before_starting_a_run(self) -> None:
        async def unexpected_run(*args, **kwargs):
            raise AssertionError("model mismatch must not start a Run")

        with mock.patch.object(lb, "_model_name", return_value="active-model"), \
                mock.patch.object(lb, "_run_chat_turn", side_effect=unexpected_run):
            response = self.client.post("/v1/chat/completions", json={
                "model": "other-model",
                "messages": [{"role": "user", "content": "hello"}],
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "model_not_configured")

    def test_usage_and_model_are_taken_from_terminal_metadata(self) -> None:
        events = [("__end__", {
            "content": "回答", "kind": "answer", "state": "completed", "ok": True,
            "model": "actual-provider-model",
            "usage": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22},
        })]
        with mock.patch.object(lb, "_model_name", return_value="active-alias"), \
                self._patch_chat_turn(events):
            response = self.client.post("/v1/chat/completions", json={
                "model": "active-alias", "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "actual-provider-model")
        self.assertEqual(body["usage"], {
            "prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22,
        })


class UnifiedErrorEnvelopeTests(_Base):
    def _assert_envelope(self, body: dict) -> None:
        self.assertIn("error", body)
        err = body["error"]
        self.assertIsInstance(err, dict, "信封里的 error 必须是对象（前端读 error.message）")
        for key in ("message", "type", "code", "retryable"):
            self.assertIn(key, err, f"信封缺字段 {key}")
        self.assertIsInstance(err["message"], str)
        self.assertIsInstance(err["code"], str)
        self.assertIsInstance(err["retryable"], bool)
        self.assertIn("request_id", body)

    def test_stream_not_found_is_enveloped_404(self) -> None:
        r = self.client.get("/v1/stream?conv_id=absent&from=0")
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self._assert_envelope(body)
        self.assertEqual(body["error"]["code"], "stream_not_found")
        # 前端也会读 conv_id 做本地状态清理，必须保留
        self.assertEqual(body["conv_id"], "absent")

    def test_missing_tool_is_enveloped_400(self) -> None:
        with self._patched_runtime(_StubRuntime()):
            r = self.client.post("/tools", json={})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self._assert_envelope(body)
        self.assertEqual(body["error"]["code"], "missing_tool")

    def test_non_string_tool_is_400_not_500(self) -> None:
        """工具名传对象会在 gate 的集合查找里抛 TypeError → 必须前置校验成 400。"""
        with self._patched_runtime(_StubRuntime()):
            r = self.client.post("/tools", json={"tool": {"nested": 1}})
        self.assertEqual(r.status_code, 400)
        self._assert_envelope(r.json())

    def test_malformed_json_body_is_400_not_500(self) -> None:
        with self._patched_runtime(_StubRuntime()):
            r = self.client.post("/tools", content=b"{not json",
                                 headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self._assert_envelope(r.json())

    def test_missing_run_id_is_enveloped_400_with_ok_false(self) -> None:
        with self._patched_runtime(_StubRuntime()):
            r = self.client.post("/v1/chat/completions/control", json={})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self._assert_envelope(body)
        self.assertEqual(body["error"]["code"], "missing_run_id")
        self.assertIs(body.get("ok"), False, "旧契约字段 ok=False 需保留")

    def test_request_id_is_echoed_when_supplied(self) -> None:
        with self._patched_runtime(_StubRuntime()):
            r = self.client.post("/tools", json={}, headers={"X-Request-Id": "trace-abc-123"})
        self.assertEqual(r.json()["request_id"], "trace-abc-123")

    def test_request_id_is_generated_when_absent(self) -> None:
        with self._patched_runtime(_StubRuntime()):
            r = self.client.post("/tools", json={})
        rid = r.json()["request_id"]
        self.assertIsInstance(rid, str)
        self.assertTrue(rid.startswith("llmb-"), rid)

    def test_envelope_shape_matches_frontend_first_branch(self) -> None:
        """锁住与前端 `kIe()` 首选分支 `r?.error?.message` 的对齐关系。

        若有人把信封改成 `{"error": "字符串"}` 或把 message 挪到顶层，
        这条会失败——而那正好会让前端退到较低优先级的兜底分支。
        """
        r = self.client.get("/v1/stream?conv_id=absent&from=0")
        body = r.json()
        self.assertIsInstance(body["error"], dict)
        self.assertTrue(body["error"].get("message"), "error.message 必须非空")


# ---------------------------------------------------------------------------
# C3 —— 200 路径契约（防止信封被误用到成功路径）
# ---------------------------------------------------------------------------
class SuccessPathContractTests(_Base):
    def test_model_lifecycle_stubs_do_not_claim_success(self) -> None:
        for path, operation in (("/models/load", "load"), ("/models/unload", "unload")):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 501)
            self.assertIs(response.json()["success"], False)
            self.assertEqual(response.json()["status"], "unsupported")
            self.assertEqual(response.json()["operation"], operation)

    def test_ungated_tool_returns_plain_text_response(self) -> None:
        stub = _StubRuntime(gated=set())
        stub.broker = _StubBroker(result="计算结果 42")
        with self._patched_runtime(stub):
            r = self.client.post("/tools", json={"tool": "think", "params": {"q": 1}})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("plain_text_response"), "计算结果 42")
        self.assertNotIn("error", body, "成功路径不得带 error 键")

    def test_tool_execution_failure_stays_200_with_string_error(self) -> None:
        """工具失败仍走 200 + 文本；即便带 `error` 也必须是字符串。"""
        stub = _StubRuntime(gated=set())
        stub.broker = _StubBroker(raises=RuntimeError("boom"))
        with self._patched_runtime(stub):
            r = self.client.post("/tools", json={"tool": "think"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        if "error" in body:
            self.assertIsInstance(body["error"], str,
                                  "200 里的 error 必须是字符串（前端做 String(a.error)）")
        self.assertIsInstance(body.get("plain_text_response"), str)

    def test_gated_tool_returns_legible_string_error(self) -> None:
        """审批分支的可读性修复。

        原实现只回 {approval_required, approval}，而 bundle 中这两个键命中数**均为 0**
        → 前端落到 executeTool 的兜底 `JSON.stringify(a)`，用户看到原始 JSON。
        现在必须同时给出一条**字符串** error，使阻断有可读反馈。
        """
        stub = _StubRuntime(gated={"write_code_file"})
        with self._patched_runtime(stub):
            r = self.client.post("/tools", json={"tool": "write_code_file"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "tool_requires_agent_run")
        self.assertEqual(stub.broker.calls, [], "被审批门拦下的工具绝不能被真正执行")

    def test_side_effect_tool_is_never_directly_executed(self) -> None:
        stub = _StubRuntime(gated=set())
        with self._patched_runtime(stub):
            response = self.client.post("/tools", json={"tool": "write_code_file"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "tool_requires_agent_run")
        self.assertEqual(stub.broker.calls, [])

    def test_tools_list_shape_unchanged(self) -> None:
        with self._patched_runtime(_StubRuntime()):
            r = self.client.get("/tools")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertIsInstance(rows, list)
        self.assertIn("definition", rows[0])
        self.assertIn("permissions", rows[0])


# ---------------------------------------------------------------------------
# C6 —— 绑定断言（webapp.main 只允许回环监听）
# ---------------------------------------------------------------------------
class LoopbackBindingAssertionTests(unittest.TestCase):
    def test_loopback_detection(self) -> None:
        import webapp

        for host in ("127.0.0.1", "127.0.0.5", "::1", "[::1]", "localhost", "LOCALHOST"):
            self.assertTrue(webapp._is_loopback_host(host), host)
        for host in ("0.0.0.0", "192.168.1.5", "10.0.0.1", "example.com", "", "::", "8.8.8.8"):
            self.assertFalse(webapp._is_loopback_host(host), host)

    def _run_main(self, host: str, allow: bool):
        import webapp
        from ui_frozen import ENABLE_FLAG

        argv = ["webapp.py", "--metrics-port", "0", ENABLE_FLAG, "--host", host]
        env = ({"FORGE_ALLOW_NONLOCAL_UI": "1",
                "FORGE_UI_API_TOKEN": "test-token-" + "x" * 32} if allow else {})
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("uvicorn.run") as mocked_run, \
                mock.patch("webbrowser.open"):
            if not allow:
                os.environ.pop(webapp.ALLOW_NONLOCAL_ENV, None)
            webapp.main()
        return mocked_run

    def test_nonlocal_host_refused_without_optin(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._run_main("0.0.0.0", allow=False)
        self.assertEqual(ctx.exception.code, 2)

    def test_nonlocal_host_proceeds_with_optin(self) -> None:
        self._run_main("0.0.0.0", allow=True).assert_called_once()

    def test_nonlocal_host_refused_without_auth_token(self) -> None:
        import webapp
        from ui_frozen import ENABLE_FLAG

        argv = ["webapp.py", "--metrics-port", "0", ENABLE_FLAG, "--host", "0.0.0.0"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {webapp.ALLOW_NONLOCAL_ENV: "1"}, clear=False), \
                mock.patch("uvicorn.run") as mocked_run:
            os.environ.pop(webapp.UI_API_TOKEN_ENV, None)
            with self.assertRaises(SystemExit) as ctx:
                webapp.main()
        self.assertEqual(ctx.exception.code, 2)
        mocked_run.assert_not_called()

    def test_loopback_host_proceeds(self) -> None:
        self._run_main("127.0.0.1", allow=False).assert_called_once()

    def test_auth_middleware_protects_remote_app_routes(self) -> None:
        import webapp
        from starlette.testclient import TestClient

        old_required = getattr(webapp.app.state, "require_ui_auth", False)
        old_token = getattr(webapp.app.state, "ui_api_token", "")
        webapp.app.state.require_ui_auth = True
        webapp.app.state.ui_api_token = "test-token-" + "x" * 32
        try:
            client = TestClient(webapp.app, follow_redirects=False)
            self.assertEqual(client.get("/").status_code, 401)
            self.assertEqual(client.get("/", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            response = client.get("/", headers={
                "Authorization": "Bearer " + webapp.app.state.ui_api_token,
            })
            self.assertEqual(response.status_code, 302)
            import base64
            basic = base64.b64encode(
                ("operator:" + webapp.app.state.ui_api_token).encode("utf-8")
            ).decode("ascii")
            self.assertEqual(client.get("/", headers={
                "Authorization": "Basic " + basic,
            }).status_code, 302)
        finally:
            webapp.app.state.require_ui_auth = old_required
            webapp.app.state.ui_api_token = old_token

    def test_refusal_never_reaches_uvicorn(self) -> None:
        """拒绝路径绝不能顺手把服务起来（与 UI 冻结门禁同一防守原则）。"""
        import webapp
        from ui_frozen import ENABLE_FLAG

        argv = ["webapp.py", "--metrics-port", "0", ENABLE_FLAG, "--host", "0.0.0.0"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("uvicorn.run") as mocked_run:
            os.environ.pop(webapp.ALLOW_NONLOCAL_ENV, None)
            with self.assertRaises(SystemExit):
                webapp.main()
            mocked_run.assert_not_called()


class StreamCacheBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_streams = dict(lb._STREAMS)
        self.old_alias = dict(lb._STREAM_ALIAS)
        self.old_per_stream = lb._STREAM_MAX_BYTES
        self.old_total = lb._STREAM_TOTAL_MAX_BYTES
        lb._STREAMS.clear()
        lb._STREAM_ALIAS.clear()
        lb._STREAM_MAX_BYTES = 16
        lb._STREAM_TOTAL_MAX_BYTES = 24

    def tearDown(self) -> None:
        lb._STREAMS.clear()
        lb._STREAMS.update(self.old_streams)
        lb._STREAM_ALIAS.clear()
        lb._STREAM_ALIAS.update(self.old_alias)
        lb._STREAM_MAX_BYTES = self.old_per_stream
        lb._STREAM_TOTAL_MAX_BYTES = self.old_total

    def test_run_scoped_identity_and_byte_budgets(self) -> None:
        first = lb._stream_open("conversation::model", "run-one")
        second = lb._stream_open("conversation::model", "run-two")
        self.assertNotEqual(first.key, second.key)
        self.assertIs(lb._stream_find(first.key), first)
        self.assertIs(lb._stream_find("conversation::model"), second)

        first.publish("a" * 20)
        second.publish("b" * 20)
        self.assertLessEqual(len(first.buf), lb._STREAM_MAX_BYTES)
        self.assertLessEqual(len(second.buf), lb._STREAM_MAX_BYTES)
        self.assertLessEqual(sum(len(st.buf) for st in lb._STREAMS.values()), lb._STREAM_TOTAL_MAX_BYTES)
        self.assertGreater(second.base_offset, 0)

    def test_expired_byte_offset_returns_410(self) -> None:
        stream = lb._stream_open("conversation", "run-expired")
        stream.publish("x" * 20)
        with TestClient(_make_app()) as client:
            response = client.get(f"/v1/stream?conv_id={stream.key}&from=0")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "stream_offset_expired")


if __name__ == "__main__":
    unittest.main()
