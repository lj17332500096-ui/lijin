"""Laya 前置二分类快筛器（runtime/laya_router.py）单测。

默认环境（未装 laya/torch）走降级路径：enabled=False、screen=None，
现有 tool_router 不受影响。mock 场景验证高/低置信度与开关行为。
"""
from __future__ import annotations

import importlib.util
import unittest
from unittest import mock

import runtime.laya_router as lr


def _laya_installed() -> bool:
    return bool(
        importlib.util.find_spec("laya")
        and importlib.util.find_spec("torch")
    )


class LayaRouterAvailableTests(unittest.TestCase):
    def test_available_reflects_real_install(self):
        self.assertEqual(lr.laya_available(), _laya_installed())

    def test_router_enabled_false_when_laya_missing(self):
        # 未装 laya/torch 时，无论 env 怎么设都不得启用
        with mock.patch.dict("os.environ", {"FORGE_LAYA": "on"}):
            with mock.patch.object(lr, "laya_available", return_value=False):
                self.assertFalse(lr.laya_router_enabled())

    def test_router_off_env_disables(self):
        with mock.patch.object(lr, "laya_available", return_value=True):
            with mock.patch.dict("os.environ", {"FORGE_LAYA": "off"}):
                self.assertFalse(lr.laya_router_enabled())

    def test_router_on_env_enables_when_available(self):
        with mock.patch.object(lr, "laya_available", return_value=True):
            with mock.patch.dict("os.environ", {"FORGE_LAYA": "on"}):
                self.assertTrue(lr.laya_router_enabled())


class LayaRouterScreenTests(unittest.TestCase):
    """screen 的置信度逻辑（不依赖真实模型，mock Router）。"""

    def test_screen_returns_none_when_router_unbuilt(self):
        # 未装 laya 时 _router 为 None → 一律回落 None
        r = lr.LayaRouter.__new__(lr.LayaRouter)
        r._router = None
        r.last_result = None
        self.assertIsNone(r.screen("hello"))

    def test_high_confidence_text_shortcircuits(self):
        r = lr.LayaRouter.__new__(lr.LayaRouter)
        r.last_result = None

        fake = mock.Mock()
        # 模拟 Laya 结果：text 选项校准概率 0.95 ≥ 阈值 0.85 → direct_text
        fake.predict.return_value = {"probabilities": {"text": 0.95, "tool": 0.05}}
        r._router = fake

        with mock.patch.object(lr, "laya_router_confidence_threshold", return_value=0.85):
            self.assertEqual(r.screen("what is a black hole?"), "direct_text")

    def test_high_confidence_tool_shortcircuits(self):
        r = lr.LayaRouter.__new__(lr.LayaRouter)
        r.last_result = None

        fake = mock.Mock()
        # tool 概率 0.9 → text 概率 0.1 < 阈值 → 判 tool_needed（1-text=0.9 ≥ 阈值）
        fake.predict.return_value = {"probabilities": {"text": 0.10, "tool": 0.90}}
        r._router = fake

        with mock.patch.object(lr, "laya_router_confidence_threshold", return_value=0.85):
            self.assertEqual(r.screen("帮我写一个排序算法"), "tool_needed")

    def test_low_confidence_falls_back(self):
        r = lr.LayaRouter.__new__(lr.LayaRouter)
        r.last_result = None

        fake = mock.Mock()
        # 概率接近 0.5/0.5，两边都 < 阈值 → None 回落
        fake.predict.return_value = {"probabilities": {"text": 0.5, "tool": 0.5}}
        r._router = fake

        with mock.patch.object(lr, "laya_router_confidence_threshold", return_value=0.85):
            self.assertIsNone(r.screen("ambiguous query"))

    def test_predict_exception_falls_back(self):
        r = lr.LayaRouter.__new__(lr.LayaRouter)
        r.last_result = None

        fake = mock.Mock()
        fake.predict.side_effect = RuntimeError("model load failed")
        r._router = fake

        self.assertIsNone(r.screen("any query"))

    def test_extract_confidence_missing_field(self):
        # 字段名不对齐时返回 None → screen 回落
        self.assertIsNone(lr.LayaRouter._extract_confidence({"weird": 1}, "text"))
        self.assertIsNone(lr.LayaRouter._extract_confidence({}, "text"))
        self.assertEqual(
            lr.LayaRouter._extract_confidence({"answers": {"text": 0.9}}, "text"), 0.9
        )


class LayaBugRegressionTests(unittest.TestCase):
    """P0 三连 bug 回归：F1 类调用实例方法 / F2 返回类型 / F3 模块级加载。"""

    def test_module_level_function_not_class_call(self):
        # F1：laya_fast_screen 是模块级函数，可被直接调用，不抛 TypeError
        with mock.patch.object(lr, "laya_router_enabled", return_value=False):
            self.assertIsNone(lr.laya_fast_screen("今天天气"))

    def test_fast_screen_returns_none_on_exception(self):
        # F1 强化：即使构造/推理异常，模块级入口也吞掉返回 None，不冒泡
        with mock.patch.object(lr, "laya_router_enabled", return_value=True):
            with mock.patch.object(lr, "laya_router", side_effect=RuntimeError("boom")):
                self.assertIsNone(lr.laya_fast_screen("any query"))

    def test_singleton_is_lazy(self):
        # F3：模块 import 后，未显式调用前 _SINGLETON 为 None（不再 import 即构造）
        lr._reset_singleton_for_test()
        self.assertIsNone(lr._SINGLETON)
        self.assertIsNone(lr.laya_screen_result())

    def test_classify_fallback_when_router_unbuilt(self):
        # classify 在 _router=None 时返回全 None 的 fallback，不抛
        r = lr.LayaRouter.__new__(lr.LayaRouter)
        r._router = None
        r.last_decision = None
        # Prevent _ensure() from loading an installed Laya model during a unit
        # test that specifically verifies the no-router fallback.  The previous
        # version could initialize a real checkpoint here and turn this into a
        # slow, environment-dependent model test.
        with mock.patch.object(r, "_ensure"):
            d = r.classify("hello")
        self.assertIsNone(d.intent)
        self.assertIsNone(d.route)


if __name__ == "__main__":
    unittest.main()
