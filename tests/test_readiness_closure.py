# -*- coding: utf-8 -*-
"""Task Readiness Closure（2026-09-07-v2）专项回归。

覆盖本次最小收口修复：
- RT-GATE-01..05：Readiness → Tool Capability Gate（NEEDS_USER/DISCOVERABLE/READY
  对 edit/write/read 的确定性约束；被拦工具不得执行、不得产生执行证据）；
- Bounded Discovery：同意图 + 同参数 + 连续无新信息 → 第 3 次起阻止；READY 不限制；
- Completion Gate：能力盘点类 0 工具描述回答不误报 CLAIM_UNSUPPORTED，
  而“我已经调用 YouTube MCP 下载了字幕”仍必须被拦截；
- Clarification Precision：NEEDS_USER questions 空泛措辞被 CLARIFICATION_VAGUE 拦截，
  点名缺失项的具体问题正常 PASS；readiness.missing[] 结构化透传；
- 容器级未决澄清继承：上轮 NEEDS_USER 结束后，下一轮工具门继续继承 NEEDS_USER；
  READY/普通回答不跨 Run 继承。
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import agent as agent_module
import main as main_module
from agents.tool import FunctionTool
from runtime.approval import ApprovalGate
from runtime.errors import ApprovalRequired
from runtime.completion import (
    CompletionGate,
    ExecutionEvidence,
    GateVerdict,
)
from runtime.readiness_gate import (
    DISCOVERY_EXHAUSTED,
    DISCOVERY_SAFE,
    SIDE_EFFECTING,
    STATUS_DISCOVERABLE,
    STATUS_NEEDS_USER,
    STATUS_READY,
    check_status_tool,
    check_user_tool_intent,
    classify_tool,
    clarification_precision,
    discovery_signature,
    normalize_missing,
)
from runtime.reply_parser import parse as parse_reply
from runtime.runctx import RunContext, bind as bind_runctx
from runtime.runner import AgentRuntime
from runtime.spec import spec_for
from runtime.task import TaskState
from runtime.task_manager import TaskManager


def _reply(content="ok", kind="answer", questions=None, readiness=None):
    return {
        "kind": kind, "summary": "s", "content": content,
        "questions": questions or [], "saved_file": None,
        "next_step": None, "ui": [], "readiness": readiness,
    }


class ReadinessGateUnitTests(unittest.TestCase):
    """RT-GATE 前置：工具分类必须来自 metadata/配置，禁止按名字猜。"""

    def test_metadata_classification(self):
        self.assertEqual(classify_tool("edit_project_file", spec_for("edit_project_file")),
                         SIDE_EFFECTING)
        self.assertEqual(classify_tool("write_project_file", spec_for("write_project_file")),
                         SIDE_EFFECTING)
        self.assertEqual(classify_tool("read_workspace_file", spec_for("read_workspace_file")),
                         DISCOVERY_SAFE)
        self.assertEqual(classify_tool("web_search", spec_for("web_search")),
                         DISCOVERY_SAFE)
        self.assertEqual(classify_tool("recall_memory", spec_for("recall_memory")),
                         DISCOVERY_SAFE)
        self.assertEqual(classify_tool("run_python", spec_for("run_python")),
                         SIDE_EFFECTING)
        self.assertEqual(classify_tool("remember", spec_for("remember")),
                         SIDE_EFFECTING)

    def test_mcp_policy_metadata_reused(self):
        # 分类来自 MCP 策略 metadata（allow=只读探索 / approval=外部副作用），
        # 而不是“工具名里有没有 read/write”。
        self.assertEqual(classify_tool("youtube_get-transcript", spec_for("x"), "allow"),
                         DISCOVERY_SAFE)
        self.assertEqual(classify_tool("gitee_create_issue", spec_for("x"), "approval"),
                         SIDE_EFFECTING)

    def test_unknown_conservative_default(self):
        self.assertEqual(classify_tool("brand_new_mutation_tool", None, None),
                         SIDE_EFFECTING)

    def test_unrequested_persistent_writes_are_blocked(self):
        self.assertFalse(check_user_tool_intent("save_note", "把没用的文件删了")[0])
        self.assertFalse(check_user_tool_intent("remember", "明天天气怎么样？")[0])
        self.assertTrue(check_user_tool_intent("save_note", "帮我写一份周报并保存")[0])
        self.assertTrue(check_user_tool_intent("remember", "记住我常驻北京")[0])

    def test_run_context_bounds_project_mutations(self):
        ctx = RunContext(run_id="r")
        self.assertTrue(ctx.note_mutation("edit_project_file"))
        self.assertTrue(ctx.note_mutation("edit_project_file"))
        self.assertTrue(ctx.note_mutation("edit_project_file"))
        self.assertFalse(ctx.note_mutation("edit_project_file"))

    def test_status_matrix(self):
        ok, _ = check_status_tool(None, SIDE_EFFECTING)
        self.assertTrue(ok)          # 未声明状态按旧语义放行（READY 同）
        ok, _ = check_status_tool(STATUS_READY, SIDE_EFFECTING)
        self.assertTrue(ok)          # READY 正常执行
        ok, _ = check_status_tool(STATUS_DISCOVERABLE, DISCOVERY_SAFE)
        self.assertTrue(ok)          # DISCOVERABLE 允许只读
        ok, reason = check_status_tool(STATUS_DISCOVERABLE, SIDE_EFFECTING)
        self.assertFalse(ok)
        self.assertIn("DISCOVERABLE", reason)
        ok, reason = check_status_tool(STATUS_NEEDS_USER, SIDE_EFFECTING)
        self.assertFalse(ok)
        self.assertIn("NEEDS_USER", reason)


class DiscoveryBoundUnitTests(unittest.TestCase):
    def test_signature_normalizes_same_intent(self):
        self.assertEqual(
            discovery_signature("list_workspace_files", {"directory": "./src/"}),
            discovery_signature("list_workspace_files", {"directory": "src"}),
        )
        # 输出容量字段不改变意图
        self.assertEqual(
            discovery_signature("read_workspace_file",
                                {"path": "src/a.ts", "max_chars": 12000}),
            discovery_signature("read_workspace_file",
                                {"path": "src/a.ts", "max_chars": 60000}),
        )
        # 分页/换范围是不同意图，不误伤
        self.assertNotEqual(
            discovery_signature("search_documents",
                                {"query": "login", "offset": 0}),
            discovery_signature("search_documents",
                                {"query": "login", "offset": 20}),
        )

    def test_three_identical_discovery_calls_blocked(self):
        from runtime.readiness_gate import DiscoveryTracker

        tracker = DiscoveryTracker()
        ok, _ = tracker.note("list_workspace_files", {"directory": "."})
        self.assertTrue(ok)
        ok, _ = tracker.note("list_workspace_files", {"directory": "."})
        self.assertTrue(ok)
        ok, hint = tracker.note("list_workspace_files", {"directory": "."})
        self.assertFalse(ok)
        self.assertIn(DISCOVERY_EXHAUSTED, hint)

    def test_distinct_discovery_not_capped_by_total_count(self):
        from runtime.readiness_gate import DiscoveryTracker

        tracker = DiscoveryTracker()
        for i in range(12):
            ok, _ = tracker.note("read_workspace_file", {"path": f"src/f{i}.ts"})
            self.assertTrue(ok)  # 12 个不同文件 = 真实探索，不触发总次数上限

    def test_repeated_exhaustion_hard_stops_all_discovery(self):
        from runtime.readiness_gate import DiscoveryTracker

        tracker = DiscoveryTracker()
        # 工具 A 连续 3 次 → 第 1 次耗尽信号
        for _ in range(3):
            tracker.note("list_workspace_files", {"directory": "src"})
        # 工具 B 连续 3 次 → 第 2 次耗尽信号 → 整体硬停止
        for _ in range(3):
            tracker.note("list_workspace_files", {"directory": "."})
        self.assertTrue(tracker.hard_stopped())
        # 换全新工具/参数也不得继续探索（不再“换着花样空转”）
        ok, hint = tracker.note("read_workspace_file", {"path": "never-read.ts"})
        self.assertFalse(ok)
        self.assertIn(DISCOVERY_EXHAUSTED, hint)


class _GateHarness:
    """把工具包装层（Readiness Gate + Approval + 记账）装到 fake 工具上。"""

    def __init__(self, tools: list[FunctionTool],
                 approval_enabled: bool | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="rdy_gate_"))
        self.manager = TaskManager(self.tmp / "agent.db")
        self.runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        self.runtime._initialized = True
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self._saved_approval = os.environ.get("APPROVAL")
        # None = 保持调用方（如 GuardWrapperTests.setUp 的 APPROVAL=off）设置的值原样
        if approval_enabled is True:
            os.environ["APPROVAL"] = "on"
        elif approval_enabled is False:
            os.environ["APPROVAL"] = "off"
        self.fake_agent = SimpleNamespace(tools=list(tools))
        self._patcher = mock.patch.object(agent_module, "assistant_agent", self.fake_agent)
        self._patcher.start()
        self.runtime._tools_patched = False
        self.runtime._patch_agent_tools()
        self.wrapped = {t.name: t for t in self.fake_agent.tools}
        # 单次测试复用同一个 RunContext：有界探索计数随 run 隔离而不是每次清零
        self.ctx = RunContext(run_id="rt-gate", container_id="c1",
                              session_id="s1", readiness_status=None)

    def close(self):
        self._patcher.stop()
        if self._saved_approval is None:
            os.environ.pop("APPROVAL", None)
        else:
            os.environ["APPROVAL"] = self._saved_approval

    async def call_tool(self, name: str, args: dict, status: str | None):
        ctx_obj = SimpleNamespace(tool_call_id="call-1")
        self.ctx.readiness_status = status
        bind_runctx(self.ctx)
        tool = self.wrapped[name]
        try:
            return await tool.on_invoke_tool(ctx_obj, json.dumps(args, ensure_ascii=False))
        except ApprovalRequired as exc:
            return str(exc)

    def call(self, name, args=None, status=None):
        return asyncio.run(self.call_tool(name, args or {}, status))


def _make_tool(name: str, executed: list[str], result: str | None = None) -> FunctionTool:
    async def invoke(ctx, args_json):
        executed.append(name)
        return result if result is not None else f"ok-{name}"

    return FunctionTool(name=name, description="fake", params_json_schema={},
                        on_invoke_tool=invoke)


class ReadinessToolGateTests(unittest.TestCase):
    """RT-GATE-01..05：Readiness 必须真正约束 Tool Timing。"""

    def test_rt_gate_01_needs_user_blocks_edit(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("edit_project_file", executed),
                          _make_tool("read_note", executed)])
        try:
            out = h.call("edit_project_file",
                         {"path": "src/x.py", "old": "a", "new": "b"},
                         status=STATUS_NEEDS_USER)
            self.assertIn("NEEDS_USER", out)
            self.assertEqual(executed, [])  # 函数本体不得执行
            ledger = h.runtime._ledgers.get("rt-gate", [])
            self.assertEqual(ledger[-1]["status"], "blocked")
            self.assertFalse(h.runtime._execution_evidence(
                run_id="rt-gate").executed_count())
        finally:
            h.close()

    def test_rt_gate_02_discoverable_allows_read_only(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_note", executed)])
        try:
            out = h.call("read_note", {"filename": "x.md"},
                         status=STATUS_DISCOVERABLE)
            self.assertEqual(out, "ok-read_note")
            self.assertEqual(executed, ["read_note"])
        finally:
            h.close()

    def test_rt_gate_03_discoverable_blocks_write(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("write_project_file", executed)])
        try:
            out = h.call("write_project_file",
                         {"path": "src/x.py", "content": "new"},
                         status=STATUS_DISCOVERABLE)
            self.assertIn("DISCOVERABLE", out)
            self.assertEqual(executed, [])
        finally:
            h.close()

    def test_rt_gate_04_ready_allows_edit(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("edit_project_file", executed)],
                        approval_enabled=False)
        try:
            out = h.call("edit_project_file",
                         {"path": "src/x.py", "old": "a", "new": "b"},
                         status=STATUS_READY)
            self.assertEqual(out, "ok-edit_project_file")
            self.assertEqual(executed, ["edit_project_file"])
        finally:
            h.close()

    def test_rt_gate_05_blocked_has_no_side_effect_or_evidence(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("edit_project_file", executed),
                          _make_tool("run_python", executed)])
        try:
            h.call("edit_project_file", {"path": "src/x.py"},
                   status=STATUS_NEEDS_USER)
            h.call("run_python", {"code": "print(1)"}, status=STATUS_NEEDS_USER)
            self.assertEqual(executed, [])
            ledger = h.runtime._ledgers.get("rt-gate", [])
            self.assertTrue(ledger)
            self.assertTrue(all(c["status"] == "blocked" for c in ledger))
            ev = h.runtime._execution_evidence(run_id="rt-gate")
            self.assertEqual(ev.executed_count(), 0)
        finally:
            h.close()

    def test_discovery_bound_blocks_third_identical_in_discoverable(self):
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_note", executed)])
        try:
            args = {"filename": "same.md"}
            self.assertIn("ok-", h.call("read_note", args, status=STATUS_DISCOVERABLE))
            self.assertIn("ok-", h.call("read_note", args, status=STATUS_DISCOVERABLE))
            out = h.call("read_note", args, status=STATUS_DISCOVERABLE)
            self.assertIn(DISCOVERY_EXHAUSTED, out)
            self.assertEqual(executed, ["read_note", "read_note"])
        finally:
            h.close()

    def test_readiness_ready_does_not_bound_distinct_discovery(self):
        # Phase 4：READY 下不同主题的探索不应被收敛误伤。
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_note", executed)])
        try:
            for i in range(4):
                self.assertIn("ok-", h.call("read_note", {"filename": f"f{i}.md"},
                                            status=STATUS_READY))
            self.assertEqual(len(executed), 4)
        finally:
            h.close()

    def test_readiness_ready_bounds_repeated_no_progress(self):
        # Phase 4：同一动作同一结果连续重复（无进展）即使 READY 也会被 CONVERGENCE 收口。
        executed: list[str] = []
        h = _GateHarness([_make_tool("read_note", executed)])
        try:
            args = {"filename": "same.md"}
            self.assertIn("ok-", h.call("read_note", args, status=STATUS_READY))
            self.assertIn("ok-", h.call("read_note", args, status=STATUS_READY))
            self.assertIn("ok-", h.call("read_note", args, status=STATUS_READY))
            out = h.call("read_note", args, status=STATUS_READY)
            self.assertIn("CONVERGENCE_REACHED", out)
            self.assertEqual(len(executed), 3)
        finally:
            h.close()


class ClarificationPrecisionGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = CompletionGate()

    def test_vague_questions_rejected(self):
        v = self.gate.evaluate(
            _reply("需要补充关键信息", kind="questions",
                   questions=["请补充更多信息"]),
            ExecutionEvidence(), request_text="帮我订机票")
        self.assertEqual(v, GateVerdict.CLARIFICATION_VAGUE)

    def test_specific_questions_pass(self):
        v = self.gate.evaluate(
            _reply("还缺出发地", kind="questions",
                   questions=["从哪里出发？", "乘机人是谁？"]),
            ExecutionEvidence(), request_text="帮我订机票")
        self.assertEqual(v, GateVerdict.PASS)

    def test_precision_helpers(self):
        self.assertEqual(clarification_precision(["需要补充更多信息"]), "low")
        self.assertEqual(clarification_precision(["从哪里出发？"]), "ok")
        self.assertEqual(clarification_precision([]), "empty")


class CapabilityAnswerGateTests(unittest.TestCase):
    """P1：Completion Gate 对纯描述性能力回答的误报修复。"""

    TR022_TEXT = (
        "【已连接的 MCP 扩展】\n1. Gitee 代码托管 — 读取与维护 Gitee 仓库/Issue/PR\n"
        "7. YouTube 字幕 — 读取公开 YouTube 视频字幕\n\n【内置能力】\n"
        "- 笔记管理：保存/查看/列出 notes 目录下的 Markdown 文件\n"
        "- 办公文档：读取 Word/Excel/PPT，生成 Word/Excel/PPT"
    )

    def setUp(self):
        self.gate = CompletionGate()

    def test_capability_descriptive_answer_zero_tool_passes(self):
        v = self.gate.evaluate(
            _reply(self.TR022_TEXT, kind="answer"),
            ExecutionEvidence(), request_text="你现在有哪些 MCP 可用？")
        self.assertEqual(v, GateVerdict.PASS)

    def test_plain_execution_request_with_capability_text_still_flagged(self):
        # 非能力盘点语境：0 工具列出“能力清单”仍视为完成声明无证据
        v = self.gate.evaluate(
            _reply(self.TR022_TEXT, kind="answer"),
            ExecutionEvidence(), request_text="帮我修复登录报错")
        self.assertEqual(v, GateVerdict.CLAIM_UNSUPPORTED)

    def test_youtube_download_claim_still_blocked(self):
        # 0 工具却声称“已经成功调用 YouTube MCP 下载字幕” → 必须拦截
        v = self.gate.evaluate(
            _reply("我已经成功调用 YouTube MCP 下载了字幕。", kind="answer"),
            ExecutionEvidence(), request_text="帮我下载 YouTube 字幕")
        self.assertEqual(v, GateVerdict.CLAIM_UNSUPPORTED)


class ReadinessParserClosureTests(unittest.TestCase):
    def test_parser_passes_missing_fields(self):
        parsed = parse_reply(_reply(
            kind="questions", content="",
            questions=["从哪里出发？"],
            readiness={"status": "NEEDS_USER", "missing_count": 1,
                       "missing": [{"name": "departure_location",
                                    "reason": "查询航班需要出发地"}],
                       "reason": "缺出发地"},
        ))
        rd = parsed.canonical["readiness"]
        self.assertEqual(rd["status"], "NEEDS_USER")
        self.assertEqual(rd["missing"][0]["name"], "departure_location")
        self.assertEqual(rd["missing_count"], 1)

    def test_normalize_missing_limits_and_dedup(self):
        out = normalize_missing([
            {"name": "a", "reason": "r1"}, {"name": "a"}, "b",
            {"name": "c"}, {"name": "d"}, {"name": "e"}, {"name": "f"},
            {"name": "g"}, {"name": "h"}, {"name": "i"}, {"name": "j"},
        ])
        names = [m["name"] for m in out]
        self.assertEqual(len(names), 8)
        self.assertEqual(names[0], "a")
        self.assertEqual(len(set(names)), len(names))


class ReadinessContextInheritanceTests(unittest.TestCase):
    """容器级未决澄清继承（TR-048 语境）：上一轮 NEEDS_USER → 下一轮继承；
    上一轮 READY/普通回答 → 不跨 Run 继承（避免新独立请求被旧状态卡死）。"""

    def _harness_runtime(self):
        tmp = Path(tempfile.mkdtemp(prefix="rdy_ctx_"))
        manager = TaskManager(tmp / "agent.db")
        runtime = AgentRuntime(db_path=str(tmp / "agent.db"))
        runtime._initialized = True
        runtime._tools_patched = True
        runtime.tasks = manager
        runtime.approval = ApprovalGate(manager)
        runtime.broker = object()
        runtime.artifact_dirs = (tmp / "notes",)
        (tmp / "notes").mkdir(exist_ok=True)
        return runtime, manager

    def _run(self, runtime, manager, message, reply_json):
        seen_status: list[str | None] = []

        async def fake_execute(mode, msg, **kwargs):
            from runtime.runctx import current as _cur

            ctx = _cur()
            seen_status.append(ctx.readiness_status if ctx else None)
            return json.dumps(reply_json, ensure_ascii=False)

        orig = main_module.execute_turn
        main_module.execute_turn = fake_execute
        try:
            result = asyncio.run(runtime.run_turn(message, session_id="unit",
                                                  mode="async"))
        finally:
            main_module.execute_turn = orig
        return result, seen_status

    def test_needs_user_inherited_by_next_run(self):
        runtime, manager = self._harness_runtime()
        q = _reply(kind="questions", content="",
                   questions=["从哪里出发？"],
                   readiness={"status": "NEEDS_USER", "missing_count": 1,
                              "missing": [{"name": "departure_location",
                                           "reason": "缺出发地"}]})
        r1, s1 = self._run(runtime, manager, "帮我订明天去上海的机票", q)
        # Phase 5：缺信息 Run 终态为 WAITING_USER。
        self.assertEqual(r1.task.state, TaskState.WAITING_USER)
        self.assertEqual(s1, [None])
        ev = next(e for e in manager.list_events(r1.task.id)
                  if e.event_type == "task.readiness")
        self.assertEqual(ev.payload["status"], "NEEDS_USER")
        self.assertIn("departure_location", ev.payload["missing"])
        self.assertEqual(ev.payload["precision"], "ok")

        ans = _reply(kind="answer", content="出发地北京已收到。",
                     readiness={"status": "READY", "missing_count": 0})
        r2, s2 = self._run(runtime, manager, "北京。", ans)
        self.assertEqual(r2.task.state, TaskState.COMPLETED)
        self.assertEqual(s2, [STATUS_NEEDS_USER])  # 下一轮继承未决 NEEDS_USER

    def test_ready_or_plain_answer_does_not_inherit(self):
        runtime, manager = self._harness_runtime()
        ans = _reply(kind="answer", content="普通回答。")
        r1, s1 = self._run(runtime, manager, "你好", ans)
        self.assertEqual(s1, [None])
        r2, s2 = self._run(runtime, manager, "现在几点", ans)
        self.assertEqual(r2.task.state, TaskState.COMPLETED)
        self.assertIsNone(s2[0])  # 不继承


if __name__ == "__main__":
    unittest.main()
