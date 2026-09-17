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

import tools as tools_mod

from skills.dep_doctor.tools import scan_dependencies


def call_tool(tool, **kwargs) -> str:
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=input_json)
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


class DepDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="depdoc_"))
        self._orig = tools_mod.WORKSPACE_ROOT
        tools_mod.WORKSPACE_ROOT = self._tmp

    def tearDown(self) -> None:
        tools_mod.WORKSPACE_ROOT = self._orig

    def _write(self, name: str, content: str) -> None:
        target = self._tmp / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_requirements_findings(self) -> None:
        self._write(
            "req/requirements.txt",
            "# 注释\nopenai-agents==0.22.0\nrequests>=2.31.0\nflask\nflask>=2.0\npydantic~=2.0\n",
        )
        out = call_tool(scan_dependencies, directory="req")
        self.assertIn("requirements.txt", out)
        self.assertIn("未锁定", out)
        self.assertIn("flask", out)
        self.assertIn("重复依赖", out)
        self.assertIn("已锁定", out)

    def test_pyproject_supported(self) -> None:
        self._write(
            "proj/pyproject.toml",
            '[project]\ndependencies = [\n  "requests>=2.31",\n  "flask",\n  "flask",\n]\n',
        )
        out = call_tool(scan_dependencies, directory="proj")
        self.assertIn("pyproject.toml", out)
        self.assertIn("flask", out)

    def test_empty_directory(self) -> None:
        out = call_tool(scan_dependencies, directory=".")
        self.assertIn("没有找到", out)

    def test_venv_not_scanned(self) -> None:
        self._write(".venv/site-packages/requirements.txt", "whatever==1.0\n")
        out = call_tool(scan_dependencies, directory=".")
        self.assertIn("没有找到", out)

    def test_outside_workspace_rejected(self) -> None:
        out = call_tool(scan_dependencies, directory="..")
        self.assertIn("只能读取工作区", out)


if __name__ == "__main__":
    unittest.main()
