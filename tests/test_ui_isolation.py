"""UI 隔离边界守卫（由项目阶段约定强制，见 ui_frozen.py）。

这个文件存在的意义
------------------
「只优化后端」阶段最怕的不是 UI 挂掉，而是**耦合悄悄回流**：某天后端为了图方便
`import llama_bridge` 拿个常量，或者网页入口又被默认带起来，于是后端验收重新被 UI
绑住。所以这里把三条边界写成断言：

1. 除冻结的 UI 层自身外，任何后端/CLI 模块都不得导入 `webapp` / `llama_bridge`；
2. 网页入口在未显式开启时必须拒绝启动（退出码 2），且**不得**触达 uvicorn；
3. 导入 CLI 平台不得把 webapp / llama_bridge 拉进 `sys.modules`（用干净子进程验证）。
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import ui_frozen  # noqa: E402

#: 唯一允许导入 UI 层模块的文件（冻结层自身）
FROZEN_UI_OWNERS = {"webapp.py", "llama_bridge.py"}

#: 需要守卫的目录（后端核心 + CLI 平台）
GUARDED_DIRS = ("runtime", "cli")

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(webapp|llama_bridge)\b", re.M)


def _guarded_files() -> list[Path]:
    files = [p for p in sorted(BASE.glob("*.py")) if p.name not in FROZEN_UI_OWNERS]
    for directory in GUARDED_DIRS:
        target = BASE / directory
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    return files


class UiImportBoundaryTests(unittest.TestCase):
    """边界 1：后端与 CLI 不得依赖 UI 层。"""

    def test_no_backend_module_imports_ui_layer(self) -> None:
        offenders: list[str] = []
        for path in _guarded_files():
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # pragma: no cover
                self.fail(f"读取 {path} 失败：{exc}")
            for match in _IMPORT_RE.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(BASE)}:{line} → {match.group(0).strip()}")
        self.assertEqual(
            offenders,
            [],
            "后端/CLI 不得导入已冻结的 UI 层（webapp / llama_bridge）：\n  " + "\n  ".join(offenders),
        )

    def test_guarded_set_actually_covers_backend(self) -> None:
        """守卫本身别退化成空集：至少要覆盖 runtime 与 cli。"""
        relative = {str(p.relative_to(BASE)).replace("\\", "/") for p in _guarded_files()}
        self.assertIn("main.py", relative)
        self.assertIn("agent.py", relative)
        self.assertTrue(any(p.startswith("runtime/") for p in relative), relative)
        self.assertTrue(any(p.startswith("cli/") for p in relative), relative)
        self.assertNotIn("webapp.py", relative)
        self.assertNotIn("llama_bridge.py", relative)


class UiEntryGateTests(unittest.TestCase):
    """边界 2：网页入口默认拒绝启动。"""

    def test_ui_disabled_by_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(ui_frozen.ENABLE_ENV, None)
            self.assertFalse(ui_frozen.ui_enabled([]))
            self.assertFalse(ui_frozen.ui_enabled(None))

    def test_enable_via_env_and_flag(self) -> None:
        with mock.patch.dict("os.environ", {ui_frozen.ENABLE_ENV: "1"}):
            self.assertTrue(ui_frozen.ui_enabled([]))
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(ui_frozen.ENABLE_ENV, None)
            self.assertTrue(ui_frozen.ui_enabled([ui_frozen.ENABLE_FLAG]))

    def test_frozen_notice_points_to_cli(self) -> None:
        notice = ui_frozen.frozen_notice()
        self.assertIn("main.py", notice)
        self.assertIn(ui_frozen.ENABLE_ENV, notice)

    def test_webapp_main_refuses_without_flag(self) -> None:
        """未开启时必须 SystemExit(2)，且绝不能走到 uvicorn.run。"""
        import webapp

        with mock.patch.object(sys, "argv", ["webapp.py"]), \
                mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch("uvicorn.run") as mocked_run:
            import os

            os.environ.pop(ui_frozen.ENABLE_ENV, None)
            with self.assertRaises(SystemExit) as ctx:
                webapp.main()
            self.assertEqual(ctx.exception.code, 2)
            mocked_run.assert_not_called()

    def test_webapp_main_proceeds_when_enabled(self) -> None:
        """显式开启后必须真正走到 uvicorn.run（证明开关是双向的，不是永久禁用）。"""
        import os

        import webapp

        argv = ["webapp.py", "--metrics-port", "0", ui_frozen.ENABLE_FLAG]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch("uvicorn.run") as mocked_run, \
                mock.patch("webbrowser.open"):
            os.environ.pop(ui_frozen.ENABLE_ENV, None)
            webapp.main()
            mocked_run.assert_called_once()


class UiTestFreezeSwitchTests(unittest.TestCase):
    """UI 测试的冻结开关本身也要有断言，否则「默认跳过」会变成「永远跳过」。"""

    def test_skip_if_frozen_raises_by_default(self) -> None:
        import os

        from tests._ui_freeze import FROZEN_REASON, skip_if_frozen

        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop(ui_frozen.ENABLE_ENV, None)
            with self.assertRaises(unittest.SkipTest) as ctx:
                skip_if_frozen()
            self.assertIn("FORGE_ENABLE_UI", str(ctx.exception))
            self.assertEqual(str(ctx.exception), FROZEN_REASON)

    def test_skip_if_frozen_passes_when_enabled(self) -> None:
        from tests._ui_freeze import skip_if_frozen

        with mock.patch.dict("os.environ", {ui_frozen.ENABLE_ENV: "1"}):
            skip_if_frozen()  # 不抛异常即为通过


class CliIsolationRuntimeTests(unittest.TestCase):
    """边界 3：干净子进程里导入 CLI，不得把 UI 模块拉进来。"""

    def _run(self, code: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def test_importing_cli_does_not_pull_ui_modules(self) -> None:
        code = (
            "import sys;"
            "import cli.app, cli.commands, cli.render, cli.store, cli.diagnostics;"
            "leaked=[m for m in ('webapp','llama_bridge') if m in sys.modules];"
            "print('LEAKED=' + ','.join(leaked))"
        )
        result = self._run(code)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("LEAKED=", result.stdout)
        self.assertTrue(result.stdout.strip().endswith("LEAKED="), result.stdout)

    def test_cli_commands_and_registry_load_in_clean_process(self) -> None:
        code = (
            "from cli.commands import build_registry;"
            "r=build_registry();"
            "print('N=' + str(len(r.all())) + ';EXIT=' + str(r.get('exit') is not None))"
        )
        result = self._run(code)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("EXIT=True", result.stdout)
        self.assertNotIn("N=0;", result.stdout)


if __name__ == "__main__":
    unittest.main()
