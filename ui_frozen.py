"""前端 UI 冻结开关 —— 全项目唯一事实源。

背景
----
项目进入「只优化后端」阶段：前端 UI 层（`webapp.py` 的网页入口、`llama_bridge.py`
的 OpenAI 兼容 SSE 桥、`web/llama-ui` 与 `web/shared` 静态资源）整体停用。
停用之后，后端（runtime / agent / tools）与 CLI 消息平台的任何改动都不再需要
启动网页界面，网页界面也不参与后端验收。

为什么用开关而不是删代码
------------------------
UI 层带着一批有价值的资产（桥接契约测试、流式续传测试、SvelteKit 构建产物），
将来若要复用，显式打开即可，不必去 git 历史里翻。所以这里做的是「冻结」而非「删除」。

边界约定（由 `tests/test_ui_isolation.py` 强制）
------------------------------------------------
1. 后端核心（`runtime/**`、`agent.py`、`tools.py`、`schemas.py`）绝不 import
   `webapp` / `llama_bridge`；
2. CLI 消息平台（`cli/**`、`main.py`）绝不 import `webapp` / `llama_bridge`；
3. 网页入口在未显式开启时必须拒绝启动（退出码 2），而不是静默起一个服务。

开启方式（两种等价，仅排查/特殊需要时用）
----------------------------------------
    set FORGE_ENABLE_UI=1
    python webapp.py --enable-frozen-ui

关闭方式：不设置该环境变量、也不加该参数（即默认状态）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable

# 冻结基线：True = 默认停用网页界面。
# 改成 False 会让 UI 默认可用（不推荐，会破坏「只优化后端」的阶段约定）。
UI_FROZEN = True

ENABLE_ENV = "FORGE_ENABLE_UI"
ENABLE_FLAG = "--enable-frozen-ui"

_TRUTHY = {"1", "true", "yes", "on", "y"}


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def ui_enabled(argv: Iterable[str] | None = None) -> bool:
    """是否显式开启了网页界面。

    两条通道任一命中即视为开启：环境变量 `FORGE_ENABLE_UI=1`，或命令行带
    `--enable-frozen-ui`。
    """
    if not UI_FROZEN:
        return True
    if _env_truthy(ENABLE_ENV):
        return True
    return ENABLE_FLAG in tuple(argv or ())


def frozen_notice() -> str:
    """给用户看的中文说明（网页入口被拒绝时打印）。"""
    cli_cmd = r".\.venv\Scripts\python.exe main.py"
    ui_cmd = r".\.venv\Scripts\python.exe webapp.py"
    return (
        "═" * 60 + "\n"
        "  网页界面（前端 UI）已停用 —— 当前阶段只优化后端\n"
        + "═" * 60 + "\n"
        "本项目已切到「只对后端进行优化测试」的阶段：\n"
        "  · webapp.py        网页服务入口    —— 已冻结\n"
        "  · llama_bridge.py  UI 用 SSE 桥    —— 已冻结\n"
        "  · web/llama-ui     SvelteKit 前端  —— 已冻结\n"
        "\n"
        "请改用 CLI 消息平台（进程内直连 Runtime，错误信息最真实）：\n"
        f"    {cli_cmd}\n"
        "\n"
        "确实需要在本地临时起一次网页界面（排查 UI 自身问题时）：\n"
        f"    set {ENABLE_ENV}=1 && {ui_cmd}\n"
        f"    或：{ui_cmd} {ENABLE_FLAG}\n"
        "启用后 UI 相关测试也会一并恢复运行，用完请关掉，避免后端验收被 UI 干扰。\n"
        + "═" * 60
    )


def require_ui_enabled(argv: Iterable[str] | None = None, *, printer=print) -> bool:
    """网页入口的统一门禁：允许返回 True；拒绝则打印说明并返回 False。

    调用方在收到 False 时应以退出码 2 结束，避免被 shell 当成正常成功。
    """
    if ui_enabled(argv):
        printer("[UI] 已通过 FORGE_ENABLE_UI 显式开启：本次运行启用网页界面（冻结层）。")
        return True
    printer(frozen_notice())
    return False
