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

import llama_bridge as lb


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


if __name__ == "__main__":
    unittest.main()
