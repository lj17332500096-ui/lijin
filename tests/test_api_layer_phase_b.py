"""Phase B 修复的验收测试（2026-09-22《FORGE API Layer Audit Report》§17）。

覆盖（每条对应审计报告里的一个 B 项）：
- B1  ``transition()`` 改 CAS：并发终态流转只能有一个赢家，输家不得覆盖赢家的状态
- B2  重试权收口：关闭 SDK 内建重试、单 provider 与 primary+fallback 合并的次数预算、
      L3 格式闸重跑次数显式化（``SdkRetryOwnershipTests`` / ``TotalAttemptBudgetTests``
      / ``FormatRerunBudgetTests``）
- B3  分层超时显式声明（connect / read / 总预算），read 不得等于总预算
      （``LayeredTimeoutTests``）
- B4  ``waiting_user`` 进单 Active Run 集合：追问态下不得并起第二个 Run；索引迁移能
      消化旧漏洞留下的脏数据
- B5  取消必须让同步工具的子进程树真的停下（``CancelScopeRegistryTests`` /
      ``SyncToolCancellationTests``）
- B7  轮次计数接线回归：钩子必须认得生产侧写出的 ``"success"``（``ModelTurnAccountingTests``）

全部离线：不联网、不调真实模型。
"""
from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ.setdefault("FORGE_TEST_MODE", "1")
import runtime.execution as runtime_execution

import code_exec  # noqa: E402  (需要先插好 sys.path)

from runtime.errors import AgentError
from runtime.provider_errors import ProviderErrorKind, ProviderTransportError
from runtime.task import TaskState
from runtime.task_manager import TaskManager

NO_CHANNEL = ("Error code: 503 - "
              "No available channel for model agnes-x under group TokenPlan")


def _tripwire(message: str = "格式不合规"):
    """构造格式闸异常替身，跳过需要真实 ``OutputGuardrailResult`` 的父类构造。

    ``OutputGuardrailTripwireTriggered.__init__`` 会读 ``guardrail_result.guardrail``，
    传字符串会直接 AttributeError；而 ``main._guardrail_reason`` 在没有 run_data /
    guardrail_result 时会退回 ``str(exc)``，所以只需要「它是该异常的子类」这一个属性。
    """
    import main as main_module
    class _Tripwire(main_module.OutputGuardrailTripwireTriggered):
        def __init__(self) -> None:
            Exception.__init__(self, message)

    return _Tripwire()


class TransitionCasTests(unittest.TestCase):
    """B1（P0-6a）：状态机唯一写入点必须是原子的读-校验-写。

    回归背景：旧实现是「先读（连接 A）→ 校验 → 凭空写 state=?（连接 B）」。
    两个并发的终态流转可以各自读到 `running`、各自通过 `assert_transition`、
    然后各自写成功 —— 后写的一方覆盖先写的一方。症状是「用户取消的 Run 显示成
    failed」，或 failed 被 completed 覆盖，且审计事件里会同时留下两条互相矛盾的
    终态事件。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_b_"))
        self.manager = TaskManager(self._tmp / "agent.db")

    def _make_running(self) -> str:
        task = self.manager.create_task("s_phase_b", "目标")
        self.manager.transition(task.id, TaskState.RUNNING, reason="start")
        return task.id

    def test_concurrent_terminal_transitions_have_exactly_one_winner(self) -> None:
        """两个线程同时把同一个 running Run 推进到不同终态：只能有一个成功。"""
        run_id = self._make_running()
        barrier = threading.Barrier(2)
        results: list[tuple[str, str]] = []
        lock = threading.Lock()

        def _race(target: TaskState, tag: str) -> None:
            barrier.wait(timeout=10)  # 尽量让两边的读发生在同一时间窗
            try:
                self.manager.transition(run_id, target)
                outcome = "ok"
            except AgentError as exc:
                outcome = f"error:{str(exc)[:40]}"
            except Exception as exc:  # noqa: BLE001 - 非 AgentError 也算输家，但记录下来
                outcome = f"unexpected:{type(exc).__name__}"
            with lock:
                results.append((tag, outcome))

        threads = [
            threading.Thread(target=_race, args=(TaskState.CANCELLED, "cancel")),
            threading.Thread(target=_race, args=(TaskState.FAILED, "fail")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), 2, "两个线程都必须跑完")
        winners = [r for r in results if r[1] == "ok"]
        self.assertEqual(
            len(winners), 1,
            "并发终态流转必须恰好一个成功（旧实现下两个都会成功，后者覆盖前者）：%r" % (results,),
        )

        final = self.manager.get_task(run_id)
        self.assertIsNotNone(final)
        expected = TaskState.CANCELLED if winners[0][0] == "cancel" else TaskState.FAILED
        self.assertEqual(final.state, expected, "最终状态必须是赢家写的那个")

        # 输家不得留下「我成功流转了」的审计痕迹
        terminal_events = [
            e for e in self.manager.list_events(run_id)
            if e.event_type in ("task.cancelled", "task.failed")
        ]
        self.assertEqual(
            len(terminal_events), 1,
            "只应有一条终态事件，实际：%r" % ([e.event_type for e in terminal_events],),
        )

    def test_transition_is_ignored_by_a_stale_writer(self) -> None:
        """顺序化的「丢失更新」：终态一旦写入，就不再接受任何其它流转。"""
        run_id = self._make_running()
        self.manager.transition(run_id, TaskState.CANCELLED)

        with self.assertRaises(AgentError):
            self.manager.transition(run_id, TaskState.FAILED)

        final = self.manager.get_task(run_id)
        self.assertEqual(final.state, TaskState.CANCELLED,
                         "被取消的 Run 不得被后续 failed 覆盖")

    def test_cas_preserves_normal_transitions(self) -> None:
        """CAS 不得破坏正常路径：started_at / completed_at / 事件 from 都要照常写。"""
        run_id = self._make_running()
        running = self.manager.get_task(run_id)
        self.assertTrue(running.started_at, "RUNNING 应写 started_at")

        done = self.manager.transition(run_id, TaskState.COMPLETED)
        self.assertEqual(done.state, TaskState.COMPLETED)
        self.assertTrue(done.completed_at, "终态应写 completed_at")

        transitions = [
            e for e in self.manager.list_events(run_id)
            if e.event_type in ("task.running", "task.completed")
        ]
        self.assertEqual([e.payload.get("from") for e in transitions],
                         ["submitted", "running"],
                         "事件里的 from 必须记录流转前的真实状态")


class SingleActiveRunTests(unittest.TestCase):
    """B4（P1-5）：waiting_user 必须与 running/waiting_approval 同等视为「容器忙」。

    回归背景：``ACTIVE_RUN_STATES`` 与唯一索引谓词都漏了 waiting_user。后果是
    「Run 停在追问态 → 用户又发一条消息」会在同一容器里并起第二个 Run，两个 Run
    共享同一份会话历史，消息交错、状态互相覆盖。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_b_b4_"))
        self.db = self._tmp / "agent.db"
        self.manager = TaskManager(self.db)

    def test_waiting_user_counts_as_active(self) -> None:
        from runtime.task_manager import TaskManager as TM

        self.assertIn("waiting_user", TM.ACTIVE_RUN_STATES,
                      "waiting_user 是挂起态，必须算「容器正在处理中」")

    def test_index_predicate_is_derived_from_active_states(self) -> None:
        """索引谓词不得是手抄的第二份字符串（这个 bug 的成因就是两处并存漂移）。"""
        sql = self.manager._single_active_index_sql()
        for state in self.manager.ACTIVE_RUN_STATES:
            self.assertIn(f"'{state}'", sql,
                          f"索引谓词缺少 {state}（SQL={sql}）")
        with sqlite3.connect(str(self.db)) as conn:
            idx_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'idx_runs_single_active'"
            ).fetchone()
        self.assertIsNotNone(idx_sql, "唯一索引未建立")
        for state in self.manager.ACTIVE_RUN_STATES:
            self.assertIn(f"'{state}'", idx_sql[0],
                          f"落库的索引谓词缺少 {state}：{idx_sql[0]}")

    def test_raw_create_in_container_with_waiting_user_is_rejected(self) -> None:
        """DB 层：唯一索引确实把 waiting_user 算作活跃（不能再并起第二个 Run）。"""
        first = self.manager.create_task("s_b4", "第一个目标", thread_id="tk_b4")
        self.manager.transition(first.id, TaskState.RUNNING)
        self.manager.transition(first.id, TaskState.WAITING_USER, reason="needs_user_input")

        self.assertIsNotNone(
            self.manager.find_active_run("tk_b4"),
            "追问态 Run 应被 find_active_run 认作活跃",
        )
        with self.assertRaises(AgentError) as cm:
            self.manager.create_task("s_b4", "第二个目标", thread_id="tk_b4")
        self.assertIn("已有正在处理的 Run", str(cm.exception))

    def test_next_run_supersedes_waiting_user_instead_of_being_blocked(self) -> None:
        """API 层：追问态下用户直接回一句 → 开新一轮，旧 Run 被取代而非拒绝。

        这是对审计建议 B4 的**修正**：报告建议把 waiting_user 直接并入「容器忙」集合并
        一律拒绝新 Run，但那会打断项目既有的追问闭环（用户回答追问本来就是开新一轮，
        并由下一轮继承未决的 NEEDS_USER）。审计真正要挡的是「并发 Run 共享会话历史」，
        所以正确做法是「取代」：旧 Run 收口为 cancelled 并留事件，再建新 Run。
        """
        import main as main_module

        from runtime.runner import AgentRuntime

        rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        container = self.manager.get_or_create_container("s_b4_flow")
        first = self.manager.create_task("s_b4_flow", "帮我订机票", thread_id=container["id"])
        self.manager.transition(first.id, TaskState.RUNNING)
        self.manager.transition(first.id, TaskState.WAITING_USER, reason="needs_user_input")

        seen: dict = {}

        async def fake_execute_turn(mode, message, session=None, debug=False, max_turns=20,
                                    history_limit=None, agent=None, audit=None,
                                    stream_events_cb=None, **kwargs):
            from runtime.runctx import current as _cur

            seen["run_id"] = getattr(_cur(), "run_id", None)
            # 关键不变量在「新 Run 正在跑」的这一刻检查：容器内仍然只有一个活跃 Run。
            # （等 Run 结束再查没有意义 —— 那时活跃 Run 自然是 0。）
            seen["actives_during"] = [
                t.state.value for t in rt.tasks.list_tasks(
                    container_id=container["id"], limit=20)
                if t.state.value in rt.tasks.ACTIVE_RUN_STATES
            ]
            return '{"kind":"answer","summary":"ok","content":"出发地已收到。"}'

        orig = runtime_execution.execute_turn
        runtime_execution.execute_turn = fake_execute_turn
        self.addCleanup(setattr, runtime_execution, "execute_turn", orig)
        import asyncio

        asyncio.run(rt.run_turn("北京。", session_id="s_b4_flow"))

        self.assertNotEqual(seen.get("run_id"), first.id, "应开新 Run 而不是复用追问态旧 Run")
        self.assertEqual(seen.get("actives_during"), ["running"],
                         "新 Run 执行期间，容器内必须恰好一个活跃 Run（旧 Run 已被取代）")
        old = self.manager.get_task(first.id)
        self.assertEqual(old.state, TaskState.CANCELLED,
                         "被取代的追问态 Run 应收口为 cancelled（不是执行失败）")
        events = [e for e in self.manager.list_events(first.id)
                  if e.event_type == "run.superseded"]
        self.assertTrue(events, "取代动作必须留事件说明，避免伪造终态")

    def test_migration_repairs_legacy_double_active_rows(self) -> None:
        """迁移必须能消化旧漏洞留下的脏数据。

        否则 CREATE UNIQUE INDEX 会因重复键直接失败 → TaskManager 构造抛异常 →
        整个服务起不来。这里如实构造「旧谓词允许、新谓词不允许」的库：
        先把索引换回漏 waiting_user 的旧定义，插入 waiting_user + running 双活跃行，
        再用新代码构造 TaskManager 触发迁移。
        """
        container = self.manager.create_task("s_b4_mig", "容器")

        with sqlite3.connect(str(self.db)) as conn:
            # 还原成旧定义（漏 waiting_user）—— 这就是历史库的真实样子
            conn.execute("DROP INDEX IF EXISTS idx_runs_single_active")
            conn.execute(
                "CREATE UNIQUE INDEX idx_runs_single_active ON runs(task_id) "
                "WHERE state IN ('submitted','running','waiting_approval','paused')"
            )

        stale = self.manager.create_task("s_b4_mig", "被取代的", thread_id=container.id)
        self.manager.transition(stale.id, TaskState.RUNNING)
        self.manager.transition(stale.id, TaskState.WAITING_USER)
        # 旧谓词下 waiting_user 不算活跃 → 这里能插进去（新谓词下会失败）
        fresh = self.manager.create_task("s_b4_mig", "仍在跑的", thread_id=container.id)
        self.manager.transition(fresh.id, TaskState.RUNNING)

        with sqlite3.connect(str(self.db)) as conn:
            dupes = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE task_id = ? AND state IN "
                "('submitted','running','waiting_approval','waiting_user','paused')",
                (container.id,),
            ).fetchone()[0]
        self.assertEqual(dupes, 2, "前置条件：需要同容器 2 条活跃 Run")

        # 重建 TaskManager = 触发迁移
        migrated = TaskManager(self.db)

        self.assertEqual(migrated.get_task(stale.id).state, TaskState.CANCELLED,
                         "旧的（被取代的）Run 应收敛为 cancelled")
        self.assertEqual(migrated.get_task(fresh.id).state, TaskState.RUNNING,
                         "最新的 Run 必须保留为活跃")
        self.assertIsNotNone(migrated.find_active_run(container.id))
        with sqlite3.connect(str(self.db)) as conn:
            idx_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'idx_runs_single_active'"
            ).fetchone()
            events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE event_type = 'run.superseded'"
            ).fetchone()[0]
        self.assertIsNotNone(idx_sql, "迁移后必须建起唯一索引")
        self.assertIn("waiting_user", idx_sql[0], "索引谓词必须是新定义")
        self.assertGreaterEqual(events, 1, "收敛动作必须留事件说明是迁移修复")


class ApprovedEvidenceAttributionTests(unittest.TestCase):
    """审批后自动执行的证据必须落在**目标 Run** 上，与「当前上下文残影」无关。

    回归背景（Phase B 期间定位）：``_execute_approved_invocation`` 旧实现只要
    「当前绑定了 RunContext」就无条件复用，于是任何残留/外来上下文（``run_id``
    为空或指向别的 Run）都会让证据被记到那个 Run 上 —— 表现是「工具确实执行了
    （stderr 有 TOOL_INV），但 tool.invocation 事件凭空消失」。
    触发条件极常见：同线程里任何一次未解绑的 RunContext 都会留下残影，
    测试之间会因此互相污染，生产里同一线程处理多 Run 时同理。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_b_ev_"))
        from runtime.runner import AgentRuntime

        self.rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.rt._ensure()
        self.task = self.rt.tasks.create_task(session_id="pb_ev", goal="证据归属")
        self.rt.tasks.transition(self.task.id, TaskState.RUNNING, reason="start")

    def _pending_approval(self) -> dict:
        from runtime.errors import ApprovalRequired

        gate = self.rt.approval
        gate.begin(self.task.id, "chat")
        with self.assertRaises(ApprovalRequired):
            import asyncio as _a

            _a.run(gate.check("run_tests", {"project": "m3_fixture"}, run_id=self.task.id))
        pending = self.rt.tasks.list_pending_approvals(self.task.id)
        self.assertTrue(pending, "应产生一条待审批记录")
        return pending[0]

    def test_evidence_lands_on_target_run_despite_foreign_context(self) -> None:
        import asyncio

        from runtime.runctx import RunContext, bind as _bind, current as _cur

        ap = self._pending_approval()
        self.rt.tasks.decide_approval(ap["id"], "approved")

        # 模拟「残留上下文」：run_id 为空（正是泄漏源留下的形状）
        prev = _cur()
        self.addCleanup(_bind, prev)
        _bind(RunContext(request_text="stale"))

        approved = self.rt.tasks.get_approved_unexecuted(self.task.id)
        self.assertTrue(approved, "应有待执行的已批准调用")
        asyncio.run(self.rt._execute_approved_invocation(
            approved[0]["tool_name"], approved[0]["arguments"],
            approved[0]["id"], run_rid=self.task.id,
        ))

        events = self.rt.tasks.list_events(self.task.id)
        hits = [e for e in events
                if e.event_type == "tool.invocation"
                and (e.payload or {}).get("tool_name") == "run_tests"]
        self.assertTrue(
            hits,
            "已有外来上下文时，证据仍必须写到 run_rid 指向的 Run 上"
            "（实际事件类型：%r）" % ([e.event_type for e in events],),
        )
        self.assertTrue(hits[-1].payload.get("run_id"), "事件必须带 run_id")


class TraceRunIdLinkageTests(unittest.TestCase):
    """B8（P1-6）：trace 记录必须能按 Run id 找回。

    回归背景：项目记忆里的「run_id = trace_id」铁律是错的 —— 记录里写的 ``trace_id``
    是 SDK 生成的 OTel id，与 Run id 毫无关联；而 ``trace_export`` 拿 Run id 去过滤
    ``trace_id``，条件恒为假，导出的 ``spans`` 永远是空数组（"跨层追踪"实际不存在）。
    """

    def test_records_carry_run_id(self) -> None:
        from runtime.observability import base_record
        from runtime.runctx import RunContext, bind as _bind, current as _cur

        prev = _cur()
        self.addCleanup(_bind, prev)
        rec = base_record(None, None, name="probe")
        self.assertIsNone(rec.get("run_id"), "Run 外应为 None，不得编造 id")

        _bind(RunContext(run_id="task_probe123", request_text="x"))
        rec = base_record(None, None, name="probe")
        self.assertEqual(rec.get("run_id"), "task_probe123",
                         "记录必须盖上 Run 身份（trace_id 是 SDK 生成的，无法指定）")
        self.assertIn("trace_id", rec, "OTel trace_id 仍须保留，不得被顶替")

    def test_trace_export_finds_spans_by_run_id(self) -> None:
        import json as _json

        from runtime import trace_export

        tmpdir = Path(tempfile.mkdtemp(prefix="phase_b_trace_"))
        path = tmpdir / "traces.jsonl"
        path.write_text("\n".join([
            _json.dumps({"trace_id": "otel-aaaa", "run_id": "task_abc", "name": "turn"}),
            _json.dumps({"trace_id": "otel-bbbb", "run_id": "task_xyz", "name": "turn"}),
            # 旧文件：没有 run_id 字段 → 回退按 trace_id 匹配（向后兼容）
            _json.dumps({"trace_id": "task_abc", "name": "legacy"}),
        ]), encoding="utf-8")

        spans = trace_export._load_spans("task_abc", str(path))
        self.assertEqual([s["name"] for s in spans], ["turn", "legacy"],
                         "必须按 run_id 命中，且兼容旧文件的 trace_id 回退")

    def test_runtime_tracing_is_opt_in_and_idempotent(self) -> None:
        """追踪安装不得成为「构造 Runtime」的隐藏副作用（全局改 SDK + 无界写文件）。"""
        from unittest import mock

        from runtime.runner import AgentRuntime

        rt = AgentRuntime(db_path=str(Path(tempfile.mkdtemp()) / "a.db"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_TRACE", None)
            with mock.patch("runtime.observability.install_local_tracing") as inst:
                rt._ensure()
            self.assertFalse(inst.called, "未显式开启时不得安装追踪")

        rt2 = AgentRuntime(db_path=str(Path(tempfile.mkdtemp()) / "a.db"))
        with mock.patch.dict(os.environ, {"FORGE_TRACE": "1"}):
            with mock.patch("runtime.observability.install_local_tracing") as inst:
                rt2._ensure()
                rt2._ensure()  # 幂等：第二次不得重复安装
            self.assertEqual(inst.call_count, 1, "重复调用 _ensure 只应安装一次")


class ToolCallInstrumentationTests(unittest.TestCase):
    """B7（P1-8）：tool_calls 必须能回答「多久 / 第几轮 / 参数是否同形 / 结果是否同形」。

    回归背景：表里只有 (tool_name, arguments, status, result_excerpt) —— 同步工具卡死
    多久、换词绕过守卫的调用与上次是否同一件事，都无法回答。更讽刺的是 audit 采集
    路径**本来就记了 started 时间戳却从没使用**：数据在手，只是没人写下去。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_b_b7_"))
        self.manager = TaskManager(self._tmp / "agent.db")
        self.task = self.manager.create_task("s_b7", "目标")

    def test_columns_exist_after_migration(self) -> None:
        with sqlite3.connect(str(self._tmp / "agent.db")) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")}
        for name in ("latency_ms", "turn_number", "normalized_arguments", "fingerprint"):
            self.assertIn(name, cols, f"tool_calls 缺少列 {name}")

    def test_insert_persists_new_columns_and_redacts_normalized_args(self) -> None:
        import json as _json

        secret = "sk-liveb71234567890abcdefghij"
        self.manager.insert_tool_call(
            task_id=self.task.id,
            tool_name="run_python",
            arguments={"api_key": secret},
            status="succeeded",
            result_excerpt="ok",
            latency_ms=4321,
            turn_number=2,
            normalized_arguments=_json.dumps({"api_key": secret}, sort_keys=True),
            fingerprint="abc123",
        )
        rows = self.manager.list_tool_calls(self.task.id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["latency_ms"], 4321)
        self.assertEqual(row["turn_number"], 2)
        self.assertEqual(row["fingerprint"], "abc123")
        self.assertNotIn(secret, row["normalized_arguments"],
                         "normalized_arguments 是独立入口，必须自己走脱敏")

    def _ingest_one_call(self, turn: int = 1) -> dict:
        """用审计采集路径跑一次「调用 + 结果」配对，返回落库行。"""
        from types import SimpleNamespace

        from runtime.audit import AuditCollector
        from runtime.runctx import RunContext, bind as _bind, current as _cur

        prev = _cur()
        self.addCleanup(_bind, prev)
        ctx = RunContext(run_id=self.task.id, request_text="x")
        for _ in range(turn):
            ctx.note_model_turn()
        _bind(ctx)

        result = SimpleNamespace(
            raw_responses=[],
            new_items=[
                SimpleNamespace(raw_item={"type": "function_call", "name": "web_search",
                                          "arguments": '{"query": "今天天气"}',
                                          "call_id": "call_b7_1"}),
                SimpleNamespace(raw_item={"type": "function_call_output",
                                          "output": "北京 晴 26 度"}),
            ],
        )
        AuditCollector(self.manager, self.task.id).ingest(result)
        rows = self.manager.list_tool_calls(self.task.id)
        self.assertEqual(len(rows), 1, "应恰好落一行工具调用")
        return rows[0]

    def test_ingest_populates_latency_turn_and_fingerprints(self) -> None:
        import json as _json

        row = self._ingest_one_call(turn=1)
        self.assertIsNotNone(row["latency_ms"], "latency_ms 不得再是 NULL")
        self.assertGreaterEqual(row["latency_ms"], 0)
        self.assertEqual(row["turn_number"], 1, "turn_number 应取自 Run 的模型轮次")
        self.assertTrue(row["fingerprint"], "结果指纹不得为空")
        self.assertEqual(_json.loads(row["normalized_arguments"]),
                         {"query": "今天天气"},
                         "normalized_arguments 应是排序后的紧凑 JSON")

    def test_ledger_carries_latency_and_turn(self) -> None:
        """失败路径的事实源是内存账本，它也必须带上这两个字段。"""
        import asyncio

        from runtime.runner import AgentRuntime

        rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        captured: dict = {}

        async def fake_execute_turn(mode, message, session=None, debug=False, max_turns=20,
                                    history_limit=None, agent=None, audit=None,
                                    stream_events_cb=None, **kwargs):
            from runtime.runctx import current as _cur

            rid = getattr(_cur(), "run_id", None)
            rt._record_tool("run_python", {"code": "x"}, "executed", "ok",
                            latency_ms=987, invocation_id="inv_b7")
            captured["ledger"] = list(rt._ledgers.get(rid, []))
            return '{"kind":"answer","summary":"x","content":"done"}'

        import main as main_module

        orig = runtime_execution.execute_turn
        runtime_execution.execute_turn = fake_execute_turn
        self.addCleanup(setattr, runtime_execution, "execute_turn", orig)
        asyncio.run(rt.run_turn("跑一下", session_id="b7_ledger"))

        entries = captured.get("ledger") or []
        self.assertTrue(entries)
        self.assertEqual(entries[-1].get("latency_ms"), 987)


class ToolTimeoutWiringTests(unittest.TestCase):
    """B6（P1-3）：``spec.timeout_seconds`` 必须真的被用于卡住工具。

    回归背景：该字段此前**没有任何读取方**（默认 30 却从未生效）。任何卡住的工具都能
    一直占着 Run，直到把整轮墙钟预算耗光 —— 判据只剩「超过墙钟预算」，既看不出是谁
    卡的，也让后面本该执行的步骤全部饿死。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_b_b6_"))
        import main as main_module

        self._main = main_module
        self._orig_execute = runtime_execution.execute_turn
        from runtime.runner import AgentRuntime

        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))

    def tearDown(self) -> None:
        runtime_execution.execute_turn = self._orig_execute

    def test_timeout_table_is_the_single_source(self) -> None:
        from runtime import spec

        self.assertGreater(spec.timeout_for("deep_research"), 120,
                           "长跑工具必须给足上限，否则会被误杀")
        self.assertGreater(spec.timeout_for("未知工具"), 0)
        # spec_for 必须取自同一来源
        self.assertEqual(spec.spec_for("deep_research").timeout_seconds,
                         spec.timeout_for("deep_research"))

    def test_timed_out_tool_is_uncertain_and_recorded_as_error(self) -> None:
        """慢工具必须：① 被中止（不等它自然结束）；② 记为 TOOL_ERROR；③ 给模型可读原因。"""
        import asyncio as _a

        from agent import assistant_agent
        from agents.tool import ToolContext
        from runtime import spec as spec_mod

        # 必须先 _ensure() 再取 wrapper：工具包装是**每个 Runtime 各自**打一次的全局
        # 副作用，包装闭包捕获的是「打它的那个 Runtime」。若在 _ensure() 之前取，
        # 拿到的是上一个测试留下的包装 → 记账会落到别的 Runtime 的账本上（本测试
        # 第一版就是这么失败的：工具明明返回了超时文案，账本却查不到条目）。
        rt = self.runtime
        rt._ensure()

        target = "get_current_datetime"
        wrapper = next((t for t in (getattr(assistant_agent, "tools", []) or [])
                        if getattr(t, "name", "") == target), None)
        self.assertIsNotNone(wrapper, f"{target} 未注册")
        original = getattr(wrapper, "_wrapped_original", None)
        self.assertIsNotNone(original, "wrapper 应保留原始工具引用（A 阶段约定）")

        async def _hang(_ctx, _args):
            await _a.sleep(30)
            return "never"

        saved = original.on_invoke_tool
        try:
            original.on_invoke_tool = _hang
        except Exception:
            object.__setattr__(original, "on_invoke_tool", _hang)
        self.addCleanup(lambda: setattr(original, "on_invoke_tool", saved))

        captured: dict = {}

        async def fake_execute_turn(mode, message, session=None, debug=False, max_turns=20,
                                    history_limit=None, agent=None, audit=None,
                                    stream_events_cb=None, **kwargs):
            from runtime.runctx import current as _cur

            rid = getattr(_cur(), "run_id", None)
            ctx = ToolContext(context=None, tool_name=target,
                              tool_call_id="call_b6", tool_arguments="{}")
            out = wrapper.on_invoke_tool(ctx, "{}")
            if _a.iscoroutine(out):
                out = await out
            captured["out"] = out
            captured["ledger"] = list(rt._ledgers.get(rid, []))
            return '{"kind":"answer","summary":"x","content":"done"}'

        runtime_execution.execute_turn = fake_execute_turn

        # 把超时压到 1 秒（唯一来源是 spec 表 + FORGE_TOOL_TIMEOUT 兜底）
        orig_table = dict(spec_mod.TOOL_TIMEOUT_SECONDS)
        spec_mod.TOOL_TIMEOUT_SECONDS = {}
        self.addCleanup(setattr, spec_mod, "TOOL_TIMEOUT_SECONDS", orig_table)
        with mock.patch.dict(os.environ, {"FORGE_TOOL_TIMEOUT": "1"}):
            _a.run(rt.run_turn("现在几点", session_id="b6"))

        out = str(captured.get("out") or "")
        self.assertIn("上限", out, "模型必须收到可读的超时原因，而不是笼统 tool error")
        self.assertIn("本次结果按未知处理", out)
        self.assertIn("自动重放", out)
        entries = [c for c in (captured.get("ledger") or [])
                   if c.get("name") == target]
        self.assertTrue(entries, "超时也必须记账（否则等于静默失败）")
        self.assertEqual(entries[-1].get("status"), "error",
                         "超时不得记为 executed（否则会污染完成度证据）")
        self.assertIsNotNone(entries[-1].get("latency_ms"), "超时调用也应有耗时")


class StaleRecoveryThresholdTests(unittest.TestCase):
    """B9（P2-7）：启动恢复不得把「还在预算内跑着」的长任务误判为崩溃遗留。

    回归背景有两层：
    1. ``budget_relative`` 能力已实现，但 ``auto_recover`` 调用点没开 —— 修复休眠；
    2. 更关键的是它自己的判据方向反了：``min(max_age_seconds, 预算×2)`` 对 3000s
       预算仍然只给 900s，与固定阈值等价，等于完全没修。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_b_b9_"))
        self.manager = TaskManager(self._tmp / "agent.db")

    def _stale_running_run(self, *, silence_seconds: int, wall_budget: int) -> str:
        import datetime as dt

        from runtime.task import RunBudget

        task = self.manager.create_task(
            "s_b9", "长跑任务",
            budget=RunBudget(max_wall_seconds=wall_budget),
        )
        self.manager.transition(task.id, TaskState.RUNNING, reason="start")
        # 把 updated_at 拨到过去，模拟「N 秒没有任何推进信号」
        then = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(seconds=silence_seconds)).isoformat(timespec="seconds")
        with sqlite3.connect(str(self._tmp / "agent.db")) as conn:
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (then, task.id))
        return task.id

    def test_long_budget_run_is_not_falsely_recovered(self) -> None:
        """3000s 预算的 Run 静默 1000s：仍在预算内，不得被判定崩溃。

        旧 min() 判据下 threshold = min(900, 6000) = 900 < 1000 → 会被误杀。
        """
        run_id = self._stale_running_run(silence_seconds=1000, wall_budget=3000)
        recovered = self.manager.recover_stale_tasks(budget_relative=True)
        self.assertNotIn(run_id, recovered,
                         "预算 3000s 的 Run 静默 1000s 仍属正常，不得判为崩溃")
        self.assertEqual(self.manager.get_task(run_id).state, TaskState.RUNNING)

    def test_genuinely_stale_run_is_still_recovered(self) -> None:
        """超过预算 2 倍仍无动静 → 才认定崩溃遗留（不能因为放宽而漏收）。"""
        run_id = self._stale_running_run(silence_seconds=7000, wall_budget=3000)
        recovered = self.manager.recover_stale_tasks(budget_relative=True)
        self.assertIn(run_id, recovered, "超过预算 2 倍应判定为崩溃遗留")
        self.assertEqual(self.manager.get_task(run_id).state, TaskState.FAILED)

    def test_unknown_budget_falls_back_to_fixed_threshold(self) -> None:
        """预算未知（<=0）时退回固定阈值，保持向后兼容。"""
        run_id = self._stale_running_run(silence_seconds=1000, wall_budget=0)
        recovered = self.manager.recover_stale_tasks(budget_relative=True)
        self.assertIn(run_id, recovered, "预算未知时应使用固定 900s 阈值")

    def test_budget_json_is_actually_persisted(self) -> None:
        """根因守卫：``runs.budget_json`` 不得恒为空对象。

        RunBudget 是 ``@dataclass(slots=True)`` —— 实例没有 ``__dict__``，所以
        「``budget.__dict__`` 有就取、没有就 ``{}``」的写法会**静默**落成 ``{}``，
        让 budget_json 变成只写不读的死列，上面几条按预算回收的判据也就全部失效。
        """
        from runtime.task import RunBudget

        task = self.manager.create_task(
            "s_b9_json", "目标", budget=RunBudget(max_wall_seconds=1234, max_turns=7))
        with sqlite3.connect(str(self._tmp / "agent.db")) as conn:
            raw = conn.execute(
                "SELECT budget_json FROM runs WHERE id = ?", (task.id,)
            ).fetchone()[0]
        self.assertNotEqual(raw, "{}", "budget_json 落库为空对象（__dict__ 兜底陷阱）")
        import json as _json

        self.assertEqual(_json.loads(raw).get("max_wall_seconds"), 1234)
        self.assertEqual(_json.loads(raw).get("max_turns"), 7)

    def test_auto_recover_enables_budget_relative(self) -> None:
        """启动钩子必须真的启用相对阈值（否则修复仍处于休眠状态）。"""
        from unittest import mock

        from runtime.task_manager import auto_recover

        with mock.patch.object(TaskManager, "recover_stale_tasks",
                               return_value=[]) as spy:
            auto_recover(self._tmp / "agent.db")
        self.assertTrue(spy.called, "auto_recover 必须调用 recover_stale_tasks")
        self.assertTrue(
            spy.call_args.kwargs.get("budget_relative"),
            "auto_recover 必须传 budget_relative=True，实际：%r" % (spy.call_args,),
        )


class SdkRetryOwnershipTests(unittest.TestCase):
    """B2（P0-8）：重试权必须收口到 L1，SDK 的静默重试必须关掉。

    回归背景：``openai._constants.DEFAULT_MAX_RETRIES = 2`` ⇒ 每次「尝试」实际是
    1 + 2 = 3 次 HTTP，而这 2 次重试对 ``provider.model_attempts`` **完全不可见**
    （网关只在 L1 记一条）。审计看到的尝试次数只有真实 HTTP 的 1/3，于是「最坏 36 次
    HTTP / 用户一轮」这件事在观测面上根本不存在。

    这几个用例锁死的是「重试归属只声明一处」：L0 必须为 0，且这个 0 要能被审计看见。
    """

    def _provider(self, base_url: str):
        import runtime.provider_gateway as pg

        return pg.ResilientProvider(api_key="test", base_url=base_url, use_responses=False)

    def test_remote_provider_disables_sdk_retries_without_owning_transport(self) -> None:
        """远程网关：关掉内建重试，但仍沿用 SDK 的共享连接池（不改变代理行为）。"""
        provider = self._provider("https://gateway.example/v1")
        self.assertIsNone(provider._owned_http_client,
                          "远程网关不应自建 http client（会丢掉连接复用与既有代理行为）")
        client = provider._get_client()
        self.assertEqual(client.max_retries, 0,
                         "L0 未关闭：每次尝试会被静默放大成 3 次 HTTP")

    def test_loopback_provider_disables_sdk_retries_and_keeps_proxy_bypass(self) -> None:
        """回环地址：既关重试，也保持「本地服务不经系统代理」这个既有修复。"""
        provider = self._provider("http://127.0.0.1:8080/v1")
        self.assertIsNotNone(provider._owned_http_client)
        self.assertFalse(provider._owned_http_client._trust_env)
        self.assertEqual(provider._get_client().max_retries, 0)

    def test_fallback_model_also_disables_sdk_retries(self) -> None:
        """fallback 是第三个构造点，同样不能留 SDK 默认值。"""
        import runtime.provider_gateway as pg

        with mock.patch.dict(os.environ, {
            "FORGE_FALLBACK_MODEL": "fb-model",
            "FORGE_FALLBACK_BASE_URL": "https://fb.example/v1",
            "FORGE_FALLBACK_API_KEY": "fb-key",
        }):
            provider = self._provider("https://gateway.example/v1")
            fallback = provider._get_fallback_model()
        self.assertIsNotNone(fallback, "fallback 未构造成功，用例失去意义")
        client = getattr(fallback, "_client", None)
        self.assertIsNotNone(client)
        self.assertEqual(client.max_retries, 0)

    def test_env_override_is_honored(self) -> None:
        """关闭是默认，但不是硬编码：允许显式调回（便于对照实验）。"""
        with mock.patch.dict(os.environ, {"FORGE_SDK_MAX_RETRIES": "5"}):
            provider = self._provider("https://gateway.example/v1")
            self.assertEqual(provider._get_client().max_retries, 5)

    def test_attempt_records_self_certify_the_sdk_factor(self) -> None:
        """每条尝试记录都要自证「这次尝试 = 几次 HTTP」。

        否则「36 次 HTTP」这类结论只能靠读源码推，无法从审计产物里核对。
        """
        import runtime.provider_gateway as pg

        with mock.patch.dict(os.environ, {"FORGE_PROVIDER_ATTEMPTS_PERSIST": "off"}):
            pg._ATTEMPTS.pop("run_sdk_factor", None)
            pg.record_attempt("run_sdk_factor", {"kind": "success"})
            record = pg._ATTEMPTS["run_sdk_factor"][-1]
            pg._ATTEMPTS.pop("run_sdk_factor", None)
        self.assertEqual(record["sdk_http_attempts"], 1,
                         "SDK 关掉重试后每次尝试应恰好 1 次 HTTP")
        self.assertEqual(record["provider_attempt_budget"], 6)


class LayeredTimeoutTests(unittest.TestCase):
    """B3（P1-15）：HTTP 超时必须显式声明，且 read 不得与总预算等值。

    回归背景：SDK 默认 ``httpx2.Timeout(timeout=600, connect=5.0)``，而
    ``FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS`` 默认也是 600 —— 一次挂死请求就能吃满
    整个 provider 预算，而总墙钟只在两次尝试之间检查，等于没有兜底。
    """

    def test_read_timeout_is_a_fraction_of_the_total_budget(self) -> None:
        import runtime.provider_gateway as pg

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_PROVIDER_READ_TIMEOUT_SECONDS", None)
            os.environ.pop("FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS", None)
            timeout = pg._http_timeout()
            total = pg._total_timeout_seconds()
        self.assertEqual(timeout.connect, 5.0)
        self.assertLess(timeout.read, total,
                        "read == 总预算 ⇒ 单次挂死请求吃光全部重试额度")
        self.assertAlmostEqual(timeout.read, total / 3.0)
        self.assertEqual(timeout.write, timeout.read)
        self.assertEqual(timeout.pool, timeout.connect)

    def test_every_layer_is_overridable(self) -> None:
        import runtime.provider_gateway as pg

        with mock.patch.dict(os.environ, {
            "FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS": "90",
            "FORGE_PROVIDER_CONNECT_TIMEOUT_SECONDS": "1.5",
            "FORGE_PROVIDER_READ_TIMEOUT_SECONDS": "12",
            "FORGE_PROVIDER_WRITE_TIMEOUT_SECONDS": "13",
            "FORGE_PROVIDER_POOL_TIMEOUT_SECONDS": "14",
        }):
            timeout = pg._http_timeout()
        self.assertEqual((timeout.connect, timeout.read, timeout.write, timeout.pool),
                         (1.5, 12.0, 13.0, 14.0))

    def test_disabled_total_budget_keeps_read_bounded(self) -> None:
        """总预算关闭（0=不限）时，read 仍必须是有界值，不能退回「无限等待」。"""
        import runtime.provider_gateway as pg

        with mock.patch.dict(os.environ, {"FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS": "0"}):
            timeout = pg._http_timeout()
        self.assertGreater(timeout.read, 0)

    def test_client_carries_the_declared_timeout(self) -> None:
        """声明必须真的落到 client 上（只写在常量表里等于没声明）。"""
        import runtime.provider_gateway as pg

        with mock.patch.dict(os.environ, {
            "FORGE_PROVIDER_CONNECT_TIMEOUT_SECONDS": "2",
            "FORGE_PROVIDER_READ_TIMEOUT_SECONDS": "77",
        }):
            provider = pg.ResilientProvider(
                api_key="test", base_url="https://gateway.example/v1", use_responses=False)
            timeout = provider._get_client().timeout
        self.assertEqual(timeout.connect, 2.0)
        self.assertEqual(timeout.read, 77.0)

    def test_config_summary_declares_the_layering(self) -> None:
        """状态页必须能看到分层声明（P1-15 的「显式」落点）。"""
        import runtime.provider_gateway as pg

        summary = pg.config_summary()
        self.assertEqual(summary["sdk_max_retries"], "0")
        self.assertEqual(summary["provider_max_attempts"], "3")
        self.assertEqual(summary["provider_max_total_attempts"], "6")
        self.assertIn("重试=", summary["text"])
        self.assertIn("超时=", summary["text"])


class _AlwaysFailModel:
    """永远失败（可重试类）的模型替身，只数调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        raise ProviderTransportError(NO_CHANNEL, status=503)


class _AlwaysFailStreamModel:
    """永远失败（建流即失败，仍属「首 token 前」）的流式模型替身。"""

    def __init__(self) -> None:
        self.calls = 0

    def stream_response(self, *args, **kwargs):
        self.calls += 1
        raise ProviderTransportError(NO_CHANNEL, status=503)


class _BudgetGatewayStub:
    """提供固定 fallback 的网关替身（隔离出「次数预算」这一个变量）。"""

    def __init__(self, fallback) -> None:
        self._fb = fallback

    def _fallback_config(self):
        return {"model": "fb", "base_url": None, "api_key": "k", "distinct": True}

    def _get_fallback_model(self):
        return self._fb


class TotalAttemptBudgetTests(unittest.TestCase):
    """B2（P0-8）：primary + fallback 必须合并计数，不能被重置成两套额度。

    回归背景：``local_attempts`` 在切到 fallback 时被重置为 0，于是「每个 provider
    6 次」变成「合计 12 次」；再乘 SDK 的 3 与格式闸的 2 就是报告里的 36 次 HTTP。
    另一个隐蔽问题是天花板写在两处：代码里是 ``< 6``，真正生效的却是
    ``provider_errors.MAX_RETRIES``（最大 2 ⇒ 3 次尝试），两者各自漂移。

    这里把 ``retry_policy`` 替换成「永远可以重试」，让**只有**本模块的预算能终止循环
    —— 天花板必须由本模块自己保证，不能靠策略表碰巧取到什么值。
    """

    def _drive(self, env: dict, streaming: bool = False):
        import asyncio as _aio

        import runtime.provider_gateway as pg
        from runtime.runctx import RunContext, bind as _bind, current as _cur

        primary = _AlwaysFailStreamModel() if streaming else _AlwaysFailModel()
        fallback = _AlwaysFailStreamModel() if streaming else _AlwaysFailModel()
        prev = _cur()
        self.addCleanup(_bind, prev)
        scope = {"FORGE_PROVIDER_ATTEMPTS_PERSIST": "off", **env}
        captured: dict = {}
        with mock.patch.dict(os.environ, scope), \
                mock.patch.object(pg, "retry_policy", lambda *a, **k: 0.0):
            _bind(RunContext(run_id="run_b2_budget", request_text="x"))
            pg._ATTEMPTS.pop("run_b2_budget", None)
            model = pg.ResilientModel(primary, _BudgetGatewayStub(fallback))

            async def _consume():
                if not streaming:
                    return await model.get_response(messages=[])
                async for _ in model.stream_response(messages=[]):  # pragma: no cover
                    pass
                return None

            try:
                _aio.run(_consume())
            except Exception as exc:  # noqa: BLE001 - 期望失败，只为拿到分类与文案
                captured["exc"] = exc
            captured["records"] = list(pg._ATTEMPTS.get("run_b2_budget", []))
            pg._ATTEMPTS.pop("run_b2_budget", None)
        self.assertIn("exc", captured, "两个 provider 都必然失败，应当上抛")
        return primary.calls, fallback.calls, captured["records"], captured["exc"]

    def test_default_ceiling_is_six_http_calls_in_total(self) -> None:
        """默认配置下最坏 6 次 HTTP：primary 3 + fallback 3，且顺序是先 primary。"""
        primary_calls, fallback_calls, records, _ = self._drive({})
        self.assertEqual(primary_calls, 3, "单 provider 上限应是 3 次尝试")
        self.assertEqual(fallback_calls, 3, "fallback 必须仍有机会（不能把额度全给 primary）")
        self.assertEqual(primary_calls + fallback_calls, 6)
        self.assertTrue(all(r.get("sdk_http_attempts") == 1 for r in records))

    def test_total_budget_wins_over_per_provider_cap(self) -> None:
        """总预算先耗尽时立即收口，不得为了「凑满每个 provider」继续打。"""
        primary_calls, fallback_calls, records, exc = self._drive({
            "FORGE_PROVIDER_MAX_ATTEMPTS": "6",
            "FORGE_PROVIDER_MAX_TOTAL_ATTEMPTS": "4",
        })
        self.assertEqual(primary_calls, 4)
        self.assertEqual(fallback_calls, 0, "总预算已耗尽，不允许再开新的 provider")
        budget = [r for r in records if r.get("kind") == "attempt_budget_exhausted"]
        self.assertEqual(len(budget), 1, "预算耗尽必须留下且只留下一条记录")
        self.assertEqual(budget[0].get("attempt_budget"), 4)
        self.assertEqual(budget[0].get("total_attempts"), 4)
        public = getattr(exc, "provider_public", "") or ""
        self.assertIn("已连续尝试 4 次", public)
        self.assertEqual(getattr(exc, "provider_kind", None), ProviderErrorKind.NO_CHANNEL,
                         "预算耗尽不应篡改最后一次失败的真实分类")

    def test_per_provider_cap_counts_attempts_not_retries(self) -> None:
        """``FORGE_PROVIDER_MAX_ATTEMPTS=2`` 就是 2 次尝试（含首次），不是 3 次。"""
        primary_calls, fallback_calls, _, _ = self._drive({
            "FORGE_PROVIDER_MAX_ATTEMPTS": "2",
            "FORGE_PROVIDER_MAX_TOTAL_ATTEMPTS": "100",
        })
        self.assertEqual((primary_calls, fallback_calls), (2, 2))

    def test_streaming_path_obeys_the_same_budget(self) -> None:
        """流式是生产主路径（``mode=stream``），预算必须同样生效。

        报告里最容易漏掉的一环：非流式写对了、流式忘了改，症状只在真实使用中才出现。
        """
        primary_calls, fallback_calls, records, exc = self._drive({
            "FORGE_PROVIDER_MAX_ATTEMPTS": "6",
            "FORGE_PROVIDER_MAX_TOTAL_ATTEMPTS": "4",
        }, streaming=True)
        self.assertEqual(primary_calls, 4)
        self.assertEqual(fallback_calls, 0)
        self.assertTrue(any(r.get("kind") == "attempt_budget_exhausted" for r in records),
                        "流式路径也必须留下预算耗尽的记录")
        self.assertIn("已连续尝试 4 次", getattr(exc, "provider_public", "") or "")

    def test_budget_does_not_break_the_success_path(self) -> None:
        """预算只是上限：第一次就成功时行为必须与从前完全一致。"""

        class _OkModel:
            def __init__(self):
                self.calls = 0

            async def get_response(self, *args, **kwargs):
                self.calls += 1
                return "ok"

        import asyncio as _aio

        import runtime.provider_gateway as pg

        inner = _OkModel()
        model = pg.ResilientModel(inner, _BudgetGatewayStub(None))
        out = _aio.run(model.get_response(messages=[]))
        self.assertEqual(out, "ok")
        self.assertEqual(inner.calls, 1)


class ModelTurnAccountingTests(unittest.TestCase):
    """B7 接线回归：轮次计数的钩子必须认得生产侧真正写出来的 kind。

    回归背景：hook 判据写的是 ``kind == "ok"``，而生产侧 ``record_attempt`` 一律写
    ``"success"`` —— 于是 ``note_model_turn()`` 从未被调用过，``RunContext.model_turns``
    恒为 0，``tool_calls.turn_number`` 这一列虽然迁移加了、却永远取不到值（静默死列）。
    """

    def _ctx(self):
        from runtime.runctx import RunContext, bind as _bind, current as _cur

        prev = _cur()
        self.addCleanup(_bind, prev)
        ctx = RunContext(run_id="run_turn_acc", request_text="x")
        _bind(ctx)
        return ctx

    def test_only_successful_responses_count_as_a_turn(self) -> None:
        import runtime.provider_gateway as pg

        ctx = self._ctx()
        with mock.patch.dict(os.environ, {"FORGE_PROVIDER_ATTEMPTS_PERSIST": "off"}):
            pg._ATTEMPTS.pop("run_turn_acc", None)
            for kind in ("cancelled", "deadline_exceeded", "attempt_budget_exhausted",
                         "interrupted", "exhausted", "unavailable"):
                pg.record_attempt("run_turn_acc", {"kind": kind})
            self.assertEqual(ctx.model_turns, 0, "失败/重试不产出模型输出，不算一轮")
            pg.record_attempt("run_turn_acc", {"kind": "success"})
            pg.record_attempt("run_turn_acc", {"kind": "ok"})  # 历史/兜底写法
            pg._ATTEMPTS.pop("run_turn_acc", None)
        self.assertEqual(ctx.model_turns, 2,
                         "成功的尝试必须计入轮次（'success' 与 'ok' 都要认）")


class FormatRerunBudgetTests(unittest.TestCase):
    """B2（P0-8）L3：格式闸重跑次数必须显式且真的被遵守。

    回归背景：``while attempt < 3`` 配 ``if attempt == 1: raise`` —— 读起来像有 3 次
    机会，实际只有 1 次重跑（第 3 次永远到不了）。这种「声明与实现不一致」会让所有
    关于最坏耗时的推算都失去依据。
    """

    def test_default_is_one_rerun(self) -> None:
        import main as main_module

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_FORMAT_RETRY_MAX", None)
            self.assertEqual(main_module._format_retry_max(), 1)

    def test_zero_disables_the_rerun(self) -> None:
        import main as main_module

        with mock.patch.dict(os.environ, {"FORGE_FORMAT_RETRY_MAX": "0"}):
            self.assertEqual(main_module._format_retry_max(), 0)

    def test_garbage_env_falls_back_to_default(self) -> None:
        import main as main_module

        with mock.patch.dict(os.environ, {"FORGE_FORMAT_RETRY_MAX": "abc"}):
            self.assertEqual(main_module._format_retry_max(), 1)

    def _drive_execute_turn(self, env: dict) -> int:
        """让每轮都触发格式闸，返回实际尝试次数。"""
        import asyncio as _aio

        import main as main_module

        calls: list[int] = []

        async def fake_attempt(*args, **kwargs):
            calls.append(1)
            raise _tripwire()

        async def fake_ensure_mcp():
            return None

        with mock.patch.dict(os.environ, env), \
                mock.patch.object(runtime_execution, "_run_attempt", fake_attempt), \
                mock.patch.object(runtime_execution, "ensure_mcp", fake_ensure_mcp):
            with self.assertRaises(main_module.FinalResponseFailed):
                _aio.run(runtime_execution.execute_turn("stream", "写点东西"))
        return len(calls)

    def test_execute_turn_reruns_exactly_once_by_default(self) -> None:
        self.assertEqual(self._drive_execute_turn({}), 2, "默认 1 次重跑 = 首发 + 1")

    def test_execute_turn_honors_zero(self) -> None:
        """设为 0 时首轮不过即刻收口（不再产生第二次完整 agent loop 的成本）。"""
        self.assertEqual(self._drive_execute_turn({"FORGE_FORMAT_RETRY_MAX": "0"}), 1)

    def test_execute_turn_honors_a_larger_budget(self) -> None:
        self.assertEqual(self._drive_execute_turn({"FORGE_FORMAT_RETRY_MAX": "3"}), 4)

class CancelScopeRegistryTests(unittest.TestCase):
    """B5（P1-2）：取消登记表本身的行为契约。

    这张表是「取消 = 副作用真的停止」的唯一抓手，所以它的三个性质必须被锁死：
    按 Run 分桶（并发 Run 不得互相误杀）、一次性（句柄不可重复关闭）、
    逐个隔离（一个 killer 失败不得影响其余，也不得掩盖 CancelledError）。
    """

    def setUp(self) -> None:
        from runtime import cancel_scope

        self.cs = cancel_scope
        self.cs.reset_for_tests()
        self.addCleanup(self.cs.reset_for_tests)

    def test_register_and_unregister_roundtrip(self) -> None:
        token = self.cs.register(lambda: None, run_id="run_a")
        self.assertEqual(self.cs.active_count("run_a"), 1)
        self.cs.unregister(token)
        self.assertEqual(self.cs.active_count("run_a"), 0)

    def test_invalid_handler_registers_nothing(self) -> None:
        """非 callable 不得登记（否则取消时要靠异常兜底才发现问题）。"""
        self.assertEqual(self.cs.register(None, run_id="run_a"), 0)  # type: ignore[arg-type]
        self.assertEqual(self.cs.active_count(), 0)

    def test_kill_is_scoped_to_the_cancelled_run(self) -> None:
        """并发 Run 隔离：取消 A 绝不能顺手杀掉 B 正在跑的子进程。"""
        killed: list[str] = []
        self.cs.register(lambda: killed.append("a"), run_id="run_a")
        self.cs.register(lambda: killed.append("b"), run_id="run_b")
        self.assertEqual(self.cs.kill_run("run_a"), 1)
        self.assertEqual(killed, ["a"])
        self.assertEqual(self.cs.active_count("run_b"), 1, "B 的登记项必须留着")

    def test_kill_is_one_shot(self) -> None:
        """重复取消不得重复执行 killer：句柄号可能已被系统复用。"""
        calls: list[int] = []
        self.cs.register(lambda: calls.append(1), run_id="run_a")
        self.assertEqual(self.cs.kill_run("run_a"), 1)
        self.assertEqual(self.cs.kill_run("run_a"), 0)
        self.assertEqual(calls, [1])

    def test_one_failing_killer_does_not_block_the_others(self) -> None:
        """取消路径必须逐个隔离：一个失败不能阻止别的副作用被停掉。"""
        done: list[str] = []

        def _boom() -> None:
            raise RuntimeError("killer 自身故障")

        self.cs.register(_boom, run_id="run_a")
        self.cs.register(lambda: done.append("ok"), run_id="run_a")
        self.assertEqual(self.cs.kill_run("run_a"), 2)
        self.assertEqual(done, ["ok"], "另一个 killer 仍必须被执行")

    def test_run_id_defaults_to_the_current_run(self) -> None:
        from runtime.runctx import RunContext, bind as _bind, current as _cur

        prev = _cur()
        self.addCleanup(_bind, prev)
        _bind(RunContext(run_id="run_bound", request_text="x"))
        self.cs.register(lambda: None)
        self.assertEqual(self.cs.active_count("run_bound"), 1,
                         "登记时未显式给 run_id 应自动认领当前 Run")

    def test_to_thread_worker_can_still_see_the_run_id(self) -> None:
        """B5 的承重假设：worker 线程里必须还能认出「我属于哪个 Run」。

        生产路径是 SDK 的 ``asyncio.to_thread``（同步工具），线程启动时会复制调用点的
        contextvars 上下文。若这条不成立，工具会把 killer 登记到 ``None`` 桶里，
        取消时杀不到任何东西 —— 而且不会有任何报错（静默失效）。
        """
        import asyncio as _aio

        from runtime.runctx import RunContext, bind as _bind, current as _cur

        prev = _cur()
        self.addCleanup(_bind, prev)
        _bind(RunContext(run_id="run_thread", request_text="x"))
        seen = _aio.run(_aio.to_thread(self.cs.current_run_id))
        self.assertEqual(seen, "run_thread")


class SyncToolCancellationTests(unittest.TestCase):
    """B5（P1-2）：取消必须真的让同步工具的子进程树停下。

    回归背景：``run_tests`` / ``run_python`` 是 ``def``，经 SDK 的
    ``asyncio.to_thread`` 执行。``task.cancel()`` 只取消 awaiting 的 Future，
    worker 线程与子进程继续跑完 —— 用户看到「本轮任务已被取消」，写文件 / 跑测试的
    副作用却照样落盘。**取消语义对用户是谎言**。

    这里跑真实子进程（本地 python，不联网），验证：登记 → 取消 → 进程树死掉 →
    worker 线程立刻返回，而不是等它自然跑完 30s。
    """

    def setUp(self) -> None:
        from runtime import cancel_scope

        self.cs = cancel_scope
        self.cs.reset_for_tests()
        self.addCleanup(self.cs.reset_for_tests)
        self._tmp = Path(tempfile.mkdtemp(prefix="phase_b_b5_"))
        self._orig_root = code_exec.SANDBOX_ROOT
        self._orig_env = os.environ.get("ALLOW_CODE_EXEC")
        code_exec.SANDBOX_ROOT = self._tmp / "code_sandbox"
        code_exec.SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        os.environ["ALLOW_CODE_EXEC"] = "true"

    def tearDown(self) -> None:
        code_exec.SANDBOX_ROOT = self._orig_root
        if self._orig_env is None:
            os.environ.pop("ALLOW_CODE_EXEC", None)
        else:
            os.environ["ALLOW_CODE_EXEC"] = self._orig_env

    def test_cancel_kills_the_child_process_of_a_running_sync_tool(self) -> None:
        import threading
        import time as _time

        from runtime.runctx import RunContext, bind as _bind

        run_id = "run_b5_sync"
        result: dict = {}

        def _work() -> None:
            # 线程内绑定 Run 上下文（生产里由 asyncio.to_thread 复制上下文完成）
            _bind(RunContext(run_id=run_id, request_text="跑个长任务"))
            result["out"] = code_exec.run_python_impl(
                project="demo", code="import time; time.sleep(60)", timeout=120)

        worker = threading.Thread(target=_work, daemon=True)
        started = _time.monotonic()
        worker.start()

        # 等子进程真的起来并完成登记（最多 10s）
        deadline = started + 10
        while _time.monotonic() < deadline and self.cs.active_count(run_id) == 0:
            _time.sleep(0.05)
        self.assertEqual(self.cs.active_count(run_id), 1,
                         "同步工具在等子进程时必须登记 killer，否则取消无从下手")

        killed = self.cs.kill_run(run_id)
        self.assertEqual(killed, 1)
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive(),
                         "取消后 worker 线程必须立刻返回（子进程死了，communicate 就该返回）")
        self.assertLess(_time.monotonic() - started, 20,
                        "取消没有缩短等待：说明进程树没有真的被杀掉")
        self.assertNotIn("运行超时", result.get("out", ""),
                         "这是取消路径，不应被误判成工具自身超时")
        self.assertEqual(self.cs.active_count(run_id), 0,
                         "工具结束必须注销登记（否则登记表无界增长）")

    def test_runner_cancel_run_stops_registered_side_effects(self) -> None:
        """``AgentRuntime.cancel_run`` 必须先杀树：CancelledError 要等到最内层 await
        才抛出，而同步工具正阻塞在 worker 线程里 —— 只标记不动手，副作用就停不下来。"""
        from runtime.runner import AgentRuntime

        killed: list[int] = []
        self.cs.register(lambda: killed.append(1), run_id="run_b5_runner")
        runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        # 没有在飞 asyncio.Task → 返回 False（由调用方决定 DB 收口），但杀树必须先发生
        self.assertFalse(runtime.cancel_run("run_b5_runner"))
        self.assertEqual(killed, [1], "cancel_run 未停掉已登记的子进程树")

    def test_kill_helper_swallows_errors(self) -> None:
        """取消路径上多抛一个异常，就会把真正的取消原因掩盖成别的东西。"""
        from runtime.runner import _kill_cancelled_side_effects

        def _boom() -> None:
            raise RuntimeError("boom")

        self.cs.register(_boom, run_id="run_b5_err")
        _kill_cancelled_side_effects("run_b5_err")  # 不得抛出

    def test_cleanup_is_safe_without_any_registration(self) -> None:
        from runtime.runner import _kill_cancelled_side_effects

        _kill_cancelled_side_effects("no_such_run")
        _kill_cancelled_side_effects(None)


if __name__ == "__main__":
    unittest.main()
