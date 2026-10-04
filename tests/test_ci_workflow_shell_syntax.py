"""CI workflow 的"按 shell 可解析性"回归测试。

# 为什么这条必须存在

`guard-consistency.yml` 曾写 `python scripts/ci_gate.py --stdin < changed.txt`。
该 job 是 `windows-latest` + `shell: pwsh`，而 `<` 在 PowerShell 里是
**保留运算符** → 步骤在**解析期**就失败，PR 上第一个 commit 必红。
YAML 层面完全合法，所以 `yaml.safe_load` / actionlint 之类的 YAML 校验查不出来。

本测试把 `scripts/check_workflow_shells.py` 的能力固化成 pytest 用例，
使"改 workflow 忘了换 shell 语法"在本地就红，而不是等 push 之后。

# 判据为什么用"真实解析"而不是文本匹配

不写 `assertNotIn(" < ", text)` 这种文本检查 ——
它会误伤（bash 步骤里的合法重定向）也会漏放（换一种写法仍错）。
必须用**该 shell 自己的解析器**。本测试直接调用那个脚本，
并额外断言"三个 workflow 都被覆盖到"，防止检查器悄悄只扫一个文件。
"""

import subprocess
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCRIPT = BASE / "scripts" / "check_workflow_shells.py"
WORKFLOW_DIR = BASE / ".github" / "workflows"

REQUIRED_WORKFLOWS = {"ci.yml", "guard-consistency.yml", "nightly.yml"}


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": ""}
    import os
    merged = dict(os.environ)
    merged.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=BASE, env=merged,
    )


class WorkflowShellSyntaxTests(unittest.TestCase):
    def test_all_run_steps_parse_with_their_declared_shell(self) -> None:
        """核心判据：每个 run 步骤都能被它自己声明的 shell 解析。"""
        proc = _run()
        self.assertEqual(
            proc.returncode, 0,
            "有步骤无法被其声明的 shell 解析（CI 上会红）：\n"
            + proc.stdout + proc.stderr,
        )
        self.assertIn("RESULT: ALL OK", proc.stdout)

    def test_all_three_workflows_are_covered(self) -> None:
        """防"检查器只扫了一个文件却报 OK"。"""
        proc = _run("--list")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        found = {line.split("::", 1)[0] for line in proc.stdout.splitlines() if "::" in line}
        self.assertEqual(
            REQUIRED_WORKFLOWS, found,
            f"检查覆盖的 workflow 集合不对：{found}",
        )

    def test_powershell_steps_are_actually_parsed_as_powershell(self) -> None:
        """本仓库全跑 windows-latest，默认 shell 是 pwsh 而非 bash。

        若把默认当成 bash，`New-Item` / `for (...)` 这类**本来就正确**的
        步骤会被误报成 bug —— 那是把没问题的代码"修坏"。
        故显式断言解析器认的是 pwsh。
        """
        proc = _run("--list")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        shells = {line.split("shell=")[1].split()[0]
                  for line in proc.stdout.splitlines() if "shell=" in line}
        self.assertEqual(shells, {"pwsh"}, f"shell 解析结果异常：{shells}")

    def test_no_shell_operator_smuggling_in_declared_steps(self) -> None:
        """显式声明 pwsh 的步骤不得含 bash 输入重定向。

        这是本条缺陷的**直接**判据（比通用解析更精准，报错更直白）。
        仅对 `shell: pwsh` 的步骤生效 —— bash 步骤里的 `<` 是合法的。
        """
        offenders: list[str] = []
        for wf in sorted(WORKFLOW_DIR.glob("*.yml")):
            text = wf.read_text(encoding="utf-8")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if "shell: pwsh" not in line:
                    continue
                # 从 shell 声明往后找本步骤的 run 体（到下一个 "- name/uses"）
                for probe in lines[i + 1:]:
                    stripped = probe.strip()
                    if stripped.startswith("- name:") or stripped.startswith("- uses:"):
                        break
                    if stripped in ("run: |", "run: >-"):
                        break
                    body = stripped
                    if re_redirect(body):
                        offenders.append(f"{wf.name}: {stripped}")
        self.assertEqual(
            offenders, [],
            "pwsh 步骤里出现 bash 输入重定向（PowerShell 中 '<' 是保留运算符）：\n  "
            + "\n  ".join(offenders),
        )


def re_redirect(body: str) -> bool:
    """判断一行是否是 bash 的 `< file` 输入重定向（而非 PowerShell 的 -lt 等）。"""
    if "<" not in body:
        return False
    # PowerShell 的比较运算符写法是 `-lt` / `< ` 跟在变量左边；这里只抓
    # "命令 ... < 文件" 这种重定向形态：< 前面有内容且后面不是比较表达式。
    idx = body.find("<")
    if idx < 0:
        return False
    tail = body[idx + 1:].strip()
    if not tail:                      # `... <` 结尾仍是重定向
        return True
    # 形如 `a < b` 若是纯比较（含空格等号/变量）则不是重定向；
    # 重定向的右侧通常是文件名（含扩展名或路径）。
    return bool(tail) and ("." in tail or "/" in tail) and " " not in tail


if __name__ == "__main__":
    unittest.main()
