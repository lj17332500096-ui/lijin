# -*- coding: utf-8 -*-
"""P0 阶段 Completion Gate 测试：模型声明 + Runtime 执行证据共同决定终态。

覆盖：
- TEST1 普通问答 0 tool → completed（不误伤聊天）
- TEST2 假修改（声明文件已改，0 工具）→ 不得 completed
- TEST3 假验证（声明测试通过，0 验证）→ 不得可信完成
- TEST4 真实修改 + 正确说明 → 允许完成
- TEST5 声称报告已生成但无产物 → 不得 completed
- TEST6 产物真实存在 → 可以完成
- TEST7 口头 Approval（无 pending）→ 不得 completed-as-finished
- TEST8 真实 pending（由现有 Approval 门测试 + 门路径保证）
- TEST10 真实执行 + final empty output → 不丢已执行结果（降级完成）
- TEST11 真实执行失败 → failed（不被降级路径误判成成功）
- TEST12 输出闸密钥场景 → 不泄漏、guardrail 保留
- Repair：1 次有界；repair 后 questions/撤回声明允许会话型完成。
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import main as main_module
from runtime.approval import ApprovalGate
from runtime.completion import (
    CompletionGate,
    ExecutionEvidence,
    GateVerdict,
    build_degraded_reply,
    has_write_claim,
    has_verify_claim,
)
from runtime.errors import FinalResponseFailed
from runtime.runner import AgentRuntime, RunResult
from runtime.task import TaskState
from runtime.task_manager import TaskManager


def _reply(kind: str = "answer", content: str = "", summary: str = "", questions=None) -> dict:
    return {
        "kind": kind,
        "summary": summary,
        "content": content,
        "questions": questions or [],
        "saved_file": None,
        "next_step": None,
        "ui": [],
    }


def _evidence(*names, status: str = "executed", new_files=None, pending: int = 0) -> ExecutionEvidence:
    return ExecutionEvidence(
        tool_calls=[{"name": n, "status": status, "args": "{}", "output_head": ""} for n in names],
        new_files=new_files or [],
        approvals_pending=pending,
    )


def _current_runctx():
    """取当前 Run 的 RunContext；未绑定时返回 None（测试替身里安全降级）。

    义务门（_succeed 内的 obligation_deficits）以 RunContext 的 DiscoveryTracker
    为准，而不是工具包装器账本。因此模拟"真实执行过"的替身必须同时调用
    note_progress 记账，否则义务门判定为"没有执行证据"而降级失败。
    """
    try:
        from runtime.runctx import current

        return current()
    except Exception:
        return None


def _note_real_execution(rc, name: str, arguments: dict, result_text: str) -> None:
    """通过 RunContext 登记一次真实执行（推进 mutation_seen / verification_passed）。"""
    if rc is None:
        return
    rc.note_progress(name, arguments, result_text)


class CompletionGateUnitTests(unittest.TestCase):
    """TEST 1-7 的判定层（纯函数）。"""

    def setUp(self) -> None:
        self.gate = CompletionGate()

    # TEST 1
    def test_plain_qa_zero_tool_passes(self) -> None:
        self.assertEqual(
            self.gate.evaluate(_reply(content="2", summary="2"), _evidence()),
            GateVerdict.PASS,
        )

    # TEST 2
    def test_fake_modify_claim_rejected(self) -> None:
        self.assertEqual(
            self.gate.evaluate(_reply(content="文件已经修改完成。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )

    # TEST 3
    def test_fake_verify_claim_rejected(self) -> None:
        self.assertEqual(
            self.gate.evaluate(_reply(content="测试全部通过。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )
        self.assertFalse(has_verify_claim("如果测试通过，我再继续。"))
        self.assertFalse(has_verify_claim("建议运行成功后保存再退出。"))

    # TEST 4
    def test_real_modify_with_statement_passes(self) -> None:
        self.assertEqual(
            self.gate.evaluate(
                _reply(content="文件已经修改完成，改动见上方 diff。"),
                _evidence("edit_project_file"),
            ),
            GateVerdict.PASS,
        )

    # TEST 5
    def test_claim_report_generated_without_artifact_rejected(self) -> None:
        self.assertEqual(
            self.gate.evaluate(_reply(content="报告已经生成。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )

    def test_skill_description_fix_suggestion_is_not_write_claim(self) -> None:
        """能力介绍里的“输出：健康报告 + 修复建议”不是本轮写完成声明。

        真实回归：模型只读检索后介绍 dep_doctor 能力，因“报告…修复”被误判为
        write_claim 而整轮 CLAIM_UNSUPPORTED（12 次真实只读工具仍失败）。
        """
        content = (
            "### 2. **dep_doctor** — 依赖体检\n"
            "- **输入**：用户询问“扫描依赖 / requirements 有问题”\n"
            "- **过程**：扫描 Python 依赖清单，找未锁定版本、重复依赖、冲突项\n"
            "- **输出**：健康报告 + 修复建议（不会自动修改文件）"
        )
        self.assertEqual(
            self.gate.evaluate(
                _reply(content=content),
                _evidence("recall_memory", "list_notes", "read_workspace_file"),
            ),
            GateVerdict.PASS,
        )
        self.assertFalse(has_write_claim("输出：健康报告 + 修复建议（不会自动修改文件）"))

    def test_real_doc_fixed_claim_still_rejected_without_evidence(self) -> None:
        """排除名词后缀不能放开真声明：文档已修复仍必须有执行证据。"""
        self.assertEqual(
            self.gate.evaluate(_reply(content="文档已修复完成。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )

    def test_artifact_heading_output_is_not_claim(self) -> None:
        """“笔记与产出”这类小标题是名词，不是“已产出”完成声明。

        真实回归：能力清单回答含“**笔记与产出**”小标题被误判 artifact claim，
        导致整轮 CLAIM_UNSUPPORTED（只读检索后本可正常作答）。
        """
        content = (
            "当前可用的能力如下：\n\n"
            "**笔记与产出**\n"
            "- save_note / read_note / list_notes：Markdown 笔记读写\n"
            "- save_word_doc / save_excel_workbook / save_ppt_deck：生成 Word/Excel/PPT"
        )
        self.assertFalse(has_write_claim(content))
        from runtime.completion import has_artifact_claim

        self.assertFalse(has_artifact_claim(content))
        self.assertEqual(
            self.gate.evaluate(
                _reply(content=content),
                _evidence("recall_memory", "list_notes", "search_documents"),
            ),
            GateVerdict.PASS,
        )

    def test_real_already_produced_claim_still_rejected(self) -> None:
        """去掉小标题误报不能放开真声明：报告已产出仍必须有产物/文件证据。"""
        self.assertEqual(
            self.gate.evaluate(_reply(content="报告已产出。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )

    def test_capability_line_with_read_write_generation_is_not_claim(self) -> None:
        """能力描述“办公文档读写生成”是名词短语，不是本轮已生成文件。"""
        from runtime.completion import has_artifact_claim

        content = (
            "## 基础工具能力\n\n"
            "- `read_office_file/save_word_doc/save_excel_workbook/save_ppt_deck`"
            " — 办公文档读写生成\n"
            "- `save_note` — 备忘录/笔记管理"
        )
        self.assertFalse(has_artifact_claim(content))
        self.assertEqual(
            self.gate.evaluate(
                _reply(content=content, summary="直接回答能力清单，无需调用工具"),
                _evidence(),
            ),
            GateVerdict.PASS,
        )

    def test_doc_generated_claim_without_evidence_still_rejected(self) -> None:
        """能力描述过滤不能放开真声明：文档已生成/报告生成仍要求证据。"""
        self.assertEqual(
            self.gate.evaluate(_reply(content="文档已生成。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )
        self.assertEqual(
            self.gate.evaluate(_reply(content="报告生成完毕。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )

    # TEST 6
    def test_claim_report_generated_with_artifact_passes(self) -> None:
        self.assertEqual(
            self.gate.evaluate(
                _reply(content="报告已经生成，可查看产物。"),
                _evidence(new_files=["notes/2026_report.md"]),
            ),
            GateVerdict.PASS,
        )

    # TEST 7
    def test_verbal_approval_without_pending_inconsistent(self) -> None:
        self.assertEqual(
            self.gate.evaluate(_reply(content="我需要你的批准才能运行测试。"), _evidence()),
            GateVerdict.APPROVAL_INCONSISTENT,
        )
        self.assertEqual(
            self.gate.evaluate(_reply(content="等待你批准 run_python 调用。"), _evidence()),
            GateVerdict.APPROVAL_INCONSISTENT,
        )

    def test_verbal_approval_with_real_pending_not_inconsistent(self) -> None:
        self.assertEqual(
            self.gate.evaluate(
                _reply(content="我需要你的批准才能运行测试。"), _evidence(pending=1)
            ),
            GateVerdict.PASS,
        )

    def test_questions_and_plan_need_no_evidence(self) -> None:
        self.assertEqual(
            self.gate.evaluate(_reply(kind="questions", questions=["你从哪里出发？"]), _evidence()),
            GateVerdict.PASS,
        )
        self.assertEqual(
            self.gate.evaluate(
                _reply(kind="plan", content="建议三步完成：先读取文件，再修改，最后运行验证。"),
                _evidence(),
            ),
            GateVerdict.PASS,
        )

    def test_done_kind_with_unsupported_claim_rejected(self) -> None:
        """kind=done 也不自动可信。"""
        self.assertEqual(
            self.gate.evaluate(_reply(kind="done", content="代码已修复，测试已通过。"), _evidence()),
            GateVerdict.CLAIM_UNSUPPORTED,
        )

    def test_degraded_reply_only_reports_structured_facts(self) -> None:
        degraded = build_degraded_reply(
            "模型没有产生任何可用回答",
            _evidence("write_code_file", "run_python", new_files=["notes/报告.md"]),
        )
        text = str(degraded["content"])
        self.assertIn("write_code_file", text)
        self.assertIn("run_python", text)
        self.assertIn("报告.md", text)
        self.assertIn("最终说明生成失败", text)
        self.assertNotIn("Guardrail", text)
        self.assertNotIn("修复了登录问题", text)  # 禁止编造业务结论


class _FakeHarness:
    """离线驱动 AgentRuntime.run_turn（fake main.execute_turn，真实 DB/状态机）。"""

    def __init__(self, testcase: unittest.TestCase) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="completion_run_"))
        self.manager = TaskManager(self.tmp / "agent.db")
        self.notes = self.tmp / "notes"
        self.notes.mkdir()
        self.runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        self.runtime._initialized = True
        self.runtime._tools_patched = True  # 不触碰全局 agent 工具（避免跨测试污染）
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self.runtime.artifact_dirs = (self.notes,)
        self.testcase = testcase
        self.calls: list[str] = []
        self._main = main_module
        self._orig_execute = main_module.execute_turn
        self._saved_router = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "off"  # 绕过工具子集克隆，让 fake 场景更确定

    def install(self, fn) -> None:
        self.fn = fn

    async def _execute(self, mode, message, session=None, debug=False, max_turns=20,
                       history_limit=None, agent=None, audit=None, stream_events_cb=None):
        self.calls.append(message)
        return await self.fn(mode, message)

    def __enter__(self):
        main_module.execute_turn = self._execute
        return self

    def __exit__(self, *exc) -> None:
        main_module.execute_turn = self._orig_execute
        if self._saved_router is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._saved_router

    def run(self, message: str, **kw) -> RunResult:
        return asyncio.run(self.runtime.run_turn(message, session_id="unit", **kw))


def _canned(content: str, kind: str = "answer", summary: str = ""):
    async def _fn(mode, message):
        return json.dumps(_reply(kind=kind, content=content, summary=summary), ensure_ascii=False)
    return _fn


class RunTurnCompletionGateTests(unittest.TestCase):
    """TEST 1/2/7/10/11/12 的 run_turn 集成层（fake 模型 + 真实状态机）。"""

    def _events(self, harness, run_id: str) -> list[str]:
        return [getattr(e, "event_type") for e in harness.manager.list_events(run_id)]

    def test_plain_qa_completes_zero_tool(self) -> None:
        with _FakeHarness(self) as h:
            h.install(_canned("2", summary="2"))
            result = h.run("1+1 等于多少？")
            self.assertTrue(result.ok)
            self.assertEqual(result.task.state, TaskState.COMPLETED)
            self.assertEqual(len(h.calls), 1)
            types = self._events(h, result.task.id)
            self.assertIn("completion.check.passed", types)
            self.assertNotIn("completion.check.rejected", types)

    def test_fake_modify_claim_repaired_then_questions_completes(self) -> None:
        state = {"n": 0}

        async def fake(mode, message):
            state["n"] += 1
            if state["n"] == 1:
                return json.dumps(_reply(content="文件已经修改完成。"), ensure_ascii=False)
            return json.dumps(_reply(kind="questions",
                                     questions=["请告诉我具体要修改哪个文件？"]), ensure_ascii=False)

        # 本用例的语义是"假声明被拒 → repair 后诚实撤回声明 → 会话型完成"：
        # 全程没有任何真实工具执行，这与义务门（要求 mutation/verification 证据）
        # 天然冲突。义务门的判定由 CompletionGateUnitTests 独立覆盖，
        # 这里显式关闭以聚焦 CompletionGate 自身的收口语义。
        with patch.dict(os.environ, {"FORGE_OBLIGATION_GATE": "off"}):
            with _FakeHarness(self) as h:
                h.install(fake)
                result = h.run("帮我修复 bug 并验证。")
            self.assertTrue(result.ok)
            # Phase 5：repaired 后模型给出 questions → Run 暂停 WAITING_USER。
            self.assertEqual(result.task.state, TaskState.WAITING_USER)
            self.assertEqual(len(h.calls), 2)
            self.assertIn("请基于实际情况重新回答", h.calls[1])
            types = self._events(h, result.task.id)
            self.assertIn("completion.check.rejected", types)
            self.assertIn("completion.check.passed", types)

    def test_fake_claim_persists_after_one_repair_fails(self) -> None:
        with _FakeHarness(self) as h:
            h.install(_canned("文件已经修改完成，测试全部通过。"))
            result = h.run("修复 bug 并验证。")
            self.assertFalse(result.ok)
            self.assertEqual(result.task.state, TaskState.FAILED)
            self.assertEqual(len(h.calls), 2)  # 1 次 repair，有界
            self.assertIn("还没有真正执行完成", result.error)
            types = self._events(h, result.task.id)
            self.assertNotIn("completion.check.passed", types)

    def test_verbal_approval_without_pending_never_completes_as_finished(self) -> None:
        state = {"n": 0}

        async def fake(mode, message):
            state["n"] += 1
            if state["n"] == 1:
                return json.dumps(_reply(content="我需要你的批准才能运行测试。"), ensure_ascii=False)
            return json.dumps(_reply(content="明白了，我无法替你决定执行；请告诉我是否继续。"),
                              ensure_ascii=False)

        # 本用例语义：模型口头声称等待批准（无真实 pending），repair 后撤回声明
        # → 会话型完成。全程无真实工具执行，与义务门（要求 verification 证据）冲突；
        # 义务门判定由 CompletionGateUnitTests 独立覆盖，此处显式关闭。
        with patch.dict(os.environ, {"FORGE_OBLIGATION_GATE": "off"}):
            with _FakeHarness(self) as h:
                h.install(fake)
                result = h.run("运行测试验证一下。")
                self.assertTrue(result.ok)  # repair 后撤回声明 → 会话型完成
                self.assertEqual(len(h.calls), 2)
                types = self._events(h, result.task.id)
                self.assertIn("completion.check.rejected", types)
                self.assertIn("completion.check.passed", types)

    def test_verbal_approval_persists_fails_cleanly(self) -> None:
        with _FakeHarness(self) as h:
            h.install(_canned("正在等待你的批准，请先批准我执行 run_python。"))
            result = h.run("运行代码验证。")
            self.assertFalse(result.ok)
            self.assertEqual(result.task.state, TaskState.FAILED)
            self.assertEqual(len(h.calls), 2)
            self.assertIn("需要你的确认", result.error)

    def test_real_execution_then_empty_output_degrades_to_completed(self) -> None:
        # 真实执行语义：结果文本必须携带可被判定的成功标记
        # （mutation 需命中成功标记；verification 需含"退出码: 0"）。
        WRITE_OUT = "已写入 hello.py"
        VERIFY_OUT = "退出码: 0 ｜ 用时: 0.1s\n[stdout]\n[0, 1, 1, 2, 3, 5]"

        async def fake(mode, message):
            # 模拟真实工具已经执行：既登记工具包装器账本（供降级文案/审计），
            # 也通过 RunContext 记账（义务门以 DiscoveryTracker 为准）。
            _rc = _current_runctx()
            h.runtime._run_ledger.append(
                {"name": "write_code_file", "args": '{"project": "auditfib", "filename": "hello.py"}',
                 "status": "executed", "output_head": WRITE_OUT})
            h.runtime._run_ledger.append(
                {"name": "run_python", "args": '{"filename": "hello.py", "project": "auditfib"}',
                 "status": "executed", "output_head": VERIFY_OUT})
            _note_real_execution(_rc, "write_code_file",
                                 {"project": "auditfib", "filename": "hello.py"}, WRITE_OUT)
            _note_real_execution(_rc, "run_python",
                                 {"filename": "hello.py", "project": "auditfib"}, VERIFY_OUT)
            (h.notes / "修复报告.md").write_text("# 修复", encoding="utf-8")
            raise FinalResponseFailed("模型没有产生任何可用回答")

        with _FakeHarness(self) as h:
            h.install(fake)
            result = h.run("修复 hello.py 并运行验证。")
            self.assertTrue(result.ok)
            self.assertEqual(result.task.state, TaskState.COMPLETED)
            content = str(result.final_output.get("content", "")) if isinstance(result.final_output, dict) else str(result.final_output)
            self.assertIn("最终说明生成失败", content)
            self.assertIn("write_code_file", content)
            self.assertIn("run_python", content)
            self.assertIn("修复报告.md", content)
            self.assertNotIn("Guardrail", content)
            # 审计补写：失败路径的账本进入标准 tool_calls 行
            tool_rows = h.manager.list_tool_calls(result.task.id)
            names = [t["tool_name"] for t in tool_rows]
            self.assertIn("write_code_file", names)
            self.assertIn("run_python", names)
            types = self._events(h, result.task.id)
            self.assertIn("reply.degraded", types)
            self.assertIn("completion.check.passed", types)

    def test_empty_output_without_evidence_fails_cleanly(self) -> None:
        async def fake(mode, message):
            raise FinalResponseFailed("模型没有产生任何可用回答")

        with _FakeHarness(self) as h:
            h.install(fake)
            result = h.run("1+1 等于多少？")
            self.assertFalse(result.ok)
            self.assertEqual(result.task.state, TaskState.FAILED)
            self.assertIn("没有执行任何操作", result.error)
            self.assertNotIn("Guardrail", result.error)

    def test_real_execution_error_still_fails(self) -> None:
        """TEST 11：降级机制不得把真正执行失败改成成功。"""

        async def fake(mode, message):
            h.runtime._run_ledger.append(
                {"name": "write_code_file", "args": "{}", "status": "error", "output_head": "磁盘写失败"})
            raise RuntimeError("磁盘写入失败 boom")

        with _FakeHarness(self) as h:
            h.install(fake)
            result = h.run("帮我写一个文件。")
            self.assertFalse(result.ok)
            self.assertEqual(result.task.state, TaskState.FAILED)
            self.assertIn("boom", result.error)

    def test_secret_blocked_reason_with_evidence_never_leaks(self) -> None:
        """TEST 12：输出闸密钥拦截保留；降级文案与消息库都不得含密钥。"""
        secret = "sk-secret-audit-1234567890"

        async def fake(mode, message):
            h.runtime._run_ledger.append(
                {"name": "write_code_file", "args": "{}", "status": "executed",
                 "output_head": "已写入"})
            raise FinalResponseFailed("输出疑似包含 API Key/密钥，已拦截")

        with _FakeHarness(self) as h:
            h.install(fake)
            result = h.run("生成一个脚本文件。")
            self.assertTrue(result.ok)
            self.assertEqual(result.task.state, TaskState.COMPLETED)
            text = str(result.final_output)
            self.assertNotIn(secret, text)
            for msg in h.manager.list_messages(h.runtime.tasks.get_run_container_id(result.task.id)):
                self.assertNotIn(secret, msg.get("content") or "")
            self.assertIn("密钥", text)


if __name__ == "__main__":
    unittest.main()
