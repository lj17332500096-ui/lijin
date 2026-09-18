"""Provider Gateway：每次模型调用粒度的 有限重试 + 有界 fallback（A→B，禁循环）。

拦截点：ModelProvider.get_model 返回的 Model.get_response——重试发生在“该次 LLM 生成”
边界内（工具执行之前，重试不会重放工具副作用），主 Agent Loop / RAG / 权限不受影响。

审计：每次尝试写 run-scoped 尝试记录（不存 key）：kind/status/model/provider/used_fallback/latency；
由 runner 在读点统一转成 task 事件（provider.model_attempts / provider.failure）。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openai import AsyncOpenAI
import httpx2
from agents.models.interface import Model
from agents.models.openai_provider import OpenAIProvider

from runtime.provider_errors import (
    ProviderErrorKind,
    classify,
    fallback_allowed,
    retry_policy,
)

_PRIMARY_TAG = "primary"
_FALLBACK_TAG = "fallback"

# run-scoped 尝试记录（进程内；由 runner 在 execute 后取走并清空）
_ATTEMPTS: dict[str, list[dict[str, Any]]] = {}

# P1-4：attempt 审计的跨进程持久化——写 runs 目录下 JSONL（每 run 一个文件），
# 崩溃/重启后 resume 仍可取回本 run 的 provider 尝试记录（内存 _ATTEMPTS 之外兜底）。
_PERSIST_ENV = "FORGE_PROVIDER_ATTEMPTS_PERSIST"
_PERSIST_MAX_LINES = 200  # 单文件行数上限（防异常长 run 无限增长）
_BASDIR: Path | None = None
_LOCK = threading.Lock()


def _persistence_enabled() -> bool:
    raw = os.getenv(_PERSIST_ENV, "").strip().lower()
    # 默认开启（审计完整性优先）；显式 off/false/0 才关闭。
    return raw not in ("off", "false", "0")


def _persist_path(run_id: str) -> Path | None:
    global _BASDIR
    with _LOCK:
        if _BASDIR is None:
            base = Path(os.getenv("FORGE_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
            _BASDIR = base / "provider_attempts"
        p = _BASDIR
        p.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)[:80]
        return p / f"{safe}.jsonl"


def _persist_append(run_id: str, meta: dict[str, Any]) -> None:
    if not _persistence_enabled():
        return
    try:
        path = _persist_path(run_id)
        if path is None:
            return
        with _LOCK:
            # 行数上限：超限时追加到 200 行即停写（记录本身仍保留内存态）。
            if path.exists() and sum(1 for _ in path.open(encoding="utf-8")) >= _PERSIST_MAX_LINES:
                return
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # 审计持久化失败不阻断主流程（内存记录仍然生效）。
        pass


def _persist_load(run_id: str) -> list[dict[str, Any]]:
    if not _persistence_enabled():
        return []
    out: list[dict[str, Any]] = []
    try:
        path = _persist_path(run_id)
        if path is None or not path.exists():
            return out
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return out


def _persist_reset(run_id: str) -> None:
    try:
        path = _persist_path(run_id)
        if path is not None and path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def record_attempt(run_id: str, meta: dict[str, Any]) -> None:
    if not run_id:
        return
    _ATTEMPTS.setdefault(run_id, []).append(meta)
    _persist_append(run_id, meta)


def take_attempts(run_id: str) -> list[dict[str, Any]]:
    mem = _ATTEMPTS.pop(run_id, [])
    # P1-4：先合并本进程曾持久化但内存已失的记录（崩溃前写入、重启后恢复）。
    on_disk = _persist_load(run_id)
    _persist_reset(run_id)
    if not mem and on_disk:
        return on_disk
    # 内存优先（含本轮新追加），磁盘兜底补缺（去重按 kind+latency_ms+model）。
    seen = {(m.get("kind"), m.get("latency_ms"), m.get("model")) for m in mem}
    merged = list(mem)
    for m in on_disk:
        key = (m.get("kind"), m.get("latency_ms"), m.get("model"))
        if key not in seen:
            merged.append(m)
            seen.add(key)
    return merged


def _current_run_id() -> str | None:
    try:
        from runtime.runctx import current as _rc

        ctx = _rc()
        return ctx.run_id if ctx is not None else None
    except Exception:
        return None


def _mark(exc: BaseException, kind: str, public: str, request_id: str | None) -> BaseException:
    for attr, val in (("provider_kind", kind), ("provider_public", public),
                      ("provider_request_id", request_id)):
        try:
            setattr(exc, attr, val)
        except Exception:
            pass
    return exc


def provider_public_message(exc: BaseException) -> str | None:
    try:
        pub = getattr(exc, "provider_public", None)
        if pub:
            return str(pub)
    except Exception:
        pass
    return None


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def config_summary() -> dict[str, str]:
    """静态配置摘要（不暴露 key 全文；用于 status/设置页与预检）。"""
    model = _env("AGENT_MODEL") or "SDK 默认"
    base = _env("OPENAI_BASE_URL") or "OpenAI 直连"
    key_set = bool(_env("OPENAI_API_KEY"))
    fb = _env("FORGE_FALLBACK_MODEL")
    parts = [
        f"Provider=网关(base={base})" if _env("OPENAI_BASE_URL") else "Provider=OpenAI 直连",
        f"Model={model}",
        f"Key={'已配置 ' + _env('OPENAI_API_KEY')[-4:] if key_set else '未配置'}",
        f"fallback={'已配置 ' + fb if fb else '未配置'}",
    ]
    return {"provider": "openai-gateway" if _env("OPENAI_BASE_URL") else "openai",
            "model": model, "base_url": base, "api_key_set": str(key_set).lower(),
            "api_key_tail": _env("OPENAI_API_KEY")[-4:] if key_set else "",
            "fallback_model": fb or "", "text": " · ".join(parts)}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _total_timeout_seconds() -> float:
    """provider 侧总墙钟上限（默认 600s；0 或负数 = 不限）。

    只靠"重试次数"兜不住真实耗时：单模型最多 3 次、叠加 fallback 最多 6 次，
    且每次还可能各自撞上首 token / 空闲超时（默认 90s / 120s），最坏可达 10 分钟级。
    总墙钟是最后一道兜底，避免一次模型调用把整轮任务挂死。
    """
    return _env_float("FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS", 600.0)


def _deadline_exceeded(started: float, limit: float) -> bool:
    return limit > 0 and (time.monotonic() - started) >= limit


def _deadline_message(limit: float) -> str:
    return f"模型响应超时：provider 累计耗时超过 {limit:.4g}s，已停止重试。"


class ResilientModel(Model):
    """包装 SDK 模型：每次 get_response 有限重试 + ≤1 次 fallback。

    流式（stream_response）自 2026-09 起也纳入同一重试语义，规则：
    - 首 token 之前失败（连接/429/503/timeout/no channel）→ 分类 → 有限重试 → 必要时 fallback；
    - 已输出 token 后失败 → 记录 interrupted（不自动从头 replay，避免重复 token/工具副作用）→ 上抛；
    - first-token / idle 超时由 FORGE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS（默认 90s）与
      FORGE_STREAM_IDLE_TIMEOUT_SECONDS（默认 120s）控制（0=关闭）；
    - 总墙钟兜底由 FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS（默认 600s，0=关闭）控制：
      次数上限约束不了真实耗时（每次尝试都可能各自撞上首 token/空闲超时），
      超过总墙钟后不再开新尝试，直接以 provider_timeout 收尾。
    """

    def __init__(self, inner: Model, gateway: "ResilientProvider") -> None:
        self._inner = inner
        self._gateway = gateway

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def stream_response(self, *args: Any, **kwargs: Any):
        """流式响应：首 token 前有限重试 + 可选 fallback；首 token 后失败绝不自动 replay。

        返回与 inner 相同的 TResponseStreamEvent 异步迭代器。
        """
        inner = getattr(self._inner, "stream_response", None)
        if inner is None:
            raise NotImplementedError("inner model 不支持流式响应")
        run_id = _current_run_id()
        gateway = self._gateway
        started = time.monotonic()
        used_fallback = False
        fallback_model: Model | None = None
        local_attempts = 0
        last_kind = ProviderErrorKind.INTERNAL
        last_public = ""
        last_rid = None
        last_exc: BaseException | None = None
        first_tok_timeout = _env_float("FORGE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", 90.0)
        idle_timeout = _env_float("FORGE_STREAM_IDLE_TIMEOUT_SECONDS", 120.0)
        total_timeout = _total_timeout_seconds()

        def _timeout_for(first: bool) -> float | None:
            return (first_tok_timeout if first else idle_timeout) or None

        while True:
            active = fallback_model if used_fallback else self._inner
            tag = _FALLBACK_TAG if used_fallback else _PRIMARY_TAG
            if _deadline_exceeded(started, total_timeout):
                # 总墙钟兜底：不再开新尝试（无论是否已用完重试/fallback 额度）
                record_attempt(run_id, {
                    "kind": "deadline_exceeded", "tag": tag, "attempt": local_attempts,
                    "tokens_output": False, "limit_seconds": total_timeout,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                })
                _d_exc = last_exc
                if _d_exc is None:
                    from runtime.provider_errors import ProviderTransportError

                    _d_exc = ProviderTransportError(_deadline_message(total_timeout))
                raise _mark(_d_exc, ProviderErrorKind.TIMEOUT,
                            _deadline_message(total_timeout), last_rid)
            attempt_start = time.monotonic()
            emitted = False
            stream = None
            try:
                try:
                    # SDK Model.stream_response 返回异步迭代器（不做 await；
                    # 建连/首 token 前的失败在消费处统一按“首 token 前”处理）
                    stream = active.stream_response(*args, **kwargs)
                except Exception as exc:  # 同步建流即失败（仍属“首 token 前”）
                    kind, public, rid = classify(exc)
                    last_kind, last_public, last_rid, last_exc = kind, public, rid, exc
                    record_attempt(run_id, {
                        "kind": kind, "tag": tag, "attempt": local_attempts,
                        "status": getattr(exc, "status_code", None),
                        "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                        "tokens_output": False,
                    })
                    wait = retry_policy(kind, local_attempts, headers=getattr(exc, "headers", None))
                    if wait is not None and local_attempts < 6:
                        local_attempts += 1
                        await asyncio.sleep(wait)
                        continue
                    if not used_fallback:
                        cfg = gateway._fallback_config()
                        if cfg is not None and fallback_allowed(
                                kind, fallback_configured=True,
                                distinct_credentials=cfg["distinct"]):
                            fallback_model = gateway._get_fallback_model()
                            if fallback_model is not None:
                                used_fallback = True
                                local_attempts = 0
                                continue
                    raise _mark(exc, kind, public, rid)
                # ---- 消费事件流（首 token 前/后的失败语义分开） ----
                first = True
                outcome: str | None = None  # None=继续内层; 'retry'=外层再来; 'done'=流结束
                while True:
                    try:
                        to = _timeout_for(first)
                        # 注意：不能用 asyncio.wait_for 包 anext —— 它会为 coroutine 新建 Task，
                        # 切换 contextvars 上下文，导致 SDK 流处理器（current_span）报
                        # “Token created in a different Context”。用 asyncio.timeout 保持同任务上下文。
                        if to:
                            async with asyncio.timeout(to):
                                item = await stream.__anext__()
                        else:
                            item = await stream.__anext__()
                    except StopAsyncIteration:
                        outcome = "done"
                        break
                    except asyncio.CancelledError:
                        record_attempt(run_id, {
                            "kind": "cancelled", "tag": tag, "attempt": local_attempts,
                            "tokens_output": emitted,
                            "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                        })
                        raise
                    except asyncio.TimeoutError:
                        exc = TimeoutError("stream idle/first-token timeout")
                        kind = ProviderErrorKind.TIMEOUT
                        if emitted:
                            record_attempt(run_id, {
                                "kind": "interrupted", "tag": tag, "attempt": local_attempts,
                                "error_kind": kind, "tokens_output": True,
                                "interrupted_reason": "timeout",
                                "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                            })
                            raise _mark(exc, kind,
                                        "模型流式输出超时中断（已输出部分内容，未自动重放）", None)
                        last_kind, last_public, last_rid, last_exc = kind, "", None, exc
                        record_attempt(run_id, {
                            "kind": kind, "tag": tag, "attempt": local_attempts,
                            "tokens_output": False,
                            "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                        })
                        wait = retry_policy(kind, local_attempts, headers=None)
                        if wait is not None and local_attempts < 6:
                            local_attempts += 1
                            await asyncio.sleep(wait)
                            outcome = "retry"
                            break
                        if not used_fallback:
                            cfg = gateway._fallback_config()
                            if cfg is not None and fallback_allowed(
                                    kind, fallback_configured=True,
                                    distinct_credentials=cfg["distinct"]):
                                fallback_model = gateway._get_fallback_model()
                                if fallback_model is not None:
                                    used_fallback = True
                                    local_attempts = 0
                                    outcome = "retry"
                                    break
                        raise _mark(exc, kind, "模型响应超时（首 token 前多次重试仍失败）", None)
                    except Exception as exc:  # 迭代中途异常（首 token 前/后）
                        kind, public, rid = classify(exc)
                        last_kind, last_public, last_rid, last_exc = kind, public, rid, exc
                        if emitted:
                            record_attempt(run_id, {
                                "kind": "interrupted", "tag": tag, "attempt": local_attempts,
                                "error_kind": kind, "tokens_output": True,
                                "interrupted_reason": "mid_stream_error",
                                "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                            })
                            raise _mark(exc, kind, public, rid)
                        record_attempt(run_id, {
                            "kind": kind, "tag": tag, "attempt": local_attempts,
                            "status": getattr(exc, "status_code", None),
                            "tokens_output": False,
                            "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                        })
                        wait = retry_policy(kind, local_attempts,
                                            headers=getattr(exc, "headers", None))
                        if wait is not None and local_attempts < 6:
                            local_attempts += 1
                            await asyncio.sleep(wait)
                            outcome = "retry"
                            break
                        if not used_fallback:
                            cfg = gateway._fallback_config()
                            if cfg is not None and fallback_allowed(
                                    kind, fallback_configured=True,
                                    distinct_credentials=cfg["distinct"]):
                                fallback_model = gateway._get_fallback_model()
                                if fallback_model is not None:
                                    used_fallback = True
                                    local_attempts = 0
                                    outcome = "retry"
                                    break
                        raise _mark(exc, kind, public, rid)
                    emitted = True
                    first = False
                    yield item
                try:
                    if stream is not None:
                        await stream.aclose()
                except Exception:
                    pass
                if outcome == "done":
                    record_attempt(run_id, {
                        "kind": "success", "tag": tag, "attempt": local_attempts,
                        "fallback_used": used_fallback, "tokens_output": emitted,
                        "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    })
                    return
                # outcome == 'retry'：走外层 while 重新开流（重试/fallback 已在上面处理）
                continue
            except asyncio.CancelledError:
                record_attempt(run_id, {
                    "kind": "cancelled", "tag": tag, "attempt": local_attempts,
                    "tokens_output": emitted,
                    "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                })
                raise

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        run_id = _current_run_id()
        gateway = self._gateway
        started = time.monotonic()
        total_timeout = _total_timeout_seconds()
        fallback_model: Model | None = None
        used_fallback = False
        local_attempts = 0
        last_kind = ProviderErrorKind.INTERNAL
        last_public = ""
        last_rid = None
        last_exc: BaseException | None = None

        try:
            while True:
                active = fallback_model if used_fallback else self._inner
                tag = _FALLBACK_TAG if used_fallback else _PRIMARY_TAG
                if _deadline_exceeded(started, total_timeout):
                    # 总墙钟兜底：不再开新尝试（无论重试/fallback 额度是否用尽）
                    record_attempt(run_id, {
                        "kind": "deadline_exceeded", "tag": tag, "attempt": local_attempts,
                        "limit_seconds": total_timeout,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    })
                    last_kind = ProviderErrorKind.TIMEOUT
                    # 覆盖上一次失败的文案：真正的原因是没有及时拿到结果，而非那次错误本身
                    last_public = _deadline_message(total_timeout)
                    break
                attempt_start = time.monotonic()
                try:
                    result = await active.get_response(*args, **kwargs)
                    record_attempt(run_id, {
                        "kind": "success", "tag": tag, "attempt": local_attempts,
                        "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                        "fallback_used": used_fallback,
                    })
                    return result
                except Exception as exc:  # noqa: BLE001
                    kind, public, rid = classify(exc)
                    last_kind, last_public, last_rid = kind, public, rid
                    last_exc = exc
                    record_attempt(run_id, {
                        "kind": kind, "tag": tag, "attempt": local_attempts,
                        "status": getattr(exc, "status_code", None),
                        "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                    })
                    wait = retry_policy(kind, local_attempts,
                                        headers=getattr(exc, "headers", None))
                    if wait is not None and local_attempts < 6:
                        local_attempts += 1
                        await asyncio.sleep(wait)
                        continue
                    if not used_fallback:
                        cfg = gateway._fallback_config()
                        if cfg is not None and fallback_allowed(
                                kind, fallback_configured=True,
                                distinct_credentials=cfg["distinct"]):
                            fallback_model = gateway._get_fallback_model()
                            if fallback_model is not None:
                                used_fallback = True
                                local_attempts = 0
                                continue
                    break
        except asyncio.CancelledError:
            raise
        record_attempt(run_id, {
            "kind": "exhausted", "error_kind": last_kind, "fallback_used": used_fallback,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        })
        if last_exc is None:  # pragma: no cover
            from runtime.provider_errors import ProviderTransportError

            last_exc = ProviderTransportError(last_public or last_kind)
        raise _mark(last_exc, last_kind, last_public, last_rid)


class ResilientProvider(OpenAIProvider):
    """OpenAIProvider 子类：get_model 返回 ResilientModel；fallback 配置按环境读取。"""

    _fallback_model_cache: Model | None = None

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 use_responses: bool | None = None, **kwargs: Any) -> None:
        """为回环地址的本地模型禁用系统代理。

        llama.cpp 等本地 OpenAI 兼容服务常绑定在 localhost。若 Python 继承了代理
        配置，HTTP 客户端可能把这类请求送往本地代理端口，导致服务端返回 502；
        回环地址不应经过代理。远程网关仍保留原来的代理行为。
        """
        parsed = urlparse(base_url or "")
        hostname = (parsed.hostname or "").lower()
        is_loopback = hostname in {"localhost", "127.0.0.1", "::1"}
        self._owned_http_client: httpx2.AsyncClient | None = None
        if is_loopback and "openai_client" not in kwargs:
            self._owned_http_client = httpx2.AsyncClient(trust_env=False)
            kwargs["openai_client"] = AsyncOpenAI(
                api_key=api_key or "local-no-key",
                base_url=base_url,
                http_client=self._owned_http_client,
            )
            super().__init__(use_responses=use_responses, **kwargs)
            return
        super().__init__(api_key=api_key, base_url=base_url,
                         use_responses=use_responses, **kwargs)

    def _fallback_config(self) -> dict[str, Any] | None:
        model = _env("FORGE_FALLBACK_MODEL")
        base = _env("FORGE_FALLBACK_BASE_URL")
        key = _env("FORGE_FALLBACK_API_KEY")
        if not model:
            return None
        primary_base = _env("OPENAI_BASE_URL")
        primary_key = _env("OPENAI_API_KEY")
        distinct = bool(key and base and (base != primary_base or key != primary_key)) \
            if (base or key) else bool(key and key != primary_key)
        return {"model": model, "base_url": base or None,
                "api_key": key or None, "distinct": distinct}

    def _get_fallback_model(self) -> Model | None:
        if self._fallback_model_cache is not None:
            return self._fallback_model_cache
        cfg = self._fallback_config()
        if cfg is None:
            return None
        try:
            provider = OpenAIProvider(
                api_key=cfg["api_key"],
                base_url=cfg["base_url"],
                use_responses=False,
            )
            self._fallback_model_cache = provider.get_model(cfg["model"])
        except Exception:
            self._fallback_model_cache = None
        return self._fallback_model_cache

    def get_model(self, model_name: str | None) -> Model:
        inner = super().get_model(model_name)
        return ResilientModel(inner, self)
