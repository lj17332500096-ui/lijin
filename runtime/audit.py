"""Audit Collector：从一次 Runner 结果提取 每次模型调用/工具调用 的明细并落库。

结果对象由 SDK 给出：
- result.raw_responses  → 每次模型调用（含 usage tokens、model）
- result.new_items      → 工具调用与其输出（按顺序成对，可配对出结果摘要）

执行成功路径记录明细；异常/被闸/断连路径由 Runner 的失败兜底（_backfill_failure_audit）
补记录已发生的 model/tool 事实（P1-D）。落库前统一脱敏（见 redact_value）。
"""

import re
import time
from typing import Any

#: 落库前统一脱敏的密钥/敏感模式（审计不等于保存 Secrets）
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"tvly-[A-Za-z0-9]{12,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}"),
    re.compile(r"cpk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|password|passwd|secret|token)\s*[:=]\s*[^\s,;\"']{6,}"),
]


def redact_value(obj: Any) -> Any:
    """深拷贝并脱敏字符串（审计入库前调用；失败时保守返回打码对象）。"""
    if isinstance(obj, str):
        out = obj
        for pattern in _SECRET_PATTERNS:
            out = pattern.sub("***", out)
        return out
    if isinstance(obj, dict):
        return {str(k): redact_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_value(v) for v in obj]
    return obj


def redact_text(text: str) -> str:
    out = str(text or "")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("***", out)
    return out


#: 成功路径 ingest 时把“被审批门/文件边界拦下”的调用标为 blocked，而不是 succeeded
_BLOCKED_MARKERS = ("【需要审批】", "【仍在等待审批】", "【审批拒绝】", "运行时文件边界")


def status_for_tool_output(output: str) -> str:
    head = (output or "")[:200]
    return "blocked" if any(marker in head for marker in _BLOCKED_MARKERS) else "succeeded"


def _raw_of(item: Any) -> Any:
    return getattr(item, "raw_item", None)


def _item_type(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("type", ""))
    return str(getattr(raw, "type", "") or type(raw).__name__)


def _tool_name(raw: Any) -> str:
    if isinstance(raw, dict):
        name = raw.get("name") or ""
        if not name and isinstance(raw.get("function"), dict):
            name = raw["function"].get("name", "")
        return str(name)
    return str(getattr(raw, "name", "") or "")


def _tool_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        arguments = raw.get("arguments") or raw.get("input") or {}
        if isinstance(arguments, str):
            try:
                import json

                arguments = json.loads(arguments)
            except Exception:
                arguments = {"raw": arguments}
        return arguments if isinstance(arguments, dict) else {}
    return {}


def _tool_call_id(raw: Any) -> str | None:
    """提取工具调用的 invocation id（用于与 Write-Ahead 行去重，避免审计重复计行）。"""
    if isinstance(raw, dict):
        cid = raw.get("call_id") or raw.get("id") or raw.get("tool_call_id")
    else:
        cid = (getattr(raw, "call_id", None) or getattr(raw, "id", None)
               or getattr(raw, "tool_call_id", None))
    cid = str(cid) if cid else ""
    return cid or None


def _output_text(raw: Any) -> str:
    if isinstance(raw, dict):
        value = raw.get("output") or raw.get("output_text") or raw.get("content") or ""
        return str(value) if value else ""
    return str(getattr(raw, "output", "") or getattr(raw, "output_text", "") or "")


class AuditCollector:
    def __init__(self, manager: Any, task_id: str,
                 requested_model: str | None = None,
                 profile: str | None = None):
        self.manager = manager
        self.task_id = task_id
        #: 本轮请求的模型（provider 未返回 model 时的兜底语义：requested model）
        self.requested_model = requested_model
        self.profile = profile

    def ingest(self, result: Any) -> dict:
        """把一次 run 的明细写库；返回 {model_calls, tool_calls, input_tokens, output_tokens}。"""
        totals = {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}

        for index, response in enumerate(getattr(result, "raw_responses", []) or []):
            model = getattr(response, "model", None)
            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0
            # provider 未返回 model 时记录 requested model（字段语义：model=实际/请求模型）
            recorded = str(model) if model else (self.requested_model or None)
            self.manager.insert_model_call(
                task_id=self.task_id,
                turn_number=index,
                model=recorded,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            totals["model_calls"] += 1
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens

        pending: list[tuple[Any, float]] = []  # (raw_call, started)
        for item in getattr(result, "new_items", []) or []:
            raw = _raw_of(item)
            if raw is None:
                continue
            raw_type = _item_type(raw)
            if raw_type in ("function_call", "tool_call", "custom_tool_call"):
                pending.append((raw, time.monotonic()))
            elif raw_type in ("function_call_output", "function_output", "tool_output", "function_call_result"):
                output = _output_text(raw)
                if pending:
                    call_raw, _started = pending.pop(0)
                    self.manager.insert_tool_call(
                        task_id=self.task_id,
                        tool_name=_tool_name(call_raw),
                        arguments=redact_value(_tool_args(call_raw)),
                        status=status_for_tool_output(output),
                        result_excerpt=redact_text(output[:1000]),
                        invocation_id=_tool_call_id(call_raw),
                    )
                    totals["tool_calls"] += 1
        # 没有配对输出的调用（异常/被拒），仍记录状态
        for call_raw, _started in pending:
            self.manager.insert_tool_call(
                task_id=self.task_id,
                tool_name=_tool_name(call_raw),
                arguments=redact_value(_tool_args(call_raw)),
                status="failed_or_denied",
                invocation_id=_tool_call_id(call_raw),
            )
            totals["tool_calls"] += 1

        self.manager.update_usage(
            self.task_id,
            tool_calls=totals["tool_calls"],
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
        )
        return totals
