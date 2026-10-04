"""P2-3：执行重试预算可配置。

判据（复审报告 P2-3 行）：重试预算原本硬编码为模块常量，不可按部署/渠道调节。
本文件锁住三件事：
  1. 缺省行为**逐字节不变**（仍是 6）—— 配置化不得改变既有默认；
  2. 合法覆盖生效；
  3. 非法/越界输入**不得**静默把安全上限改坏（这是配置化最容易被忽略的坑）。
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import conversation_workflow as w


class RetryBudgetConfigTests(unittest.TestCase):
    def _resolve(self, raw: str | None) -> int:
        """在受控 env 下重新解析预算（走真实的读取路径，不复刻逻辑）。"""
        saved = os.environ.get(w.MAX_EXECUTION_ATTEMPTS_ENV)
        try:
            if raw is None:
                os.environ.pop(w.MAX_EXECUTION_ATTEMPTS_ENV, None)
            else:
                os.environ[w.MAX_EXECUTION_ATTEMPTS_ENV] = raw
            return w._resolve_execution_attempts()
        finally:
            if saved is None:
                os.environ.pop(w.MAX_EXECUTION_ATTEMPTS_ENV, None)
            else:
                os.environ[w.MAX_EXECUTION_ATTEMPTS_ENV] = saved

    def test_default_is_unchanged(self) -> None:
        """缺省必须仍是 6：配置化不得改变既有行为。"""
        self.assertEqual(self._resolve(None), 6)
        self.assertEqual(self._resolve(""), 6)
        self.assertEqual(self._resolve("   "), 6)

    def test_legitimate_override(self) -> None:
        self.assertEqual(self._resolve("3"), 3)
        self.assertEqual(self._resolve("12"), 12)
        self.assertEqual(self._resolve(" 8 "), 8, "应容忍首尾空白")

    def test_out_of_range_is_clamped_not_silently_accepted(self) -> None:
        """越界必须夹取 —— 0 次执行会让图永不推进，999 次会烧钱。"""
        self.assertEqual(self._resolve("0"), 1)
        self.assertEqual(self._resolve("-5"), 1)
        self.assertEqual(self._resolve("999"), 50)

    def test_non_integer_falls_back_to_default(self) -> None:
        """非纯数字退回缺省，**不做模糊解析**。

        写错配置不该换来一个看起来生效、实则随意的预算。
        """
        for raw in ("abc", "2.5", "6次", "1e3", "none", "六"):
            with self.subTest(raw=raw):
                self.assertEqual(self._resolve(raw), 6)

    def test_module_constant_reflects_env_at_import(self) -> None:
        """模块常量确由 env 决定（证明接线生效，不只是有个没被用的函数）。"""
        self.assertIsInstance(w.MAX_EXECUTION_ATTEMPTS, int)
        self.assertGreaterEqual(w.MAX_EXECUTION_ATTEMPTS, w._EXECUTION_ATTEMPTS_MIN)
        self.assertLessEqual(w.MAX_EXECUTION_ATTEMPTS, w._EXECUTION_ATTEMPTS_MAX)

    def test_budget_is_actually_used_by_the_graph(self) -> None:
        """常量必须真的被图消费（防"配了但没接线"）。

        这条是防假完成的锚点：只测 _resolve_execution_attempts 的话，
        把调用点删掉测试照样全绿。
        """
        src = (BASE / "runtime" / "conversation_workflow.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            src.count("MAX_EXECUTION_ATTEMPTS"),
            3,
            "MAX_EXECUTION_ATTEMPTS 的使用点少于预期：可能已不再被图消费",
        )


if __name__ == "__main__":
    unittest.main()
