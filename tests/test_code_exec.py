import asyncio
import json
import os
import shutil
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


class InlineFileCleanupTests(unittest.TestCase):
    """P0-blat 回归护栏：内联代码的 `_inline_*.py` 用完即删，不留在磁盘上生长。

    缺陷背景（2026-10-06 实测）：code_exec.run_python_impl 每次用 code 参数调用都在
    工作目录落一个 `_inline_{stamp}.py`，全文件无任何 unlink ⇒ 确定性垃圾单调增长
    （实测 benchmark_fixture/ 已积55 个；仓库根那次污染里也混着 1 个）。
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="inline_cleanup_"))
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
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _inlines(self) -> list[Path]:
        return list(self._tmp.rglob("_inline_*.py"))

    def test_inline_file_removed_after_success(self) -> None:
        out = call_tool(code_exec.run_python, project="clean_demo", code="print('ok-inline')")
        self.assertIn("ok-inline", out)
        self.assertEqual(self._inlines(), [], "内联执行脚本运行后未清理")

    def test_inline_file_removed_after_runtime_error(self) -> None:
        """失败路径（抛异常）同样必须清理 —— 否则错误代码留下的垃圾更多。"""
        out = call_tool(code_exec.run_python, project="clean_demo", code="raise ValueError('x')")
        self.assertIn("ValueError", out)
        self.assertEqual(self._inlines(), [], "内联执行脚本在异常路径下未清理")

    def test_inline_file_removed_after_timeout(self) -> None:
        out = call_tool(code_exec.run_python, project="clean_demo",
                        code="import time; time.sleep(60)", timeout=2)
        self.assertIn("超时", out)
        self.assertEqual(self._inlines(), [], "内联执行脚本在超时路径下未清理")

    def test_user_supplied_file_never_deleted(self) -> None:
        """反向护栏（防过度修复）：用户自己写进沙箱的文件绝不能被清理逻辑删掉。

        ⚠️ 2026-10-06：这里原先还有 `keep.parent.rmdir()`，已删除。**它不是断言，
        是重复清理**（tearDown 的 `shutil.rmtree(self._tmp, ignore_errors=True)`
        已经覆盖），而且它引入了一个**与被测代码无关的脆弱假设**：认为
        「keepme 目录里只有本用例创建的那一个文件」。

        实测（8.3 短名 root 护栏下 100% 复现、单独跑该class 0% 复现）：
        目录里会多出一个**字面名** `%SystemDrive%\\ProgramData\\Microsoft\\Windows\\Caches`
        子树（内含 `cversions.2.db` 与 GUID 命名的 `.db`，取证实测），
        导致 `os.rmdir` 抛 `WinError 145 目录不是空的`。

        取证结论（重要，勿再误判为路径规范化不对称）：
        - 失败点在 `rmdir`，而**两条真断言都已通过**
          （`assertIn("user-file-ran")` 与 `assertTrue(keep.exists())`）；
        - 同一注入条件下**只跑本class 4 个用例 0/10 复现**，
          必须跑满护栏的 6 文件名单（60 passed + 本条红）才复现；
        - 逐个文件二分：任一文件 + 本文件单独组合都**不**复现 ⇒ 与路径规范化无关；
        - 仓库内`SystemDrive`/`ProgramData` **零命中**（grep 确认），
          仓库根本身也残留一个同款 `%SystemDrive%` 目录（mtime 11:00，非本轮创建）；
        - 子进程对照实验（`cmd /c` + 字面 `%SystemDrive%` 模板）**两臂都不创建**
          ⇒ 排除「`_sanitized_env()` 剥掉 `SystemDrive` 导致字面展开」这一假设。
        ⇒ 归因：**外部 Windows 组件把未展开的 `%SystemDrive%` 模板当成相对路径，
        写进了当时的 CWD**。属环境产物，**不是 code_exec 的缺陷**。

        修法：删掉脆弱的 `rmdir`（不是断言、不削弱护栏），
        两条真断言原样保留 —— 护栏鉴别力由中和实验另行证明（见 P0修复报告）。
        """
        keep = self._tmp / "keepme" / "user_script.py"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("print('user-file-ran')\n", encoding="utf-8")
        out = call_tool(code_exec.run_python, project="keepme", filename="user_script.py")
        self.assertIn("user-file-ran", out)
        self.assertTrue(keep.exists(), "用户传入的既有文件被误删了")


if __name__ == "__main__":
    unittest.main()
