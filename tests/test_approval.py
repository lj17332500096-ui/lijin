import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import code_exec as code_exec_mod
from agent import assistant_agent
from runtime.approval import ApprovalGate
from runtime.errors import AgentError, ApprovalRequired
from runtime.runner import AgentRuntime
from runtime.task_manager import TaskManager


def asyncio_call(coro):
    return asyncio.run(coro)


def call_with_approval_check(gate, tool_name, args, task_id):
    """Helper: call gate.check, catching ApprovalRequired and returning its message."""
    try:
        result = asyncio_call(gate.check(tool_name, args, run_id=task_id))
        return result  # None = allowed, string = blocked (pending/denied)
    except ApprovalRequired as exc:
        return str(exc)  # Immediate suspension — return the message for test assertions


class ApprovalManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="approval_"))
        self.manager = TaskManager(self._tmp / "agent.db")

    def test_create_find_decide(self) -> None:
        task = self.manager.create_task("s1", "跑代码")
        row_id = self.manager.create_approval(task.id, "run_python", "key1", {"code": "print(1)"})
        found = self.manager.find_approval(task.id, "run_python", "key1")
        self.assertEqual(found["status"], "pending")
        self.assertEqual(found["arguments"]["code"], "print(1)")
        pending = self.manager.list_pending_approvals(task.id)
        self.assertEqual([p["id"] for p in pending], [row_id])
        self.manager.decide_approval(row_id, "approved", actor="user")
        self.assertEqual(self.manager.find_approval(task.id, "run_python", "key1")["status"], "approved")
        self.assertEqual(self.manager.list_pending_approvals(task.id), [])
        with self.assertRaises(AgentError):
            self.manager.decide_approval(row_id, "denied", actor="user")
        types = [e.event_type for e in self.manager.list_events(task.id)]
        self.assertIn("task.approval.pending", types)
        self.assertIn("task.approval.approved", types)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="approval_gate_"))
        self.manager = TaskManager(self._tmp / "agent.db")
        self.gate = ApprovalGate(self.manager)
        self._saved = os.environ.get("APPROVAL")
        self._saved_list = os.environ.get("APPROVAL_GATED_TOOLS")
        os.environ["APPROVAL"] = "on"

    def tearDown(self) -> None:
        for name, value in (("APPROVAL", self._saved), ("APPROVAL_GATED_TOOLS", self._saved_list)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_chat_channel_creates_pending_and_blocks(self) -> None:
        task = self.manager.create_task("s1", "x")
        self.gate.begin(task.id, channel="chat")
        decision = call_with_approval_check(self.gate, "run_python", {"code": "print(1)"}, task.id)
        self.assertIsNotNone(decision)
        self.assertIn("需要审批", decision)
        self.assertEqual(len(self.gate.pending_this_run), 1)
        self.gate.end()
        self.assertEqual(len(self.manager.list_pending_approvals(task.id)), 1)

    def test_scheduled_channel_auto_denies(self) -> None:
        task = self.manager.create_task("s1", "x")
        self.gate.begin(task.id, channel="scheduled")
        decision = asyncio_call(self.gate.check("forget_memory", {"entry_id": "mem_x"}))
        self.assertIsNotNone(decision)
        self.assertIn("拒绝", decision)
        self.assertEqual(len(self.gate.denied_this_run), 1)
        self.gate.end()

    def test_approved_allows_denied_blocks(self) -> None:
        task = self.manager.create_task("s1", "x")
        self.gate.begin(task.id, channel="chat")
        call_with_approval_check(self.gate, "schedule_remove", {"task_id": "task_a"}, task.id)
        approval_id = self.gate.pending_this_run[0]
        self.manager.decide_approval(approval_id, "approved", actor="user")
        self.assertIsNone(asyncio_call(self.gate.check("schedule_remove", {"task_id": "task_a"}, run_id=task.id)))

        call_with_approval_check(self.gate, "forget_memory", {"entry_id": "x"}, task.id)
        deny_id = self.gate.pending_this_run[-1]
        self.manager.decide_approval(deny_id, "denied", actor="user")
        text = asyncio_call(self.gate.check("forget_memory", {"entry_id": "x"}, run_id=task.id))
        self.assertIn("拒绝", text)

    def test_unknown_tool_not_gated(self) -> None:
        task = self.manager.create_task("s1", "x")
        self.gate.begin(task.id, channel="chat")
        self.assertIsNone(asyncio_call(self.gate.check("calculate", {"expression": "1+1"})))
        self.gate.end()

    def test_code_loop_and_rollback_are_gated_by_default(self) -> None:
        """P1-A：code_loop / sandbox_rollback 默认在审批门内（不再可绕过）。"""
        for name, args in (("code_loop", {"project": "p", "filename": "a.py"}),
                           ("sandbox_rollback", {"project": "p", "snapshot_id": "snap_x"})):
            task = self.manager.create_task("s1", name)
            self.gate.begin(task.id, channel="chat")
            decision = call_with_approval_check(self.gate, name, args, task.id)
            self.assertIsNotNone(decision)
            self.assertIn("需要审批", decision)
            self.assertEqual(len(self.gate.pending_this_run), 1)
            self.assertEqual(self.manager.list_pending_approvals(task.id)[0]["tool_name"], name)
            self.gate.end()

    def test_all_semantics_covers_risk_ops_and_real_file_edits(self) -> None:
        """P1-A：APPROVAL_GATED_TOOLS=all 不再是"默认 3 件套"。"""
        saved = os.environ.get("APPROVAL_GATED_TOOLS")
        os.environ["APPROVAL_GATED_TOOLS"] = "all"
        task = self.manager.create_task("s1", "x")
        try:
            self.gate.begin(task.id, channel="chat")
            # 执行 / 破坏恢复 / 真实文件编辑都在门内
            for name, args in (("code_loop", {"project": "p", "filename": "a.py"}),
                               ("run_python", {"code": "print(1)"}),
                               ("sandbox_rollback", {"project": "p"}),
                               ("forget_memory", {"entry_id": "m1"}),
                               ("write_project_file", {"path": "x.md", "content": "x"}),
                               ("edit_project_file", {"path": "x.md", "old_string": "a", "new_string": "b"})):
                decision = call_with_approval_check(self.gate, name, args, task.id)
                self.assertIsNotNone(decision, "%s 应在 all 语义下被门拦" % name)
            # SAFE 只读工具仍然放行
            self.assertIsNone(asyncio_call(self.gate.check("calculate", {"expression": "1+1"}, run_id=task.id)))
            self.assertIsNone(asyncio_call(self.gate.check("read_workspace_file", {"path": "x"}, run_id=task.id)))
            self.gate.end()
        finally:
            if saved is None:
                os.environ.pop("APPROVAL_GATED_TOOLS", None)
            else:
                os.environ["APPROVAL_GATED_TOOLS"] = saved


class GatedToolWrapperTests(unittest.TestCase):
    """真实链：run_python 被门拦住 → 批准 → 放行并真正执行。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="approval_wrap_"))
        self._orig_sandbox = code_exec_mod.SANDBOX_ROOT
        code_exec_mod.SANDBOX_ROOT = self._tmp
        self._orig_allow = os.environ.get("ALLOW_CODE_EXEC")
        os.environ["ALLOW_CODE_EXEC"] = "true"
        self._saved_approval = os.environ.get("APPROVAL")
        os.environ["APPROVAL"] = "on"
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
        names = {t.name for t in assistant_agent.tools}
        self.assertIn("run_python", names)

    def tearDown(self) -> None:
        code_exec_mod.SANDBOX_ROOT = self._orig_sandbox
        if self._orig_allow is None:
            os.environ.pop("ALLOW_CODE_EXEC", None)
        else:
            os.environ["ALLOW_CODE_EXEC"] = self._orig_allow
        if self._saved_approval is None:
            os.environ.pop("APPROVAL", None)
        else:
            os.environ["APPROVAL"] = self._saved_approval

    def test_blocked_then_approved_executes(self) -> None:
        tool = next(t for t in assistant_agent.tools if t.name == "run_python")
        task = self.runtime.tasks.create_task("s1", "x")
        gate = self.runtime.approval
        gate.begin(task.id, channel="chat")
        from agents.tool_context import ToolContext

        input_json = '{"project": "demo", "code": "print(\'APPROVAL_OK\')"}'
        ctx = ToolContext(context=None, tool_name="run_python", tool_call_id="t", tool_arguments=input_json)
        # The wrapped tool raises ApprovalRequired (immediate suspension)
        with self.assertRaises(ApprovalRequired):
            blocked = tool.on_invoke_tool(ctx, input_json)
            if asyncio.iscoroutine(blocked):
                asyncio.run(blocked)
        approval_id = gate.pending_this_run[0]
        gate.end()

        self.runtime.tasks.decide_approval(approval_id, "approved", actor="user")
        gate.begin(task.id, channel="chat")
        out = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(out):
            out = asyncio.run(out)
        self.assertIn("APPROVAL_OK", str(out))
        gate.end()

    def test_code_loop_cannot_bypass_approval(self) -> None:
        """P1-A：code_loop 第一次调用被拦（副作用零）、批准后才运行内部代码。"""
        project_dir = self._tmp / "demo"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "hello.py").write_text("print('CODE_LOOP_RAN_OK')", encoding="utf-8")
        code_loop_tool = next(t for t in assistant_agent.tools if t.name == "code_loop")

        task = self.runtime.tasks.create_task("s1", "x")
        gate = self.runtime.approval
        from agents.tool_context import ToolContext

        input_json = '{"project": "demo", "filename": "hello.py", "max_attempts": 1}'
        ctx = ToolContext(context=None, tool_name="code_loop", tool_call_id="t", tool_arguments=input_json)

        gate.begin(task.id, channel="chat")
        # Raised immediately — tool loop breaks
        with self.assertRaises(ApprovalRequired):
            blocked = code_loop_tool.on_invoke_tool(ctx, input_json)
            if asyncio.iscoroutine(blocked):
                asyncio.run(blocked)
        self.assertTrue(gate.pending_this_run)
        gate.end()

        approval_id = self.runtime.tasks.list_pending_approvals(task.id)[0]["id"]
        self.runtime.tasks.decide_approval(approval_id, "approved", actor="user")
        gate.begin(task.id, channel="chat")
        out = code_loop_tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(out):
            out = asyncio.run(out)
        self.assertIn("CODE_LOOP_RAN_OK", str(out))  # 批准后内部 run_python_impl 才真正执行
        gate.end()

    def test_sandbox_rollback_gated_before_effect(self) -> None:
        """P1-A：sandbox_rollback 默认需要审批（策略一致性：执行要批、恢复也要批）。"""
        tool = next(t for t in assistant_agent.tools if t.name == "sandbox_rollback")
        task = self.runtime.tasks.create_task("s1", "x")
        gate = self.runtime.approval
        from agents.tool_context import ToolContext

        input_json = '{"project": "demo", "dry_run": true}'
        ctx = ToolContext(context=None, tool_name="sandbox_rollback", tool_call_id="t",
                          tool_arguments=input_json)
        gate.begin(task.id, channel="chat")
        with self.assertRaises(ApprovalRequired):
            blocked = tool.on_invoke_tool(ctx, input_json)
            if asyncio.iscoroutine(blocked):
                asyncio.run(blocked)
        self.assertEqual(len(self.runtime.tasks.list_pending_approvals(task.id)), 1)
        gate.end()

        # 拒绝 → 不执行（保持 blocked 语义，且无新的执行副作用）
        approval_id = self.runtime.tasks.list_pending_approvals(task.id)[0]["id"]
        self.runtime.tasks.decide_approval(approval_id, "denied", actor="user")
        gate.begin(task.id, channel="chat")
        denied = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(denied):
            denied = asyncio.run(denied)
        self.assertIn("拒绝", str(denied))
        gate.end()


if __name__ == "__main__":
    unittest.main()
