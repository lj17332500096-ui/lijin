import sys
import tempfile
import unittest
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import code_exec as code_exec_mod
import runtime.codex_loop as cl


class CodexLoopTests(unittest.TestCase):
    """Codex 循环：运行→失败→修复重跑→四段总结（LLM/运行均为 mock，不联网）。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="codex_loop_"))
        code_exec_mod.SANDBOX_ROOT = self._tmp
        cl._exec_enabled = lambda: True
        cl._project_dir = code_exec_mod._project_dir
        cl._file_target = code_exec_mod._file_target
        self._orig_impl = code_exec_mod.run_python_impl
        self._orig_fix = cl._fix_with_llm

    def tearDown(self) -> None:
        code_exec_mod.run_python_impl = self._orig_impl
        cl._fix_with_llm = self._orig_fix

    def _seed(self, content: str = "print(1/0)\n"):
        (self._tmp / "demo").mkdir(exist_ok=True)
        (self._tmp / "demo" / "app.py").write_text(content, encoding="utf-8")

    def test_success_pass_returns_four_sections(self) -> None:
        self._seed("print('ok')\n")
        code_exec_mod.run_python_impl = lambda *a, **kw: "退出码: 0 ｜ 用时: 0.1s\n[stdout]\nok"
        out = cl.code_loop_impl("demo", "app.py")
        self.assertIn("【循环结果】", out)
        self.assertIn("通过", out)
        self.assertIn("做了什么", out)
        self.assertIn("为何这样做", out)
        self.assertIn("验证结果", out)
        self.assertIn("下一步", out)
        self.assertIn("ok", out)

    def test_fail_then_fix_then_pass(self) -> None:
        self._seed("print(1/0)\n")
        calls = {"n": 0}

        def fake_run(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return "退出码: 1 ｜ 用时: 0.1s\n[stderr]\nZeroDivisionError: division by zero"
            return "退出码: 0 ｜ 用时: 0.1s\n[stdout]\nfixed"

        code_exec_mod.run_python_impl = fake_run
        cl._fix_with_llm = lambda code, err: "print('fixed')\n"
        out = cl.code_loop_impl("demo", "app.py")
        self.assertIn("通过", out)
        self.assertIn("2/3", out)  # 第二次尝试即通过
        self.assertEqual((self._tmp / "demo" / "app.py").read_text(encoding="utf-8"), "print('fixed')\n")

    def test_three_attempts_give_up_with_attempts(self) -> None:
        self._seed()

        def always_fail(*a, **kw):
            return "退出码: 1 ｜ 用时: 0.1s\n[stderr]\nZeroDivisionError"

        code_exec_mod.run_python_impl = always_fail
        cl._fix_with_llm = lambda code, err: "print(1/0)\n"  # 修复无效
        out = cl.code_loop_impl("demo", "app.py", max_attempts=3)
        self.assertIn("失败", out)
        self.assertIn("3 次", out)
        self.assertIn("验证结果", out)
        self.assertIn("第1次", out)
        self.assertIn("第3次", out)

    def test_no_fix_gives_up_early(self) -> None:
        self._seed()
        code_exec_mod.run_python_impl = lambda *a, **kw: "退出码: 2 ｜ 用时: 0.1s\n[stderr]\nKeyError: 'x'"
        cl._fix_with_llm = lambda code, err: None
        out = cl.code_loop_impl("demo", "app.py")
        self.assertIn("失败", out)
        self.assertIn("停止", out)

    def test_secret_pattern_rejected_by_validation(self) -> None:
        self.assertFalse(cl._validate_fix("key=sk-" + "a" * 24))
        self.assertFalse(cl._validate_fix("AIza" + "b" * 24))
        self.assertFalse(cl._validate_fix(""))
        self.assertTrue(cl._validate_fix("x = 1\nprint(x)\n"))

    def test_safe_write_rejects_escaped_path(self) -> None:
        (self._tmp / "demo").mkdir(exist_ok=True)
        self.assertFalse(cl._safe_write(self._tmp / "demo", "../evil.py", "x = 1\n"))
        self.assertFalse((self._tmp / "evil.py").exists())

    def test_disabled_env_message(self) -> None:
        cl._exec_enabled = lambda: False
        out = cl.code_loop_impl("demo", "app.py")
        self.assertIn("ALLOW_CODE_EXEC", out)


if __name__ == "__main__":
    unittest.main()
