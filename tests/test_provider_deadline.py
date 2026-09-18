# -*- coding: utf-8 -*-
"""provider 总墙钟 deadline（离线，不联网）。

背景：重试只有"次数上限"（单模型 ≤3 次、叠加 fallback ≤6 次），但每次尝试都可能
各自撞上首 token / 空闲超时（默认 90s / 120s），最坏可累积到 10 分钟级——
次数上限约束不了真实耗时，需要总墙钟兜底。

断言：
- 超过 FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS 后不再开新尝试，抛出 TIMEOUT 分类异常；
- 未超时时行为不变（正常重试 / 正常成功）；
- 设为 0 则关闭该兜底（退回原有"按次数耗尽"语义）。
"""

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import runtime.provider_gateway as pg  # noqa: E402
from runtime.provider_errors import (  # noqa: E402
    ProviderErrorKind,
    ProviderTransportError,
)
from runtime.provider_gateway import ResilientModel  # noqa: E402

NO_CHANNEL = ("Error code: 503 - "
              "No available channel for model agnes-x under group TokenPlan")


class _GatewayStub:
    """不做 fallback 的网关替身（隔离出 deadline 这一个变量）。"""

    def _fallback_config(self):
        return None

    def _get_fallback_model(self):
        return None


class _SlowFailModel:
    """每次 get_response 都失败，且真实消耗一点墙钟（让 deadline 可被触发）。"""

    def __init__(self, delay: float = 0.06):
        self.calls = 0
        self.delay = delay

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        time.sleep(self.delay)
        raise ProviderTransportError(NO_CHANNEL, status=503)


class _SlowFailStreamModel(_SlowFailModel):
    """stream_response 同步建流即失败（仍属"首 token 前"）。"""

    def stream_response(self, *args, **kwargs):
        self.calls += 1
        time.sleep(self.delay)
        raise ProviderTransportError(NO_CHANNEL, status=503)


def _model(inner):
    return ResilientModel(inner, _GatewayStub())


class DeadlineConfigTests(unittest.TestCase):
    def test_default_is_600s(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS", None)
            self.assertEqual(pg._total_timeout_seconds(), 600.0)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS": "42.5"}):
            self.assertEqual(pg._total_timeout_seconds(), 42.5)

    def test_zero_disables_deadline(self):
        with mock.patch.dict(os.environ, {"FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS": "0"}):
            self.assertFalse(pg._deadline_exceeded(0.0, pg._total_timeout_seconds()))

    def test_garbage_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS": "abc"}):
            self.assertEqual(pg._total_timeout_seconds(), 600.0)


class GetResponseDeadlineTests(unittest.TestCase):
    def _run(self, limit: str, inner):
        with mock.patch.dict(os.environ,
                             {"FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS": limit}):
            return asyncio.run(inner)

    def test_deadline_stops_retrying_and_raises_timeout(self):
        inner = _SlowFailModel()
        model = _model(inner)
        with self.assertRaises(ProviderTransportError) as cm:
            self._run("0.05", model.get_response(messages=[]))
        # 关键：只尝试了 1 次——第二次在循环入口就被 deadline 拦下
        self.assertEqual(inner.calls, 1)
        self.assertEqual(getattr(cm.exception, "provider_kind", None),
                         ProviderErrorKind.TIMEOUT)
        self.assertIn("provider 累计耗时", getattr(cm.exception, "provider_public", "") or "")

    def test_zero_keeps_legacy_retry_until_exhausted(self):
        """关闭兜底后退回原有语义：按次数耗尽，分类仍是 NO_CHANNEL。"""
        inner = _SlowFailModel()
        model = _model(inner)
        with self.assertRaises(ProviderTransportError) as cm:
            self._run("0", model.get_response(messages=[]))
        self.assertGreater(inner.calls, 1)
        self.assertEqual(getattr(cm.exception, "provider_kind", None),
                         ProviderErrorKind.NO_CHANNEL)

    def test_deadline_does_not_break_success_path(self):
        class _OkModel:
            def __init__(self):
                self.calls = 0

            async def get_response(self, *args, **kwargs):
                self.calls += 1
                return "ok"

        inner = _OkModel()
        model = _model(inner)
        out = self._run("600", model.get_response(messages=[]))
        self.assertEqual(out, "ok")
        self.assertEqual(inner.calls, 1)


class StreamResponseDeadlineTests(unittest.TestCase):
    def test_deadline_stops_retrying_and_raises_timeout(self):
        inner = _SlowFailStreamModel()
        model = _model(inner)

        async def consume():
            got = []
            async for _ in model.stream_response(messages=[]):  # pragma: no cover
                got.append(_)
            return got

        with mock.patch.dict(os.environ,
                             {"FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS": "0.05"}):
            with self.assertRaises(ProviderTransportError) as cm:
                asyncio.run(consume())
        self.assertEqual(inner.calls, 1)
        self.assertEqual(getattr(cm.exception, "provider_kind", None),
                         ProviderErrorKind.TIMEOUT)
        self.assertIn("provider 累计耗时", getattr(cm.exception, "provider_public", "") or "")


if __name__ == "__main__":
    unittest.main()
