"""通用 pre-commit 门禁：按改动文件精确选测试，并支持「已知失败基线」。

设计要点（2026-09-26 重构）：
1. **精确选子集**：改动文件 → 测试文件的映射集中在本文件 CHANGE_TEST_MAP，
   不再散落在 bash 钩子里。没有映射的改动不跑测试（避免"改一行注释触发全量"）。
2. **已知失败基线**：`.ci/known_failures.txt` 里登记的失败只警告、不阻断；
   未登记的失败才阻断。每条基线**必须写原因**（否则脚本报错），防止把红灯
   无声无息地永久化 —— 基线是"已确认并排期"的凭证，不是免检牌。
3. 退出码：0 = 放行；1 = 有未登记的失败；2 = 用法/解析错误。

用法：
    # 按改动文件选子集
    # 从 stdin 读改动文件列表（pre-commit 钩子走这条）
    git diff --cached --name-only | python scripts/ci_gate.py --stdin
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_FILE = PROJECT_ROOT / ".ci" / "known_failures.txt"

#: 改动文件（精确路径）→ 需要跑的测试文件。顺序即去重后的执行顺序。
CHANGE_TEST_MAP: list[tuple[tuple[str, ...], list[str]]] = [
    (("cli/tui/app.py", "cli/tui/panels.py", "cli/tui/models.py"),
     ["tests/test_tui.py"]),
    (("runtime/approval.py", "runtime/errors.py"),
     ["tests/test_approval.py"]),
    (("runtime/public_activity.py",),
     ["tests/test_public_activity.py"]),
    (("runtime/provider_gateway.py",),
     ["tests/test_provider_errors.py"]),
]

VENV_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)


def tests_for_changed(changed: list[str]) -> list[str]:
    """按映射表选出测试文件（去重、保持顺序）。"""
    out: list[str] = []
    for patterns, tests in CHANGE_TEST_MAP:
        if any(c in patterns for c in changed):
            for t in tests:
                if t not in out:
                    out.append(t)
    return out


def load_baseline() -> dict[tuple[str, str], str]:
    """读已知失败基线 → {(文件路径, 用例名): 原因}。

    行格式：`tests/foo.py::test_bar   原因（必填）`
    缺原因的行视为格式错误，直接抛错 —— 宁可让提交失败，也不允许无理由的红灯常驻。
    """
    if not BASELINE_FILE.exists():
        return {}
    entries: dict[tuple[str, str], str] = {}
    for lineno, raw in enumerate(BASELINE_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = line.split("#", 1)[0].strip() if "#" in line else line
        comment = line.split("#", 1)[1].strip() if "#" in line else ""
        if "::" not in body:
            raise SystemExit(
                f"{BASELINE_FILE}:{lineno} 基线格式错误（需 path::test_name）：{raw!r}\n"
                f"   示例：tests/test_runtime.py::test_x  原因：xxx"
            )
        if not comment:
            raise SystemExit(
                f"{BASELINE_FILE}:{lineno} 基线缺原因说明（# 原因：…），拒绝加载"
            )
        path, name = body.split("::", 1)
        entries[(path.strip(), name.strip())] = comment
    return entries


def run_tests(tests: list[str], junit_path: Path) -> int:
    cmd = [str(VENV_PY), "-m", "pytest", *tests, "-q",
           f"--junitxml={junit_path}"]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    return result.returncode


def collect_failures(junit_path: Path) -> list[tuple[str, str]]:
    """从 junit xml 里取 (文件路径, 用例名) 列表。"""
    if not junit_path.exists():
        return []
    root = ET.parse(junit_path).getroot()
    out: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        failed = any(child.tag in ("failure", "error") for child in case)
        if not failed:
            continue
        cls = case.get("classname") or ""
        name = case.get("name") or ""
        parts = cls.split(".")
        path = "/".join(parts[:2]) + ".py" if len(parts) >= 2 else cls
        out.append((path, name))
    return out


def main(argv: list[str]) -> int:
    tests: list[str] = []
    changed: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tests":
            tests.append(argv[i + 1]); i += 2
        elif a == "--changed":
            changed.append(argv[i + 1]); i += 2
        elif a == "--stdin":
            changed.extend(
                l.strip() for l in sys.stdin.read().splitlines() if l.strip()
            )
            i += 1
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            return 2
        if i > len(argv):
            break

    explicit = bool(tests)
    if not tests:
        tests = tests_for_changed(changed)
    if not tests:
        print("[CI Gate] 改动文件无对应测试映射，跳过。")
        return 0

    why = "显式指定" if explicit else "改动命中映射"
    print(f"[CI Gate] {why}，跑：{', '.join(tests)}")
    baseline = load_baseline()
    with tempfile.TemporaryDirectory() as td:
        junit = Path(td) / "junit.xml"
        rc = run_tests(tests, junit)
        if rc == 0:
            print("\n[CI Gate] PASSED — 允许提交。")
            return 0
        failures = collect_failures(junit)
        blocking: list[tuple[str, str]] = []
        for f in failures:
            reason = baseline.get(f)
            if reason:
                print(f"[CI Gate] 已知失败（不阻断）：{f[0]}::{f[1]} —— {reason}")
            else:
                blocking.append(f)
        if not blocking:
            print("\n[CI Gate] 失败全部在基线内 —— 允许提交（请尽快排期修复）。")
            return 0
        print("\n[CI Gate] FAILED — 阻断提交。以下失败未登记基线：", file=sys.stderr)
        for f in blocking:
            print(f"    {f[0]}::{f[1]}", file=sys.stderr)
        print(f"\n  确认是既有问题且不阻塞本提交时，把它加进 {BASELINE_FILE}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
