"""Phase 18 —— Finalization Repair Semantics 回归套件（确定性，无模型）。

覆盖：obligation repair 与 completion repair 分离、deficit signature、bounded failure、
verification fail 阻止 completed、新 mutation 产生新 deficit、verification PASS → completed。
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402
from runtime.completion import TOOL_EXECUTED  # noqa: E402
from runtime.runner import AgentRuntime  # noqa: E402
from runtime.task import TaskState  # noqa: E402


def _done(content: str) -> str:
    return json.dumps({"kind": "done", "summary": "s", "content": content,
                       "questions": [], "saved_file": None, "next_step": None},
                      ensure_ascii=False)


class _Harness:
    def __init__(self, script):
        self._tmp = Path(tempfile.mkdtemp(prefix="p18_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self._orig = main_module.execute_turn
        self._saved_gate = os.environ.get("FORGE_OBLIGATION_GATE")
        self.script = script  # list of callables(runtime, rctx) -> str(final output)
        self.calls = 0

    def __enter__(self):
        rt = self.runtime

        async def fake(mode, message, **kw):
            from runtime.runctx import current
            rc = current()
            idx = min(self.calls, len(self.script) - 1)
            self.calls += 1
            return self.script[idx](rt, rc)

        main_module.execute_turn = fake
        return self

    def __exit__(self, *exc):
        main_module.execute_turn = self._orig
        if self._saved_gate is None:
            os.environ.pop("FORGE_OBLIGATION_GATE", None)
        else:
            os.environ["FORGE_OBLIGATION_GATE"] = self._saved_gate

    def run(self, msg, **kw):
        os.environ["FORGE_OBLIGATION_GATE"] = "on"
        return asyncio.run(self.runtime.run_turn(msg, session_id="unit", mode="async", **kw))

    def events(self, task_id):
        return [e.event_type for e in self.runtime.tasks.list_events(task_id)]


def _mutate(rt, rc, path="calc.py"):
    rt._record_tool("edit_project_file", {"path": path}, TOOL_EXECUTED, "已修改")
    rc.note_progress("edit_project_file", {"path": path}, "已修改")
    rc.note_execution_identity("edit_project_file", {"path": path})
    return _done("已修改完成")


def _verify_pass(rt, rc):
    out = "pytest 退出码: 0 ｜ 用时: 0.5s ｜ PASS\n2 passed"
    rt._record_tool("run_tests", {"project": "micro_fixture"}, TOOL_EXECUTED, out)
    rc.note_progress("run_tests", {"project": "micro_fixture"}, out)
    return _done("已验证通过")


def _verify_fail(rt, rc):
    out = "pytest 退出码: 1 ｜ 用时: 0.5s ｜ FAIL\n1 failed"
    rt._record_tool("run_tests", {"project": "micro_fixture"}, TOOL_EXECUTED, out)
    rc.note_progress("run_tests", {"project": "micro_fixture"}, out)
    return _done("测试未通过")


MSG = "修复 calc.py 的 add 函数并运行测试确认通过。"


class RepairSemanticsTests(unittest.TestCase):
    def test_obligation_block_then_verification_completes(self):
        with _Harness([_mutate, _verify_pass]) as h:
            r = h.run(MSG)
            self.assertEqual(r.task.state, TaskState.COMPLETED)
            evs = h.events(r.task.id)
            self.assertIn("completion.obligation_blocked", evs)
            self.assertNotIn("repair_exhausted", [e for e in evs])

    def test_repeated_same_deficit_bounded_failure(self):
        # 首次 mutation；之后只 final（deficit signature 保持不变）→ 第二次同 deficit 有界失败
        def final_only(rt, rc):
            return _done("已修改完成")

        with _Harness([_mutate, final_only]) as h:
            r = h.run(MSG)
            self.assertEqual(r.task.state, TaskState.FAILED)
            events = h.runtime.tasks.list_events(r.task.id)
            reasons = [str((e.payload or {}).get("reason")) for e in events
                       if e.event_type == "completion.check.rejected"]
            self.assertTrue(any("missing_required_verification" in x for x in reasons),
                            reasons)
            blocked = [e for e in events if e.event_type == "completion.obligation_blocked"]
            self.assertTrue(blocked)

    def test_verification_fail_never_completed(self):
        with _Harness([_mutate, _verify_fail]) as h:
            r = h.run(MSG)
            self.assertNotEqual(r.task.state, TaskState.COMPLETED)

    def test_new_mutation_new_deficit_then_pass_completes(self):
        def mutate_twice(rt, rc):
            rt._record_tool("edit_project_file", {"path": "a.py"}, TOOL_EXECUTED, "已修改 a.py")
            rc.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
            rc.note_execution_identity("edit_project_file", {"path": "a.py"})  # epoch1
            rt._record_tool("edit_project_file", {"path": "b.py"}, TOOL_EXECUTED, "已修改 b.py")
            rc.note_progress("edit_project_file", {"path": "b.py"}, "已修改 b.py")
            rc.note_execution_identity("edit_project_file", {"path": "b.py"})  # epoch2
            return _done("已修改完成")

        with _Harness([mutate_twice, _verify_pass]) as h:
            r = h.run(MSG)
            self.assertEqual(r.task.state, TaskState.COMPLETED)

    def test_control_no_verification_required(self):
        # 无 verification 义务（只修改）→ 正常 completed
        def just_mutate(rt, rc):
            rt._record_tool("edit_project_file", {"path": "a.py"}, TOOL_EXECUTED, "已修改 a.py")
            rc.note_progress("edit_project_file", {"path": "a.py"}, "已修改 a.py")
            rc.note_execution_identity("edit_project_file", {"path": "a.py"})
            return _done("已修改完成")

        with _Harness([just_mutate]) as h:
            r = h.run("把 a.py 的变量名改一下。")
            self.assertEqual(r.task.state, TaskState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
