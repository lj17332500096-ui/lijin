"""Regression checks for the memory contract fixes."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.tool_context import ToolContext

import tools as tools_mod
from runtime.episode_store import EpisodeStore
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager
from runtime.tool_router import select_tool_names


def call_tool(tool, **kwargs) -> str:
    payload = json.dumps(kwargs, ensure_ascii=False)

    async def invoke() -> str:
        context = ToolContext(
            context=None, tool_name=tool.name, tool_call_id="memory-contract-test",
            tool_arguments=payload,
        )
        result = tool.on_invoke_tool(context, payload)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(invoke())


class MemoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory_contract_")
        self.db = Path(self.temp.name) / "agent.db"
        self.old_file = tools_mod._MEMORY_FILE
        self.old_db = tools_mod._MEMORY_DB_PATH
        tools_mod._MEMORY_FILE = tools_mod._DEFAULT_MEMORY_FILE
        tools_mod._MEMORY_DB_PATH = self.db
        tools_mod.clear_active_memory_binding()
        self.manager = TaskManager(self.db)

    def tearDown(self) -> None:
        tools_mod._MEMORY_FILE = self.old_file
        tools_mod._MEMORY_DB_PATH = self.old_db
        tools_mod.clear_active_memory_binding()
        self.temp.cleanup()

    def test_global_switch_blocks_memory_tools_and_router(self) -> None:
        self.manager.memory_upsert_row(
            {"id": "mem_seed", "text": "用户偏好深色主题", "tags": ["偏好"]}
        )
        with patch.dict("os.environ", {"FORGE_MEMORY_ENABLED": "0"}):
            self.assertIn("已关闭", call_tool(tools_mod.remember, text="新的偏好内容测试"))
            self.assertIn("已关闭", call_tool(tools_mod.recall_memory, keyword="深色"))
            self.assertIn("已关闭", call_tool(tools_mod.forget_memory, entry_id="mem_seed"))
            selected = select_tool_names(
                "查看我的记忆", ["remember", "recall_memory", "forget_memory"]
            )
        self.assertEqual(selected, [])
        self.assertEqual(len(self.manager.memory_rows()), 1)

    def test_global_project_scope_writes_one_project_copy_and_updates_tags(self) -> None:
        project = self.manager.get_or_create_container("global-memory-project", title="项目甲")
        self.manager.update_project(project["id"], memory_scope="global")
        tools_mod.set_active_memory_binding(project["id"], "global")

        first = call_tool(tools_mod.remember, text="用户偏好先列提纲", tags="偏好")
        second = call_tool(tools_mod.remember, text="用户偏好先列提纲", tags="写作")

        rows = self.manager.list_project_memories(project["id"])
        self.assertIn("当前项目记忆", first)
        self.assertIn("当前项目记忆", second)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], ["偏好", "写作"])
        self.assertIn("用户偏好先列提纲", call_tool(tools_mod.recall_memory, keyword="提纲"))

    def test_forget_enforces_current_project_and_scope(self) -> None:
        project_a = self.manager.get_or_create_container("memory-project-a", title="项目甲")
        project_b = self.manager.get_or_create_container("memory-project-b", title="项目乙")
        self.manager.update_project(project_a["id"], memory_scope="project_only")
        self.manager.update_project(project_b["id"], memory_scope="global")
        global_row = self.manager.add_project_memory(project_b["id"], "项目乙内部约定")
        tools_mod.set_active_memory_binding(project_a["id"], "project_only")
        self.assertIn("不能删除", call_tool(tools_mod.forget_memory, entry_id=global_row["id"]))
        self.assertEqual(len(self.manager.list_project_memories(project_b["id"])), 1)

        own_row = self.manager.add_project_memory(project_a["id"], "项目甲自己的约定")
        self.assertIn("已删除", call_tool(tools_mod.forget_memory, entry_id=own_row["id"]))

    def test_disabled_memory_is_not_injected_into_project_context(self) -> None:
        project = self.manager.get_or_create_container("memory-context-project", title="记忆上下文")
        self.manager.add_project_memory(project["id"], "私密的项目记忆短语")
        runtime = AgentRuntime(db_path=str(self.db))
        runtime._ensure()

        with patch.dict("os.environ", {"FORGE_MEMORY_ENABLED": "0"}):
            context = runtime._project_context_block(
                project["id"], self.manager.get_container(project["id"])
            )
        self.assertIn("已关闭", context)
        self.assertNotIn("私密的项目记忆短语", context)

    def test_terminal_event_automatically_ingests_episode(self) -> None:
        run = self.manager.create_task("episode-auto", "读取 README 并总结项目结构")
        self.manager.transition(run.id, TaskState.RUNNING, reason="test")
        self.manager.transition(run.id, TaskState.COMPLETED, reason="test")

        with patch.dict("os.environ", {"FORGE_MEMORY_ENABLED": "1", "EPISODE_INGEST": "1"}):
            self.manager.add_terminal_event(
                run.id, TaskState.COMPLETED,
                {"kind": "completed", "state": "completed", "error": ""},
            )

        self.assertEqual(EpisodeStore(self.db).stats()["total"], 1)


if __name__ == "__main__":
    unittest.main()
