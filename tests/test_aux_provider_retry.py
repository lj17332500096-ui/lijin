# -*- coding: utf-8 -*-
"""辅助直连点（compact/research/multimodal/codex_loop）统一接入 provider_errors 重试层。

离线、不联网：注入 429→成功 / 持续 503 / 401，断言：
- 429/5xx 有限重试后成功，或耗尽后抛出带用户可读文案的异常；
- 401 不重试；
- 工具返回文本只含分类后的用户文案，不透传网关内部原始错误。
"""

import os
import json
import sys
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import compact
import multimodal
import research
import runtime.codex_loop as codex_loop
from runtime.provider_errors import (ProviderErrorKind, ProviderTransportError,
                                     attach_provider_meta,
                                     call_with_provider_retry,
                                     provider_public_text)


def call_tool(tool, **kwargs) -> str:
    """FunctionTool 包装后的调用方式（与 tests 其它文件一致，零网络）。"""
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="test_call",
            tool_arguments=input_json,
        )
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


class _FakeContent:
    def __init__(self, text: str):
        self.content = text


class _FakeChoice:
    def __init__(self, text: str):
        self.message = _FakeContent(text)


class _FakeResponse:
    def __init__(self, text: str = "ok"):
        self.choices = [_FakeChoice(text)]


class _FakeCompletions:
    """chat.completions.create 故障注入：前 fail_count 次抛错，之后成功；或恒失败。"""

    def __init__(self, *, ok_text: str = "ok", status: int = 429,
                 fail_count: int = 0, fail_forever: bool = False):
        self.calls = 0
        self.ok_text = ok_text
        self.status = status
        self.fail_count = fail_count
        self.fail_forever = fail_forever

    def create(self, **kwargs) -> _FakeResponse:
        self.calls += 1
        if self.fail_forever or self.calls <= self.fail_count:
            headers = {"retry-after": "0"} if self.status == 429 else None
            detail = (
                f"Error code: {self.status} - rate limit exceeded, slow down"
                if self.status == 429 else
                f"Error code: {self.status} - "
                "No available channel for model agnes-x under group TokenPlan (distributor)"
            )
            raise ProviderTransportError(detail, status=self.status, headers=headers)
        return _FakeResponse(self.ok_text)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeClient:
    def __init__(self, completions: _FakeCompletions):
        self.chat = _FakeChat(completions)


def _env(**extra):
    base = {"AGENT_MODEL": "agnes-test", "OPENAI_API_KEY": "k-test",
            "OPENAI_BASE_URL": "http://gateway.test/v1"}
    base.update(extra)
    return mock.patch.dict(os.environ, base)


def _noop_wait(_seconds: float) -> None:
    return None


class RetryHelperTests(unittest.TestCase):
    def test_429_limited_retry_then_success(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ProviderTransportError("Error code: 429 - slow down",
                                             status=429, headers={"retry-after": "0"})
            return "ok"

        self.assertEqual(call_with_provider_retry(fn, wait=_noop_wait), "ok")
        self.assertEqual(calls["n"], 3)

    def test_401_never_retried_and_marked(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ProviderTransportError("Error code: 401 - 无效的令牌", status=401)

        with self.assertRaises(ProviderTransportError) as cm:
            call_with_provider_retry(fn, wait=_noop_wait)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(getattr(cm.exception, "provider_kind", None),
                         ProviderErrorKind.AUTH)
        self.assertIn("认证失败", provider_public_text(cm.exception) or "")

    def test_503_no_channel_exhausts_after_two_attempts(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ProviderTransportError(
                "Error code: 503 - No available channel under TokenPlan", status=503)

        with self.assertRaises(ProviderTransportError) as cm:
            call_with_provider_retry(fn, wait=_noop_wait)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(getattr(cm.exception, "provider_kind", None),
                         ProviderErrorKind.NO_CHANNEL)
        self.assertIn("没有可用通道", provider_public_text(cm.exception) or "")


class CompactRetryTests(unittest.TestCase):
    def test_summarize_retries_transient_gateway_error(self):
        comp = _FakeCompletions(ok_text="压缩后的摘要", status=429, fail_count=2)
        with mock.patch.object(compact, "_openai_client",
                               return_value=_FakeClient(comp)), _env():
            out = compact.summarize_transcript("很长的一段历史")
        self.assertEqual(out, "压缩后的摘要")
        self.assertEqual(comp.calls, 3)


class MultimodalRetryTests(unittest.TestCase):
    def _png(self):
        d = Path(tempfile.mkdtemp(prefix="mm_retry_"))
        p = d / "shot.png"
        p.write_bytes(b"not-a-real-png-bytes")
        return p

    def test_ask_model_retries_then_ok(self):
        comp = _FakeCompletions(ok_text="图里有表格", status=429, fail_count=1)
        p = self._png()
        with mock.patch("openai.OpenAI",
                        lambda *a, **k: _FakeClient(comp)), _env(VISION_MODEL="v-test"):
            out = multimodal._ask_model_with_image(p, "表格第二列是什么")
        self.assertIn("图里有表格", out)
        self.assertEqual(comp.calls, 2)

    def test_tool_output_uses_public_text_not_raw_gateway_error(self):
        comp = _FakeCompletions(status=503, fail_forever=True)
        p = self._png()
        with mock.patch.object(multimodal, "_resolve_image_path",
                               return_value=p), \
             mock.patch("openai.OpenAI",
                        lambda *a, **k: _FakeClient(comp)), _env(VISION_MODEL="v-test"):
            out = call_tool(multimodal.ask_image, image_path=str(p), question="图里写了什么")
        self.assertIn("看图失败", out)
        self.assertIn("没有可用通道", out)
        self.assertNotIn("TokenPlan", out)
        self.assertNotIn("ProviderTransportError", out)


class ResearchRetryTests(unittest.TestCase):
    def test_llm_text_retries_then_ok(self):
        comp = _FakeCompletions(ok_text='"queries": ["天气"]', status=429, fail_count=1)
        with mock.patch("openai.OpenAI",
                        lambda *a, **k: _FakeClient(comp)), _env():
            out = research._llm_text("sys", "user")
        self.assertIn("天气", out)
        self.assertEqual(comp.calls, 2)

    def test_deep_research_plan_failure_surfaces_public_text(self):
        err = ProviderTransportError(
            "Error code: 503 - No available channel under group TokenPlan (distributor)",
            status=503, code="model_not_found")

        def _raise_marked(*_a, **_k):
            attach_provider_meta(err)
            raise err

        with mock.patch.object(research, "_llm_text", side_effect=_raise_marked), _env():
            out = call_tool(research.deep_research, topic="某个主题")
        self.assertIn("深度调研失败", out)
        self.assertIn("没有可用通道", out)
        self.assertNotIn("TokenPlan", out)
        self.assertNotIn("ProviderTransportError", out)


class CodexLoopRetryTests(unittest.TestCase):
    def test_fix_retries_then_returns_code(self):
        comp = _FakeCompletions(ok_text="print('fixed')\n", status=429, fail_count=1)
        with mock.patch("openai.OpenAI",
                        lambda *a, **k: _FakeClient(comp)), _env():
            text = codex_loop._fix_with_llm("print(1/0)", "ZeroDivisionError")
        self.assertEqual(text, "print('fixed')")
        self.assertEqual(comp.calls, 2)

    def test_fix_failure_reports_public_text(self):
        comp = _FakeCompletions(status=503, fail_forever=True)
        with mock.patch("openai.OpenAI",
                        lambda *a, **k: _FakeClient(comp)), _env():
            text = codex_loop._fix_with_llm("print(1/0)", "ZeroDivisionError")
        self.assertIsNotNone(text)
        self.assertTrue(str(text).startswith("__FIX_ERROR__:"))
        self.assertIn("没有可用通道", str(text))
        self.assertNotIn("ProviderTransportError", str(text))


if __name__ == "__main__":
    unittest.main()
