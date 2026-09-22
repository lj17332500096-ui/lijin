"""结构化日志（审计 D3 / P2-3 落地）：单点 JSON 行格式 + run_id 贯穿。

# 设计边界（与审计报告 §19 的取舍一致）
- **不是全项目重写**：只在 3 个关键模块（runner / task_manager / provider_gateway）
  的**既有日志点**上启用 JSON 行格式，共 12 处，不新增日志点。
- **request_id/run_id 贯穿**：日志行的 `run_id` 字段由 formatter 自动从
  `RunContext.current()` 取（Run 内有效，无 Run 时缺省），调用方**无需**手工传键。
  这与事件/审计的 run_id = task.id 同源（见 `runner._bind_runctx`），
  因此 grep `run_id=...` 可同时命中事件流与日志流。
- **opt-in**：`FORGE_STRUCTURED_LOG=1` 才挂 JSON handler（默认 stdout）；
  `FORGE_STRUCTURED_LOG_FILE` 可改输出到文件。默认关闭 → 全量测试基线不受影响，
  生产/排障时再开。
- **不落盘在 `_ensure_tracing` 那套审计事件里**：那是结构化事件（SQLite），
  本模块是**运维日志**（stdout/file），两者互补不替代。

# 为什么用 contextvar 而不是日志字段手工传
调用点（runner 10 处 + task_manager 2 处）已有 `task_id`/`run_rid` 局部变量，
手工传键在并发 Run 间会**串台**（同一线程串行日志没问题，但异步 to_thread
工具里上下文已复制）。formatter 在**写日志的那一刻**读 `RunContext.current()`，
与事件流的 `run_id` 天然同源。

用法（调用点一行）：
    from runtime.structured_log import slog
    slog.warning("义务门拦截", where=..., task=..., missing=...)
调用点用 `slog` 别名，避免与既有 `_logger`（标准库、测试/第三方依赖）混淆；
`slog` 输出走 opt-in 的 JSON handler，未开启时**静默无输出**（不破坏现有行为）。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

__all__ = ["slog", "install_structured_logging", "new_request_id",
           "current_request_id", "STRUCTURED_LOG_NAME"]

# 独立 logger 名：与模块默认 logger（`__name__`）分离，避免标准库
# 的第三方调用（SDK、starlette）误用 JSON 格式。
STRUCTURED_LOG_NAME = "forge.structured"

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "forge_request_id", default=None)


def new_request_id(prefix: str = "req") -> str:
    """生成一个新 request_id 并绑定到当前 context（跨 await 生效）。

    HTTP 入口（llama_bridge）每请求调一次；非 HTTP 入口（CLI/main）
    每 turn 调一次。后续日志行自动带上该 id。
    """
    rid = f"{prefix}-{uuid.uuid4().hex[:12]}"
    _request_id.set(rid)
    return rid


def current_request_id() -> str | None:
    return _request_id.get()


def _run_id() -> str | None:
    """Run 内的 run_id（= task.id）；无 Run 时 None。延迟导入避免循环。"""
    try:
        from runtime.runctx import current
        ctx = current()
        return getattr(ctx, "run_id", None) if ctx is not None else None
    except Exception:
        return None


class _StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": _run_id(),
            "request_id": _request_id.get(),
            "msg": record.getMessage(),
        }
        # 调用方 slog.xxx(msg, **fields) 传入的结构化字段
        extra = getattr(record, "forge_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _current() -> logging.Logger:
    return logging.getLogger(STRUCTURED_LOG_NAME)


class _SlogProxy:
    """按模块名惰性取 logger，调用点 `slog.warning("...", k=v)` 即可。

    未 install 时走 root logger（默认无 handler → 静默），
    开启后自动路由到 `forge.structured` 的 JSON handler。
    """

    def __init__(self) -> None:
        self._target: logging.Logger | None = None

    def _bind(self) -> logging.Logger:
        if self._target is None:
            self._target = _current()
        return self._target

    def _emit(self, level: int, msg: str, *fields: Any,
              extra: dict[str, Any] | None = None, exc_info: bool = True,
              stack_info: bool = False, **kw: Any) -> None:
        # 用 record 的 forge_fields 携带结构字段（不进 getMessage，避免混进 msg）
        merged: dict[str, Any] = {}
        # 位置参数：k=v 对
        for i in range(0, len(fields), 2):
            if i + 1 < len(fields):
                k, v = fields[i], fields[i + 1]
                if isinstance(k, str):
                    merged[k] = v
        # 关键字参数（key=val）也合并进结构字段
        for k, v in kw.items():
            if isinstance(k, str):
                merged[k] = v
        if extra:
            merged.update(extra)
        target = self._bind()
        record_extra: dict[str, Any] = {"forge_fields": merged} if merged else {}
        target.log(level, msg, extra=record_extra,
                   exc_info=exc_info, stack_info=stack_info)

    def debug(self, msg: str, *f: Any, **kw: Any) -> None:
        self._emit(logging.DEBUG, msg, *f,
                   extra=kw.pop("extra", None),
                   exc_info=kw.pop("exc_info", True),
                   stack_info=kw.pop("stack_info", False),
                   **kw)

    def info(self, msg: str, *f: Any, **kw: Any) -> None:
        self._emit(logging.INFO, msg, *f,
                   extra=kw.pop("extra", None),
                   exc_info=kw.pop("exc_info", True),
                   stack_info=kw.pop("stack_info", False),
                   **kw)

    def warning(self, msg: str, *f: Any, **kw: Any) -> None:
        self._emit(logging.WARNING, msg, *f,
                   extra=kw.pop("extra", None),
                   exc_info=kw.pop("exc_info", True),
                   stack_info=kw.pop("stack_info", False),
                   **kw)

    def error(self, msg: str, *f: Any, **kw: Any) -> None:
        self._emit(logging.ERROR, msg, *f,
                   extra=kw.pop("extra", None),
                   exc_info=kw.pop("exc_info", True),
                   stack_info=kw.pop("stack_info", False),
                   **kw)

    def exception(self, msg: str, *f: Any, **kw: Any) -> None:
        kw.setdefault("extra", None)
        self._emit(logging.ERROR, msg, *f, extra=kw.pop("extra"),
                   exc_info=True, stack_info=False)


slog = _SlogProxy()


def install_structured_logging(stream: Any | None = None,
                               *, level: int = logging.INFO,
                               filename: str | None = None) -> bool:
    """挂 JSON handler 到 `forge.structured`（幂等）。返回是否新增 handler。

    - 默认输出到 `stream`（None=stdout）；`FORGE_STRUCTURED_LOG_FILE` 或
      显式 `filename` 优先写文件。
    - 已有 handler 则不重复挂（幂等），便于多次调用/测试。
    """
    if os.environ.get("FORGE_STRUCTURED_LOG") not in ("1", "true", "yes"):
        # 未开启：仍返回 False 让调用方知道没挂；调用点静默无输出。
        return False
    log = _current()
    if any(getattr(h, "_forge_structured", False) for h in log.handlers):
        return True
    sink = filename or os.environ.get("FORGE_STRUCTURED_LOG_FILE") or stream
    handler: logging.Handler
    if sink:
        if isinstance(sink, (str, os.PathLike)):
            handler = logging.FileHandler(str(sink), encoding="utf-8")
        else:
            handler = logging.StreamHandler(sink)
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_StructuredFormatter())
    handler._forge_structured = True  # type: ignore[attr-defined]
    handler.setLevel(level)
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False  # 避免 root 的普通 handler 再打一份人类可读
    return True
