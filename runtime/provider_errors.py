"""Provider / Gateway 错误分类与重试策略（针对真实网关 401/503 故障）。

范围：只做 Model Provider 层的错误语义（分类/有限重试/有界 fallback/用户文案/审计字段），
不触碰 Agent Runtime 主链；Provider 故障绝不进入 stalled/repair 语义（异常直接收口失败）。

已知真实故障（本仓库外网关侧）：
- 503 `No available channel for model <m> under group TokenPlan (distributor)`
  → TokenPlan/Channel 是外部网关概念；本层分类为 PROVIDER_NO_CHANNEL（网关侧需改 Channel）。
- 401 `无效的令牌，数据库查询出错` → 网关侧令牌数据库/凭据问题；本层分类为 PROVIDER_AUTH_ERROR。
分类与策略在本模块内完成；FORGE 不伪造“已修复”，只保证：0 retry(401/403/400/404)、
有限 retry(429/5xx/timeout)、有界 fallback(仅不同凭据时允许 AUTH fallback；禁 A→B→A)。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable
import time as _time

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------

class ProviderErrorKind(str):
    BAD_REQUEST = "provider_bad_request"
    AUTH = "provider_auth_error"
    PERMISSION = "provider_permission_error"
    MODEL_NOT_FOUND = "provider_model_not_found"
    RATE_LIMITED = "provider_rate_limited"
    NO_CHANNEL = "provider_no_channel"
    UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "provider_timeout"
    NETWORK = "provider_network_error"
    CONTEXT_OVERFLOW = "provider_context_overflow"
    INTERNAL = "provider_internal_error"


_NO_RETRY_KINDS = {ProviderErrorKind.BAD_REQUEST, ProviderErrorKind.AUTH,
                   ProviderErrorKind.PERMISSION, ProviderErrorKind.MODEL_NOT_FOUND,
                   ProviderErrorKind.CONTEXT_OVERFLOW}
_RETRYABLE_KINDS = {ProviderErrorKind.RATE_LIMITED, ProviderErrorKind.UNAVAILABLE,
                    ProviderErrorKind.NO_CHANNEL, ProviderErrorKind.TIMEOUT,
                    ProviderErrorKind.NETWORK}

MAX_RETRIES = {ProviderErrorKind.RATE_LIMITED: 2,
               ProviderErrorKind.UNAVAILABLE: 2,
               ProviderErrorKind.NO_CHANNEL: 1,
               ProviderErrorKind.TIMEOUT: 1,
               ProviderErrorKind.NETWORK: 1}

_CONTEXT_OVERFLOW_MARKERS = ("context_length_exceeded", "context length", "maximum context",
                             "too many tokens", "context window", "token limit",
                             "exceeded the model's context window", "context_length")


def is_context_overflow(exc: BaseException) -> bool:
    """判断一次 Provider 异常是否为"上下文超出模型窗口"（400 context_length 等）。"""
    if getattr(exc, "provider_kind", None) == ProviderErrorKind.CONTEXT_OVERFLOW:
        return True
    low = str(getattr(exc, "message", "") or exc).lower()
    return any(marker in low for marker in _CONTEXT_OVERFLOW_MARKERS)

PUBLIC_TEXT: dict[str, str] = {
    ProviderErrorKind.AUTH: ("模型服务认证失败（401）。请检查 FORGE 使用的 API Key 是否有效、"
                             "是否与当前网关账号绑定；如网关提示‘令牌数据库查询出错’，"
                             "属于网关侧令牌数据问题，请在网关后台核对凭据后重试。"),
    ProviderErrorKind.PERMISSION: ("模型服务拒绝访问（403）：当前凭据对该模型没有权限。"),
    ProviderErrorKind.MODEL_NOT_FOUND: ("模型不存在或名称不可用（404/model not found）："
                                        "请检查 AGENT_MODEL 与网关可用的模型名是否一致。"),
    ProviderErrorKind.BAD_REQUEST: ("模型服务返回参数错误（400）：请求参数不被接受，"
                                    "请检查模型名/输出额度等配置。"),
    ProviderErrorKind.CONTEXT_OVERFLOW: ("上下文超出模型窗口（context length exceeded）："
                                         "运行时会强制压缩历史后重试一次；仍失败则按错误收口。"),
    ProviderErrorKind.NO_CHANNEL: ("模型服务暂不可用：当前模型在该网关下没有可用通道"
                                   "（网关 5xx：No available channel）。请在网关后台为当前模型"
                                   "启用/绑定可用 Channel，或调整服务配额与路由后重试。"),
    ProviderErrorKind.RATE_LIMITED: ("模型服务限流（429）：已按 Retry-After 有限重试后仍被限流，"
                                     "请稍后再试或提高额度。"),
    ProviderErrorKind.TIMEOUT: ("模型服务超时：已有限重试仍未成功，请稍后再试或更换模型服务。"),
    ProviderErrorKind.NETWORK: ("连接模型服务失败（网络错误）：请检查 OPENAI_BASE_URL 可达性。"),
    ProviderErrorKind.UNAVAILABLE: ("模型服务暂不可用（5xx）：已有限重试（如配置 fallback 则已尝试）"
                                    "仍失败，请稍后重试或检查网关侧服务状态。"),
    ProviderErrorKind.INTERNAL: ("模型调用出现未分类错误，详情见运行记录。"),
}

_REQUEST_ID_RE = re.compile(r"request\s*id[:\s]*\(?([0-9a-zA-Z_-]{8,})\)?")

_NO_CHANNEL_MARKERS = ("no available channel", "tokenplan", "channel")


def extract_request_id(message: str) -> str | None:
    m = _REQUEST_ID_RE.search(message or "")
    return m.group(1) if m else None


# 传输层模拟（测试/统一化）：真实 openai 错误带 status_code，优先读取属性。
class ProviderTransportError(RuntimeError):
    """携带 HTTP 上下文的传输错误（真实 openai 异常也会被同路径分类）。"""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None, headers: dict | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status
        self.code = code
        self.headers = headers or {}
        self.request_id = request_id

    def __str__(self) -> str:
        head = f"Error code: {self.status_code}" if self.status_code else "Error"
        return f"{head} - {super().__str__()}"


def _attrs_of(exc: BaseException) -> dict[str, Any]:
    return {
        "status": getattr(exc, "status_code", None) or getattr(exc, "status", None),
        "code": getattr(exc, "code", None),
        "headers": getattr(exc, "headers", None) or {},
        "message": str(exc),
    }


def classify(exc: BaseException | None = None, *, status: int | None = None,
             body: Any = None, headers: dict | None = None,
             message: str | None = None) -> tuple[str, str, str | None]:
    """返回 (kind, public_text, request_id)。优先显式参数，其次异常属性/链。"""
    status = status
    code: str | None = None
    msg = message or ""
    hdrs: dict = headers or {}
    if exc is not None:
        attrs = _attrs_of(exc)
        if status is None:
            status = attrs["status"]
        if not code:
            code = attrs["code"]
        hdrs = attrs["headers"] or hdrs
        msg = msg or attrs["message"]
        # 标记属性（gateway 包装器设置）
        kind_attr = getattr(exc, "provider_kind", None)
        if kind_attr:
            return kind_attr, PUBLIC_TEXT.get(kind_attr, msg)[:400], \
                getattr(exc, "provider_request_id", None) or extract_request_id(msg)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = code or err.get("code")
            msg = msg or str(err.get("message") or "")
    low = (msg or "").lower()

    if status == 401:
        kind = ProviderErrorKind.AUTH
    elif status == 403:
        kind = ProviderErrorKind.PERMISSION
    elif status == 404 or (code and "not_found" in str(code).lower()):
        kind = ProviderErrorKind.MODEL_NOT_FOUND
    elif status == 400:
        kind = ProviderErrorKind.BAD_REQUEST
    elif status == 429:
        kind = ProviderErrorKind.RATE_LIMITED
    elif status in (502, 503, 504):
        kind = ProviderErrorKind.UNAVAILABLE
    elif status is not None and status >= 500:
        kind = ProviderErrorKind.INTERNAL
    else:
        kind = ProviderErrorKind.INTERNAL

    if any(marker in low for marker in _CONTEXT_OVERFLOW_MARKERS):
        kind = ProviderErrorKind.CONTEXT_OVERFLOW
    elif any(marker in low for marker in _NO_CHANNEL_MARKERS):
        kind = ProviderErrorKind.NO_CHANNEL
    elif kind == ProviderErrorKind.UNAVAILABLE and low and "channel" in low:
        kind = ProviderErrorKind.NO_CHANNEL
    if kind == ProviderErrorKind.INTERNAL and isinstance(exc, asyncio.TimeoutError):
        kind = ProviderErrorKind.TIMEOUT
    request_id = extract_request_id(msg)
    return kind, PUBLIC_TEXT.get(kind, (msg or "")[:400]), request_id


def retry_after_seconds(headers: dict | None) -> float | None:
    raw = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return min(60.0, max(0.0, float(str(raw).strip())))
    except ValueError:
        return None


def retry_policy(kind: str, attempt: int, headers: dict | None = None) -> float | None:
    """返回第 attempt 次（0-based）失败后建议等待秒数；None=不再重试。"""
    if kind in _NO_RETRY_KINDS:
        return None
    maximum = MAX_RETRIES.get(kind, 0)
    if attempt >= maximum:
        return None
    if kind == ProviderErrorKind.RATE_LIMITED:
        ra = retry_after_seconds(headers)
        return ra if ra is not None else 1.0 * (attempt + 1)
    return {0: 0.5, 1: 1.2}.get(attempt, None)


def fallback_allowed(kind: str, *, fallback_configured: bool,
                     distinct_credentials: bool) -> bool:
    """A→B 有界 fallback：auth/permission 仅在独立凭据下允许一次；其余按类型允许。"""
    if not fallback_configured:
        return False
    if kind in (ProviderErrorKind.AUTH, ProviderErrorKind.PERMISSION):
        return distinct_credentials
    if kind in _RETRYABLE_KINDS:
        return True
    return False


def mask_request_id(request_id: str | None) -> str | None:
    if not request_id:
        return None
    return request_id[:10] + "…" if len(request_id) > 10 else request_id


def attach_provider_meta(exc: BaseException) -> BaseException:
    """把分类结果附到原始异常上（provider_kind/provider_public/provider_request_id，幂等）。

    供主链 gateway 与辅助直连点（compact/research/multimodal/codex_loop）统一使用：
    上层拿到异常即可输出用户可读文案，不再把网关原始错误直接透传。
    """
    kind, public, rid = classify(exc)
    for attr, value in (("provider_kind", kind), ("provider_public", public),
                        ("provider_request_id", rid)):
        try:
            setattr(exc, attr, value)
        except Exception:
            pass
    return exc


def provider_public_text(exc: BaseException) -> str | None:
    """读取已分类的用户可读文案；无则返回 None（由调用方决定是否暴露原始错误）。"""
    try:
        pub = getattr(exc, "provider_public", None)
        return str(pub) if pub else None
    except Exception:
        return None


def call_with_provider_retry(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 6,
    wait: Callable[[float], None] | None = None,
) -> Any:
    """同步“有限重试”包装：给辅助直连点复用主链同一套分类/退避策略。

    语义与 ResilientModel 对齐：429/5xx/timeout/network/no_channel 按 retry_policy
    有限退避重试；401/400/404/context_overflow/内部错误不重试。耗尽后抛出已附加
    provider_kind/provider_public/provider_request_id 的原始异常，绝不无限重试。
    max_attempts 只是总尝试上限护栏（默认 6，高于各 kind 自身的策略上限）。
    """
    sleep = wait or _time.sleep
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 先分类再决定是否重试
            attach_provider_meta(exc)
            kind = getattr(exc, "provider_kind", None) or classify(exc)[0]
            if attempt >= max_attempts - 1:
                raise
            delay = retry_policy(kind, attempt, headers=getattr(exc, "headers", None))
            if delay is None:
                raise
            attempt += 1
            sleep(delay)
