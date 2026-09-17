# -*- coding: utf-8 -*-
"""P1 Runtime 可靠性与安全：Phase B/C/D 的离线测试。

- Phase B（FileScope）：跨 Project/WorkLocation 隔离 + 路径安全矩阵（纯函数）
- Phase C（RunContext）：并发 Run 下 Approval / Memory Scope 隔离
- Phase D（Audit）：失败路径兜底（tool/model 可追踪）+ 落库脱敏
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
import tools as tools_mod
from runtime.approval import ApprovalGate
from runtime.filescope import authorize_tool, build_file_scope
from runtime.runctx import RunContext, bind as bind_ctx
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager


def invoke_tool(tool, **kwargs) -> str:
    """按测试惯例调用 @function_tool 包装的工具（不经过模型）。"""
    import json as _json

    from agents.tool_context import ToolContext

    input_json = _json.dumps(kwargs, ensure_ascii=False)

    async def _inv() -> str:
        ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="p1",
                          tool_arguments=input_json)
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_inv())


# =====================================================================
# Phase B：FileScope 纯函数
# =====================================================================

class FileScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="filescope_"))
        (self.tmp / "wlA").mkdir()
        (self.tmp / "wlB").mkdir()
        base = self.tmp / "forge"
        base.mkdir()
        (base / "notes").mkdir(parents=True)
        (base / "forge_data" / "projects" / "tk_A").mkdir(parents=True)
        (base / "forge_data" / "projects" / "tk_B").mkdir(parents=True)
        self.base = base
        self.ws = self.tmp
        self.scopeA = build_file_scope(
            container_id="tk_A", session_id="proj-a", work_location_path=str(self.tmp / "wlA"),
            base_dir=base, workspace_root=self.tmp, notes_dir=base / "notes")
        self.scopeB = build_file_scope(
            container_id="tk_B", session_id="proj-b", work_location_path=str(self.tmp / "wlB"),
            base_dir=base, workspace_root=self.tmp, notes_dir=base / "notes")
        self.scopeNoWl = build_file_scope(
            container_id="tk_C", session_id="proj-c", work_location_path=None,
            base_dir=base, workspace_root=self.tmp, notes_dir=base / "notes")
        self.scopeLegacy = build_file_scope(
            container_id=None, session_id="personal", work_location_path=None,
            base_dir=base, workspace_root=self.tmp, notes_dir=base / "notes")

    def _read_ok(self, scope, target: str) -> bool:
        ok, reason, _ = authorize_tool("read_workspace_file", {"path": target}, scope)
        return ok

    def _write_ok(self, scope, target: str) -> bool:
        ok, reason, _ = authorize_tool("write_project_file", {"path": target, "content": "x"}, scope)
        return ok

    def test_A_reads_own_worklocation_ok(self) -> None:
        self.assertTrue(self._read_ok(self.scopeA, str(self.tmp / "wlA" / "src" / "a.py")))
        self.assertTrue(self._write_ok(self.scopeA, "a.py"))  # 相对路径 → wl 根

    def test_A_cannot_read_B_worklocation(self) -> None:
        self.assertFalse(self._read_ok(self.scopeA, str(self.tmp / "wlB" / "private.txt")))

    def test_cross_project_data_denied(self) -> None:
        b_source = str(self.base / "forge_data" / "projects" / "tk_B" / "sources" / "x.md")
        self.assertFalse(self._read_ok(self.scopeA, b_source))
        self.assertTrue(self._read_ok(self.scopeA,
                                      str(self.base / "forge_data" / "projects" / "tk_A" / "sources" / "x.md")))

    def test_system_directory_denied(self) -> None:
        for scope in (self.scopeA, self.scopeB, self.scopeNoWl):
            self.assertFalse(self._read_ok(scope, r"C:\Windows\win.ini"))
            self.assertFalse(self._read_ok(scope, str(self.tmp.parent / "outside.txt")))

    def test_no_worklocation_project_is_read_only(self) -> None:
        self.assertTrue(self.scopeNoWl.read_only)
        self.assertFalse(self._write_ok(self.scopeNoWl, "note.md"))
        self.assertFalse(self._write_ok(self.scopeNoWl, "..\\..\\..\\evil.txt"))

    def test_legacy_personal_scope_unchanged(self) -> None:
        self.assertFalse(self.scopeLegacy.strict)
        self.assertTrue(self._read_ok(self.scopeLegacy, str(self.ws / "any.txt")))

    def test_path_security_matrix(self) -> None:
        wl = self.tmp / "wlA"
        for bad in ("..\\..\\outside.txt", "../wlB/private.txt",
                    str(self.tmp / "wlB"), "\\\\server\\share\\x"):
            ok, reason, _ = authorize_tool("read_workspace_file", {"path": bad}, self.scopeA)
            self.assertFalse(ok, bad)
        # 大小写/子路径回绕：仍在 wl 内 → 放行；wl 外 → 拒绝
        case_ok, _, _ = authorize_tool("read_workspace_file",
                                       {"path": str(wl).upper() + "\\A.TXT"}, self.scopeA)
        self.assertTrue(case_ok)
        traverse_ok, _, _ = authorize_tool("read_workspace_file",
                                           {"path": str(wl / "sub") + "\\..\\..\\wlB\\x"}, self.scopeA)
        self.assertFalse(traverse_ok)

    def test_dotenv_and_sensitive_still_blocked_by_tool_layer(self) -> None:
        """边界层放行路径后，工具层原有 .env 等拒绝继续生效（回归锚点）。"""
        self.assertTrue(self._read_ok(self.scopeLegacy, "my_creative_agent/.env"))
        # 工具层实测拒绝 .env
        result = invoke_tool(tools_mod.read_workspace_file, path="my_creative_agent/.env")
        self.assertIn("不允许读取", str(result))


# =====================================================================
# Phase C：Approval / Memory 并发隔离
# =====================================================================

class ApprovalIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gate_conc_"))
        self.manager = TaskManager(self.tmp / "agent.db")
        self.gate = ApprovalGate(self.manager)
        self._saved = os.environ.get("APPROVAL")
        os.environ["APPROVAL"] = "on"

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("APPROVAL", None)
        else:
            os.environ["APPROVAL"] = self._saved

    def test_concurrent_runs_approvals_isolated(self) -> None:
        """两个并发 Run 交错调用 gated tool：审批行只挂在各自 run 下。"""
        from runtime.errors import ApprovalRequired

        async def scenario(run_tag: str) -> dict:
            task = self.manager.create_task(f"s_{run_tag}", "x")
            self.gate.begin(task.id, channel="chat")
            # 交错：A 先挂起，B 再挂起，A 批准，B 仍 pending
            await asyncio.sleep(0)
            try:
                r1 = await self.gate.check("run_python", {"code": f"print('{run_tag}1')"}, run_id=task.id)
                r1_blocked = r1 is not None
            except ApprovalRequired:
                r1_blocked = True
            await asyncio.sleep(0.02 if run_tag == "A" else 0)
            try:
                r2 = await self.gate.check("code_loop", {"project": "p", "filename": "a.py"}, run_id=task.id)
                r2_blocked = r2 is not None
            except ApprovalRequired:
                r2_blocked = True
            if run_tag == "A":
                for aid_row in self.manager.list_pending_approvals(task.id):
                    self.manager.decide_approval(aid_row["id"], "approved", actor="user")
            self.gate.end()
            rows = self.manager.list_approvals(task_id=task.id, limit=50)
            self.gate.clear_run(task.id)
            return {"task": task.id, "rows": rows, "r1_blocked": r1_blocked,
                    "r2_blocked": r2_blocked}

        async def run_all() -> list[dict]:
            return await asyncio.gather(scenario("A"), scenario("B"),
                                        scenario("A2"), scenario("B2"))

        results = asyncio.run(run_all())
        for res in results:
            self.assertTrue(res["r1_blocked"] and res["r2_blocked"])
            for row in res["rows"]:
                self.assertEqual(row["task_id"], res["task"])
        # A 批准自己的 run_python 不影响 B 的同名 pending
        a_rows = results[0]["rows"]
        b_rows = results[2]["rows"] if False else results[1]["rows"]
        a_approved = {r["id"] for r in a_rows if r["status"] == "approved"}
        b_pending = {r["id"] for r in b_rows if r["status"] == "pending"}
        self.assertTrue(a_approved)
        self.assertTrue(b_pending)
        self.assertFalse(a_approved & b_pending)
        # 批准只影响同 run 同参数
        task_c = self.manager.create_task("s_C", "x")
        self.gate.begin(task_c.id, channel="chat")
        from runtime.errors import ApprovalRequired
        with self.assertRaises(ApprovalRequired):
            blocked = asyncio.run(self.gate.check("run_python", {"code": "print('A1')"}, run_id=task_c.id))
        self.gate.end()


class MemoryScopeConcurrentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mem_conc_"))
        db = self.tmp / "agent.db"
        self.manager = TaskManager(str(db))
        # 把 tools 的记忆后端指向临时库，避免污染真实 agent.db
        self._orig_db = tools_mod._MEMORY_DB_PATH
        tools_mod._MEMORY_DB_PATH = db
        self.marker_a = "PMEM_A_ONLY_777"
        self.marker_g = "GMEM_GLOBAL_888"
        self.manager.add_project_memory("tk_A", self.marker_a, ["audit"])
        self.manager.memory_upsert_row(
            {"id": "mem_g", "text": self.marker_g, "tags": [], "created_at": "x", "updated_at": "x"},
            scope_type="user", scope_id="personal", memory_type="semantic",
        )

    def tearDown(self) -> None:
        tools_mod._MEMORY_DB_PATH = self._orig_db

    def test_memory_scope_never_leaks_across_concurrent_tasks(self) -> None:
        """P1-C：并发任务各自绑定 project_only / global，交错 recall 不串。"""

        async def scenario_a() -> str:
            bind_ctx(RunContext(run_id="rA", container_id="tk_A", session_id="proj-a",
                                channel="chat", memory_scope="project_only"))
            await asyncio.sleep(0.02)
            out = await asyncio.to_thread(invoke_tool, tools_mod.recall_memory, keyword="777")
            bind_ctx(None)
            return out

        async def scenario_b() -> str:
            bind_ctx(RunContext(run_id="rB", container_id="tk_B", session_id="proj-b",
                                channel="chat", memory_scope="global"))
            await asyncio.sleep(0.01)
            out = await asyncio.to_thread(invoke_tool, tools_mod.recall_memory, keyword="888")
            bind_ctx(None)
            return out

        async def run_all() -> tuple[str, str]:
            return await asyncio.gather(scenario_a(), scenario_b())

        for _ in range(10):
            a, b = asyncio.run(run_all())
            self.assertIn(self.marker_a, a)
            self.assertNotIn(self.marker_g, a)
            self.assertIn(self.marker_g, b)
            self.assertNotIn(self.marker_a, b)


# =====================================================================
# Phase D：Audit 失败路径兜底 + 脱敏
# =====================================================================

class AuditBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="audit_fail_"))
        self.manager = TaskManager(self.tmp / "agent.db")
        self.runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        self.runtime._initialized = True
        self.runtime._tools_patched = True
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self._orig_execute = main_module.execute_turn
        self._saved_router = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "off"

    def tearDown(self) -> None:
        main_module.execute_turn = self._orig_execute
        if self._saved_router is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._saved_router

    def test_failed_run_keeps_tool_and_model_trace(self) -> None:
        """P1-D：真实执行后失败 → tool_calls/model_calls 都可追踪。"""

        async def fake(mode, message, session=None, debug=False, max_turns=20,
                       history_limit=None, agent=None, audit=None, stream_events_cb=None):
            self.runtime._run_ledger.append(
                {"name": "write_code_file", "args": '{"filename": "a.py"}',
                 "status": "executed", "output_head": "已写入"})
            self.runtime._run_ledger.append(
                {"name": "run_python", "args": '{"filename": "a.py"}',
                 "status": "error", "output_head": "SyntaxError"})
            raise RuntimeError("boom after partial work")

        main_module.execute_turn = fake
        result = asyncio.run(self.runtime.run_turn(
            "写文件并运行", session_id="unit", mode="sync"))
        self.assertFalse(result.ok)
        tools_rows = self.manager.list_tool_calls(result.task.id)
        names = [t["tool_name"] for t in tools_rows]
        self.assertIn("write_code_file", names)
        self.assertIn("run_python", names)
        model_rows = self.manager.list_model_calls(result.task.id)
        self.assertGreaterEqual(len(model_rows), 1)
        self.assertEqual(model_rows[0]["status"], "attempted")


class RedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="redact_"))
        self.manager = TaskManager(self.tmp / "agent.db")
        self.task = self.manager.create_task("s1", "x")

    def test_tool_arguments_and_excerpt_redacted(self) -> None:
        row_id = self.manager.insert_tool_call(
            task_id=self.task.id,
            tool_name="write_project_file",
            arguments={"path": "x.py", "content": "OPENAI_API_KEY=sk-1234567890abcdefghij"},
            status="succeeded",
            result_excerpt="got sk-1234567890abcdefghij and tvly-secret1234567890",
        )
        rows = self.manager.list_tool_calls(self.task.id)
        text = json.dumps(rows[0], ensure_ascii=False)
        self.assertNotIn("sk-1234567890abcdefghij", text)
        self.assertNotIn("tvly-secret1234567890", text)
        self.assertIn("***", text)

    def test_approval_arguments_redacted(self) -> None:
        from runtime.audit import redact_value

        self.manager.create_approval(self.task.id, "run_python", "k1",
                                     {"code": "x", "api_key": "sk-secretvalue1234567890abcdef"})
        rows = self.manager.list_approvals(task_id=self.task.id, limit=10)
        text = json.dumps(rows[0], ensure_ascii=False)
        self.assertNotIn("sk-secretvalue1234567890abcdef", text)


if __name__ == "__main__":
    unittest.main()
