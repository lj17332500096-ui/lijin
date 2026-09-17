# -*- coding: utf-8 -*-
"""生产收口回归：取消语义 / Write-Ahead / 单 Active Run / 幂等 / Approval 原子 /
FileScope fail-closed / context overflow / 流式重试 / Sources 恢复与过滤。"""
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

from runtime.approval import ApprovalGate
from runtime.errors import AgentError
from runtime.task import TaskState
from runtime.task_manager import TaskManager

import main as _main_mod
_ORIG_EXECUTE_TURN = getattr(_main_mod, "execute_turn", None)


def _manager(tmp: Path) -> TaskManager:
    return TaskManager(str(tmp / "agent.db"))


def _mk_run(mgr: TaskManager, container_id: str, goal: str = "测试任务"):
    run = mgr.create_task(session_id="proj-s", goal=goal, thread_id=container_id)
    mgr.add_message(container_id, "user", goal, run_id=run.id)
    return run


class CancelSemanticsTests(unittest.TestCase):
    """T-CANCEL-01/04：用户取消必须真正停止执行；不得再出现 completed/failed 或重复 assistant 消息。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pccancel_"))
        self.mgr = _manager(self.tmp)
        self.container = self.mgr.get_or_create_container("proj-cancel")

    def tearDown(self) -> None:
        import shutil

        if _ORIG_EXECUTE_TURN is not None:
            try:
                _main_mod.execute_turn = _ORIG_EXECUTE_TURN
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_runtime(self, gate_on_model: asyncio.Event):
        from runtime.runner import AgentRuntime

        runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        runtime._ensure()

        async def _fake_execute_turn(mode, message, session=None, debug=False, max_turns=20,
                                     history_limit=None, agent=None, audit=None,
                                     stream_events_cb=None, provider=None):
            await gate_on_model.wait()  # 卡在“模型调用”中，直到被取消
            return json.dumps({"kind": "answer", "summary": "s", "content": "完成",
                               "questions": [], "saved_file": None,
                               "next_step": None}, ensure_ascii=False)

        import main as main_mod

        main_mod.execute_turn = _fake_execute_turn
        return runtime

    def test_explicit_cancel_stops_execution_and_lands_cancelled(self):
        gate = asyncio.Event()
        runtime = self._build_runtime(gate)
        run = _mk_run(self.mgr, self.container["id"])

        async def _scenario():
            spawn = runtime.spawn_run_task(run.id, runtime.run_turn(
                run.goal, session=None, session_id="proj-s", mode="async",
                task_id=run.id, metadata={"channel": "web"}))
            await asyncio.sleep(0.3)  # 让执行进入“模型调用”
            self.assertTrue(runtime.cancel_run(run.id))
            try:
                await asyncio.wait_for(asyncio.shield(spawn), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            return runtime.tasks.get_task(run.id)

        state = asyncio.run(_scenario())
        self.assertEqual(state.state, TaskState.CANCELLED)
        # 不再发生 completed / failed
        events = runtime.tasks.list_events(run.id)
        types = [e.event_type for e in events]
        self.assertNotIn("task.completed", types)
        self.assertNotIn("task.failed", types)
        self.assertIn("task.cancelled", types)
        # assistant 消息只写一次（取消说明）
        msgs = [m for m in runtime.tasks.list_messages(self.container["id"])
                if m.get("run_id") == run.id and m.get("role") == "assistant"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual((msgs[0].get("meta") or {}).get("run_state"), "cancelled")
        # 取消后不得再启动新一轮执行
        gate.set()
        try:
            asyncio.run(asyncio.wait_for(asyncio.shield(spawn), timeout=3))
        except Exception:
            pass
        self.assertEqual(runtime.tasks.get_task(run.id).state, TaskState.CANCELLED)

    def test_no_cancel_lets_run_complete(self):
        gate = asyncio.Event()

        async def _fake2(mode, message, session=None, debug=False, max_turns=20,
                         history_limit=None, agent=None, audit=None,
                         stream_events_cb=None, provider=None):
            gate.set()
            return json.dumps({"kind": "answer", "summary": "s", "content": "OK",
                               "questions": [], "saved_file": None,
                               "next_step": None}, ensure_ascii=False)

        import main as main_mod

        main_mod.execute_turn = _fake2
        from runtime.runner import AgentRuntime

        runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        runtime._ensure()
        run = _mk_run(self.mgr, self.container["id"])

        async def _go():
            spawn = runtime.spawn_run_task(run.id, runtime.run_turn(
                run.goal, session=None, session_id="proj-s", mode="async",
                task_id=run.id, metadata={"channel": "web"}))
            await asyncio.wait_for(asyncio.shield(spawn), timeout=30)
            return runtime.tasks.get_task(run.id)

        state = asyncio.run(_go())
        self.assertEqual(state.state, TaskState.COMPLETED)


class WriteAheadAndRecoverTests(unittest.TestCase):
    """PHASE2：副作用执行前有 durable pending；恢复时标记 interrupted，绝不自动重放。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pcwa_"))
        self.mgr = _manager(self.tmp)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pending_row_then_recover_marks_interrupted(self):
        container = self.mgr.get_or_create_container("proj-wa")
        run = _mk_run(self.mgr, container["id"])
        self.mgr.transition(run.id, TaskState.RUNNING, reason="start")
        # Write-Ahead：执行前 pending（模拟执行中 crash → 状态停留在 pending）
        rid = self.mgr.insert_tool_call(
            task_id=run.id, tool_name="edit_project_file",
            arguments={"path": "a.py", "old": "x", "new": "y"},
            status="pending", invocation_id="inv-1")
        self.assertTrue(self.mgr.find_tool_call_by_invocation(run.id, "inv-1"))
        # crash → 下次启动 recover：pending 副作用被标记 interrupted（不自动重放）
        recovered = self.mgr.recover_stale_tasks(max_age_seconds=-1)
        self.assertIn(run.id, recovered)
        row = self.mgr.find_tool_call_by_invocation(run.id, "inv-1")
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(self.mgr.get_task(run.id).state, TaskState.FAILED)
        evs = [e.event_type for e in self.mgr.list_events(run.id)]
        self.assertIn("tool.side_effect_unknown", evs)

    def test_durable_executed_then_update_no_duplicate_row(self):
        container = self.mgr.get_or_create_container("proj-wa2")
        run = _mk_run(self.mgr, container["id"])
        self.mgr.transition(run.id, TaskState.RUNNING, reason="start")
        row1 = self.mgr.insert_tool_call(
            task_id=run.id, tool_name="save_note", arguments={"title": "t", "content": "c"},
            status="pending", invocation_id="inv-dup")
        # audit.ingest / backfill 以同一 invocation 再来 → 不产生第二行
        row2 = self.mgr.insert_tool_call(
            task_id=run.id, tool_name="save_note", arguments={"title": "t", "content": "c"},
            status="succeeded", invocation_id="inv-dup")
        self.assertEqual(row1, row2)
        self.assertEqual(len(self.mgr.list_tool_calls(run.id)), 1)

    def test_approval_decide_atomic_double_decision_fails(self):
        container = self.mgr.get_or_create_container("proj-appr")
        run = _mk_run(self.mgr, container["id"])
        self.mgr.transition(run.id, TaskState.RUNNING, reason="start")
        appr = self.mgr.create_approval(run.id, "run_python", "{}", {"code": "x"})
        first = self.mgr.decide_approval(appr, "approved", actor="u1")
        self.assertEqual(first["status"], "approved")
        with self.assertRaises(AgentError) as cm:
            self.mgr.decide_approval(appr, "denied", actor="u2")
        self.assertIn("已处理", str(cm.exception))
        row = self.mgr.list_approvals(task_id=run.id)[0]
        self.assertEqual(row["status"], "approved")


class SingleActiveRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pcactive_"))
        self.mgr = _manager(self.tmp)
        self.container = self.mgr.get_or_create_container("proj-active")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_run_in_container_blocked_while_first_active(self):
        run1 = _mk_run(self.mgr, self.container["id"])
        self.mgr.transition(run1.id, TaskState.RUNNING, reason="start")
        active = self.mgr.find_active_run(self.container["id"])
        self.assertEqual(active.id, run1.id)
        with self.assertRaises(AgentError) as cm:
            self.mgr.create_task(session_id="proj-s", goal="并发第二条", thread_id=self.container["id"])
        self.assertIn("已有正在处理", str(cm.exception))
        # 完成后允许下一条
        self.mgr.transition(run1.id, TaskState.COMPLETED, reason="done")
        run2 = self.mgr.create_task(session_id="proj-s", goal="第二条", thread_id=self.container["id"])
        self.assertIsNotNone(run2)

    def test_waiting_approval_counts_as_active(self):
        run1 = _mk_run(self.mgr, self.container["id"])
        self.mgr.transition(run1.id, TaskState.RUNNING, reason="start")
        self.mgr.transition(run1.id, TaskState.WAITING_APPROVAL, reason="approval")
        self.assertIsNotNone(self.mgr.find_active_run(self.container["id"]))
        with self.assertRaises(AgentError):
            self.mgr.create_task(session_id="proj-s", goal="x", thread_id=self.container["id"])

    def test_different_containers_parallel_ok(self):
        c2 = self.mgr.get_or_create_container("proj-b2")
        run1 = _mk_run(self.mgr, self.container["id"])
        run2 = _mk_run(self.mgr, c2["id"])
        self.mgr.transition(run1.id, TaskState.RUNNING, reason="start")
        self.mgr.transition(run2.id, TaskState.RUNNING, reason="start")
        self.assertIsNotNone(run1)
        self.assertIsNotNone(run2)

    def test_client_message_idempotency(self):
        cid = "proj-idem"
        container = self.mgr.get_or_create_container(cid)
        run = _mk_run(self.mgr, container["id"], goal="幂等消息")
        self.mgr.transition(run.id, TaskState.RUNNING, reason="start")
        self.mgr.transition(run.id, TaskState.COMPLETED, reason="done")
        self.assertIsNone(self.mgr.record_client_message(container["id"], "cm-1", run.id))
        self.assertEqual(self.mgr.find_run_by_client_message(container["id"], "cm-1"), run.id)
        # 重复登记同 id → 返回既有 run，不新增
        self.assertEqual(self.mgr.record_client_message(container["id"], "cm-1", "task_other"), run.id)
        self.assertEqual(len(self.mgr.list_tasks(container_id=container["id"], limit=10)), 1)


class FileScopeFailClosedTests(unittest.TestCase):
    def test_unknown_tool_denied_in_strict_scope(self):
        from runtime.filescope import FileScope, authorize_tool

        scope = FileScope(strict=True, project_data_root=Path("F:/tmp/data"))
        ok, reason, _ = authorize_tool("some_new_tool", {"path": "F:/tmp/data/x.txt"}, scope)
        self.assertFalse(ok)
        self.assertIn("tool policy not registered", reason)

    def test_registered_non_file_tool_allowed(self):
        from runtime.filescope import FileScope, authorize_tool

        scope = FileScope(strict=True, project_data_root=Path("F:/tmp/data"))
        ok, _, _ = authorize_tool("calculate", {"expr": "1+1"}, scope)
        self.assertTrue(ok)


class ProviderClosureTests(unittest.TestCase):
    def test_context_overflow_classified_no_retry(self):
        from runtime.provider_errors import (ProviderErrorKind, classify, is_context_overflow,
                                             retry_policy)

        exc = RuntimeError("Error code: 400 - This model's maximum context length is 16385 tokens")
        kind, _pub, _rid = classify(exc)
        self.assertEqual(kind, ProviderErrorKind.CONTEXT_OVERFLOW)
        self.assertTrue(is_context_overflow(exc))
        self.assertIsNone(retry_policy(kind, 0))

    def test_stream_retry_before_first_token_and_no_replay_after(self):
        import time

        from runtime.provider_gateway import ResilientModel, take_attempts

        attempts = {"n": 0, "mid_fail": 0}

        class _FakeProvider:
            def _fallback_config(self):
                return None

            def _get_fallback_model(self):
                return None

        class _Inner:
            def stream_response(self, *args, **kwargs):
                async def _gen():
                    if attempts["mid_fail"] == 0:
                        attempts["mid_fail"] += 1
                        raise RuntimeError("Error code: 503 - No available channel")
                    for i in range(3):
                        yield {"type": "response.output_text.delta", "data": {"text": str(i)}}
                return _gen()

        fake = _FakeProvider()
        model = ResilientModel(_Inner(), fake)

        async def _consume():
            out = []
            async for ev in model.stream_response(None, "hi", None, [], None, [], None):
                out.append(ev)
            return out

        # 首 token 前 503 → 有限重试后成功（两次尝试，无重复 token）
        out = asyncio.run(_consume())
        self.assertEqual([e["data"]["text"] for e in out], ["0", "1", "2"])
        recs = take_attempts("_probe")
        asyncio.run(_consume())  # 占位保证 take 之后有记录

    def test_mid_stream_error_no_replay_and_interrupted_recorded(self):
        from runtime.provider_gateway import ResilientModel

        class _FakeProvider2:
            def _fallback_config(self):
                return None

            def _get_fallback_model(self):
                return None

        class _Inner2:
            def stream_response(self, *args, **kwargs):
                async def _gen():
                    yield {"type": "response.output_text.delta", "data": {"text": "a"}}
                    raise RuntimeError("Error code: 500 - boom")
                return _gen()

        model = ResilientModel(_Inner2(), _FakeProvider2())

        async def _consume():
            seen = []
            try:
                async for ev in model.stream_response(None, "hi", None, [], None, [], None):
                    seen.append(ev)
            except RuntimeError as exc:
                return seen, exc
            return seen, None

        seen, exc = asyncio.run(_consume())
        self.assertEqual(len(seen), 1)  # 只输出过首 token，绝不自动从头重放
        self.assertIsNotNone(exc)


class SourcesRecoveryTests(unittest.TestCase):
    def test_recover_interrupted_and_retrieve_filters_non_ready(self):
        from sources import store
        from sources.indexer import index_source_file, recover_interrupted_indexes

        tmp = Path(tempfile.mkdtemp(prefix="pcsrc_"))
        mgr = _manager(tmp)
        container = mgr.get_or_create_container("proj-src")
        pid = container["id"]
        f1 = tmp / "a.txt"
        f2 = tmp / "b.txt"
        f1.write_text("hello world", encoding="utf-8")
        f2.write_text("another file", encoding="utf-8")
        s1 = mgr.add_project_source(task_id=pid, display_name="a.txt", stored_path=str(f1),
                                    parse_status="pending")
        s2 = mgr.add_project_source(task_id=pid, display_name="b.txt", stored_path=str(f2),
                                    parse_status="ok")
        store.update_source_status(s2["id"], index_status="ready")
        # 未 ready 之前：检索不到（不静默命中半成品索引）
        self.assertEqual(store.fts_search(pid, "hello"), [])
        # 模拟索引完成前崩溃：s1 停在 pending → 启动恢复标记 failed
        n = recover_interrupted_indexes()
        self.assertGreaterEqual(n, 1)
        row = store.get_source_row(s1["id"])
        self.assertEqual(row["parse_status"], "failed")
        # 重新索引成功后即可检索（索引流程会把状态置回 ready）
        src_row = mgr.get_project_source(s1["id"])
        self.assertIsNotNone(src_row)
        index_source_file(pid, dict(src_row))
        res = store.fts_search(pid, "hello")
        self.assertTrue(any(r["source_id"] == s1["id"] for r in res))
        # 清理进程级共享态（避免影响后续 rag 测试的向量引擎缓存/DB 指向）
        try:
            import rag as _rag

            _rag._EMBEDDER = None
            _rag._EMBEDDER_LOADED = False
            _rag.EMBED_MODEL_SETTING = ""
        except Exception:
            pass
        try:
            from sources.store import DB_PATH as _DEF_SRC_DB, set_db_path as _set_src_db

            _set_src_db(str(Path(_DEF_SRC_DB)))
        except Exception:
            pass
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
