import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import code_exec


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


class SandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="code_exec_test_"))
        self._orig_root = code_exec.SANDBOX_ROOT
        self._orig_env = os.environ.get("ALLOW_CODE_EXEC")
        code_exec.SANDBOX_ROOT = self._tmp
        os.environ["ALLOW_CODE_EXEC"] = "true"

    def tearDown(self) -> None:
        code_exec.SANDBOX_ROOT = self._orig_root
        if self._orig_env is None:
            os.environ.pop("ALLOW_CODE_EXEC", None)
        else:
            os.environ["ALLOW_CODE_EXEC"] = self._orig_env

    def test_path_traversal_rejected(self) -> None:
        out = call_tool(code_exec.write_code_file, project="demo", filename="../evil.py", content="x")
        self.assertIn("越界", out)

    def test_absolute_path_rejected(self) -> None:
        out = call_tool(code_exec.write_code_file, project="demo", filename=r"C:\evil.py", content="x")
        self.assertIn("错误", out)

    def test_bad_project_name_rejected(self) -> None:
        out = call_tool(code_exec.write_code_file, project="../..", filename="a.py", content="x")
        self.assertIn("项目名", out)

    def test_write_read_list_roundtrip(self) -> None:
        out = call_tool(code_exec.write_code_file, project="demo", filename="utils/math.py", content="def add(a,b):\n    return a+b\n")
        self.assertIn("已写入", out)
        content = call_tool(code_exec.read_code_file, project="demo", filename="utils/math.py")
        self.assertIn("def add", content)
        listing = call_tool(code_exec.list_code_files, project="demo")
        self.assertIn("utils/math.py", listing)

    def test_run_disabled_without_consent(self) -> None:
        os.environ["ALLOW_CODE_EXEC"] = "false"
        out = call_tool(code_exec.run_python, project="demo", code="print(1)")
        self.assertIn("未开启", out)

    def test_run_inline_code(self) -> None:
        out = call_tool(code_exec.run_python, project="demo", code="print('hello from sandbox')")
        self.assertIn("hello from sandbox", out)
        self.assertIn("退出码: 0", out)

    def test_run_file_with_args(self) -> None:
        call_tool(code_exec.write_code_file, project="demo", filename="args.py", content="import sys; print(sys.argv[1])")
        out = call_tool(code_exec.run_python, project="demo", filename="args.py", args="你好")
        self.assertIn("你好", out)

    def test_timeout_kills_long_run(self) -> None:
        start = time.monotonic()
        out = call_tool(code_exec.run_python, project="demo", code="import time; time.sleep(60)", timeout=2)
        self.assertIn("超时", out)
        self.assertLess(time.monotonic() - start, 30)

    def test_secret_env_not_passed(self) -> None:
        os.environ["OPENAI_API_KEY"] = "sk-test-secret-123"
        out = call_tool(
            code_exec.run_python,
            project="demo",
            code="import os; print('HAS_KEY=' + str('OPENAI_API_KEY' in os.environ))",
        )
        self.assertIn("HAS_KEY=False", out)

    def test_runtime_error_reported(self) -> None:
        out = call_tool(code_exec.run_python, project="demo", code="raise ValueError('boom')")
        self.assertIn("ValueError", out)
        self.assertIn("boom", out)


if __name__ == "__main__":
    unittest.main()
