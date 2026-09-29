"""P0-C think / scratchpad 工具回归（2026-09-21，P0 三件套最后一块）。

按 docs/archive/2026-09/FORGE-ARCHITECTURE-AUDIT-2026-09-21.md §2.3 落地 think 工具：
- 零副作用、只写 Run 级 scratchpad（不落库、不进主 context、不当 evidence）；
- 补「模型→自己」的过程地板（与 decision_hint「运行时→模型」互补）；
- 随时可用、无目标参数、不在任何 mutation/discovery/read 分类表里。

本组用例钉死：
1. think 被调后写进 RunContext.think_notes（累计条数递增、截断 500 字符、最多保留 20 条）；
2. think 不在 _P9_MUTATION_TOOLS（调用后不 bump epoch、不算 mutation）；
3. think 不在 _P9_DISCOVERY_TOOLS / _P9_READ_TOOLS（不被收敛禁止、不算读目标资源）；
4. tools.think 在 Run 内返回确认文案、无 Run 时降级提示；
5. think_notes_snapshot 返回只读副本（改动快照不影响 rctx）。
"""
from __future__ import annotations

import asyncio
import json
import unittest

import tools
from agents.tool_context import ToolContext
from runtime import runctx
from runtime.readiness_gate import _P9_DISCOVERY_TOOLS, _P9_MUTATION_TOOLS, _P9_READ_TOOLS


def invoke_think(text: str) -> str:
    """按项目测试惯例调 @function_tool 包装的 think（不经过模型）。"""
    input_json = json.dumps({"text": text}, ensure_ascii=False)

    async def _inv() -> str:
        ctx = ToolContext(context=None, tool_name=tools.think.name,
                          tool_call_id="t1", tool_arguments=input_json)
        result = tools.think.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_inv())


class ThinkNotesStorageTests(unittest.TestCase):
    """RunContext.think_notes 的存储语义。"""

    def _fresh(self) -> "runctx.RunContext":
        return runctx.RunContext(run_id="r1", session_id="s1", container_id="c1")

    def test_note_think_appends_and_counts(self) -> None:
        r = self._fresh()
        self.assertEqual(r.think_notes_snapshot(), [])
        self.assertEqual(r.note_think("第一步思路"), 1)
        self.assertEqual(r.note_think("第二步"), 2)
        self.assertEqual(r.think_notes_snapshot(), ["第一步思路", "第二步"])

    def test_note_think_truncates_to_500(self) -> None:
        r = self._fresh()
        r.note_think("x" * 900)
        self.assertEqual(len(r.think_notes_snapshot()[0]), 500)

    def test_note_think_keeps_at_most_20(self) -> None:
        r = self._fresh()
        for i in range(30):
            r.note_think(f"n{i}")
        snap = r.think_notes_snapshot()
        self.assertEqual(len(snap), 20)
        # 保留的是较新的（n10..n29），较旧的被弹出
        self.assertEqual(snap[0], "n10")
        self.assertEqual(snap[-1], "n29")

    def test_note_think_empty_is_noop(self) -> None:
        r = self._fresh()
        self.assertEqual(r.note_think("   "), 0)
        self.assertEqual(r.think_notes_snapshot(), [])

    def test_snapshot_is_read_only_copy(self) -> None:
        r = self._fresh()
        r.note_think("a")
        snap = r.think_notes_snapshot()
        snap.append("mutated")
        self.assertEqual(r.think_notes_snapshot(), ["a"])


class ThinkNotInClassificationTablesTests(unittest.TestCase):
    """think 必须不在任何 mutation / discovery / read 分类表里。"""

    def test_think_not_mutation(self) -> None:
        self.assertNotIn("think", _P9_MUTATION_TOOLS)

    def test_think_not_discovery(self) -> None:
        self.assertNotIn("think", _P9_DISCOVERY_TOOLS)

    def test_think_not_read_target(self) -> None:
        self.assertNotIn("think", _P9_READ_TOOLS)


class ThinkToolCallableTests(unittest.TestCase):
    """tools.think 的行为（Run 内 / 无 Run 降级）。"""

    def test_think_in_run_returns_confirmation(self) -> None:
        r = runctx.RunContext(run_id="r2", session_id="s2", container_id="c2")
        runctx.bind(r)
        self.addCleanup(runctx.bind, None)
        out = invoke_think("打算先读 README")
        self.assertIn("已记录", out)
        self.assertEqual(len(r.think_notes_snapshot()), 1)
        self.assertEqual(r.think_notes_snapshot()[0], "打算先读 README")

    def test_think_without_run_degrades(self) -> None:
        runctx.bind(None)
        out = invoke_think("x")
        self.assertIn("无活跃 Run", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
