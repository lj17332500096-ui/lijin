"""离线回归测试入口：python tests/run_tests.py [--exclude mod1,mod2]

不调用任何模型/网络，确定性离线跑完约 2 分钟；全部通过返回 0，否则返回 1。
--exclude：逗号分隔的模块名（不含 tests. 前缀，如 test_theme_cdp），
           用于在“无浏览器/无后端”环境跳过需浏览器的主题 CDP 用例。
"""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    exclude: set[str] = set()
    i = 0
    while i < len(argv):
        if argv[i] == "--exclude" and i + 1 < len(argv):
            exclude.update(n.strip() for n in argv[i + 1].split(",") if n.strip())
            i += 2
        else:
            i += 1
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    modules = sorted(
        p.stem for p in (BASE / "tests").glob("test_*.py")
        if p.stem.startswith("test_")
    )
    for name in modules:
        if name in exclude:
            continue
        suite.addTests(loader.loadTestsFromName(f"tests.{name}"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
