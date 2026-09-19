"""UI 层冻结的测试开关（冻结范围与原因见项目根目录 `ui_frozen.py`）。

默认行为
--------
UI 相关测试**整模块跳过**：这些测试是有价值的资产（桥接契约、流式续传），
但当前阶段只优化后端，它们不该进入后端验收的绿色基线，也不该因为后端重构
而变红、掩盖真实回归。

恢复运行
--------
    set FORGE_ENABLE_UI=1
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_webapp.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FROZEN_REASON = (
    "前端 UI 已冻结（当前阶段只优化后端）：设置 FORGE_ENABLE_UI=1 后恢复运行；"
    "冻结范围与边界约定见 ui_frozen.py"
)


def ui_frozen() -> bool:
    """网页界面是否处于冻结（默认）状态。"""
    from ui_frozen import ui_enabled

    return not ui_enabled()


def skip_if_frozen() -> None:
    """在模块顶层调用：冻结时整模块跳过（unittest / pytest 都识别）。"""
    if ui_frozen():
        raise unittest.SkipTest(FROZEN_REASON)
