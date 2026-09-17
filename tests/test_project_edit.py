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

from agents.tool_context import ToolContext

import project_edit as pe


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


class ProjectEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="proj_edit_"))
        self._orig_base = pe.BASE_DIR
        self._orig_env = os.environ.get("ALLOW_PROJECT_EDIT")
        pe.BASE_DIR = self._tmp
        os.environ["ALLOW_PROJECT_EDIT"] = "true"

    def tearDown(self) -> None:
        pe.BASE_DIR = self._orig_base
        if self._orig_env is None:
            os.environ.pop("ALLOW_PROJECT_EDIT", None)
        else:
            os.environ["ALLOW_PROJECT_EDIT"] = self._orig_env

    def test_disabled_without_consent(self) -> None:
        os.environ["ALLOW_PROJECT_EDIT"] = "false"
        out = call_tool(pe.write_project_file, path="a.py", content="x = 1")
        self.assertIn("未开启", out)

    def test_write_and_backup(self) -> None:
        p = call_tool(pe.write_project_file, path="demo.py", content="x = 1\n")
        self.assertIn("新建", p)
        self.assertTrue((self._tmp / "demo.py").exists())
        second = call_tool(pe.write_project_file, path="demo.py", content="x = 2\n")
        self.assertIn("覆盖", second)
        backups = (self._tmp / "logs" / "backups").glob("*.py")
        self.assertEqual(len(list(backups)), 1)

    def test_protected_and_escape_rejected(self) -> None:
        for path in (".env", "logs/x.py", "../outside.py", "C:/Windows/x.py", ".venv/lib/x.py"):
            out = call_tool(pe.write_project_file, path=path, content="x")
            self.assertIn("受保护", out)

    def test_suffix_and_binary_and_secret_rejected(self) -> None:
        out = call_tool(pe.write_project_file, path="a.exe", content="x")
        self.assertIn("白名单", out)
        out = call_tool(pe.write_project_file, path="a.py", content="a\x00b")
        self.assertIn("二进制", out)
        out = call_tool(pe.write_project_file, path="a.py", content="key=sk-" + "a" * 24)
        self.assertIn("密钥", out)

    def test_edit_unique_and_multiple(self) -> None:
        call_tool(pe.write_project_file, path="a.py", content="print('hi')\nprint('hi')\n")
        multi = call_tool(pe.edit_project_file, path="a.py", old_string="print('hi')", new_string="print('yo')")
        self.assertIn("出现 2 次", multi)
        ok = call_tool(pe.edit_project_file, path="a.py", old_string="print('hi')", new_string="print('yo')", replace_all=True)
        self.assertIn("替换全部 2 处", ok)
        self.assertNotIn("print('hi')", (self._tmp / "a.py").read_text(encoding="utf-8"))

    def test_edit_returns_diff_by_default_and_can_skip(self) -> None:
        call_tool(pe.write_project_file, path="a.py", content="x = 1\nprint(x)\n")
        out = call_tool(pe.edit_project_file, path="a.py", old_string="x = 1", new_string="x = 2")
        self.assertIn("[Diff 变更]", out)
        self.assertIn("-x = 1", out)
        self.assertIn("+x = 2", out)
        self.assertIn("a/a.py", out)
        out2 = call_tool(pe.edit_project_file, path="a.py", old_string="x = 2", new_string="x = 3", diff=False)
        self.assertNotIn("[Diff 变更]", out2)
        self.assertIn("替换", out2)
        self.assertEqual((self._tmp / "a.py").read_text(encoding="utf-8"), "x = 3\nprint(x)\n")

    def test_edit_missing_or_absent(self) -> None:
        out = call_tool(pe.edit_project_file, path="不存在.py", old_string="a", new_string="b")
        self.assertIn("不存在", out)
        call_tool(pe.write_project_file, path="b.py", content="hello\n")
        out = call_tool(pe.edit_project_file, path="b.py", old_string="zzz", new_string="x")
        self.assertIn("没有找到", out)

    def test_markdown_editable(self) -> None:
        out = call_tool(pe.write_project_file, path="docs/说明.md", content="# 标题\n正文")
        self.assertIn("新建", out)
        self.assertTrue((self._tmp / "docs" / "说明.md").exists())


if __name__ == "__main__":
    unittest.main()
