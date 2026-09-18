# -*- coding: utf-8 -*-
"""真实并发压力矩阵（P1 余项补测）：同 Runtime 并发多 Run，混合审批 / FileScope / compact。

离线确定性：不调模型、不连网。用真实 TaskManager(agent.db WAL) + AgentRuntime 主链，
execute_turn 由受控驱动代替（在真实 RunContext 下调用被包装的真实工具），并发交错验证：
- 多容器并行 Run 状态/账本/审批互不串写；
- 审批：并发两个 pending，批准 A 不影响 B；resume 同一 Run；单 Active 容器并发被拒；
- FileScope：严格 Project 内路径放行、范围外路径被拒且不执行；
- compact：per-session 锁并发不重叠，跨会话可并行。
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import main as main_mod
from runtime.context import _soft_compact_once
from runtime.errors import AgentError
from runtime.runner import AgentRuntime


def _invoke_async(tool, name: str, call_id: str, **kwargs):
    args = json.dumps(kwargs, ensure_ascii=False)
    ctx = ToolContext(context=None, tool_name=name, tool_call_id=call_id,
                      tool_arguments=args)
    result = tool.on_invoke_tool(ctx, args)
    if asyncio.iscoroutine(result):
        return result
    async def _wrap():
        return result
    return _wrap()


def _canonical(summary: str, content: str) -> dict:
    return {"kind": "answer", "summary": summary, "content": content}


class FakeSession:
    """最小 SDK 会话替身：只支持 compact 需要的三个异步方法。"""

    def __init__(self, session_id: str, items: list[dict]):
        self.session_id = session_id
        self._items = list(items)

    async def get_items(self, limit=None):
        return list(self._items)

    async def clear_session(self):
        self._items = []

    async def add_items(self, items):
        self._items = list(items)


class ConcurrentRunStressTests(unittest.TestCase):
    """真实 AgentRuntime 并发矩阵：多容器并行 + 审批隔离 + FileScope + 单 Active。"""

    def setUp(self) -> None:
        self._orig_execute = main_mod.execute_turn
        self._tmp = Path(tempfile.mkdtemp(prefix="forge_stress_"))
        self._project_dirs: list[Path] = []
        self.rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.rt._ensure()
        self.tools = {t.name: t for t in main_mod.assistant_agent.tools}
        self.outputs: dict[str, str] = {}
        self.markers: dict[str, str] = {}

    def tearDown(self) -> None:
        main_mod.execute_turn = self._orig_execute
        shutil.rmtree(self._tmp, ignore_errors=True)
        # 清理本次测试在仓库 forge_data/projects 下创建的临时文件
        for d in self._project_dirs:
            shutil.rmtree(d, ignore_errors=True)

    async def _fake_execute(self, mode, message, **kwargs):
        from runtime.runctx import current as _cur

        rctx = _cur()
        rid = rctx.run_id if rctx is not None else "?"
        if rid not in self.markers:
            self.markers[rid] = ("@calc" if "@calc" in message else
                                 "@forget" if "@forget" in message else
                                 "@projin" if "@projin" in message else
                                 "@projout" if "@projout" in message else
                                 "@sleep" if "@sleep" in message else "@plain")
        marker = self.markers[rid]
        try:
            if marker == "@calc":
                res = await _invoke_async(self.tools["calculate"], "calculate", rid + "-calc",
                                          expression="1+2")
                self.outputs[rid] = str(res)
                return _canonical("计算调用结束", "calculate 调用已结束。")
            if marker == "@forget":
                res = await _invoke_async(self.tools["forget_memory"], "forget_memory",
                                          rid + "-fm", id="mem_stress_not_exist")
                self.outputs[rid] = str(res)
                return _canonical("删除记忆调用结束", str(res)[:300])
            if marker in ("@projin", "@projout"):
                path = self._scope_in if marker == "@projin" else self._scope_out
                res = await _invoke_async(self.tools["read_workspace_file"],
                                          "read_workspace_file", rid + "-read",
                                          path=str(path))
                self.outputs[rid] = str(res)
                return _canonical("读取调用结束", "read_workspace_file 调用已结束。")
            if marker == "@sleep":
                await asyncio.sleep(0.6)
                return _canonical("睡眠结束", "plain")
            return _canonical("普通回答结束", "plain answer")
        except Exception as exc:  # noqa: BLE001 - 驱动层兜底记录
            self.outputs[rid] = f"EXC:{type(exc).__name__}:{exc}"
            return _canonical("异常收尾", "驱动异常已记录")

    def _strict_project_file(self, session_id: str) -> tuple[str, Path]:
        """建严格 Project 容器并放一个项目数据根内的文件。"""
        tm = self.rt.tasks
        container = tm.get_or_create_container(session_id)
        cid = container["id"]
        proj_dir = BASE / "forge_data" / "projects" / cid
        proj_dir.mkdir(parents=True, exist_ok=True)
        self._project_dirs.append(proj_dir)
        inside = proj_dir / "inside.txt"
        inside.write_text("PROJ_INSIDE_MARKER_7f3a", encoding="utf-8")
        return cid, inside

    def test_mixed_parallel_runs_approval_and_filescope_isolation(self):
        async def run():
            main_mod.execute_turn = self._fake_execute
            tm = self.rt.tasks

            # 4 个普通容器（2 计算 + 2 纯回答）
            calc_tasks = [
                asyncio.create_task(
                    self.rt.run_turn("@calc 计算", session_id=f"stress-calc-{i}",
                                     mode="async", max_turns=3))
                for i in range(2)
            ]
            plain_tasks = [
                asyncio.create_task(
                    self.rt.run_turn("@plain 普通", session_id=f"stress-plain-{i}",
                                     mode="async", max_turns=3))
                for i in range(2)
            ]
            # 2 个并发审批容器
            appr_tasks = [
                asyncio.create_task(
                    self.rt.run_turn("@forget 删除记忆", session_id=f"stress-appr-{i}",
                                     mode="async", max_turns=3))
                for i in range(2)
            ]
            # 2 个严格 Project 容器：范围内外各一
            cid_in, inside = self._strict_project_file("proj-stress-in")
            self._scope_in = inside
            self._scope_out = Path(tempfile.mkdtemp(prefix="stress_out_")) / "outside.txt"
            self._scope_out.write_text("OUTSIDE", encoding="utf-8")
            projin_task = asyncio.create_task(
                self.rt.run_turn("@projin 读项目内", task_container_id=cid_in,
                                 session_id="proj-stress-in", mode="async", max_turns=3))
            cid_out, _ = self._strict_project_file("proj-stress-out")
            projout_task = asyncio.create_task(
                self.rt.run_turn("@projout 读范围外", task_container_id=cid_out,
                                 session_id="proj-stress-out", mode="async", max_turns=3))

            await asyncio.gather(*(calc_tasks + plain_tasks + appr_tasks +
                                   [projin_task, projout_task]))

            for r in calc_tasks + plain_tasks:
                self.assertTrue(r.result().ok and
                                r.result().task.state.value == "completed")
            self.assertEqual(len(calc_tasks + plain_tasks), 4)

            # 两个审批 Run 都停在 WAITING_APPROVAL，且 pending 各自一条
            waits = [t.result() for t in appr_tasks]
            for w in waits:
                self.assertTrue(w.waiting_approval)
                self.assertEqual(w.task.state.value, "waiting_approval")
            pends = tm.list_approvals(state="pending")
            self.assertEqual(len(pends), 2, pends)
            self.assertEqual({p["task_id"] for p in pends},
                             {w.task.id for w in waits})
            self.assertEqual({p["tool"] for p in pends}, {"forget_memory"})
            self.assertEqual(len({p["id"] for p in pends}), 2)

            # FileScope：范围内真实读取完成；范围外被拦
            in_run = projin_task.result()
            out_run = projout_task.result()
            self.assertTrue(in_run.ok and in_run.task.state.value == "completed")
            in_text = next(v for k, v in self.outputs.items() if k == in_run.task.id)
            self.assertIn("PROJ_INSIDE_MARKER_7f3a", in_text)
            self.assertTrue(out_run.ok and out_run.task.state.value == "completed")
            out_text = next(v for k, v in self.outputs.items() if k == out_run.task.id)
            self.assertIn("文件边界", out_text)

            # 批准 A → resume A；B 仍 pending；再批准 B → resume B
            wA, wB = waits
            pa = tm.list_approvals(task_id=wA.task.id, state="pending")[0]
            pb = tm.list_approvals(task_id=wB.task.id, state="pending")[0]
            self.assertNotEqual(pa["id"], pb["id"])
            # resume 段：@forget 删除记忆 带 mutation 义务，而 forget_memory 不属于
            # mutation 工具集（note_progress 无法置位 mutation_seen），义务门必然判缺证据。
            # 本用例验证的是并发审批隔离与 resume 收口，不是义务门语义，故此处显式关闭。
            with mock.patch.dict(os.environ, {"FORGE_OBLIGATION_GATE": "off"}):
                tm.decide_approval(pa["id"], "approved", actor="stress", reason="A")
                rA = await self.rt.run_turn("", task_id=wA.task.id, mode="async", max_turns=3)
                self.assertTrue(rA.ok and rA.task.id == wA.task.id)
                self.assertEqual(rA.task.state.value, "completed")
                left = tm.list_approvals(state="pending")
                self.assertEqual([p["task_id"] for p in left], [wB.task.id])
                tm.decide_approval(pb["id"], "approved", actor="stress", reason="B")
                rB = await self.rt.run_turn("", task_id=wB.task.id, mode="async", max_turns=3)
                self.assertTrue(rB.ok and rB.task.state.value == "completed")
                self.assertEqual(tm.list_approvals(state="pending"), [])

            # 工具调用行归属各自 Run（无串写）
            for w in waits:
                rows = tm.list_tool_calls(w.task.id)
                self.assertTrue(rows)
                self.assertTrue(all(c.get("tool_name") == "forget_memory" for c in rows))
                self.assertTrue(all(c.get("invocation_id") for c in rows))

        asyncio.run(run())

    def test_single_active_run_rejects_concurrent_second_run(self):
        async def run():
            main_mod.execute_turn = self._fake_execute
            first = asyncio.create_task(
                self.rt.run_turn("@sleep 慢任务", session_id="stress-single",
                                 mode="async", max_turns=3))
            await asyncio.sleep(0.15)  # 确保首个 Run 已 RUNNING
            with self.assertRaises(AgentError) as cm:
                await self.rt.run_turn("@plain 第二个", session_id="stress-single",
                                       mode="async", max_turns=3)
            self.assertIn("已有正在处理", str(cm.exception))
            r1 = await first
            self.assertTrue(r1.ok and r1.task.state.value == "completed")
            r2 = await self.rt.run_turn("@plain 恢复后可再跑", session_id="stress-single",
                                        mode="async", max_turns=3)
            self.assertTrue(r2.ok and r2.task.state.value == "completed")

        asyncio.run(run())


class CompactLockStressTests(unittest.TestCase):
    """并发 compact：per-session 锁保证同会话不重叠，跨会话可并行。"""

    def _session_items(self) -> list[dict]:
        items: list[dict] = []
        body = "用户需求：请把这段历史整理成摘要。" * 20
        for i in range(6):
            items.append({"role": "user", "content": f"第{i}轮：{body}"})
            items.append({"role": "assistant", "content": f"第{i}轮回复要点。"})
        return items

    async def _noop_emit(self, *a, **k):
        return None

    def test_per_session_lock_serializes_same_session(self):
        env_patch = mock.patch.dict(os.environ, {
            "AUTO_SUMMARY_MIN_TURNS": "2",
            "AUTO_SUMMARY_TRIGGER_TURNS": "3",
            "AUTO_SUMMARY_TRIGGER_CHARS": "500",
            "AUTO_SUMMARY_KEEP_TURNS": "1",
        })
        counters = {"active": 0, "max_active": 0}
        guard = threading.Lock()
        calls = {"n": 0}

        def slow_summarize(transcript: str) -> str:
            with guard:
                counters["active"] += 1
                counters["max_active"] = max(counters["max_active"], counters["active"])
                calls["n"] += 1
            time.sleep(0.12)
            with guard:
                counters["active"] -= 1
            return "并发压力摘要"

        async def run():
            import compact

            with env_patch, mock.patch.object(compact, "summarize_transcript",
                                              side_effect=slow_summarize):
                s = FakeSession("sess-compact-same", self._session_items())
                await asyncio.gather(
                    _soft_compact_once(s, self._noop_emit),
                    _soft_compact_once(s, self._noop_emit),
                    _soft_compact_once(s, self._noop_emit),
                )
            self.assertEqual(counters["max_active"], 1)
            self.assertGreaterEqual(calls["n"], 1)

        asyncio.run(run())

    def test_different_sessions_compact_in_parallel(self):
        env_patch = mock.patch.dict(os.environ, {
            "AUTO_SUMMARY_MIN_TURNS": "2",
            "AUTO_SUMMARY_TRIGGER_TURNS": "3",
            "AUTO_SUMMARY_TRIGGER_CHARS": "500",
            "AUTO_SUMMARY_KEEP_TURNS": "1",
        })
        counters = {"active": 0, "max_active": 0}
        guard = threading.Lock()

        def slow_summarize(transcript: str) -> str:
            with guard:
                counters["active"] += 1
                counters["max_active"] = max(counters["max_active"], counters["active"])
            time.sleep(0.12)
            with guard:
                counters["active"] -= 1
            return "并行摘要"

        async def run():
            import compact

            with env_patch, mock.patch.object(compact, "summarize_transcript",
                                              side_effect=slow_summarize):
                s1 = FakeSession("sess-compact-x1", self._session_items())
                s2 = FakeSession("sess-compact-x2", self._session_items())
                await asyncio.gather(
                    _soft_compact_once(s1, self._noop_emit),
                    _soft_compact_once(s2, self._noop_emit),
                    _soft_compact_once(s1, self._noop_emit),
                    _soft_compact_once(s2, self._noop_emit),
                )
            self.assertGreaterEqual(counters["max_active"], 2)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
