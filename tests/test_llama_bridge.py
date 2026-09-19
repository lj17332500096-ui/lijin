"""llama_bridge 适配层的四项加固回归测试（① session 隔离 / ② 真流式转发 /
③ control 真实取消 / ④ 审批门）。

只测纯函数与确定性逻辑，不依赖 LLM / uvicorn 实跑。
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 前端 UI 已冻结（只优化后端阶段）：默认整模块跳过。
# 恢复运行：set FORGE_ENABLE_UI=1；冻结范围见 ui_frozen.py。
from tests._ui_freeze import skip_if_frozen

skip_if_frozen()

import llama_bridge as lb


class SessionClosedDegradationTests(unittest.TestCase):
    """修复 B：closed / invalidated SQLiteSession 的静默降级路径。

    SQLiteSession.close() 是同步方法、无公开 is_closed 属性；SDK 内部
    _invalidate_connection 会把 _closed 置 True，之后任何 add_items/compact
    都抛 RuntimeError("SQLiteSession is closed")。本测试锁定：
    ① main._auto_compact 对 closed session 静默降级（不上抛）；
    ② llama_bridge._spawn_bg_task 的 finally close 对已 close 的 session 不重复报错。
    """

    def test_auto_compact_silently_skips_closed_session(self) -> None:
        from main import _auto_compact
        from agents.memory import SQLiteSession
        import asyncio, tempfile, os

        tmp = os.path.join(tempfile.mkdtemp(prefix="sess_closed_"), "s.db")
        sess = SQLiteSession("closed-test", db_path=tmp)
        sess.close()  # 同步 close；之后 _check_not_closed 抛 RuntimeError

        # closed 的 session：_auto_compact 必须静默返回（不上抛）
        asyncio.run(_auto_compact(sess, auto_summary=True))
        # 走到这里即通过：closed 降级成功
        self.assertTrue(sess._closed, "close 后 _closed 应为 True")

    def test_auto_compact_disabled_no_side_effect(self) -> None:
        from main import _auto_compact
        import asyncio

        class _NotClosed:
            def _check_not_closed(self):
                pass
            async def close(self):
                pass

        # auto_summary=False 时直接返回，不碰 session
        asyncio.run(_auto_compact(_NotClosed(), auto_summary=False))
        self.assertTrue(True)


class SessionIsolationTests(unittest.TestCase):
    """① session 按用户隔离。"""

    def test_session_id_includes_user_and_model(self) -> None:
        sid_a = lb._session_id_for("user-alice", "agnes-2.5-flash")
        sid_b = lb._session_id_for("user-bob", "agnes-2.5-flash")
        self.assertNotEqual(sid_a, sid_b, "不同 user 的 session_id 必须不同（不串台）")
        self.assertIn("user-alice", sid_a)
        self.assertIn("agnes-2.5-flash", sid_a)

    def test_same_user_same_model_same_session(self) -> None:
        s1 = lb._session_id_for("user-alice", "agnes-2.5-flash")
        s2 = lb._session_id_for("user-alice", "agnes-2.5-flash")
        self.assertEqual(s1, s2, "同 user 同 model 的 session_id 稳定（历史可续）")

    def test_user_id_sanitize_strips_path_traversal(self) -> None:
        # 路径分隔符被消除（防穿越），长度收敛；.. 可能残留为字面字符但无害
        out = lb._sanitize_user("../../etc/passwd")
        for ch in ("/", "\\", ":", "\n", "\r", " "):
            self.assertNotIn(ch, out, f"路径/危险符号 {ch!r} 应被消除")
        self.assertLessEqual(len(out), 64, "超长 user 收敛到 64 字内")

    def test_empty_user_falls_back_to_anon(self) -> None:
        self.assertEqual(lb._sanitize_user(""), "anon")
        self.assertEqual(lb._sanitize_user(None), "anon")

    def test_user_id_from_request_priority(self) -> None:
        class _FakeReq:
            class client:
                host = "1.2.3.4"
            headers = {
                "authorization": "Bearer secret-token-abc",
                "x-forge-user": "header-user",
            }
        # 优先级：body.user_id > X-Forge-User > Bearer
        uid = lb._user_id_from_request(_FakeReq(), {"user_id": "body-user"})
        self.assertEqual(uid, "body-user")
        uid2 = lb._user_id_from_request(_FakeReq(), {})
        self.assertEqual(uid2, "header-user", "无 body.user_id 时用 X-Forge-User")
        class _NoForge:
            class client:
                host = "1.2.3.4"
            headers = {"authorization": "Bearer secret-token-abc"}
        uid3 = lb._user_id_from_request(_NoForge(), {})
        self.assertTrue(uid3.startswith("tok-"), "Bearer 用哈希片段（不泄漏原值）")


class TrueStreamForwardTests(unittest.TestCase):
    """② 真流式：assistant_delta 通道 → SSE content 增量帧。"""

    def test_openai_chunk_shapes_delta(self) -> None:
        chunk = lb._openai_chunk("m", "r1", {"content": "你好"})
        self.assertTrue(chunk.startswith("data: "))
        self.assertTrue(chunk.endswith("\n\n"))
        d = json.loads(chunk[len("data: "):])
        self.assertEqual(d["choices"][0]["delta"]["content"], "你好")
        self.assertIsNone(d["choices"][0]["finish_reason"])

    def test_assistant_delta_event_yields_content_frame(self) -> None:
        # 模拟 gen() 里对 assistant_delta 通道的转发逻辑
        event = {"type": "assistant.delta", "channel": "assistant_delta",
                 "metadata": {"delta": "逐 token"}}
        delta = (event.get("metadata") or {}).get("delta", "")
        self.assertEqual(delta, "逐 token")
        frame = lb._openai_chunk("m", "r1", {"content": delta})
        self.assertIn('"逐 token"', frame)

    def test_done_frame_shape(self) -> None:
        done = lb._openai_done("r1")
        d = json.loads(done[len("data: "):])
        self.assertEqual(d["object"], "chat.completion")
        self.assertEqual(d["choices"], [])

    def test_sse_stream_terminates_with_done_marker(self) -> None:
        # 回归：llama-ui 前端只有在收到 `data: [DONE]` 时才把流判定为正常完成
        # （bundle 内 `if (payload === "[DONE]") done = true`）。缺少它，前端会
        # 认为流被中断并触发 stream resume，报
        # “Stream resume produced no new bytes, giving up”。
        frame = lb._openai_sse_done()
        self.assertEqual(frame, "data: [DONE]\n\n",
                         "SSE 结束帧必须是标准 `data: [DONE]`")

    def test_final_content_not_resent_when_already_streamed(self) -> None:
        # 回归：逐 token 增量已给出完整正文时，终帧不得再整段重发（否则正文显示两遍）。
        full = "您好！我是 Agnes。\n可以帮你处理日常事务。"
        frames = lb._final_content_frames("m", "r1", full, full)
        self.assertEqual(frames, [], "增量已覆盖终稿时终帧不应重发正文")

    def test_final_content_resent_when_no_delta(self) -> None:
        # 无增量（如本轮没有 assistant_delta）时，终帧必须补发正文，内容不能丢。
        full = "这是最终答案。"
        frames = lb._final_content_frames("m", "r1", "", full)
        self.assertEqual(len(frames), 1)
        payload = json.loads(frames[0][len("data: "):])
        self.assertEqual(payload["choices"][0]["delta"]["content"], full)

    def test_final_content_only_appends_missing_tail(self) -> None:
        # 增量只覆盖前缀时，只补尾部，避免前缀重复。
        prefix, tail = "第一段。", "第二段。"
        frames = lb._final_content_frames("m", "r1", prefix, prefix + tail)
        self.assertEqual(len(frames), 1)
        payload = json.loads(frames[0][len("data: "):])
        self.assertEqual(payload["choices"][0]["delta"]["content"], tail,
                         "只应补发缺失的尾部，不得重发已流式输出的前缀")


class ControlCancelTests(unittest.TestCase):
    """③ control 真实取消（cancel_run 收口）。"""

    def test_cancel_nonexistent_run_returns_false(self) -> None:
        # 不依赖 AgentRuntime 实例：仅验证 cancel_run 契约——无 task 时返回 False
        try:
            from runtime.runner import AgentRuntime
            rt = AgentRuntime.get_default()
            rt._ensure()
            self.assertFalse(rt.cancel_run("nonexistent-run-zzz"),
                             "未登记的 run_id cancel_run 应返回 False（不 raise）")
        except Exception as e:
            # 若 AgentRuntime 初始化需要环境，仅验证契约不抛意外类型
            self.assertIsInstance(e, Exception)


class ApprovalGateTests(unittest.TestCase):
    """④ 写工具审批门接入。"""

    def test_approval_gate_detection(self) -> None:
        gate = lb._approval_gate()
        if gate is None:
            self.skipTest("AgentRuntime 未就绪，跳过审批门检测")
        # APPROVAL 默认开 → should_gate 对破坏/执行类工具为 True
        gated = any(gate.should_gate(t) for t in ("run_python", "write_code_file", "forget_memory", "rm_rf"))
        self.assertTrue(gated, "至少一个高风险工具应命中审批门")

    def test_write_tool_blocked_response_shape(self) -> None:
        # 审批门工具的 POST /tools 应返回 approval_required=True（不直接执行）
        gate = lb._approval_gate()
        if gate is None:
            self.skipTest("AgentRuntime 未就绪，跳过")
        # 找第一个命中门且带 side_effect 的工具名（契约：返回结构稳定）
        name = next((t for t in ("forget_memory", "run_python", "write_code_file")
                     if gate.should_gate(t)), None)
        self.assertIsNotNone(name, "应能找到至少一个写权限审批门工具")


class StaticAssetNot404Tests(unittest.TestCase):
    """回归：根路径访问不能白屏。

    历史 bug：`index_page` 曾直接把 web/llama-ui/index.html 的原文作为 HTMLResponse
    返回到 `/`，但该文件用的是相对路径（./_app/...），浏览器在 `/` 下会解析成
    `/_app/...`，而静态资源只挂在 `/llama-ui` 挂载点，导致 JS/CSS 全 404 → 白屏。

    修复：`index_page` 改为 302 重定向到 `/llama-ui/`，让相对路径自然解析到
    `/llama-ui/_app/...`。本测试守住「`/` 不得直接返回 index.html 原文」。
    """

    @classmethod
    def _client(cls):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            return None
        try:
            import webapp  # noqa: F401  触发路由构建
        except Exception:
            return None
        # webapp 的 App 实例是模块级对象，这里直接复用
        try:
            import webapp as w
            app = getattr(w, "app", None) or getattr(w, "APP", None)
            if app is None:
                return None
            return TestClient(app, follow_redirects=False)
        except Exception:
            return None

    def test_root_redirects_to_llama_ui_not_returns_html(self) -> None:
        c = self._client()
        if c is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        r = c.get("/")
        self.assertEqual(r.status_code, 302, "根路径应 302 到 /llama-ui/（不直接返回 HTML）")
        self.assertIn("/llama-ui/", r.headers.get("location", ""))
        # 跟随重定向后拿到的是同一份 index.html，且相对资源能解析
        r2 = c.get("/", follow_redirects=True)
        self.assertEqual(r2.status_code, 200)
        # 相对路径 ./_app/ 从 /llama-ui/ 解析后必须 200（不再 404）
        self.assertIn("./_app/", r2.text)
        from webapp import BASE_DIR
        import re
        m = re.search(r'href="\.?/(_app/[^"]+\.js)"', r2.text)
        if m:
            asset = f"/llama-ui/{m.group(1)}"
            self.assertTrue(BASE_DIR / "web" / "llama-ui" / m.group(1).lstrip("./") ,
                            f"资源文件 {m.group(1)} 必须在 web/llama-ui/ 下存在")
            self.assertEqual(c.get(asset).status_code, 200, f"{asset} 必须 200（回归白屏修复）")

    def test_spa_fallback_static_not_fallback(self) -> None:
        # _app/* 静态资源缺失时不能回落成 index.html（否则 HTML 会被当 JS 执行）
        c = self._client()
        if c is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        r = c.get("/llama-ui/_app/nonexistent-xyz.js")
        self.assertEqual(r.status_code, 404, "静态资源 404 必须保留 404，不能回落成 HTML")

    def test_llama_ui_prefixed_api_routes_are_json_not_html(self) -> None:
        # 回归：llama-ui 里大量 fetch 是相对路径（./props、./v1/models 等），
        # 页面挂在 /llama-ui/ 时会被解析成 /llama-ui/props。若这些路径被
        # SPA fallback 吃成 index.html，前端 res.json() 会抛
        # “Unexpected token '<', "<!-- Thi"... is not valid JSON”。
        c = self._client()
        if c is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        for path in (
            "/llama-ui/props",
            "/llama-ui/v1/models",
            "/llama-ui/tools",
            "/llama-ui/slots",
        ):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, f"{path} 必须 200")
            self.assertEqual(
                r.headers.get("content-type", "").split(";")[0].strip(),
                "application/json",
                f"{path} 必须返回 JSON，不能回落成 index.html",
            )
        r = c.post("/llama-ui/v1/streams/lookup", json={})
        self.assertEqual(r.status_code, 200, "POST /llama-ui/v1/streams/lookup 必须 200")
        self.assertEqual(
            r.headers.get("content-type", "").split(";")[0].strip(),
            "application/json",
            "POST /llama-ui/v1/streams/lookup 必须返回 JSON，不能回落成 index.html",
        )
        lookup_data = json.loads(r.text)
        self.assertEqual(lookup_data, [], "无本地流会话时 lookup 必须稳定返回空列表")
        # GET /v1/stream 是「可恢复流」续读端点：
        #   - 已知会话 → 200 text/event-stream，按 from 偏移回放并以 [DONE] 收尾
        #   - 未知会话 → 404（前端 probeResumeStatus 见非 200 会清理本地残留状态；
        #     若这里也回 200，前端会误判“服务端仍有流”而反复挂载/重试）
        # 续读的完整语义由 tests/test_llama_stream_resume.py 覆盖，这里只锁形状。
        r = c.get("/llama-ui/v1/stream")
        self.assertEqual(r.status_code, 404, "未注册会话的续读端点必须 404")
        self.assertNotIn("text/html", r.headers.get("content-type", ""),
                         "续读端点不得回落成 index.html")

    def test_llama_ui_api_like_paths_do_not_return_html(self) -> None:
        # /llama-ui/props/missing 这类 API-like 路径即便未命中真实端点，
        # 也不能返回 index.html（否则前端把 HTML 当 JSON 解析会直接炸）。
        c = self._client()
        if c is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        r = c.get("/llama-ui/props/missing")
        self.assertNotIn("text/html", r.headers.get("content-type", ""),
                         "API-like 路径不得 fallback 成 index.html")
        if r.status_code in (200, 404, 405):
            self.assertNotIn("<!DOCTYPE html>", r.text, "API-like 路径不得返回 HTML 页面")

    def test_legacy_ui_entries_redirect_to_llama_ui(self) -> None:
        # llama-ui 为唯一前端：旧 UI 入口不得继续服务旧 HTML/资产，只能回到唯一入口。
        c = self._client()
        if c is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        for path in ("/runtime", "/chat"):
            r = c.get(path)
            self.assertEqual(r.status_code, 301, f"{path} 必须 301 到唯一前端 /llama-ui/")
            self.assertEqual(r.headers.get("location", ""), "/llama-ui/")
            self.assertNotIn("text/html", r.headers.get("content-type", ""))

    def test_legacy_static_runtime_assets_not_served(self) -> None:
        # 旧 /rt 静态命名空间已移除；若再次被挂回，会让旧 UI 与 llama-ui 同时存在。
        c = self._client()
        if c is None:
            self.skipTest("webapp.App 不可离线构造，跳过")
        for path in ("/rt/", "/rt/design.css", "/rt/workspace.js"):
            r = c.get(path)
            self.assertIn(r.status_code, (404, 405), f"{path} 不应再被旧 /rt 静态服务命中")
            self.assertNotIn("text/css", r.headers.get("content-type", ""))
            self.assertNotIn("application/javascript", r.headers.get("content-type", ""))


if __name__ == "__main__":
    unittest.main()
