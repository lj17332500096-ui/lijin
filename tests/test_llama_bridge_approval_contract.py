"""P3：`llama_bridge.api_chat_completions` 的「审批死分支」已诚实降级。

回归背景：该端点非流式路径曾有一个恒不成立的分支：

    if kind in ("needs_approval", "approval_required"):
        body["choices"][0]["finish_reason"] = "tool_calls"
        body["choices"][0]["message"]["approval_required"] = True

两条独立证据（均实测）证明它是死代码：
1. **不可达** —— `kind` 经 `runtime/reply_parser.parse()`，被白名单
   `KINDS = {answer, plan, note, questions, done}` 限制，未知值降级 "answer"；
2. **无消费方** —— 冻结 bundle 里 `approval_required` / `approval` /
   `finish_reason` 命中数均为 0。

本文件锁住「降级后仍成立」的四件事：
- 伪造字段彻底消失（计数断言：键一次都不许出现）；
- 不再声称 `finish_reason="tool_calls"` 却给不出 tool_calls 数组；
- 真实审批/追问态改为在**顶层** `state` 如实回传（bridge 自有字段）；
- 冻结契约未被动过：choices[0].message 结构与 content/finish_reason 不变。

全部离线：不联网、不调真实模型（桩掉 _run_chat_turn）。
"""
from __future__ import annotations
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import llama_bridge as lb  # noqa: E402

#: 冻结 bundle（web/llama-ui/_app/immutable/bundle.CfD0eHgL.js）里这些键的命中数。
#: 改动前端产物时此文件应同步失效 —— 那正是「死分支」判据的来源。
BUNDLE = BASE / "web" / "llama-ui" / "_app" / "immutable" / "bundle.CfD0eHgL.js"


def _app() -> Starlette:
    return Starlette(routes=[
        Route("/v1/chat/completions", lb.api_chat_completions, methods=["POST"]),
    ])


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(_app(), follow_redirects=False)

    def _patch_turn(self, events):
        queue = asyncio.Queue()
        for item in events:
            queue.put_nowait(item)

        async def fake(*a, **k):
            return "run-123", "session-123", queue, asyncio.sleep(0)

        return mock.patch.object(lb, "_run_chat_turn", side_effect=fake)

    def _post(self, events, **extra):
        payload = {"stream": False, "messages": [{"role": "user", "content": "hi"}]}
        payload.update(extra)
        with self._patch_turn(events):
            return self.client.post("/v1/chat/completions", json=payload)


class DeadApprovalBranchRemovedTests(_Base):
    """伪造字段必须彻底消失 —— 用计数断言，不用 assertIn（多处出现时无效）。

    ⚠️ 这四条**全部**用 `kind="needs_approval"` 构造事件：旧实现正是在这个
    kind 下写出伪造字段。若改回普通 kind="answer"，这些断言在 bug 复现时
    依然会绿（变成空转断言）—— 中和实验已实测过这一点。
    """

    #: 旧实现唯一会触发的 kind（生产上不可达，但足以检验分支确已移除）
    _DEAD_KIND = "needs_approval"

    def _dead_kind_event(self):
        return [("__end__", {"content": "ok", "kind": self._DEAD_KIND,
                             "state": "waiting_approval", "ok": True})]

    def test_response_never_contains_fabricated_approval_key(self) -> None:
        r = self._post(self._dead_kind_event())
        self.assertEqual(r.status_code, 200)
        raw = r.text
        self.assertEqual(raw.count("approval_required"), 0,
                         "approval_required 已删除，响应里不得再出现")
        self.assertEqual(raw.count("tool_calls"), 0,
                         "不得声称 tool_calls 却给不出 tool_calls 数组")

    def test_finish_reason_is_stop_not_tool_calls(self) -> None:
        body = self._post(self._dead_kind_event()).json()
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")

    def test_message_has_no_extra_fabricated_keys(self) -> None:
        """choices[0].message 仍是标准 {role, content}，没被塞进私有键。"""
        msg = self._post(self._dead_kind_event()).json()["choices"][0]["message"]
        self.assertEqual(sorted(msg.keys()), ["content", "role"],
                         "message 里出现了不该有的键")

    def test_approval_key_absent_even_if_kind_were_injected(self) -> None:
        """即便有人手工把 kind 塞成 needs_approval，也不再伪造 tool_calls。

        （旧实现在这里会写出 finish_reason=tool_calls + approval_required。）
        """
        body = self._post(self._dead_kind_event()).json()
        self.assertNotIn("approval_required", body["choices"][0]["message"])
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("tool_calls", body["choices"][0]["message"])


class RealStateSurfacedInsteadTests(_Base):
    """真实审批/追问态改为顶层如实回传（bridge 自有字段，不污染标准结构）。"""

    def test_waiting_approval_state_is_surfaced(self) -> None:
        events = [("__end__", {"content": "需要你批准", "kind": "answer",
                               "state": "waiting_approval", "ok": True})]
        body = self._post(events).json()
        self.assertEqual(body["state"], "waiting_approval")

    def test_waiting_user_state_is_surfaced(self) -> None:
        events = [("__end__", {"content": "请补充信息", "kind": "questions",
                               "state": "waiting_user", "ok": True})]
        body = self._post(events).json()
        self.assertEqual(body["state"], "waiting_user")

    def test_state_defaults_to_completed_when_absent(self) -> None:
        events = [("__end__", {"content": "ok", "kind": "answer", "ok": True})]
        body = self._post(events).json()
        self.assertEqual(body["state"], "completed")

    def test_frozen_contract_preserved(self) -> None:
        """既有断言不得被打破：content / finish_reason / OpenAI 骨架原样。"""
        events = [("__end__", {"content": "今天晴。", "kind": "answer",
                               "state": "completed", "ok": True})]
        body = self._post(events).json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["index"], 0)
        self.assertEqual(body["choices"][0]["message"],
                         {"role": "assistant", "content": "今天晴。"})
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertIn("id", body)
        self.assertIn("created", body)
        self.assertIn("model", body)


class BundleContractIsStillTheSourceOfTruthTests(unittest.TestCase):
    """锁住「死分支」判据本身：前端 bundle 确实不消费那些键。

    这条测试的意义：若将来前端**真的**开始消费 approval_required，
    本文件会失败，提示应改为「让死分支变活」而不是继续删除。
    """

    def test_bundle_does_not_consume_the_removed_keys(self) -> None:
        if not BUNDLE.exists():
            self.skipTest("冻结 bundle 不在（构建产物未入库）")
        text = BUNDLE.read_text(encoding="utf-8", errors="replace")
        self.assertEqual(text.count("approval_required"), 0,
                         "前端开始消费 approval_required 了 → 死分支该复活")
        self.assertEqual(text.count("finish_reason"), 0,
                         "前端开始消费 finish_reason 了 → 降级方案需重新评估")

    def test_kind_whitelist_makes_old_branch_unreachable(self) -> None:
        """第二重证据：kind 永远取不到 needs_approval/approval_required。"""
        from runtime.reply_parser import KINDS, parse

        self.assertNotIn("needs_approval", KINDS)
        self.assertNotIn("approval_required", KINDS)
        got = parse({"kind": "needs_approval", "content": "hi"}).canonical["kind"]
        self.assertEqual(got, "answer", "未知 kind 必须降级 answer")


if __name__ == "__main__":
    unittest.main()
