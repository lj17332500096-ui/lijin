"""`runtime_paths` 的 `.env` 加载语义必须是「真实env 恒胜」，且要被测试锁住。

**为什么这个看似无害的 `override=False` 值得一条测试**：

`runtime_paths` 被**所有模块传递导入**（`runtime_paths` / `code_exec` / `tools` /
`skills_loader` / `rag` / `project_edit` / `integrations.*` 全部经它 POLLUTES）。
它在 **import 期**执行 `load_dotenv(PROJECT_ROOT/".env", override=False)`，
是整个进程里 `.env` 唯一的注入口。

`override=False` 的语义是：**真实环境变量恒胜，`.env` 只能填空、不能降级**。
这正是 P0-2 启动护栏能被信任的前提 —— 运维显式设
`FORGE_APPROVAL_FAILCLOSED=off` 或 `FORGE_UNATTENDED=` 时，`.env` 里残留的
`on` / `scheduled` **无法把它改回去**。护栏判定读到的永远是显式声明的意图。

**风险在于这个参数没有任何测试保护**：有人为了「让 `.env` 里的新配置生效」，
很自然会把它改成 `override=True`（这是 python-dotenv 的默认值，也是最常见的
「修 bug」动作）。那样一来 `.env` 就获得了**降级显式安全配置**的能力，
且**不会报任何错** —— 护栏从 fail-closed 静默退化为看 `.env` 说话。
这类改动不会有任何现有测试变红，因此必须在此显式钉住。

判据选择（见 MEMORY.md §2.1）：这里测的是**可观测行为**（子进程里
`os.environ` 的最终值），不是 grep 源码里的 `override=False` 字面量 ——
后者只证明文本在，不证明语义在。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATHS = PROJECT_ROOT / "runtime_paths.py"


def _run_child(code: str, env: dict[str, str], *, dotenv_body: str = "") -> str:
    """在独立子进程里跑一段代码并返回 stdout。

    必须在子进程里跑：`.env` 加载与 `RUNTIME_ROOT` 求值都发生在 import 期，
    同进程内无法在import 后改写结果（这正是「配置在 import 期加载 =
    隐藏全局单例」那条铁律的体现）。

    `dotenv_body` 非空时，先在**临时工程目录**里造一个带 `.env` 的副本，
    用于验证「`.env` 里的值与真实 env 冲突时谁胜」。
    """
    envs = {k: v for k, v in env.items() if v is not None}
    if dotenv_body:
        import tempfile

        td = Path(tempfile.mkdtemp())
        (td / "runtime_paths.py").write_text(
            RUNTIME_PATHS.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (td / ".env").write_text(dotenv_body, encoding="utf-8")
        target = td / "runtime_paths.py"
    else:
        target = RUNTIME_PATHS

    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(target.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": "", **envs},
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"子进程失败 EXIT={proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout.strip()


class DotenvPrecedenceTests(unittest.TestCase):
    """`.env` **只能填空，不能降级** —— 这条语义必须有测试保护。"""

    PROBE = """
        import runtime_paths, os
        print(os.environ.get("FORGE_APPROVAL_FAILCLOSED"))
    """

    def test_real_env_wins_over_dotenv(self) -> None:
        """真实 env 显式设 `off` 时，`.env` 里的 `on` **不能**把它改回on。

        这是本文件存在的核心理由：一旦这条不成立，`.env` 就获得了覆盖
        运维显式安全配置的能力，fail-closed 护栏形同虚设。
        """
        got = _run_child(
            self.PROBE,
            {"FORGE_APPROVAL_FAILCLOSED": "off"},
            dotenv_body="FORGE_APPROVAL_FAILCLOSED=on\n",
        )
        self.assertEqual(
            got, "off",
            "`.env` 覆盖了真实环境变量 —— `load_dotenv` 的 `override` 很可能被"
            "改成了 True。这会让 `.env` 能降级显式安全配置，且不报任何错。",
        )

    def test_dotenv_fills_gap_when_env_absent(self) -> None:
        """真实 env 未设时，`.env` **应当**能填上（否则 `.env` 就完全没用了）。"""
        got = _run_child(
            self.PROBE,
            {"FORGE_APPROVAL_FAILCLOSED": None},
            dotenv_body="FORGE_APPROVAL_FAILCLOSED=on\n",
        )
        self.assertEqual(
            got, "on",
            "真实 env 缺席时 `.env` 也没能填上 —— `override=False` 之外还有别的"
            "东西在丢值（检查 load_dotenv 是否被 `except: pass` 吞掉）。",
        )

    def test_unattended_declaration_cannot_be_downgraded_by_dotenv(self) -> None:
        """`FORGE_UNATTENDED` 同理：`.env` 不能把「显式清空」变回「已声明」。

        `FORGE_UNATTENDED` 是 fail-closed 护栏的放行凭据，语义是
        「非法值视为未声明」。若 `.env` 能注入 `scheduled`，
        就等于让「没声明 unattended」悄悄变成「声明了 unattended」——
        审批护栏会被静默放宽。空字符串与未设必须同义。
        """
        code = """
            import runtime_paths, os
            print(repr(os.environ.get("FORGE_UNATTENDED")))
        """
        got = _run_child(
            code,
            {"FORGE_UNATTENDED": ""},
            dotenv_body="FORGE_UNATTENDED=scheduled\n",
        )
        self.assertEqual(
            got, "''",
            "空字符串被 `.env` 里的 `scheduled` 覆盖了 —— 显式的「未声明」"
            "被静默升级成「已声明 unattended」，审批护栏被放宽。",
        )


class RuntimeRootResolutionTests(unittest.TestCase):
    """`FORGE_RUNTIME_DIR` 的解析：绝对/相对路径 + legacy 回退的前提。"""

    def test_absolute_runtime_dir_is_honored(self) -> None:
        code = """
            import runtime_paths
            print(runtime_paths.RUNTIME_ROOT_CONFIGURED,
                  runtime_paths.RUNTIME_ROOT.is_absolute())
        """
        target = (PROJECT_ROOT / "var" / "_ci_probe_runtime_root").resolve()
        got = _run_child(code, {"FORGE_RUNTIME_DIR": str(target)})
        self.assertEqual(
            got, "True True",
            f"显式 FORGE_RUNTIME_DIR 绝对路径未被采纳（读到 {got}）",
        )

    def test_relative_runtime_dir_is_anchored_to_project_root(self) -> None:
        """相对路径必须锚到 `PROJECT_ROOT`，不能锚到 CWD。

        锚到 CWD 的话，同一份配置在不同启动目录下会写到不同地方 ——
        「换个目录启动就丢了状态」这类故障极难排查。
        """
        code = """
            import runtime_paths
            print(runtime_paths.RUNTIME_ROOT == runtime_paths.PROJECT_ROOT / "relprobe")
        """
        got = _run_child(code, {"FORGE_RUNTIME_DIR": "relprobe"})
        self.assertEqual(
            got, "True",
            "相对 FORGE_RUNTIME_DIR 未锚到 PROJECT_ROOT —— 换目录启动会写到别处。",
        )


if __name__ == "__main__":
    unittest.main()
