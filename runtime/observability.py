"""可观测性：把 Agents SDK 的 trace/span 转成 **OTel 语义约定** 的本地记录（可移植格式）。

设计：
- 每行 JSONL 都是 OTel Semantic Convention 风格的结构：
  {trace_id, span_id, parent_span_id, name, kind, start_time_unix_nano, end_time_unix_nano,
   attributes:{gen_ai.* / agent.* / tool 约定}, status, resource/scope 元信息}；
- 不再是"本项目专属字段"，collector 的 filelog/otlp-json 转换器可直接消费，
  也便于将来切 opentelemetry SDK 导出器（hook 已预留 exporters 列表）。
- trace_start/trace_end 事件保留，用于还原"这一段会话从哪来到哪去"。
"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agents.tracing import (
    Trace,
    TracingProcessor,
    set_trace_processors,
    set_tracing_disabled,
)
from runtime_paths import TRACE_DIR

TRACES_DIR = TRACE_DIR
OTEL_VERSION = "1.0-local-semconv-2026"
SERVICE_NAME = "assistant-agent"
_EXPORTERS: list[Callable[[dict], None]] = []

_trace_log_path: Path | None = None
_PII_PATTERNS = (
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _to_nano(value: Any) -> int | None:
    """尽力把时间戳转成 unix 纳秒；识别 datetime/ISO 字符串/秒/毫秒。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1_000_000_000)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        if abs(value) > 1_000_000_000_000_000:  # 已是纳秒
            return int(value)
        if abs(value) > 1_000_000_000_000:  # 微秒
            return int(value * 1000)
        return int(value * 1_000_000_000)  # 秒
    return None


def _preview(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    text = " ".join(text.split())
    try:
        from runtime.audit import redact_text

        text = redact_text(text)
    except Exception:
        pass
    for pattern in _PII_PATTERNS:
        text = pattern.sub("***", text)
    return text if len(text) <= limit else text[:limit] + "…"


def _usage_tokens(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {
            "gen_ai.usage.input_tokens": int(usage.get("input_tokens", 0) or 0),
            "gen_ai.usage.output_tokens": int(usage.get("output_tokens", 0) or 0),
        }
    return {
        "gen_ai.usage.input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "gen_ai.usage.output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }


def span_attributes(span: Any) -> dict[str, Any]:
    """把 span 映射成 OTel GenAI 语义约定属性。"""
    span_data = getattr(span, "span_data", None)
    export = span_data.export() if hasattr(span_data, "export") else {}
    kind = getattr(span_data, "type", None) or export.get("type", type(span_data).__name__)
    attributes: dict[str, Any] = {}
    error = getattr(span, "error", None)

    if kind == "agent":
        attributes.update(
            {
                "agent.name": export.get("name") or "assistant",
                "agent.tools": _preview(export.get("tools"), 600),
            }
        )
    elif kind == "task":
        attributes.update(
            {
                "agent.task": export.get("name") or "",
                # Session/group identifiers can contain user supplied names.
            }
        )
        attributes.update(_usage_tokens(export.get("usage")))
    elif kind == "turn":
        data = export.get("data") or {}
        attributes["agent.turn"] = data.get("turn")
        attributes["agent.turn.agent"] = data.get("agent_name") or ""
        if error:
            attributes["agent.turn.error"] = _preview(str(error), 300)
    elif kind == "function":
        tool_input = export.get("input")
        tool_output = export.get("output")
        attributes.update(
            {
                "gen_ai.tool.name": export.get("name") or "unknown_tool",
                # Keep payloads out of traces; only their sizes are operationally useful.
                "local.io.input_chars": len(str(tool_input)) if tool_input is not None else 0,
                "local.io.output_chars": len(str(tool_output)) if tool_output is not None else 0,
            }
        )
    elif kind == "generation":
        attributes.update(
            {
                "gen_ai.system": "openai-compatible-gateway",
                "gen_ai.operation.name": "chat",
            }
        )
        if export.get("model"):
            attributes["gen_ai.request.model"] = export.get("model")
        attributes.update(_usage_tokens(export.get("usage")))
    elif kind == "guardrail":
        attributes.update(
            {
                "agent.guardrail.name": export.get("name") or "",
                "agent.guardrail.result_type": type(export.get("output")).__name__,
            }
        )
    elif kind == "handoff":
        attributes.update(
            {
                "agent.handoff.from": _preview(export.get("from_agent") or export.get("from"), 200),
                "agent.handoff.to": _preview(export.get("to_agent") or export.get("to"), 200),
            }
        )
    elif kind in ("response", "custom", "transcription", "speech"):
        attributes["local.kind.detail"] = type(export).__name__
    else:
        attributes["local.kind.detail"] = type(export).__name__
    return attributes


def span_kind_name(span: Any) -> str:
    span_data = getattr(span, "span_data", None)
    kind = getattr(span_data, "type", None) if span_data is not None else None
    kind = kind or "unknown"
    return {
        "agent": "agent",
        "task": "task",
        "turn": "turn",
        "function": "tool",
        "generation": "model",
        "guardrail": "guardrail",
        "handoff": "handoff",
    }.get(kind, kind)


def add_exporter(export_fn: Callable[[dict], None]) -> None:
    """注册外部导出器（如未来接入 opentelemetry SDK / OTLP 时用）。"""
    _EXPORTERS.append(export_fn)


def _current_run_id() -> str | None:
    """当前 Run 的 id（不在 Run 内则 None）。

    B8（P1-6，2026-09-22）：SDK 生成的 OTel ``trace_id`` 与 Run id **毫无关联**，
    而 ``trace_export`` 是按 Run id 过滤 span 的 —— 不盖这个字段，过滤条件恒为假、
    导出的 ``spans`` 永远是空数组（``traces/`` 目录在 Web 路径下也从未产生）。
    这里**不篡改** ``trace_id``（OTel 语义要保持，span_id/parent 关系都依赖它），
    而是额外盖上 Run 身份，让「跨层追踪」有一个真实可用的键。
    """
    try:
        from runtime.runctx import current as _cur

        ctx = _cur()
        return getattr(ctx, "run_id", None) if ctx is not None else None
    except Exception:
        return None


def base_record(trace: Trace | None, span: Any | None = None, name: str = "") -> dict[str, Any]:
    trace_id = getattr(trace, "trace_id", None) if trace is not None else getattr(span, "trace_id", None)
    span_id = getattr(span, "span_id", None) if span is not None else None
    parent_id = getattr(span, "parent_id", None) if span is not None else None
    started = getattr(span, "started_at", None) if span is not None else None
    ended = getattr(span, "ended_at", None) if span is not None else None
    record: dict[str, Any] = {
        "otel_schema": OTEL_VERSION,
        "resource": {"service.name": SERVICE_NAME},
        "trace_id": trace_id,
        # B8：Run 身份（与 OTel trace_id 并存，后者由 SDK 生成、无法指定）
        "run_id": _current_run_id(),
        "span_id": span_id,
        "parent_span_id": parent_id,
        "name": name,
        "ts": _now_iso(),
    }
    if span is not None:
        record["kind"] = span_kind_name(span)
        nano_start = _to_nano(started)
        nano_end = _to_nano(ended)
        if nano_start is not None:
            record["start_time_unix_nano"] = nano_start
        if nano_end is not None:
            record["end_time_unix_nano"] = nano_end
        error = getattr(span, "error", None)
        if error:
            record["status"] = {"code": 2, "message": _preview(str(error), 300)}
        else:
            record["status"] = {"code": 1 if span_id else 0}
    return record


class LocalJsonlProcessor(TracingProcessor):
    """把 SDK 事件写成 OTel 语义约定的 JSONL，并转发给外部 exporters。"""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _emit(self, record: dict[str, Any]) -> None:
        with self._lock:
            from runtime.rotating_jsonl import append_jsonl

            append_jsonl(self.log_path, record, env_prefix="FORGE_TRACE_LOG")
        for export_fn in list(_EXPORTERS):
            try:
                export_fn(dict(record))
            except Exception:
                pass

    def on_trace_start(self, trace: Trace) -> None:
        record = base_record(trace, name="trace.start")
        record["attributes"] = {
            "agent.workflow": getattr(trace, "workflow_name", None),
        }
        self._emit(record)

    def on_trace_end(self, trace: Trace) -> None:
        record = base_record(trace, name="trace.end")
        record["attributes"] = {
            "agent.workflow": getattr(trace, "workflow_name", None),
        }
        self._emit(record)

    def on_span_start(self, span: Any) -> None:
        record = base_record(None, span=span, name=f"{span_kind_name(span)}.start")
        record["attributes"] = span_attributes(span)
        self._emit(record)

    def on_span_end(self, span: Any) -> None:
        record = base_record(None, span=span, name=f"{span_kind_name(span)}.end")
        record["attributes"] = span_attributes(span)
        self._emit(record)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


def install_local_tracing(enabled: bool = True, log_path: Path | None = None) -> Path | None:
    """开启本地追踪（OTel 语义 JSONL）。返回日志路径；enabled=False 时关闭并返回 None。"""
    global _trace_log_path
    if not enabled:
        _trace_log_path = None
        return None

    os.environ.pop("OPENAI_AGENTS_DISABLE_TRACING", None)
    set_tracing_disabled(False)
    path = log_path or (TRACES_DIR / "traces.jsonl")
    processor = LocalJsonlProcessor(path)
    set_trace_processors([processor])
    _trace_log_path = path
    return path


def trace_log_path() -> Path | None:
    return _trace_log_path
