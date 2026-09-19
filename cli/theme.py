"""CLI 前端的轻量样式层。

设计约束
--------
1. 不引入第三方依赖（项目至今只用已装的 SDK / starlette，终端界面不该再加依赖）；
2. 控制台不支持颜色时**自动降级为纯文本**，重定向到文件/管道时不写 ANSI；
3. Windows 上主动打开 VT 处理（否则 Git Bash / 旧 conhost 会把 ESC 序列打出来）；
4. 颜色开关可被测试强制指定，避免结果依赖运行环境。
"""

from __future__ import annotations

import os
import re
import shutil
import sys

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "grey": "90",
}

_TRUTHY = {"1", "true", "yes", "on", "y"}

_color = False


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _enable_windows_vt() -> bool:
    """在 Windows 控制台打开 VT（虚拟终端）处理；失败则视为不支持颜色。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                return False
        return True
    except Exception:
        return False


def configure(stream=None, force: bool | None = None) -> bool:
    """决定是否启用颜色，返回最终状态。

    force=True/False 直接指定（测试用）；None 时按 NO_COLOR / FORCE_COLOR /
    isatty / VT 支持自动判断。
    """
    global _color
    if force is not None:
        _color = bool(force)
        return _color
    if _env_truthy("NO_COLOR"):
        _color = False
        return False
    if _env_truthy("FORCE_COLOR"):
        _enable_windows_vt()
        _color = True
        return True
    target = stream if stream is not None else sys.stdout
    if not getattr(target, "isatty", lambda: False)():
        _color = False
        return False
    _color = _enable_windows_vt()
    return _color


def enabled() -> bool:
    return _color


def style(text: object, *names: str) -> str:
    """给文本套样式；未启用颜色时原样返回。"""
    body = str(text)
    if not _color or not names:
        return body
    codes = ";".join(_CODES[n] for n in names if n in _CODES)
    if not codes:
        return body
    return f"\x1b[{codes}m{body}\x1b[0m"


def strip(text: object) -> str:
    """去掉 ANSI 序列（写日志、比较文本时用）。"""
    return _ANSI_RE.sub("", str(text))


def width(default: int = 100) -> int:
    """终端可用宽度，限制在 [48, 200]，取不到时用 default。"""
    try:
        columns = shutil.get_terminal_size(fallback=(default, 24)).columns
    except Exception:
        return default
    return max(48, min(int(columns), 200))


def dim(text: object) -> str:
    return style(text, "dim")


def ok(text: object) -> str:
    return style(text, "green")


def warn(text: object) -> str:
    return style(text, "yellow")


def err(text: object) -> str:
    return style(text, "red")


def head(text: object) -> str:
    return style(text, "bold", "cyan")


def tag(text: object) -> str:
    return style(text, "magenta")
