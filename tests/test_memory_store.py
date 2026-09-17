import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import tools as tools_mod
from runtime.task_manager import TaskManager


def call_tool(tool, **kwargs) -> str:
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=input_json)
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


class MemoryStoreTests(unittest.TestCase):
    """SQLite 记忆后端：结构化列 + 写闸 + 工具消息兼容。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="memory_sql_"))
        self._db = self._tmp / "agent.db"
        self._orig_file = tools_mod._MEMORY_FILE
        self._orig_db = tools_mod._MEMORY_DB_PATH
        self._orig_max = tools_mod._MEMORY_MAX
        tools_mod._MEMORY_FILE = tools_mod._DEFAULT_MEMORY_FILE  # 走 SQLite 分支
        tools_mod._MEMORY_DB_PATH = self._db
        tools_mod._MEMORY_MAX = 25
        self.manager = TaskManager(self._db)

    def tearDown(self) -> None:
        tools_mod._MEMORY_FILE = self._orig_file
        tools_mod._MEMORY_DB_PATH = self._orig_db
        tools_mod._MEMORY_MAX = self._orig_max

    def _remember(self, text: str, tags: str = "") -> str:
        return call_tool(tools_mod.remember, text=text, tags=tags)

    def _recall(self, keyword: str = "") -> str:
        return call_tool(tools_mod.recall_memory, keyword=keyword)

    def _forget(self, entry_id: str) -> str:
        return call_tool(tools_mod.forget_memory, entry_id=entry_id)

    def test_remember_recall_forget_roundtrip(self) -> None:
        out = self._remember("用户偏好简约风格的设计", tags="用户偏好,风格")
        self.assertIn("已记住", out)
        mem_id = re.search(r"mem_[0-9a-z]+", out).group(0)

        recalled = self._recall("简约")
        self.assertIn("简约", recalled)
        self.assertIn(mem_id, recalled)

        deleted = self._forget(mem_id)
        self.assertIn("已删除", deleted)
        after = self._recall("简约")
        self.assertTrue("没有找到" in after or "还没有任何内容" in after, after)

    def test_duplicate_text_updates_in_place(self) -> None:
        self._remember("我喜欢早上 8 点运动", tags="习惯")
        out2 = self._remember("我喜欢早上 8 点运动", tags="运动")
        self.assertIn("已更新已有记忆", out2)
        rows = self.manager.memory_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("运动", rows[0]["tags"])

    def test_structured_columns_written(self) -> None:
        self._remember("我习惯用中文回答", tags="偏好")
        rows = self.manager.memory_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["memory_type"], "preference")
        self.assertAlmostEqual(rows[0]["confidence"], 1.0)
        self.assertEqual(rows[0]["source"], "agent-tool")

    def test_secret_blocked_by_write_gate(self) -> None:
        out = self._remember("这是密钥 sk-" + "a" * 24)
        self.assertIn("密钥", out)
        self.assertEqual(self.manager.memory_rows(), [])

    def test_cap_enforced(self) -> None:
        cap = tools_mod._MEMORY_MAX
        for i in range(cap + 5):
            result = self._remember(f"第{i}条记忆内容占位足够长保证不是空壳")
            if "上限" in result:
                break
        self.assertEqual(len(self.manager.memory_rows()), cap)


class LegacyJsonCompatibilityTests(unittest.TestCase):
    """测试/旧数据兼容：_MEMORY_FILE 被替换为临时 JSON 时行为不变。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="memory_legacy_"))
        self._orig_file = tools_mod._MEMORY_FILE
        self._orig_db = tools_mod._MEMORY_DB_PATH
        tools_mod._MEMORY_FILE = self._tmp / "memory.json"
        tools_mod._MEMORY_DB_PATH = self._tmp / "unused.db"

    def tearDown(self) -> None:
        tools_mod._MEMORY_FILE = self._orig_file
        tools_mod._MEMORY_DB_PATH = self._orig_db

    def test_json_mode_still_works(self) -> None:
        out = call_tool(tools_mod.remember, text="JSON 模式兼容测试内容", tags="测试")
        self.assertIn("已记住", out)
        self.assertTrue(tools_mod._MEMORY_FILE.exists())
        recalled = call_tool(tools_mod.recall_memory, keyword="兼容")
        self.assertIn("兼容", recalled)


if __name__ == "__main__":
    unittest.main()
