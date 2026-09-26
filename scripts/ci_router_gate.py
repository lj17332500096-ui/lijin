"""CI 钩子：tests/test_tool_router.py 必须全过才允许合入（薄封装）。

用法：
  1. 本地 pre-commit（通用门禁已接管，见 scripts/ci_gate.py）
  2. GitHub Actions：`.github/workflows/router-gate.yml` 调用本脚本
  3. 退出码 0 = 全过；非 0 = 有失败 → 阻断合入

2026-09-26：测试选择与基线判定统一下沉到 scripts/ci_gate.py，本脚本保留
原有命令行接口（无参数 = 跑 router 测试）以兼容既有 CI 配置。

依赖：pytest（项目 .venv 里已有）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_gate import main  # noqa: E402

ROUTER_TESTS = ["tests/test_tool_router.py"]

if __name__ == "__main__":
    argv = sys.argv[1:] or ["--tests", *ROUTER_TESTS]
    sys.exit(main(argv))
