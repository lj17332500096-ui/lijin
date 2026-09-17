import asyncio
import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import multimodal


def call_tool(tool, **kwargs) -> str:
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


class AskImageGuardTests(unittest.TestCase):
    """ask_image 的错误路径（不联网、不调模型）。"""

    def test_missing_file_returns_error(self) -> None:
        out = call_tool(multimodal.ask_image, image_path="不存在_xyz.png", question="图里有什么")
        self.assertIn("错误", out)

    def test_empty_question_via_tool(self) -> None:
        out = call_tool(multimodal.ask_image, image_path="a.png", question="   ")
        self.assertIn("不能为空", out)

    def test_outside_workspace_rejected(self) -> None:
        out = call_tool(multimodal.ask_image, image_path="..\\..\\Windows\\win.ini", question="这是什么")
        self.assertIn("只能读取工作区", out)


if __name__ == "__main__":
    unittest.main()
