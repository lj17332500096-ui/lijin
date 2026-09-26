"""验证 M 键能打开模型 popup，↑↓/Enter/Esc 键盘交互正常工作。

模式与 test_tui.py 一致：async def _run() + asyncio.run(_run())，
run_test() 作为异步上下文管理器。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["FORGE_TEST_MODE"] = "1"

import pytest

from cli.tui.app import ForgeTuiApp

#: 模型列表由 App 启动时**异步**拉取（真实 HTTP 打网关 /models + 本地 /v1/models）。
#: 依赖真实网络时序会让这批用例在全量跑时随机失败：实测与 test_tui.py 同跑时，
#: 前面的 MCP/异步测试把线程池占满 → 150ms 内拉不回来 → picker._models 为空 →
#: arrow_nav 的 n>=1 断言失败、enter_confirms 取 _models[_selected] 抛 IndexError。
#: 这批用例验证的是 popup 的键盘交互，与模型来源无关，故注入固定列表使其确定性。
_FAKE_MODELS = [
    ("agnes-3.0-flash", "gateway"),
    ("agnes-2.5-flash", "gateway"),
    ("Ornith-9B", "local"),
]


@pytest.fixture(autouse=True)
def _stub_gateway_models(monkeypatch):
    """把模型拉取换成固定列表，去掉网络时序依赖（消除跨文件跑时的 flaky）。"""
    async def _fake(self):
        return list(_FAKE_MODELS)

    monkeypatch.setattr(ForgeTuiApp, "_query_gateway_models", _fake)


def test_m_opens_popup() -> None:
    """M 键打开 popup，焦点移到 popup body。"""
    async def _run():
        app = ForgeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = app._bar()
            assert bar.input.has_focus, "Input should have focus after startup"

            # 按 m → popup 打开
            await pilot.press("m")
            await pilot.pause(0.15)
            popup = app._model_popup()
            assert popup is not None, "model popup widget should exist"
            assert popup.is_visible(), "M key should open popup"
            assert app._model_picker_visible, "_model_picker_visible flag should be set"

            # 焦点应在 popup body
            body = popup._body
            assert body.has_focus, f"Popup body should have focus, focused widget: {bar.input.has_focus}"

    asyncio.run(_run())


def test_m_popup_arrow_nav() -> None:
    """popup 打开后 ↑↓ 移动选区。"""
    async def _run():
        app = ForgeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause(0.15)
            popup = app._model_popup()
            picker = app._model_picker()
            n = len(picker._models)
            assert n >= 1, f"should have at least 1 model, got {n}"

            sel0 = popup._selected
            # down → 下移
            await pilot.press("down")
            await pilot.pause(0.1)
            assert popup._selected == (sel0 + 1) % n, "down should move selection down"

            # up → 上移（回到原位）
            await pilot.press("up")
            await pilot.pause(0.1)
            assert popup._selected == sel0, "up should move selection back"

    asyncio.run(_run())


def test_m_popup_enter_confirms() -> None:
    """popup 打开后 Enter 确认选择，模型切换，popup 关闭。"""
    async def _run():
        app = ForgeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause(0.15)
            popup = app._model_popup()
            picker = app._model_picker()
            assert popup.is_visible()

            target = picker._models[popup._selected]
            await pilot.press("enter")
            await pilot.pause(0.15)
            assert not popup.is_visible(), "Enter should close popup"
            assert not app._model_picker_visible, "flag should be cleared"
            assert picker._current == target[0], (
                f"Model should switch to {target[0]}, got {picker._current}"
            )

    asyncio.run(_run())


def test_m_popup_esc_cancels() -> None:
    """popup 打开后 Esc 取消，焦点回到 Input。"""
    async def _run():
        app = ForgeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause(0.15)
            popup = app._model_popup()
            assert popup.is_visible()

            await pilot.press("escape")
            await pilot.pause(0.15)
            assert not popup.is_visible(), "Esc should close popup"
            assert not app._model_picker_visible, "flag should be cleared"
            bar = app._bar()
            assert bar.input.has_focus, "Focus should return to Input after Esc"

    asyncio.run(_run())


def test_m_toggle_open_close() -> None:
    """M 键两次：打开 → 再按 M → 关闭（toggle 行为）。"""
    async def _run():
        app = ForgeTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # 第一次 M → 打开
            await pilot.press("m")
            await pilot.pause(0.15)
            popup = app._model_popup()
            assert popup.is_visible(), "First M should open popup"

            # 第二次 M → 关闭（焦点在 popup body 上，m 键不消费 → binding 触发 toggle）
            await pilot.press("m")
            await pilot.pause(0.15)
            assert not popup.is_visible(), "Second M should close popup (toggle)"
            assert not app._model_picker_visible, "flag should be cleared after toggle close"

    asyncio.run(_run())
