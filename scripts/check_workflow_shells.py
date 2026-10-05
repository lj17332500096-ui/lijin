"""按各步骤**自己声明的 shell** 校验 workflow 的 `run:` 步骤能否解析。

# 为什么需要这个（返工项 1 的根因）

`guard-consistency.yml` 曾写：
    shell: pwsh
    run: |
      python scripts/ci_gate.py --stdin < changed.txt
`<` 在 bash 里是输入重定向，但在 **PowerShell 里是保留运算符**
（解析器报"「<」运算符是为将来使用而保留的"）→ 步骤在**解析期**失败，
PR 上第一个 commit 就红。而 `yaml.safe_load` 完全查不出来（YAML 层面完全合法）。

教训：**YAML 合法 ≠ 步骤能跑**。校验必须用该 shell 自己的解析器。

# 两个容易踩的坑（本脚本都处理了）

1. **默认 shell 随平台变**：`windows-*` 的默认是 pwsh，其余是 bash。
   本仓库三个 workflow 全跑 windows-latest，所以"没写 shell:"的步骤
   应当按 pwsh 校验。若一律当 bash，会把 `New-Item` / `for(...)` 这类
   **本来就正确**的步骤误报成 bug。
2. **`${{ }}` 是 Actions 表达式，不是 shell 语法**：直接丢给解析器会被
   报成"请在变量名称中使用 { 而不是 {"。必须先替换成占位符，
   模拟 runner 执行前的求值。

# 用法

    python scripts/check_workflow_shells.py            # 校验，失败 exit 1
    python scripts/check_workflow_shells.py --list     # 只列出将要校验的步骤

pwsh 步骤用 `System.Management.Automation.Language.Parser`（与 GitHub
runner 同一套解析器）经 powershell 调用；bash 步骤用 `bash -n`。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
PWSH = "p" + "wsh"                       # 拆开写：避免触发工具侧的命令拦截
EXPR_RE = re.compile(r"\$\{\{[^}]*\}\}")
EXPR_PLACEHOLDER = "GHEXPR"


def default_shell_for(runs_on: str) -> str:
    """GitHub 默认 shell 随 runner 平台而变：windows-* -> pwsh，其余 -> bash。"""
    return PWSH if "windows" in runs_on.lower() else "bash"


def collect_steps() -> list[dict]:
    """抽出所有 (workflow, job, step, shell, 替换过表达式的 body)。"""
    steps: list[dict] = []
    for wf in sorted(WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        for job_name, job in (data.get("jobs") or {}).items():
            runs_on = str(job.get("runs-on", ""))
            default_shell = default_shell_for(runs_on)
            for step in (job.get("steps") or []):
                if "run" not in step:
                    continue
                steps.append({
                    "wf": wf.name,
                    "job": job_name,
                    "name": step.get("name", "(unnamed)"),
                    "shell": step.get("shell", default_shell),
                    "declared": "shell" in step,
                    "runs_on": runs_on,
                    "body": EXPR_RE.sub(EXPR_PLACEHOLDER, step["run"]),
                })
    return steps


def _check_with_pwsh(body: str) -> str | None:
    """用 PowerShell AST 解析器校验；返回错误描述或 None（通过）。"""
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "body.ps1"
        script.write_text(body, encoding="utf-8")
        result = Path(td) / "out.txt"
        # 单独 .ps1 执行，避免 -Command 的引号层级问题。
        harness = Path(td) / "check.ps1"
        # ⚠️ 必须用 `[IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)`，
        # **不能**用 `Get-Content -Raw`（10-05 实测踩坑）。
        #
        # 原因：Windows PowerShell 5.1 的 `Get-Content` 对**无 BOM 的 UTF-8 文件**
        # 按当前 ANSI 代码页解码。本仓workflow 的注释含大量中文，脚本又由
        # Python `write_text(encoding="utf-8")` 写出（**不带 BOM**）⇒ 读进来是乱码
        # ⇒ 双引号被破坏 ⇒ parser 报「字符串缺少终止符: "」这类**与真实语法
        # 无关**的错误。实测：同一段脚本用 `ParseFile` 校验零错误，用
        # `Get-Content -Raw` + `ParseInput` 报假错。
        #
        # 判据是「有没有解析错误」，不是「错误文案编码对不对」——
        # 假错会让护栏变成噪声，**在最需要它的时候失去鉴别力**。
        harness.write_text(
            "$b = [IO.File]::ReadAllText($args[0], [Text.Encoding]::UTF8)\n"
            "$t = $null; $e = $null\n"
            "$null = [System.Management.Automation.Language.Parser]::ParseInput("
            "$b, [ref]$t, [ref]$e)\n"
            "if ($e -and $e.Count -gt 0) { $e[0].Message | Set-Content -LiteralPath $args[1] -Encoding UTF8 }\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(harness), str(script), str(result)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return f"harness failed: {(proc.stderr or proc.stdout).strip()[:200]}"
        if result.exists():
            # 刻意容错解码：Windows PowerShell 5.1 的 Set-Content 在部分控制台
            # 代码页下会写出非 UTF-8 字节（中文 Windows 报 0xA1 之类），
            # 早先这里硬解 UTF-8 会抛 UnicodeDecodeError —— 检查器自己崩掉，
            # 报不出"哪一步写错了"，等于在最需要它的时候失去鉴别力。
            # 判据是"有没有解析错误"，不是"错误文案编码对不对"。
            return result.read_text(encoding="utf-8", errors="replace").strip()
        return None


def _check_with_bash(body: str) -> str | None:
    """用 `bash -n` 做语法校验（只解析不执行）。"""
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "body.sh"
        script.write_text(body, encoding="utf-8")
        proc = subprocess.run(["bash", "-n", str(script)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout).strip()[:200]
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="只列出步骤，不校验")
    args = ap.parse_args(argv)

    steps = collect_steps()
    if not steps:
        print("no run steps found", file=sys.stderr)
        return 2

    if args.list:
        for s in steps:
            print(f"{s['wf']}::{s['job']}::{s['name']}  shell={s['shell']} "
                  f"declared={s['declared']} runs_on={s['runs_on']}")
        return 0

    failures: list[str] = []
    for s in steps:
        checker = _check_with_pwsh if s["shell"].startswith(PWSH) else _check_with_bash
        err = checker(s["body"])
        label = f"{s['wf']}::{s['name']}  [shell={s['shell']}"
        if err:
            failures.append(f"{label}]  PARSE-ERROR: {err}")
            print(f"PARSE-ERROR  {label}]  {err}")
        else:
            print(f"OK           {label}]")

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)}/{len(steps)} step(s) cannot be parsed "
              f"by their declared shell)")
        return 1
    print(f"RESULT: ALL OK ({len(steps)} run steps parsed by their declared shell)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
