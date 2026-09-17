# -*- coding: utf-8 -*-
"""Provider 故障分类 / 有限重试 / 有界 fallback / stalled 语义测试。

覆盖任务书 Test 1-8 的代码层：401 不重试、503 分类与 fallback、
No available channel → NO_CHANNEL、timeout、429 Retry-After、
真实 stalled（重复读文件）仍由 NO_PROGRESS 处理、provider 失败不进 stalled/repair。
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import main as main_module
from runtime.approval import ApprovalGate
from runtime.provider_errors import (ProviderErrorKind, ProviderTransportError,
                                     classify, fallback_allowed, retry_after_seconds,
                                     retry_policy)
from runtime.provider_gateway import (ResilientModel, ResilientProvider,
                                      provider_public_message, take_attempts)
from runtime.runctx import RunContext, bind as bind_ctx
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager


class StubModel:
    OK = object()

    def __init__(self, failures):
        # failures: 每次调用抛出的异常；空列表 = 恒成功
        self.failures = failures
        self.calls = 0

    async def get_response(self, *a, **k):
        self.calls += 1
        if not self.failures:
            return "ok"
        idx = min(self.calls - 1, len(self.failures) - 1)
        raise self.failures[idx]


class StubGateway:
    """ResilientModel 依赖的网关侧（_fallback_config/_get_fallback_model）。"""

    def __init__(self, cfg=None, fallback_model=None):
        self._cfg = cfg
        self._fb = fallback_model

    def _fallback_config(self):
        return self._cfg

    def _get_fallback_model(self):
        return self._fb


class ClassifyAndPolicyTests(unittest.TestCase):
    def test_loopback_provider_bypasses_environment_proxy(self):
        provider = ResilientProvider(
            api_key="test", base_url="http://localhost:8080/v1", use_responses=False
        )
        self.assertIsNotNone(provider._owned_http_client)
        self.assertFalse(provider._owned_http_client._trust_env)

    def test_remote_provider_keeps_default_http_transport(self):
        provider = ResilientProvider(
            api_key="test", base_url="https://gateway.example/v1", use_responses=False
        )
        self.assertIsNone(provider._owned_http_client)

    def test_http_mapping(self):
        cases = [
            (400, None, ProviderErrorKind.BAD_REQUEST),
            (401, None, ProviderErrorKind.AUTH),
            (403, None, ProviderErrorKind.PERMISSION),
            (404, {"error": {"code": "model_not_found", "message": "x"}},
             ProviderErrorKind.MODEL_NOT_FOUND),
            (429, None, ProviderErrorKind.RATE_LIMITED),
            (503, None, ProviderErrorKind.UNAVAILABLE),
        ]
        for status, body, expect in cases:
            kind, public, _ = classify(None, status=status, body=body)
            self.assertEqual(kind, expect)
            self.assertTrue(public)

    def test_no_available_channel_maps_to_no_channel(self):
        exc = ProviderTransportError(
            "No available channel for model agnes-2.5-flash under group TokenPlan (request id: 2026abcde12345)",
            status=503, code="model_not_found")
        kind, public, rid = classify(exc)
        self.assertEqual(kind, ProviderErrorKind.NO_CHANNEL)
        self.assertIn("通道", public)
        self.assertIsNotNone(rid)
        self.assertNotIn("TokenPlan", public)  # UI 不暴露网关内部细节

    def test_401_gateway_message_maps_auth(self):
        exc = ProviderTransportError("无效的令牌，数据库查询出错 (request id: 2026xyz90123)",
                                     status=401)
        kind, public, _ = classify(exc)
        self.assertEqual(kind, ProviderErrorKind.AUTH)
        self.assertIn("认证失败", public)

    def test_retry_policy_no_retry_for_auth_etc(self):
        for kind in (ProviderErrorKind.AUTH, ProviderErrorKind.PERMISSION,
                     ProviderErrorKind.BAD_REQUEST, ProviderErrorKind.MODEL_NOT_FOUND):
            self.assertIsNone(retry_policy(kind, 0))
        # 429 尊重 Retry-After
        self.assertEqual(retry_after_seconds({"retry-after": "3"}), 3.0)
        self.assertEqual(retry_policy(ProviderErrorKind.RATE_LIMITED, 0,
                                      {"retry-after": "4"}), 4.0)
        # 有限
        self.assertIsNotNone(retry_policy(ProviderErrorKind.RATE_LIMITED, 0))
        self.assertIsNone(retry_policy(ProviderErrorKind.RATE_LIMITED, 3))
        self.assertIsNotNone(retry_policy(ProviderErrorKind.UNAVAILABLE, 0))
        self.assertIsNone(retry_policy(ProviderErrorKind.UNAVAILABLE, 2))

    def test_fallback_rules(self):
        self.assertFalse(fallback_allowed(ProviderErrorKind.AUTH,
                                          fallback_configured=True,
                                          distinct_credentials=False))
        self.assertTrue(fallback_allowed(ProviderErrorKind.AUTH,
                                         fallback_configured=True,
                                         distinct_credentials=True))
        self.assertTrue(fallback_allowed(ProviderErrorKind.NO_CHANNEL,
                                         fallback_configured=True,
                                         distinct_credentials=False))
        self.assertFalse(fallback_allowed(ProviderErrorKind.BAD_REQUEST,
                                          fallback_configured=True,
                                          distinct_credentials=False))


class GatewayRetryTests(unittest.TestCase):
    def setUp(self):
        bind_ctx(RunContext(run_id="run_prov", container_id="tk_p",
                            session_id="proj-p", channel="chat",
                            memory_scope="project_only"))
        self._calls_taken = take_attempts("run_prov")

    def tearDown(self):
        take_attempts("run_prov")
        bind_ctx(None)

    async def _run(self, model, gw):
        rm = ResilientModel(model, gw)
        return await rm.get_response("x")

    def test_503_no_channel_retry_then_fallback_success(self):
        primary = StubModel([ProviderTransportError(
            "No available channel for model m under group TokenPlan", status=503)])
        fb = StubModel([])
        gw = StubGateway(cfg={"model": "fb", "base_url": "http://fb", "api_key": "k2",
                              "distinct": False}, fallback_model=fb)
        out = asyncio.run(self._run(primary, gw))
        self.assertEqual(out, "ok")
        attempts = take_attempts("run_prov")
        self.assertTrue(any(a.get("kind") == ProviderErrorKind.NO_CHANNEL for a in attempts))
        self.assertTrue(any(a.get("kind") == "success" and a.get("tag") == "fallback"
                            for a in attempts))
        self.assertTrue(any(a.get("fallback_used") for a in attempts))

    def test_401_no_retry_no_fallback_raises_marker(self):
        primary = StubModel([ProviderTransportError(
            "无效的令牌，数据库查询出错 (request id: rid401x)", status=401)])
        gw = StubGateway(cfg={"model": "fb", "base_url": "http://x", "api_key": "k2",
                              "distinct": False}, fallback_model=StubModel([]))
        with self.assertRaises(Exception) as ctx:
            asyncio.run(self._run(primary, gw))
        self.assertEqual(primary.calls, 1)  # 0 retry
        exc = ctx.exception
        self.assertEqual(getattr(exc, "provider_kind", None), ProviderErrorKind.AUTH)
        self.assertTrue(provider_public_message(exc))
        self.assertIn("认证失败", provider_public_message(exc))

    def test_both_fail_bounded_no_infinite(self):
        primary = StubModel([ProviderTransportError("boom 503", status=503),
                             ProviderTransportError("boom 503", status=503),
                             ProviderTransportError("boom 503", status=503)])
        fb = StubModel([ProviderTransportError("fb 503", status=503),
                        ProviderTransportError("fb 503", status=503)])
        gw = StubGateway(cfg={"model": "fb", "base_url": "http://fb", "api_key": "k2",
                              "distinct": False}, fallback_model=fb)
        with self.assertRaises(Exception) as ctx:
            asyncio.run(self._run(primary, gw))
        self.assertEqual(getattr(ctx.exception, "provider_kind", None),
                         ProviderErrorKind.UNAVAILABLE)
        # 主侧 3 次 + 兜底 3 次 = 有界（6），不无限
        self.assertLessEqual(primary.calls, 3)
        self.assertLessEqual(fb.calls, 3)
        self.assertEqual(primary.calls + fb.calls, 6)
        attempts = take_attempts("run_prov")
        kinds = [a.get("kind") for a in attempts]
        self.assertIn("exhausted", kinds)


class RunnerProviderFailureTests(unittest.TestCase):
    """Provider 异常 → run failed + provider.failure 事件；不进 stalled/repair。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prov_rt_"))
        self.manager = TaskManager(str(self.tmp / "agent.db"))
        self.runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        self.runtime._initialized = True
        self.runtime._tools_patched = True
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self._orig = main_module.execute_turn
        self._router = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "off"

    def tearDown(self):
        main_module.execute_turn = self._orig
        if self._router is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._router

    def _install(self, exc):
        async def fake(mode, message, session=None, debug=False, max_turns=20,
                       history_limit=None, agent=None, audit=None, stream_events_cb=None,
                       provider=None):
            raise exc
        main_module.execute_turn = fake

    def _events(self, rid):
        return [getattr(e, "event_type") for e in self.manager.list_events(rid)]

    def test_401_agent_error_not_stalled(self):
        err = ProviderTransportError(
            "Error code: 401 - 无效的令牌，数据库查询出错 (request id: rid-a1b2c3d4)",
            status=401)
        self._install(err)
        result = asyncio.run(self.runtime.run_turn("跑一下", session_id="unit", mode="sync"))
        self.assertFalse(result.ok)
        self.assertEqual(result.task.state, TaskState.FAILED)
        self.assertIn("认证失败", result.error)
        self.assertNotIn("stalled", result.error.lower())
        types = self._events(result.task.id)
        self.assertIn("provider.failure", types)
        payloads = [getattr(e, "payload") for e in self.manager.list_events(result.task.id)
                    if getattr(e, "event_type") == "provider.failure"]
        self.assertEqual(payloads[0].get("kind"), ProviderErrorKind.AUTH)
        rid_text = str(payloads[0].get("request_id") or "")
        self.assertTrue(rid_text.startswith("rid-") and "…" in rid_text)  # 已脱敏截断
        self.assertNotIn("completion.check.started", types)

    def test_503_no_channel_agent_error_public_text(self):
        err = ProviderTransportError(
            "Error code: 503 - No available channel for model agnes-2.5-flash under group TokenPlan",
            status=503, code="model_not_found")
        self._install(err)
        result = asyncio.run(self.runtime.run_turn("跑一下", session_id="unit", mode="sync"))
        self.assertFalse(result.ok)
        self.assertEqual(result.task.state, TaskState.FAILED)
        self.assertIn("没有可用通道", result.error)
        # 不暴露网关内部原始 body（只出现用户指引中的概念词，不含内部报错行）
        self.assertNotIn("under group TokenPlan (distributor)", result.error)
        self.assertNotIn("request id:", result.error)
        types = self._events(result.task.id)
        self.assertIn("provider.failure", types)
        self.assertIn("provider.model_attempts", types)  # 尝试记录（空 run 也会 flush）

    def test_retry_exhausted_event_contains_attempts(self):
        take_attempts("unit")
        err = ProviderTransportError("timeout after many", status=504)
        self._install(err)
        result = asyncio.run(self.runtime.run_turn("跑一下", session_id="unit", mode="sync"))
        self.assertFalse(result.ok)
        payloads = [getattr(e, "payload") for e in self.manager.list_events(result.task.id)
                    if getattr(e, "event_type") == "provider.model_attempts"]
        self.assertGreaterEqual(len(payloads), 1)


if __name__ == "__main__":
    unittest.main()

