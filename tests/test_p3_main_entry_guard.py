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
    自接（那里本来就是 P3 返工项 2 的落点）。所以要区分两种"已接住"：
      - 在 main.py 里被 `_run_guarded` 包住；
      - 或自身内部已接住（`run_cli` 的情况）。
    """

    #: 自身已接护栏、不需要在 main.py 再包一层（它们在别的模块里自接）
    SELF_GUARDED = frozenset({"run_cli"})

    #: 建 Runtime 的**真实信号**（护栏就在这些点上触发）。
    #: 用它做可达性分析，而不是硬编码"哪些函数是入口"——
    #: 硬编码白名单只检查"名单内的名字是否被包住"，
    #: **不检查"是否存在第 N+1 个漏网的"**（P3 测试工程师中和实验 2 实测：
    #: 注入一个全新入口 `run_voice()`，白名单断言照样 8 passed）。
    RUNTIME_TRIGGERS = frozenset({
        "AgentRuntime", "TaskManager", "get_default",
    })

    @classmethod
    def _module_functions(cls) -> dict[str, ast.AST]:
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        return {n.name: n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    @classmethod
    def _reaches_runtime(cls, fn: ast.AST, funcs: dict[str, ast.AST],
                         seen: set[str] | None = None) -> bool:
        """fn（可同步/异步）是否**传递地**触达建 Runtime 的调用。

        传递闭包是必要的：`daemon_loop` 自己不建 Runtime，但它调用的
        `run_scheduled_task_once` 会。只看直接调用会漏。
        """
        seen = seen if seen is not None else set()
        if id(fn) in seen:
            return False
        seen.add(id(fn))
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            # AgentRuntime.get_default() / TaskManager() / run_tui(...)
            if isinstance(f, ast.Attribute) and f.attr in cls.RUNTIME_TRIGGERS:
                return True
            if isinstance(f, ast.Name):
                if f.id in cls.RUNTIME_TRIGGERS:
                    return True
                callee = funcs.get(f.id)
                if callee is not None and cls._reaches_runtime(callee, funcs, seen):
                    return True
        return False

    def test_no_unguarded_runtime_entry(self) -> None:
        """**反向断言**：凡会建 Runtime 的入口调用点，必须在护栏壳内。

        两个设计要点（都是被 P3 测试工程师的中和实验逼出来的）：

        1. **不按名字判定入口**。原实现用 `GUARDED_CALLS` 白名单，只检查
           "这 3 个名字是否被包住" —— 注入第 4 个漏网入口照样全绿。
           改为：从 `main()` 里找出所有**会传递地触达建 Runtime** 的调用
           （`_reaches_runtime` 闭包分析），不看名字。
           这项改动当场抓出真实漏网入口 `chat_voice`（`--voice`），见 main.py。
        2. **按调用点判定，不按函数名判定**。同一函数被调两次时，
           若按"函数名是否在壳内出现过"判定，壳内那一次会把壳外那次**掩盖**掉
           —— 注入实验正是这种形态。必须按 **AST 节点身份**判断每次调用的位置。
        """
        funcs = self._module_functions()
        main_fn = funcs["main"]

        # _run_guarded(...) 调用子树里的所有节点 —— 用节点身份而非名字判断归属
        guarded_nodes: set[int] = set()
        for node in ast.walk(main_fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_run_guarded"):
                guarded_nodes.update(id(sub) for sub in ast.walk(node))

        unprotected: list[str] = []
        checked = 0
        for node in ast.walk(main_fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            name = node.func.id
            if name not in funcs or name in self.SELF_GUARDED:
                continue
            if not self._reaches_runtime(funcs[name], funcs):
                continue
            checked += 1
            if id(node) not in guarded_nodes:
                unprotected.append(f"{name}@line{node.lineno}")

        self.assertEqual(
            unprotected, [],
            "这些会建 Runtime 的调用点没有护栏壳：" + ", ".join(unprotected)
            + "。护栏阻断会表现为裸崩栈而非可读提示。"
              " 修法：用 _run_guarded(...) 包起来；若它自接了护栏，"
              "请加进 SELF_GUARDED 并在该模块内补上 except StartupGuardBlocked。",
        )
        # 防分析器退化成"什么都没检查"（否则上面的空集恒真）。
        # 数到 3：voice / daemon / run-task 三个是模块级函数调用；
        # run_tui 走 `from cli.tui.app import run_tui` 局部导入（在 main() 内
        # 是 ImportFrom 而非模块级函数），故不计入本分析的 checked。
        self.assertGreaterEqual(
            checked, 3,
            f"只识别到 {checked} 个需检查的 Runtime 入口调用点，可达性分析可能已失效",
        )

    def test_self_guarded_entry_really_guards(self) -> None:
        """`run_cli` 声称自接护栏 —— 用**计数**核实，不��存在性。

        存在性断言（assertIn）在有 2 处 except 时，删掉 1 处仍满足
        （P3 测试工程师中和实验 3 实测：8 passed）。故改成精确计数。
        """
        src = (BASE / "cli" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        run_cli = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "run_cli"),
            None,
        )
        self.assertIsNotNone(run_cli)
        seg = ast.unparse(run_cli)
        # run_cli 有两处需要接：构造期（ChatApp(...)）与运行期（app.run()）。
        # 只接一处 => 另一条路径仍是裸崩栈。
        self.assertEqual(
            seg.count("except StartupGuardBlocked"), 2,
            "run_cli 必须有 2 处 except StartupGuardBlocked"
            "（构造期 + 运行期）；只接一处，另一条路径仍会裸崩栈",
        )
        # 每一处 except 都必须用专用退出码（否则接住了却仍以 1 退出）
        self.assertEqual(
            seg.count("EXIT_GUARD_BLOCKED"), 2,
            "两处 except 都必须返回 EXIT_GUARD_BLOCKED（实测应为 2）",
        )

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
