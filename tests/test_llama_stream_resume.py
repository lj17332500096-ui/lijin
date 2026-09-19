# -*- coding: utf-8 -*-
"""可恢复流会话（`/v1/stream`）回归测试——离线，不依赖 LLM / uvicorn。

真实故障背景（已用真实浏览器 CDP 取证）
--------------------------------------
llama-ui 前端在「页面重新可见」且距上次收到字节 >3s 时会**主动 cancel** 当前
SSE（bundle 内 `const P = () => ... g.cancel()`），随后请求

    GET /v1/stream?conv_id=<会话>&from=<已读字节数>

续读；`from` 就是它已完整处理的字节数。用户只要在模型思考期间切一下窗口再
点回来，就必然走这条路径。

此前服务端只返回假占位帧 `/v1/stream`，导致：
  1) 前端报 “Stream connection lost and could not be resumed”；
  2) 本轮已生成的正文**全部丢失**（实测页面文本为空）；
  3) 前端反复轮询 /v1/streams/lookup。

修复：把「生成」与「HTTP 连接」解耦——帧先写会话缓冲，HTTP 只做订阅者；
`/v1/stream` 按字节偏移回放并继续跟随；`/v1/streams/lookup` 报告在飞会话。
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 前端 UI 已冻结（只优化后端阶段）：默认整模块跳过。
# 恢复运行：set FORGE_ENABLE_UI=1；冻结范围见 ui_frozen.py。
from tests._ui_freeze import skip_if_frozen

skip_if_frozen()

import llama_bridge as lb  # noqa: E402


def _reset_registry() -> None:
    with lb._STREAM_LOCK:
        lb._STREAMS.clear()
        lb._STREAM_ALIAS.clear()


class ConvStreamBufferTests(unittest.TestCase):
    """缓冲区的偏移语义（这是「不丢正文、不重复」的核心）。"""

    def setUp(self) -> None:
        _reset_registry()

    def test_read_from_zero_replays_everything(self) -> None:
        st = lb._ConvStream("c1", "r1")
        st.publish("data: A\n\n")
        st.publish("data: B\n\n")
        st.finish()

        async def collect():
            return b"".join([c async for c in st.read_from(0)])

        self.assertEqual(asyncio.run(collect()), b"data: A\n\ndata: B\n\n")

    def test_read_from_offset_only_tail(self) -> None:
        # from = 前端已处理字节数 → 只补尾部，既不重复也不漏
        st = lb._ConvStream("c1", "r1")
        st.publish("data: A\n\n")
        head = len("data: A\n\n")
        st.publish("data: B\n\n")
        st.finish()

        async def collect():
            return b"".join([c async for c in st.read_from(head)])

        self.assertEqual(asyncio.run(collect()), b"data: B\n\n")

    def test_offset_beyond_end_yields_nothing(self) -> None:
        st = lb._ConvStream("c1", "r1")
        st.publish("data: A\n\n")
        st.finish()

        async def collect():
            return b"".join([c async for c in st.read_from(10_000)])

        self.assertEqual(asyncio.run(collect()), b"")

    def test_follows_live_growth_then_terminates(self) -> None:
        """流未结束时订阅者应持续跟随新字节，finish 后正常收尾（不挂死）。"""
        st = lb._ConvStream("c1", "r1")
        out: list[bytes] = []

        async def scenario():
            async def reader():
                async for chunk in st.read_from(0):
                    out.append(chunk)
            task = asyncio.create_task(reader())
            await asyncio.sleep(0)
            st.publish("data: 1\n\n")
            await asyncio.sleep(0)
            st.publish("data: 2\n\n")
            await asyncio.sleep(0)
            st.finish()
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())
        self.assertEqual(b"".join(out), b"data: 1\n\ndata: 2\n\n")

    def test_finish_is_idempotent(self) -> None:
        st = lb._ConvStream("c1", "r1")
        st.finish()
        first = st.closed_at
        st.finish()
        self.assertEqual(st.closed_at, first)
        self.assertTrue(st.done)


class StreamKeyTests(unittest.TestCase):
    """会话标识解析：POST 头 / 查询参数两端必须能对上。"""

    def setUp(self) -> None:
        _reset_registry()

    def test_find_tolerates_model_suffix_mismatch(self) -> None:
        # POST 带 X-Conversation-Id: conv::model；续读只给裸 conv
        st = lb._stream_open("conv-9::agnes-2.5-flash", "r1")
        self.assertIs(lb._stream_find("conv-9"), st)
        self.assertIs(lb._stream_find("conv-9::agnes-2.5-flash"), st)

    def test_find_unknown_returns_none(self) -> None:
        self.assertIsNone(lb._stream_find("nope"))
        self.assertIsNone(lb._stream_find(""))

    def test_conv_key_prefers_header(self) -> None:
        class _Req:
            headers = {"x-conversation-id": "conv-from-header"}
        self.assertEqual(lb._conv_key_from_request(_Req(), {"conversation_id": "body"}), "conv-from-header")

    def test_conv_key_falls_back_to_body(self) -> None:
        class _Req:
            headers = {}
        self.assertEqual(lb._conv_key_from_request(_Req(), {"conversation_id": "body-conv"}), "body-conv")
        self.assertEqual(lb._conv_key_from_request(_Req(), {"conv_id": "alt"}), "alt")
        self.assertEqual(lb._conv_key_from_request(_Req(), {}), "")


class StreamEndpointTests(unittest.TestCase):
    """HTTP 面：GET 续读 / 未知会话 404 / lookup 只报在飞 / DELETE 取消。"""

    def setUp(self) -> None:
        _reset_registry()
        self._client = self._make_client()

    @staticmethod
    def _make_client():
        try:
            from starlette.testclient import TestClient
        except ImportError:
            return None
        try:
            import webapp as w
        except Exception:
            return None
        app = getattr(w, "app", None) or getattr(w, "APP", None)
        if app is None:
            return None
        return TestClient(app, follow_redirects=False)

    def _finished_stream(self, key: str, run_id: str = "run-1"):
        st = lb._stream_open(key, run_id)
        st.publish(lb._openai_chunk("m", run_id, {"role": "assistant", "content": ""}))
        st.publish(lb._openai_chunk("m", run_id, {"content": "答案是 5"}))
        st.publish(lb._openai_chunk("m", run_id, {}, finish="stop"))
        st.publish(lb._openai_done(run_id))
        st.publish(lb._SSE_STREAM_DONE)
        st.finish()
        return st

    def test_unknown_conv_is_404_not_html(self) -> None:
        if self._client is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        r = self._client.get("/llama-ui/v1/stream?conv_id=not-exist&from=0")
        self.assertEqual(r.status_code, 404, "未知会话必须 404（前端据此清理残留状态）")
        self.assertNotIn("text/html", r.headers.get("content-type", ""))

    def test_resume_replays_from_offset_and_ends_with_done(self) -> None:
        if self._client is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        st = self._finished_stream("conv-ep-1")
        head = len(bytes(st.buf))
        self.assertIn(b"[DONE]", bytes(st.buf))

        full = self._client.get("/llama-ui/v1/stream?conv_id=conv-ep-1&from=0")
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.headers.get("content-type", "").split(";")[0], "text/event-stream")
        self.assertTrue(full.text.rstrip().endswith("data: [DONE]"),
                        "续读必须回放完整结束帧，前端才判定为正常完成")
        self.assertIn("答案是 5", full.text)

        # 从尾部续读：不应重复回放已处理内容
        tail_only = self._client.get(f"/llama-ui/v1/stream?conv_id=conv-ep-1&from={head}")
        self.assertEqual(tail_only.status_code, 200)
        self.assertEqual(tail_only.text, "")

    def test_resume_bad_from_param_does_not_crash(self) -> None:
        if self._client is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        self._finished_stream("conv-ep-2")
        r = self._client.get("/llama-ui/v1/stream?conv_id=conv-ep-2&from=abc")
        self.assertEqual(r.status_code, 200, "非法 from 应回退为 0 而不是 500")

    def test_lookup_reports_only_live_streams(self) -> None:
        if self._client is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        done = self._finished_stream("conv-done")
        live = lb._stream_open("conv-live", "run-live")
        live.publish(lb._openai_chunk("m", "run-live", {"content": "生成中"}))

        r = self._client.post("/llama-ui/v1/streams/lookup",
                              json={"conversation_ids": ["conv-done", "conv-live", "absent"]})
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1, "只应报告仍在生成的会话")
        self.assertEqual(rows[0]["conversation_id"], live.key)
        self.assertFalse(rows[0]["is_done"])
        self.assertIn("started_at", rows[0])
        self.assertFalse(done.done is False)

    def test_lookup_bad_body_returns_empty_list(self) -> None:
        if self._client is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        r = self._client.post("/llama-ui/v1/streams/lookup", json={"conversation_ids": "oops"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_delete_cancels_run(self) -> None:
        if self._client is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        st = lb._stream_open("conv-cancel", "run-cancel")
        try:
            from runtime.runner import AgentRuntime
        except Exception:
            self.skipTest("runtime.runner 不可用，跳过")
        with mock.patch.object(AgentRuntime, "_ensure", lambda self: None), \
             mock.patch.object(AgentRuntime, "cancel_run", lambda self, run_id: run_id == st.run_id):
            r = self._client.request("DELETE", "/llama-ui/v1/stream?conv_id=conv-cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("cancelled"), "DELETE 应真实取消该会话在飞的 run")

    def test_delete_unknown_is_noop(self) -> None:
        if self._client is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        r = self._client.request("DELETE", "/llama-ui/v1/stream?conv_id=absent")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json().get("cancelled"))


class PumpDecouplingTests(unittest.TestCase):
    """核心不变量：帧由独立任务写入缓冲，与 HTTP 连接生死无关。"""

    def setUp(self) -> None:
        _reset_registry()

    def test_pump_keeps_writing_after_subscriber_gone(self) -> None:
        st = lb._stream_open("conv-pump", "run-pump")

        async def scenario():
            queue: asyncio.Queue = asyncio.Queue()
            pump = lb._spawn_pump(_fake_pump(queue, st))
            # 第一个订阅者读一点就走（模拟前端 cancel 掉 POST）
            first = []
            async for chunk in st.read_from(0):
                first.append(chunk)
                break
            queue.put_nowait(("assistant_delta", {"metadata": {"delta": "后半段正文"}}))
            queue.put_nowait(("__end__", {"content": "完整正文"}))
            await asyncio.wait_for(pump, timeout=5)
            # 第二个订阅者（模拟 GET /v1/stream 续读）拿到全部内容
            rest = b"".join([c async for c in st.read_from(0)])
            return b"".join(first), rest

        _, rest = asyncio.run(scenario())
        self.assertIn("后半段正文".encode(), rest, "连接断开后缓冲仍须继续增长")
        self.assertTrue(rest.rstrip().endswith(b"data: [DONE]"))


async def _fake_pump(queue: asyncio.Queue, st: "lb._ConvStream") -> None:
    """与 llama_bridge._pump 同语义的最小替身（避免测试触达真实 run）。"""
    st.publish(lb._openai_chunk("m", "run-pump", {"role": "assistant", "content": ""}))
    streamed: list[str] = []
    try:
        while True:
            channel, ev = await queue.get()
            if channel == "__end__":
                content = ev.get("content") or ""
                for frame in lb._final_content_frames("m", "run-pump", "".join(streamed), content):
                    st.publish(frame)
                st.publish(lb._openai_chunk("m", "run-pump", {}, finish="stop"))
                st.publish(lb._openai_done("run-pump"))
                st.publish(lb._SSE_STREAM_DONE)
                return
            if channel == "assistant_delta":
                delta = (ev.get("metadata") or {}).get("delta", "")
                if delta:
                    streamed.append(delta)
                    st.publish(lb._openai_chunk("m", "run-pump", {"content": delta}))
    finally:
        st.finish()


if __name__ == "__main__":
    unittest.main()
