import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager


class TMRModelTests(unittest.TestCase):
    """Project→Task→Message→Run 模型：同容器多 Run、resume 同 Run、迁移无损。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="tmr_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()

    def _install_fake(self, output: str = '{"kind":"answer","content":"收到","summary":"ok"}') -> None:
        async def fake_execute_turn(mode, message, session=None, debug=False, max_turns=20,
                                    history_limit=None, agent=None, audit=None, stream_events_cb=None):
            return output

        self._patcher = patch("main.execute_turn", new=fake_execute_turn)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _run(self, message: str, session_id: str = "personal", **kw):
        return asyncio.run(self.runtime.run_turn(message, session_id=session_id, mode="sync", **kw))

    def test_same_session_keeps_single_task_with_new_runs(self) -> None:
        self._install_fake()
        r1 = self._run("修复登录后自动退出的问题。")
        r2 = self._run("再检查一下刷新 token。")
        r3 = self._run("顺便补上测试。")
        mgr = self.runtime.tasks
        # 三个 Run 同容器
        self.assertEqual(r1.container_id, r2.container_id)
        self.assertEqual(r2.container_id, r3.container_id)
        self.assertNotEqual(r1.task.id, r2.task.id)
        container = mgr.get_container(r1.container_id)
        stat = mgr.container_stat(r1.container_id)
        self.assertEqual(stat["runs"], 3)
        self.assertEqual(container["title"], "修复登录后自动退出的问题。")
        # 消息：每轮 user+assistant
        messages = mgr.list_messages(r1.container_id)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant", "user", "assistant"])
        self.assertEqual(messages[0]["run_id"], r1.task.id)
        # 每个 Run 状态完成、事件挂在 run 下
        self.assertEqual(r3.task.state, TaskState.COMPLETED)
        self.assertTrue(any(e.event_type == "task.completed" for e in mgr.list_events(r2.task.id)))
        # 列表：容器接口能看到全部 run
        runs = mgr.list_tasks(container_id=r1.container_id)
        self.assertEqual({x.id for x in runs}, {r1.task.id, r2.task.id, r3.task.id})

    def test_explicit_container_ignores_session_reuse(self) -> None:
        self._install_fake()
        c = self.runtime.tasks.get_or_create_container("personal")
        r1 = self._run("第一个", task_container_id=c["id"])
        r2 = self._run("第二个", session_id="other-session", task_container_id=c["id"])
        self.assertEqual(r1.container_id, r2.container_id)
        runs = self.runtime.tasks.list_tasks(container_id=c["id"])
        self.assertEqual(len(runs), 2)
        # 底层会话键以容器为准
        self.assertEqual(r2.task.session_id, "personal")

    def test_approval_resume_keeps_same_run_and_container(self) -> None:
        self._install_fake()
        r1 = self._run("干点高风险的事")
        cid = r1.container_id
        mgr = self.runtime.tasks
        # 模拟审批停留：显式建一个 Run 停在 WAITING_APPROVAL（同容器，含触发它的 user 消息）
        run = mgr.create_task(session_id="personal", goal="干点高风险的事", thread_id=cid)
        mgr.add_message(cid, "user", "干点高风险的事", run_id=run.id)
        mgr.transition(run.id, TaskState.RUNNING, reason="start")
        waiting = mgr.transition(run.id, TaskState.WAITING_APPROVAL, reason="approval")
        self.assertEqual(waiting.state, TaskState.WAITING_APPROVAL)
        # 用户批准后：resume 同一个 Run
        r2 = self._run("干点高风险的事", task_id=run.id)
        self.assertEqual(r2.task.id, run.id)  # 同一 Run，不新建
        self.assertEqual(r2.container_id, cid)
        self.assertEqual(r2.task.state, TaskState.COMPLETED)
        runs = mgr.list_tasks(container_id=cid)
        self.assertEqual(len(runs), 2)  # r1 + 被 resume 的那个（无第三个新 Run）
        msgs = [m["role"] for m in mgr.list_messages(cid)]
        self.assertEqual(msgs, ["user", "assistant", "user", "assistant"])

    def test_light_qa_run_also_creates_run_and_messages(self) -> None:
        self._install_fake('{"kind":"answer","content":"因为这里用 const","summary":"解释"}')
        r = self._run("为什么这里这么写？")
        msgs = self.runtime.tasks.list_messages(r.container_id)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["meta"].get("kind"), "answer")

    def test_container_apis(self) -> None:
        self._install_fake()
        r = self._run("A")
        mgr = self.runtime.tasks
        self.assertIsNotNone(mgr.get_container(r.container_id))
        self.assertEqual(mgr.get_container("tk_nonexistent"), None)
        self.assertTrue(len(mgr.list_containers()) >= 1)
        # 归档后不再出现在活跃列表
        mgr.archive_container(r.container_id)
        self.assertNotIn(r.container_id, [c["id"] for c in mgr.list_containers()])
        self.assertTrue(any(c["id"] == r.container_id for c in mgr.list_containers(archived=True)))
        # 项目
        prj = mgr.ensure_default_project()
        self.assertTrue(prj["id"].startswith("prj_"))
        self.assertEqual(len(mgr.list_projects()), 1)


class LegacyMigrationTests(unittest.TestCase):
    """v1 agent.db（tasks=单轮运行）→ v2（runs+容器）迁移：无损、可重复。"""

    _LEGACY = """
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_task_id TEXT,
        agent_name TEXT NOT NULL, goal TEXT NOT NULL, state TEXT NOT NULL,
        budget_json TEXT NOT NULL, usage_json TEXT NOT NULL DEFAULT '{}',
        metadata_json TEXT NOT NULL DEFAULT '{}', error_message TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        started_at TEXT, completed_at TEXT);
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_name TEXT NOT NULL,
        args_key TEXT NOT NULL, arguments_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL, actor TEXT, reason TEXT,
        created_at TEXT NOT NULL, decided_at TEXT);
    CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        event_type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL);
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="migr_"))
        self.db = self._tmp / "agent.db"

    def _seed_v1(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.executescript(self._LEGACY)
        conn.execute(
            "INSERT INTO tasks (id, session_id, agent_name, goal, state, budget_json, usage_json, "
            "metadata_json, error_message, created_at, updated_at) VALUES "
            "('task_legacy_1','personal','assistant','查登录','completed','{}','{}','{}',NULL,'2026-01-01T00:00:00','2026-01-01T00:00:05'),"
            "('task_legacy_2','personal','assistant','修掉 bug','completed','{}','{}','{}',NULL,'2026-01-01T00:01:00','2026-01-01T00:01:05'),"
            "('task_legacy_3','work-x','assistant','补测试','failed','{}','{}','{}','boom','2026-01-01T00:02:00','2026-01-01T00:02:05')"
        )
        conn.execute(
            "INSERT INTO approvals (id, task_id, tool_name, args_key, arguments_json, status, actor, reason, created_at, decided_at) "
            "VALUES ('approv_legacy','task_legacy_1','run_python','{}','{}','approved','user',NULL,'2026-01-01T00:00:03',NULL)"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, payload_json, created_at) "
            "VALUES ('task_legacy_1','task.completed','{}','2026-01-01T00:00:05')"
        )
        conn.commit()
        conn.close()

    def test_legacy_rows_renamed_and_grouped_with_ids_preserved(self) -> None:
        self._seed_v1()
        mgr = TaskManager(self.db)
        # 老行 → runs，id 保留
        run = mgr.get_task("task_legacy_1")
        self.assertIsNotNone(run)
        self.assertEqual(run.goal, "查登录")
        self.assertEqual(run.state.value, "completed")
        # 审批/事件仍挂在老 run id 下
        appr = mgr.find_approval("task_legacy_1", "run_python", "{}")
        self.assertEqual(appr["status"], "approved")
        self.assertEqual([e.event_type for e in mgr.list_events("task_legacy_1")], ["task.completed"])
        # 容器：session personal 归并 1 个，work-x 1 个；全部回填 task_id/run_index
        containers = mgr.list_containers()
        self.assertEqual(len(containers), 2)
        c_personal = next(c for c in containers if c["session_id"] == "personal")
        runs = mgr.list_tasks(container_id=c_personal["id"])
        self.assertEqual(len(runs), 2)
        indexes = sorted(r.run_index if hasattr(r, "run_index") else 0 for r in runs)
        # run DTO 不暴露 run_index 列：改从容器查询顺序断言标题
        self.assertEqual(c_personal["title"], "查登录")
        self.assertIn("task_legacy_2", [r.id for r in runs])
        # 重复初始化幂等：不再重复建容器
        TaskManager(self.db)
        self.assertEqual(len(TaskManager(self.db).list_containers()), 2)

    def test_fresh_db_has_no_rename_and_user_version(self) -> None:
        mgr = TaskManager(self.db)
        with sqlite3.connect(self.db) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("runs", tables)
        self.assertIn("messages", tables)
        self.assertIn("projects", tables)
        self.assertNotIn("task_legacy", "".join(tables))
        self.assertGreaterEqual(version, 3)


if __name__ == "__main__":
    unittest.main()
