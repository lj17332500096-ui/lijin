# -*- coding: utf-8 -*-
"""P1-1 Verification 结果语义（Phase 1）：运行过 ≠ 通过。

T1 exit0+“测试通过”→ allow
T2 exit1+“测试通过”→ reject
T3 exit1+如实说明失败（无成功声明、无验证意图）→ 不误判为虚假
T4 用户意图“修复并确保测试通过”+ 实际失败 → 不得 completed-as-success
T5 失败→修复→再验证通过 → completed
T6 声称文件生成但无产物 → reject
T7 文件真实存在 → allow
T8 0-tool QA → completed
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
from runtime.approval import ApprovalGate
from runtime.completion import (
    CompletionGate,
    ExecutionEvidence,
    GateVerdict,
    TOOL_EXECUTED,
    verification_outcome_of,
)
from runtime.runner import AgentRuntime, RunResult
from runtime.task import TaskState
from runtime.task_manager import TaskManager


def reply(kind="answer", content="", summary="", questions=None):
    return {"kind": kind, "summary": summary, "content": content,
            "questions": questions or [], "saved_file": None,
            "next_step": None, "ui": []}


def run_call(text, status=TOOL_EXECUTED):
    return {"name": "run_python", "status": status, "args": "{}", "output_head": text}


def write_call(text="已写入 x.py"):
    return {"name": "write_code_file", "status": TOOL_EXECUTED,
            "args": '{"filename": "x.py"}', "output_head": text}


EVID_OK = "退出码: 0 ｜ 用时: 0.1s\n[stdout]\n[0, 1, 1, 2, 3, 5]"
EVID_FAIL = "退出码: 1 ｜ 用时: 0.1s\n[stdout]\nAssertionError: expected 5 got 4"
EVID_LOOP_OK = "【循环结果】✅ 通过（1/1 次尝试，用时 0.0s）\n验证结果：退出码: 0 ｜ 用时: 0.1s"
EVID_LOOP_FAIL = "【循环结果】未通过（3/3 次尝试）\n做了什么：连续 3 次仍未通过。\n退出码: 1"


class OutcomeParserTests(unittest.TestCase):
    def test_exit0_is_success(self):
        self.assertEqual(verification_outcome_of(run_call(EVID_OK)), "success")

    def test_exit1_is_failed(self):
        self.assertEqual(verification_outcome_of(run_call(EVID_FAIL)), "failed")

    def test_code_loop_pass_and_giveup(self):
        self.assertEqual(verification_outcome_of(run_call(EVID_LOOP_OK)), "success")
        self.assertEqual(verification_outcome_of(run_call(EVID_LOOP_FAIL)), "failed")

    def test_blocked_or_unknown_is_not_success(self):
        self.assertNotEqual(verification_outcome_of(run_call("【需要审批】", status="blocked")), "success")
        self.assertEqual(verification_outcome_of(run_call("")), "unknown")


class VerificationGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = CompletionGate()

    # T1
    def test_claim_pass_with_exit0_allowed(self):
        ev = ExecutionEvidence(tool_calls=[write_call(), run_call(EVID_OK)])
        self.assertEqual(self.gate.evaluate(reply(content="修复完成，测试通过。"), ev),
                         GateVerdict.PASS)

    # T2
    def test_claim_pass_with_exit1_rejected(self):
        ev = ExecutionEvidence(tool_calls=[write_call(), run_call(EVID_FAIL)])
        self.assertEqual(self.gate.evaluate(reply(content="测试已经通过。"), ev),
                         GateVerdict.CLAIM_UNSUPPORTED)

    # T3：如实报告失败 + 无验证意图 → 不是虚假声明（write 证据存在则按会话型通过）
    def test_honest_failure_without_intent_not_penalized(self):
        ev = ExecutionEvidence(tool_calls=[write_call(), run_call(EVID_FAIL)])
        text = "修改已经完成，但测试仍然失败（2 项断言未过），我先把问题列出。"
        self.assertEqual(self.gate.evaluate(reply(content=text), ev), GateVerdict.PASS)

    # T4：同样内容但用户明确“确保测试通过”→ 不得 success 收尾
    def test_intent_require_pass_with_failure_blocked(self):
        ev = ExecutionEvidence(tool_calls=[write_call(), run_call(EVID_FAIL)])
        text = "修改已经完成，但测试仍然失败。"
        self.assertEqual(
            self.gate.evaluate(reply(content=text), ev,
                               request_text="帮我修复这个 bug 并确保测试通过"),
            GateVerdict.VERIFICATION_FAILED)

    # T5：失败 →（repair 语义）修复 → exit0 → 通过
    def test_fail_then_repair_then_pass(self):
        ev1 = ExecutionEvidence(tool_calls=[write_call(), run_call(EVID_FAIL)])
        self.assertEqual(self.gate.evaluate(reply(content="测试通过。"), ev1,
                                            request_text="修复并验证"),
                         GateVerdict.CLAIM_UNSUPPORTED)
        ev2 = ExecutionEvidence(tool_calls=[write_call("v1"), run_call(EVID_FAIL),
                                            write_call("v2"), run_call(EVID_OK)])
        self.assertEqual(self.gate.evaluate(reply(content="已修复，测试通过。"), ev2,
                                            request_text="修复并验证"),
                         GateVerdict.PASS)

    # T6 / T7
    def test_artifact_claim_needs_real_file(self):
        ev_empty = ExecutionEvidence(tool_calls=[])
        self.assertEqual(self.gate.evaluate(reply(content="报告已经生成。"), ev_empty),
                         GateVerdict.CLAIM_UNSUPPORTED)
        ev_real = ExecutionEvidence(tool_calls=[], new_files=["notes/2026_report.md"])
        self.assertEqual(self.gate.evaluate(reply(content="报告已经生成。"), ev_real),
                         GateVerdict.PASS)

    # T8
    def test_plain_qa_still_passes(self):
        self.assertEqual(self.gate.evaluate(reply(content="2"), ExecutionEvidence()),
                         GateVerdict.PASS)


class VerificationRunTurnTests(unittest.TestCase):
    """run_turn 层：验证失败 + 明确意图 → 绝不 completed-as-success（≤1 repair 后 failed）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verify_rt_"))
        self.manager = TaskManager(str(self.tmp / "agent.db"))
        self.runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        self.runtime._initialized = True
        self.runtime._tools_patched = True
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self._orig = main_module.execute_turn
        self._router = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "off"

    def tearDown(self):
        main_module.execute_turn = self._orig
        if self._router is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._router

    def _install(self, content):
        async def fake(mode, message, session=None, debug=False, max_turns=20,
                       history_limit=None, agent=None, audit=None, stream_events_cb=None):
            self.runtime._run_ledger.append(write_call())
            self.runtime._run_ledger.append(run_call(EVID_FAIL))
            return json.dumps(reply(content=content), ensure_ascii=False)

        main_module.execute_turn = fake

    def test_verify_intent_with_failing_run_never_completes(self):
        self._install("修改已经完成，但测试仍然失败。")
        result = asyncio.run(self.runtime.run_turn(
            "帮我修复这个 bug 并确保测试通过", session_id="unit", mode="sync"))
        self.assertFalse(result.ok)
        self.assertEqual(result.task.state, TaskState.FAILED)
        self.assertIn("这次的验证没有通过", result.error)
        types = [getattr(e, "event_type") for e in self.manager.list_events(result.task.id)]
        self.assertIn("completion.check.rejected", types)
        self.assertNotIn("completion.check.passed", types)


if __name__ == "__main__":
    unittest.main()
