"""Project 产品模型重构（v3）离线测试：Project/Message/Run、Sources、记忆边界、WorkLocation。

测试 7/8（记忆边界）验证 tools 层 binding：project_only 不得读到全局/其他项目；global 可读全局。
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import tools as tools_mod
from agents.tool_context import ToolContext
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager


def call_tool(tool, **kwargs) -> str:
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t",
                          tool_arguments=input_json)
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


class ProjectModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="pmodel_"))
        self.db = self._tmp / "agent.db"
        self.mgr = TaskManager(self.db)

    def test_01_create_project_without_work_location(self) -> None:
        # 直接走容器创建 + update（与 /api/projects/create 同构）
        c = self.mgr.get_or_create_container("proj-demo-1", title="银行流水整理")
        c = self.mgr.update_project(c["id"], memory_scope="project_only", instructions="无WL也能用")
        self.assertIsNone(c["work_location_id"])
        self.assertEqual(c["memory_scope"], "project_only")
        self.assertIn("无WL也能用", c["instructions"])

    def _install_fake(self, output='{"kind":"answer","summary":"s","content":"ok"}'):
        async def fake(mode, message, session=None, debug=False, max_turns=20,
                       history_limit=None, agent=None, audit=None, stream_events_cb=None):
            return output
        self._patcher = patch("main.execute_turn", new=fake)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _run(self, message, project_id, session_id="personal"):
        rt = AgentRuntime(db_path=str(self.db))
        rt._ensure()
        return asyncio.run(rt.run_turn(message, session_id=session_id,
                                       task_container_id=project_id, mode="sync"))

    def test_02_03_messages_stay_in_one_project_with_new_runs(self) -> None:
        self._install_fake()
        c = self.mgr.get_or_create_container("proj-demo-2", title="修复登录问题")
        pid = c["id"]
        r1 = self._run("帮我检查 UI", pid)
        r2 = self._run("再生成一份报告", pid)
        self.assertEqual(r1.container_id, pid)
        self.assertEqual(r2.container_id, pid)
        runs = self.mgr.list_tasks(container_id=pid)
        self.assertEqual(len(runs), 2)
        roles = [m["role"] for m in self.mgr.list_messages(pid)]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        projects = self.mgr.list_containers()
        self.assertEqual(len([p for p in projects if p["id"] == pid]), 1)  # 没有新项目

    def test_04_05_source_persist_and_delete_consistency(self) -> None:
        from runtime.task_manager import project_sources_dir

        c = self.mgr.get_or_create_container("proj-src", title="合同分析")
        pid = c["id"]
        dest = project_sources_dir(pid)
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "合同.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        row = self.mgr.add_project_source(task_id=pid, display_name="合同.pdf", stored_path=str(f),
                                          size_bytes=f.stat().st_size, sha256="deadbeef")
        # 重启（新 TaskManager 实例重开库）
        mgr2 = TaskManager(self.db)
        rows = mgr2.list_project_sources(pid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "合同.pdf")
        self.assertTrue(Path(rows[0]["stored_path"]).exists())
        # 删除一致性：DB 行 + 文件
        deleted = mgr2.delete_project_source(row["id"])
        self.assertIsNotNone(deleted)
        self.assertFalse(f.exists())
        self.assertEqual(len(mgr2.list_project_sources(pid)), 0)

    def test_06_artifacts_separated_from_sources(self) -> None:
        c = self.mgr.get_or_create_container("proj-art", title="报告项目")
        pid = c["id"]
        run = self.mgr.create_task(session_id="proj-art", goal="生成报告", thread_id=pid)
        self.mgr.register_artifact(task_id=run.id, session_id="x", name="报告.md", kind="markdown",
                                   storage_path=str(self._tmp / "报告.md"), sha256="1", size_bytes=2)
        src_dir_dir = (Path(__file__).resolve().parents[1] / "forge_data" / "projects" / pid / "sources")
        src_dir_dir.mkdir(parents=True, exist_ok=True)
        sf = src_dir_dir / "输入.docx"
        sf.write_bytes(b"xx")
        self.mgr.add_project_source(task_id=pid, display_name="输入.docx", stored_path=str(sf), size_bytes=2)
        arts = self.mgr.list_project_artifacts(pid)
        self.assertEqual(len(arts), 1)
        self.assertNotEqual(arts[0]["name"], "输入.docx")
        self.assertEqual(arts[0]["name"], "报告.md")
        srcs = self.mgr.list_project_sources(pid)
        self.assertEqual([s["display_name"] for s in srcs], ["输入.docx"])


class MemoryBoundaryTests(unittest.TestCase):
    """测试 7/8：project_only 不得读全局/其他项目；global 可读全局。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="mbound_"))
        self.db = self._tmp / "agent.db"
        self.mgr = TaskManager(self.db)
        tools_mod._MEMORY_DB_PATH = self.db
        self._orig_binding = dict(tools_mod._MEMORY_BINDING)
        tools_mod.clear_active_memory_binding()

    def tearDown(self) -> None:
        tools_mod._MEMORY_BINDING.update(self._orig_binding)

    def _project(self, name, scope):
        c = self.mgr.get_or_create_container("proj-" + name[:6] + "-x", title=name)
        self.mgr.update_project(c["id"], memory_scope=scope)
        return c["id"]

    def test_07_project_only_cannot_see_global_or_other_project(self) -> None:
        pa = self._project("项目A", "project_only")
        pb = self._project("项目B", "project_only")
        # 全局记忆（personal）
        tools_mod.set_active_memory_binding(None, None)  # legacy 写全局
        out = call_tool(tools_mod.remember, text="全局偏好：喜欢深色主题")
        self.assertIn("已记住", out)
        # 项目B写一条
        tools_mod.set_active_memory_binding(pb, "project_only")
        call_tool(tools_mod.remember, text="B项目的密钥约定")
        # 项目A（project_only）只应看到自己的
        tools_mod.set_active_memory_binding(pa, "project_only")
        call_tool(tools_mod.remember, text="A项目：登录流程说明")
        text = call_tool(tools_mod.recall_memory, keyword="")
        self.assertIn("A项目：登录流程说明", text)
        self.assertNotIn("B项目", text)
        self.assertNotIn("全局偏好", text)
        # 明确关键词也不返回其他项目/全局
        text2 = call_tool(tools_mod.recall_memory, keyword="全局偏好")
        self.assertNotIn("全局偏好", text2)
        text3 = call_tool(tools_mod.recall_memory, keyword="B项目")
        self.assertNotIn("B项目", text3)

    def test_08_global_scope_can_read_global(self) -> None:
        tools_mod.set_active_memory_binding(None, None)
        call_tool(tools_mod.remember, text="用户城市偏好：杭州")
        pa = self._project("项目C", "global")
        tools_mod.set_active_memory_binding(pa, "global")
        text = call_tool(tools_mod.recall_memory, keyword="城市")
        self.assertIn("杭州", text)

    def test_07b_remember_project_only_not_written_globally(self) -> None:
        pa = self._project("项目D", "project_only")
        tools_mod.set_active_memory_binding(pa, "project_only")
        call_tool(tools_mod.remember, text="D内部规格")
        tools_mod.set_active_memory_binding(None, None)
        text = call_tool(tools_mod.recall_memory, keyword="D内部规格")
        self.assertNotIn("D内部规格", text)
        rows = self.mgr.list_project_memories(pa)
        self.assertEqual(len(rows), 1)


class ProjectContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="pctx_"))
        self.mgr = TaskManager(self._tmp / "agent.db")
        self.rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.rt._ensure()

    def _ctx(self, project, source=False, memory=False):
        pid = project["id"]
        if source:
            from runtime.task_manager import project_sources_dir

            d = project_sources_dir(pid)
            d.mkdir(parents=True, exist_ok=True)
            f = d / "来源.md"
            f.write_text("# 内容", encoding="utf-8")
            self.mgr.add_project_source(task_id=pid, display_name="来源.md", stored_path=str(f))
        if memory:
            self.mgr.add_project_memory(pid, "用户要求尽量简洁")
        return self.rt._project_context_block(pid, self.mgr.get_container(pid))

    def test_project_only_context_injects_scope(self) -> None:
        c = self.mgr.get_or_create_container("proj-ctx1", title="学习平台")
        self.mgr.update_project(c["id"], memory_scope="project_only",
                                instructions="面向普通用户设计。")
        ctx = self._ctx(c, source=True, memory=True)
        self.assertIn("学习平台", ctx)
        self.assertIn("面向普通用户设计", ctx)
        self.assertIn("仅此项目", ctx)
        self.assertIn("来源.md", ctx)
        self.assertIn("用户要求尽量简洁", ctx)

    def test_global_scope_and_work_location_lines(self) -> None:
        c = self.mgr.get_or_create_container("proj-ctx2", title="重构项目")
        self.mgr.update_project(c["id"], memory_scope="global")
        wl = self.mgr.create_work_location("工作区", r"D:\Work\repo")
        self.mgr.update_project(c["id"], work_location_id=wl["id"])
        ctx = self._ctx(c)
        self.assertIn("使用全局记忆", ctx)
        self.assertIn(r"D:\Work\repo", ctx)


if __name__ == "__main__":
    unittest.main()
