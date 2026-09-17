import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import tools


def call_tool(tool, **kwargs) -> str:
    """调用被 @function_tool 包装的工具对象。"""
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="test_call",
            tool_arguments=input_json,
        )
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


class CalculateToolTests(unittest.TestCase):
    def test_basic_arithmetic(self) -> None:
        self.assertIn("= 14", call_tool(tools.calculate, expression="2 + 3 * 4"))

    def test_functions_and_constants(self) -> None:
        self.assertIn("= 4", call_tool(tools.calculate, expression="sqrt(16)"))
        self.assertIn("= 2", call_tool(tools.calculate, expression="round(2.4)"))

    def test_code_injection_rejected(self) -> None:
        out = call_tool(tools.calculate, expression="__import__('os').system('dir')")
        self.assertNotIn("执行成功", out)
        self.assertTrue("无法计算" in out or "错误" in out or "不支持" in out)

    def test_deep_nesting_rejected(self) -> None:
        # 左结合的深层二叉表达式会触发递归深度上限
        expr = "+".join(["1"] * 200)
        out = call_tool(tools.calculate, expression=expr)
        self.assertNotIn("= 200", out)
        self.assertIn("无法计算", out)


class WorkspaceFileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._last_repeat_calls.clear()

    def test_list_hides_env_and_venv(self) -> None:
        out = call_tool(tools.list_workspace_files, directory="my_creative_agent")
        self.assertIn("目录:", out)
        self.assertNotIn("\n.env", out)
        self.assertNotIn(".venv", out)

    def test_path_escape_rejected(self) -> None:
        out = call_tool(tools.read_workspace_file, path="../../Windows/win.ini")
        self.assertIn("只能读取工作区", out)

    def test_dotenv_rejected(self) -> None:
        out = call_tool(tools.read_workspace_file, path="my_creative_agent/.env")
        self.assertIn("不允许读取", out)

    def test_missing_file_reports_error(self) -> None:
        out = call_tool(tools.read_workspace_file, path="my_creative_agent/不存在_xyz.md")
        self.assertIn("找不到", out)

    def test_reads_text_file(self) -> None:
        out = call_tool(tools.read_workspace_file, path="my_creative_agent/README.md", max_chars=300)
        self.assertIn("文件:", out)
        self.assertIn("全能助手", out)

    def test_repeat_list_call_reminds_model(self) -> None:
        first = call_tool(tools.list_workspace_files, directory="my_creative_agent")
        second = call_tool(tools.list_workspace_files, directory="my_creative_agent")
        self.assertIn("目录:", first)
        self.assertIn("提醒", second)
        # 不同参数不受影响
        other = call_tool(tools.list_workspace_files, directory="my_creative_agent/tests")
        self.assertIn("目录:", other)


class LongTermMemoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="agent_mem_test_"))
        self._orig_file = tools._MEMORY_FILE
        tools._MEMORY_FILE = self._tmp / "memory.json"

    def tearDown(self) -> None:
        tools._MEMORY_FILE = self._orig_file

    def test_remember_recall_forget(self) -> None:
        out = call_tool(tools.remember, text="用户偏好简约风格的设计", tags="用户偏好")
        self.assertIn("已记住", out)

        recalled = call_tool(tools.recall_memory, keyword="简约")
        self.assertIn("简约", recalled)
        self.assertIn("mem_", recalled)

        import re

        match = re.search(r"mem_[0-9a-z]+", recalled)
        self.assertIsNotNone(match)
        deleted = call_tool(tools.forget_memory, entry_id=match.group(0))
        self.assertIn("已删除", deleted)

        recalled_after = call_tool(tools.recall_memory, keyword="简约")
        self.assertTrue(
            "没有找到" in recalled_after or "还没有任何内容" in recalled_after,
            recalled_after,
        )


if __name__ == "__main__":
    unittest.main()
