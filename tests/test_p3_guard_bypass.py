"""P3 返工项 2 回归：护栏 fail-closed 不得被降级成 fail-open。

# 缺陷（P3 测试工程师实测，见 04-test/测试报告.md §4.3）

`cli/app.py` 的 `ChatApp.container_id()` 有 `except Exception: return ""`
（设计意图是"容器还没建好不该终结会话"）。而护栏异常此前是普通
`RuntimeError` → **被这个 bare except 静默吞掉**：

    APPROVAL=off 启动 CLI  →  横幅正常、进程正常、退出码 0
                            报错推迟到第一次 Run，并被包装成 E-UNKNOWN

即 P0-2 的设计"拒绝启动"实际变成"照常启动、稍后报个不明错误" ——
护栏在最典型的绕过路径上失效（fail-closed → fail-slow）。

# 修法

`runtime.errors.StartupGuardBlocked` 继承 **BaseException 而非 Exception**：
`except Exception:` 捕不到它 → 任何"局部失败不影响整体"的兜底都不会误吞
安全阻断。代价是必须由进程入口**显式**接住（cli/app.py::run_cli）并转成
可读提示 + 非零退出码（EXIT_GUARD_BLOCKED=78，EX_CONFIG）。

方向不对称是刻意的：**宁可崩栈，也不可静默放行**。

注意：这比"把 except Exception 收窄成 sqlite3.Error"更彻底 ——
收窄 except 仍可能被未来的 `except BaseException` 或第三方代码绕过，
而异常类型本身落在 Exception 之外则无法被它们捕获。

本文件是 P3 测试工程师探针的**修复后版本**：类名与用例名沿用原探针
（StartupPathBypassTests / AntiHeuristicDeclarationTests），
原先"证明缺陷存在"的那条已改为"证明缺陷已修复"。
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from cli.app import EXIT_GUARD_BLOCKED
from runtime.errors import StartupGuardBlocked
from runtime.startup_guard import (
    UNATTENDED_ENV,
    enforce_approval_guard,
    format_block_message,
)


class _EnvMixin:
    MANAGED = ("APPROVAL", UNATTENDED_ENV, "FORGE_APPROVAL_FAILCLOSED")

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self.MANAGED}
        for k in self.MANAGED:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _force_block(self) -> None:
        """护栏阻断态：APPROVAL 关闭 + 未声明无人值守。"""
        os.environ["APPROVAL"] = "off"
        os.environ.pop(UNATTENDED_ENV, None)
        os.environ.pop("FORGE_APPROVAL_FAILCLOSED", None)


class StartupPathBypassTests(_EnvMixin, unittest.TestCase):
    """护栏阻断必须穿透到进程顶层，不能被局部兜底吞掉。"""

    def test_cli_container_id_swallows_guard_error(self) -> None:
        """原探针的用例名保留，但断言**反转为"不再被吞"**。

        原语义（修复前）：`container_id()` 返回 ""，护栏错误被吞 ⇒ 缺陷存在。
        现语义：必须抛出 `StartupGuardBlocked`。
        名不改是为了让"这条用例从红变绿"这件事在历史记录里可追溯。

        注意 `_container_id` 必须预置为真值：它是 `if self._container_id:` 的
        条件，为假时 `self.store` 属性根本不会被访问，也就走不到
        `runtime._ensure()` —— 那样测的就不是这条绕过路径了
        （我第一版就踩了这个坑：用例"通过"但什么都没验证）。
        """
        self._force_block()
        from cli.app import ChatApp

        app = ChatApp.__new__(ChatApp)     # 绕开 __init__ 的其它依赖
        app._runtime = None
        app._store = None                  # 迫使 store 属性去建 Runtime
        app._container_id = "tk_existing"  # 非空 -> 必定访问 self.store
        app.session_name = "guard-bypass-probe"

        with self.assertRaises(StartupGuardBlocked):
            app.container_id()

    def test_generic_except_cannot_swallow_guard(self) -> None:
        """核心机制：`except Exception:` 捕不到护栏阻断。

        这条同时钉住类型选择：若有人把 StartupGuardBlocked 改回继承
        Exception，本条立刻红。

        写法说明：被测函数返回标记而**不**让异常逃出测试，所以外层用
        `except BaseException` 兜住"它确实飞出来了"这件事 ——
        若护栏异常改回 Exception 子类，内层 `except Exception` 就会
        把它吃掉并返回 "swallowed"，断言随即失败。
        """
        self._force_block()

        def local_failure_tolerant():        # 等价于 container_id() 的兜底
            try:
                enforce_approval_guard()
            except Exception:                # noqa: BLE001 - 正是要验证它捕不到
                return "swallowed"
            return "not-swallowed"

        try:
            outcome = local_failure_tolerant()
        except BaseException:                # noqa: BLE001 - 飞出来了 = 修复生效
            outcome = "escaped-uncatchable"

        self.assertEqual(
            outcome, "escaped-uncatchable",
            "护栏阻断被普通 except Exception 吞掉 —— fail-closed 被降级成 fail-open",
        )

    def test_exception_type_is_outside_exception_hierarchy(self) -> None:
        self.assertFalse(
            issubclass(StartupGuardBlocked, Exception),
            "StartupGuardBlocked 必须继承 BaseException 而非 Exception",
        )
        self.assertTrue(issubclass(StartupGuardBlocked, BaseException))

    def test_block_message_is_actionable(self) -> None:
        """提示要说清"为什么"和"怎么办"，不能只抛内部术语。"""
        self._force_block()
        with self.assertRaises(StartupGuardBlocked) as cm:
            enforce_approval_guard()
        text = format_block_message(cm.exception)
        self.assertIn("拒绝启动", text)
        self.assertIn("FORGE_UNATTENDED", text)
        self.assertIn("APPROVAL", text)

    def test_exit_code_is_non_zero_and_distinct_from_generic_failure(self) -> None:
        """退出码非 0，且与"一般失败(1)"区分得开（处置方式不同）。"""
        self.assertNotEqual(EXIT_GUARD_BLOCKED, 0)
        self.assertNotEqual(EXIT_GUARD_BLOCKED, 1)


class AntiHeuristicDeclarationTests(_EnvMixin, unittest.TestCase):
    """豁免必须显式声明；子串/前缀/真值别名都**不是**声明。

    （类名与用例沿用 P3 探针：这部分原探针已全部通过，本轮未改判定逻辑，
    保留以防回归。）
    """

    def test_exact_modes_accepted(self) -> None:
        from runtime.startup_guard import approval_guard_verdict

        for mode in ("scheduled", "daemon", "both"):
            with self.subTest(mode=mode):
                os.environ["APPROVAL"] = "off"
                os.environ[UNATTENDED_ENV] = mode
                self.assertFalse(
                    approval_guard_verdict().should_block,
                    f"显式声明 {mode!r} 应放行",
                )

    def test_substrings_and_aliases_rejected(self) -> None:
        """`scheduled-ish` / `cron` / `on` 都不是声明。

        这是判据的核心纪律：靠猜会让"忘记配置"和"故意无人值守"无法区分。
        """
        from runtime.startup_guard import approval_guard_verdict

        for decoy in ("scheduled-ish", "notdaemon", "daemonized", "unattended",
                      "cron", "s", "on", "true", "1", "yes"):
            with self.subTest(decoy=decoy):
                os.environ["APPROVAL"] = "off"
                os.environ[UNATTENDED_ENV] = decoy
                self.assertTrue(
                    approval_guard_verdict().should_block,
                    f"{decoy!r} 不得被当作无人值守声明",
                )

    def test_case_and_whitespace_normalised(self) -> None:
        from runtime.startup_guard import approval_guard_verdict

        for raw in ("  SCHEDULED ", "Both", "\tdaemon\n"):
            with self.subTest(raw=raw):
                os.environ["APPROVAL"] = "off"
                os.environ[UNATTENDED_ENV] = raw
                self.assertFalse(
                    approval_guard_verdict().should_block,
                    f"{raw!r} 的大小写/空白应被归一化后接受",
                )


class OrdinaryFailuresStillToleratedTests(_EnvMixin, unittest.TestCase):
    """**反向用例**：普通异常不得被当成护栏阻断。

    这是本次改动最危险的误伤方向 —— 若为了"让护栏穿透"把
    `container_id()` 的兜底一并删掉，它原本保护的"DB 暂时不可用"
    场景就会变成崩溃。
    """

    def test_ordinary_db_error_still_returns_empty_string(self) -> None:
        os.environ["APPROVAL"] = "on"
        from cli.app import ChatApp

        class _Boom:
            class mgr:  # noqa: N801 - 最小替身
                @staticmethod
                def get_container(_cid):
                    raise RuntimeError("db temporarily unavailable")

                @staticmethod
                def get_or_create_container(_name):
                    raise RuntimeError("db temporarily unavailable")

        app = ChatApp.__new__(ChatApp)
        app._store = _Boom()
        app._container_id = None
        app.session_name = "ordinary-failure"

        self.assertEqual(
            app.container_id(), "",
            "普通故障被误当成护栏阻断 —— container_id 的兜底语义被破坏了",
        )

    def test_keyboard_interrupt_is_not_a_guard_block(self) -> None:
        self.assertFalse(isinstance(KeyboardInterrupt(), StartupGuardBlocked))

    def test_format_block_message_tolerates_plain_exception(self) -> None:
        """入口会复用它处理任意异常，不能因缺 reason/hint 而崩。"""
        self.assertIn("普通错误", format_block_message(RuntimeError("普通错误")))


if __name__ == "__main__":
    unittest.main()
