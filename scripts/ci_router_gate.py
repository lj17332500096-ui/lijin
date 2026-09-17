"""CI 钩子：tests/test_tool_router.py 必须全过才允许合入。

用法：
  1. 本地 pre-commit：`python scripts/ci_router_gate.py`
  2. GitHub Actions：调用同一脚本作为 CI 步骤
  3. 退出码 0 = 全过；非 0 = 有失败 → 阻断合入

依赖：pytest（项目 .venv 里已有）
"""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS = ["tests/test_tool_router.py"]
VENV_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)


def main() -> int:
    print(f"[CI Router Gate] Running {', '.join(TESTS)} ...")
    cmd = [str(VENV_PY), "-m", "pytest", *TESTS, "-x", "-q"]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print("\n[CI Router Gate] FAILED — 阻断合入。修复后重跑。", file=sys.stderr)
        return 1
    print("\n[CI Router Gate] PASSED — 允许合入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
