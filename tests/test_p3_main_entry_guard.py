"""P3 收尾：`main.py` 三个入口分支必须接住护栏阻断。

# 缺口（P3 测试工程师实测）

`StartupGuardBlocked` 继承 `BaseException`（P3 返工项 2），所以 `except Exception`
捕不到它 —— 这是**故意**的。代价是每个进程入口都必须显式接住。

但 P3 返工时只接了默认入口（经 `cli.app.run_cli`），`main.py` 的另外三个分支漏接：

    main.py  --run-task  -> run_scheduled_task_once(...)   未接
    main.py  --daemon     -> daemon_loop(...)               未接
    main.py  --tui        -> run_tui(...)                    未接
    main.py  默认          -> cli.app.run_cli(...)            已接

实测 `--daemon`（未声明无人值守）：`EXIT=1`，安全语义**正确**（异常照常上抛、
进程非 0 退出），但用户看到的是**裸崩栈**而不是 `format_block_message()` 那条
可读提示 —— 运维拿到的信息量差别很大。

本文件锁住四个分支都被接住，且退出码与 `cli.app` 一致。
"""

import ast
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.errors import StartupGuardBlocked

MAIN = BASE / "main.py"


class GuardedEntryTests(unittest.TestCase):
    """`_run_guarded` 的行为。"""

    def test_guard_block_becomes_readable_exit_78(self) -> None:
        import main

        def boom():
            raise StartupGuardBlocked("x", reason="审批门关闭", hint="设 FORGE_UNATTENDED=daemon")

        with self.assertRaises(SystemExit) as cm:
            main._run_guarded(boom)
        self.assertEqual(cm.exception.code, 78, "护栏阻断必须以 EX_CONFIG(78) 退出")

    def test_normal_call_passes_through(self) -> None:
        """合规路径不得受影响（不能为了护栏把正常启动也拦了）。"""
        import main

        self.assertEqual(main._run_guarded(lambda: "ok"), "ok")

    def test_ordinary_exception_is_not_converted(self) -> None:
        """普通异常**不得**被转成护栏退出码 —— 那是误伤。"""
        import main

        def boom():
            raise RuntimeError("数据库暂时不可用")

        with self.assertRaises(RuntimeError) as cm:
            main._run_guarded(boom)
        self.assertNotIsInstance(cm.exception, SystemExit)

    def test_keyboard_interrupt_passes_through(self) -> None:
        import main

        def boom():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            main._run_guarded(boom)


class ExitCodeConsistencyTests(unittest.TestCase):
    """两处退出码必须一致（main.py 不能 import cli.app，故显式对齐数值）。"""

    def test_exit_codes_match(self) -> None:
        import main
        from cli.app import EXIT_GUARD_BLOCKED

        self.assertEqual(
            main._EXIT_GUARD_BLOCKED, EXIT_GUARD_BLOCKED,
            "main.py 与 cli/app.py 的护栏退出码不一致："
            "运维按不同代码分支处理会漏",
        )


class AllEntryBranchesGuardedTests(unittest.TestCase):
    """AST 级：会建 Runtime 的入口都必须有护栏壳。

    行为测试只能覆盖我想到的分支；这条按**源码结构**核对"每个会建 Runtime 的
    入口是否被接住"，将来新增入口时也会被提醒。

    注意 `run_cli` 不在 main.py 的壳里 —— 它在 `cli/app.py::run_cli` 内部
    自接（那里本来就是 P3 返工项 2 的落点）。所以这里要区分两种"已接住"：
      - 在 main.py 里被 `_run_guarded` 包住；
      - 或自身内部已接住（run_cli 的情况）。
    """

    #: 会在运行期建 Runtime（因而可能触发护栏）的入口调用
    GUARDED_CALLS = ("daemon_loop", "run_scheduled_task_once", "run_tui")
    #: 自身已接护栏、不需要在 main.py 再包一层
    SELF_GUARDED = ("run_cli",)

    def test_main_wraps_every_runtime_entry(self) -> None:
        src = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(src)
        main_fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "main"),
            None,
        )
        self.assertIsNotNone(main_fn, "未找到 main()")

        guarded: set[str] = set()
        for node in ast.walk(main_fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "_run_guarded"):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id in self.GUARDED_CALLS:
                    guarded.add(sub.func.id)

        missing = sorted(set(self.GUARDED_CALLS) - guarded)
        self.assertEqual(
            missing, [],
            f"这些入口没有套 _run_guarded：{missing}。"
            " 护栏阻断会表现为裸崩栈而非可读提示（P3 收尾要求）",
        )

    def test_self_guarded_entry_really_guards(self) -> None:
        """`run_cli` 声称自接护栏 —— 核实它不是空头声明。"""
        src = (BASE / "cli" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        run_cli = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "run_cli"),
            None,
        )
        self.assertIsNotNone(run_cli)
        seg = ast.unparse(run_cli)
        self.assertIn(
            "StartupGuardBlocked", seg,
            "run_cli 声称自接护栏但没有捕获 StartupGuardBlocked",
        )
        self.assertIn("EXIT_GUARD_BLOCKED", seg,
                      "run_cli 捕获了护栏异常却没有用专用退出码")

    def test_guard_helper_exists_and_is_used(self) -> None:
        src = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("_run_guarded", names)
        self.assertIn("_guard_block_exit", names)
        # 壳必须真的捕获护栏异常，否则等于没接
        helper = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "_run_guarded")
        seg = ast.unparse(helper)
        self.assertIn("StartupGuardBlocked", seg,
                      "_run_guarded 没有捕获 StartupGuardBlocked —— 壳是空的")


if __name__ == "__main__":
    unittest.main()
