"""Phase 12 —— 三态 Obligation Detection（M1–M8）+ Verification Due（确定性，无需模型）。"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.completion import extract_obligations  # noqa: E402
from runtime.runctx import RunContext  # noqa: E402

# M1–M8 prompts（与 microbenchmark 对齐）
M = {
    "M1": "修复 calc.py 中 add 函数结果错误的问题，并运行测试确认通过。",
    "M2": "修改 a.py 与 b.py 的返回值，并跑测试确认。",
    "M3": "修复 bug 并运行测试；如果测试失败，继续修复直到通过。",
    "M4": "修改配置后验证修改是否生效。",
    "M5": "先找到相关文件，修改后运行测试确认。",
    "M6": "只阅读 calc.py，解释 add 函数，不要修改任何文件。",
    "M7": "修改 calc.py 的 add 函数，让它返回两数之和。",
    "M8": "修改测试说明文档的措辞，让它更清晰。",
}


class ObligationDetectionTests(unittest.TestCase):
    def test_m1_required_required(self):
        self.assertEqual(extract_obligations(M["M1"]),
                         {"mutation": "required", "verification": "required"})

    def test_m2_required_required(self):
        self.assertEqual(extract_obligations(M["M2"]),
                         {"mutation": "required", "verification": "required"})

    def test_m3_required_required(self):
        self.assertEqual(extract_obligations(M["M3"])["verification"], "required")

    def test_m4_verify_action(self):
        self.assertEqual(extract_obligations(M["M4"])["verification"], "required")

    def test_m5_required_required(self):
        self.assertEqual(extract_obligations(M["M5"]),
                         {"mutation": "required", "verification": "required"})

    def test_m6_read_only_no_mutation_no_verification(self):
        ob = extract_obligations(M["M6"])
        self.assertEqual(ob["mutation"], "not_required")
        self.assertNotEqual(ob["verification"], "required")

    def test_m7_mutation_without_verification(self):
        ob = extract_obligations(M["M7"])
        self.assertEqual(ob["mutation"], "required")
        self.assertNotEqual(ob["verification"], "required")

    def test_m8_keyword_false_positive_not_required(self):
        # 含“测试”一词，但语义是“修改测试说明文档”，不得要求执行验证
        ob = extract_obligations(M["M8"])
        self.assertNotEqual(ob["verification"], "required")


class VerificationDueTests(unittest.TestCase):
    def test_due_after_mutation_when_required(self):
        rc = RunContext(run_id="r1", request_text=M["M1"])
        self.assertFalse(rc.verification_due())  # 尚未 mutation
        rc.note_progress("edit_project_file", {"path": "calc.py"}, "已修改 calc.py")
        rc.note_execution_identity("edit_project_file", {"path": "calc.py"})
        self.assertTrue(rc.verification_due())
        self.assertIn("verification", rc.obligation_deficits())

    def test_not_due_when_verification_not_required(self):
        rc = RunContext(run_id="r1", request_text=M["M7"])
        rc.note_progress("edit_project_file", {"path": "calc.py"}, "已修改 calc.py")
        rc.note_execution_identity("edit_project_file", {"path": "calc.py"})
        self.assertFalse(rc.verification_due())

    def test_due_cleared_by_latest_verification(self):
        rc = RunContext(run_id="r1", request_text=M["M1"])
        rc.note_progress("edit_project_file", {"path": "calc.py"}, "已修改 calc.py")
        rc.note_execution_identity("edit_project_file", {"path": "calc.py"})  # epoch 1
        rc.note_progress("run_python", {"filename": "test_calc.py"}, "退出码: 0")  # verified=1
        self.assertFalse(rc.verification_due())
        # 新 mutation → 旧验证失效
        rc.note_progress("edit_project_file", {"path": "calc.py"}, "edited2")
        rc.note_execution_identity("edit_project_file", {"path": "calc.py"})  # epoch 2
        self.assertTrue(rc.verification_due())

    def test_obligation_ledger_shape(self):
        rc = RunContext(run_id="r1", request_text=M["M1"])
        ledger = rc.obligation_ledger()
        self.assertEqual(ledger["mutation"]["status"], "required")
        self.assertEqual(ledger["verification"]["status"], "required")
        self.assertEqual(ledger["verification"]["satisfied"], "unsatisfied")


class CompletionObligationGateTests(unittest.TestCase):
    """通过 run_turn 验证：verification 义务未满足时，不得 completed。"""

    def setUp(self):
        import main as main_module
        import tempfile
        from runtime.runner import AgentRuntime

        self._main = main_module
        self._orig = main_module.execute_turn
        self._tmp = tempfile.mkdtemp(prefix="p12_gate_")
        self.runtime = AgentRuntime(db_path=str(Path(self._tmp) / "agent.db"))
        self._saved = os.environ.get("FORGE_OBLIGATION_GATE")
        os.environ["FORGE_OBLIGATION_GATE"] = "on"

    def tearDown(self):
        self._main.execute_turn = self._orig
        if self._saved is None:
            os.environ.pop("FORGE_OBLIGATION_GATE", None)
        else:
            os.environ["FORGE_OBLIGATION_GATE"] = self._saved

    def test_missing_verification_blocks_completion(self):
        import asyncio
        import json as _json

        async def fake(mode, message, **kw):
            return _json.dumps({"kind": "done", "summary": "done", "content": "已完成",
                                "questions": [], "saved_file": None, "next_step": None},
                               ensure_ascii=False)

        self._main.execute_turn = fake
        result = asyncio.run(self.runtime.run_turn(
            "修复 calc.py 的 add 函数并运行测试确认通过。", session_id="unit", mode="async"))
        # 未满足 mutation+verification 义务 → 不得 completed
        self.assertNotEqual(result.task.state.value, "completed")
        events = [e.event_type for e in self.runtime.tasks.list_events(result.task.id)]
        self.assertIn("completion.obligation_blocked", events)


if __name__ == "__main__":
    unittest.main()
