"""Provider Gateway：每次模型调用粒度的 有限重试 + 有界 fallback（A→B，禁循环）。

拦截点：ModelProvider.get_model 返回的 Model.get_response——重试发生在“该次 LLM 生成”
边界内（工具执行之前，重试不会重放工具副作用），主 Agent Loop / RAG / 权限不受影响。

审计：每次尝试写 run-scoped 尝试记录（不存 key）：kind/status/model/provider/used_fallback/latency；
由 runner 在读点统一转成 task 事件（provider.model_attempts / provider.failure）。

── 重试与超时的归属（审计 P0-8 / P1-15，2026-09-22）──────────────────────────────
修复前是四层各自为政，最坏叠加 36 次 HTTP / 用户一轮，且 SDK 那两层对审计完全不可见：

  L0 SDK 内建重试   `max_retries=2` ⇒ 每次「尝试」= 1 + 2 = 3 次 HTTP（静默、不记录）
  L1 Provider 重试  本模块（原为 `local_attempts < 6`，与 `retry_policy` 的真实上限漂移）
  L1 fallback       A→B 一次；切换时 `local_attempts` 被重置 ⇒ 上限翻倍
  L3 格式闸重跑     `main.execute_turn` 的 `while attempt < 3`

现在把「谁负责重试」显式收口到 L1：

  L0 关闭     每次尝试 = 恰好 1 次 HTTP（`_sdk_max_retries()`，默认 0），
              SDK 的 HTTP 次数以 `sdk_http_attempts` 写进每条尝试记录 ⇒ 可审计；
  L1 显式上限 单 provider `_provider_max_attempts()`，primary + fallback 合计
              `_provider_max_total_attempts()`（默认 6）—— 天花板由本模块保证，
              不再依赖 `retry_policy` 表里的 MAX_RETRIES 恰好取到什么值；
  L3 显式上限 `main._format_retry_max()`（默认 1 次重跑），与 L1 相乘即该轮最坏值。

超时同样分层显式声明（不再吃 SDK 默认的 `Timeout(timeout=600, connect=5.0)` ——
它的 read 与总预算等值，一次挂死请求就能吃掉整个 provider 预算）：

  connect = 5s            `_http_timeout()`（客户端底层握手）
  read    = 总预算 / 3    `_http_timeout()`（单次 HTTP 不产出就强制失败）
  首 token = 90s          `FORGE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS`
  空闲    = 120s          `FORGE_STREAM_IDLE_TIMEOUT_SECONDS`
  总墙钟  = 600s          `_total_timeout_seconds()`（只在尝试之间检查，是最后兜底）
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openai import AsyncOpenAI
import httpx2
from agents.models.interface import Model
from agents.models.openai_provider import OpenAIProvider, shared_http_client

from runtime.provider_errors import (
    ProviderErrorKind,
    classify,
    fallback_allowed,
    retry_policy,
)
from runtime_paths import LOG_DIR

# P5-修复：Windows 下 httpcore2 async connect_tcp 默认走 getaddrinfo 全量结果
# （含 IPv6），而本机 IPv6 出站可能不通 → "All connection attempts failed"。
# 注册一个只取 IPv4（AF_INET）的 resolver，让 httpcore2 始终走 IPv4 直连。
def _force_ipv4_httpcore() -> None:
    try:
        import httpcore2._backends.auto as _auto
        import socket as _socket

        _orig = _auto._get_backend() if hasattr(_auto, "_get_backend") else None
        # httpcore2 的 connect_tcp 走 backend；我们在模块级 patch 一个
        # 只用 AF_INET 的 resolver 注入 socket.getaddrinfo 行为。
        # 最简单可靠的方式：patch socket.getaddrinfo 让它只返回 AF_INET。
        if not hasattr(_socket, "_getaddrinfo_orig_forge"):
            _socket._getaddrinfo_orig_forge = _socket.getaddrinfo
            def _v4_only_getaddrinfo(host, port, *args, **kwargs):
                # 先试 AF_INET
                results = _socket._getaddrinfo_orig_forge(host, port, _socket.AF_INET,
                                                           _socket.SOCK_STREAM)
                if results:
                    return results
                # 回落到全量（IPv6-only 主机）
                return _socket._getaddrinfo_orig_forge(host, port, *args, **kwargs)
            _socket.getaddrinfo = _v4_only_getaddrinfo
    except Exception:
        pass  # patch 失败不影响功能，只是可能回落到 IPv6

_force_ipv4_httpcore()

#: 进程级共享连接池（trust_env=False）。
#: 背景：本机系统代理是 CC Switch 127.0.0.1:50106（端口不通），任何 trust_env=True 的
#: client 都会把所有 LLM 请求打成 ConnectError；而 SDK 自带的 shared_http_client()
#: 正是 trust_env=True，不可用。
#: 但"每个 Provider 各建一个 client"会丢连接复用，也让"远程网关不持有 client"的
#: 约定失效（_owned_http_client 应只在回环地址场景出现）。
#: 所以这里维护**进程级共享**的 trust_env=False 池：既跳过坏代理，又保留连接复用。
_SHARED_NO_PROXY_CLIENT: "httpx2.AsyncClient | None" = None


class _DoneCheckedSSEStream(httpx2.AsyncByteStream):
    """Pass SSE bytes through, but reject a clean EOF without the protocol marker.

    The Agents SDK currently turns ordinary iterator exhaustion into a completed
    response. Chat Completions uses ``data: [DONE]`` to distinguish a completed
    stream from a gateway that closed the socket after a partial response.
    """

    def __init__(self, inner: httpx2.AsyncByteStream) -> None:
        self._inner = inner
        self._line_buffer = b""
        self._done = False

    async def __aiter__(self):
        async for chunk in self._inner:
            self._scan(chunk)
            yield chunk
        if not self._done:
            raise httpx2.RemoteProtocolError(
                "Chat Completions SSE ended without the data: [DONE] marker"
            )

    def _scan(self, chunk: bytes) -> None:
        self._line_buffer += chunk
        lines = self._line_buffer.split(b"\n")
        self._line_buffer = lines.pop()
        for line in lines:
            if line.rstrip(b"\r").strip() == b"data: [DONE]":
                self._done = True
        if self._line_buffer.rstrip(b"\r").strip() == b"data: [DONE]":
            self._done = True

    async def aclose(self) -> None:
        await self._inner.aclose()


class _ProtocolCheckingAsyncClient(httpx2.AsyncClient):
    """Validate terminal markers on streamed Chat Completions responses."""

    async def send(self, request, *, stream=False, auth=httpx2.USE_CLIENT_DEFAULT,
                   follow_redirects=httpx2.USE_CLIENT_DEFAULT):
        response = await super().send(
            request, stream=stream, auth=auth, follow_redirects=follow_redirects
        )
        path = request.url.path.rstrip("/")
        content_type = response.headers.get("content-type", "").lower()
        if (stream and path.endswith("/chat/completions")
                and "text/event-stream" in content_type):
            response.stream = _DoneCheckedSSEStream(response.stream)
        return response


def _shared_no_proxy_client() -> "httpx2.AsyncClient":
    """返回（必要时创建）进程级共享的 trust_env=False 连接池。"""
    global _SHARED_NO_PROXY_CLIENT
    if _SHARED_NO_PROXY_CLIENT is None:
        _SHARED_NO_PROXY_CLIENT = _ProtocolCheckingAsyncClient(trust_env=False)
    return _SHARED_NO_PROXY_CLIENT


_PRIMARY_TAG = "primary"
_FALLBACK_TAG = "fallback"

# 成功尝试的 kind 标记。生产侧写 "success"；测试与 runner 的落库兜底用 "ok"。
# 两者都必须算作「一轮模型响应」，否则 note_model_turn() 静默失效，
# tool_calls.turn_number 会恒为 NULL（迁移加了列却永远取不到值）。
_SUCCESS_ATTEMPT_KINDS = frozenset({"success", "ok"})

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
            legacy_data = (os.getenv("FORGE_DATA_DIR") or "").strip()
            _BASDIR = (Path(legacy_data).expanduser() / "provider_attempts"
                       if legacy_data else LOG_DIR / "provider_attempts")
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
            if path.exists():
                # Count under the same lock as append; close the descriptor
                # deterministically (the previous generator over path.open()
                # leaked it until GC on every provider attempt).
                with path.open(encoding="utf-8") as existing:
                    if sum(1 for _ in existing) >= _PERSIST_MAX_LINES:
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
    """删除该 run 的尝试记录文件（纯清理，任何失败都必须静默）。

    注意：这里必须同时吞掉 SystemExit，而不只是 Exception。清理属于副作用，
    但某些运行环境（沙箱 / 安全护栏 / 自定义 sitecustomize）在删除文件时会
    直接抛 SystemExit；一旦冒泡，就会顺着 take_attempts -> _close_run ->
    _succeed 一路终止整个 uvicorn 进程（表现为“服务跑一段时间后突然退出”）。
    KeyboardInterrupt 不吞，保证 Ctrl+C 语义不受影响。
    """
    try:
        path = _persist_path(run_id)
        if path is not None and path.exists():
            path.unlink(missing_ok=True)
    except (Exception, SystemExit):
        pass


#: 失败尝试记录里 error_message 的最大长度。够放下异常类型 + 首行原因，
#: 又不会把整段 HTML 错误页/堆栈灌进 detail_json（该列入库上限 4000 字符）。
_ERROR_MESSAGE_MAX = 500


def _error_fields(exc: BaseException) -> dict[str, str]:
    """把异常转成可落库的诊断字段（脱敏后）。

    为什么需要它：`provider_internal_error` 的对外文案是「详情见运行记录」，
    但修复前尝试记录里**没有任何能定位原因的字段** —— 只有 kind/status/latency_ms。
    `kind` 是分类结果（同一 kind 可能是十种完全不同的故障），`status` 常常是 None。
    于是那句话是空话：真要定位只能靠对照实验重跑（上一轮排查为此花了三轮）。

    脱敏：复用 A8「四条审计出口统一脱敏」的 `runtime.audit.redact_text`，
    不另写一套 —— 异常消息里确实会出现 API key / Authorization 头 /
    带凭据的 base URL（`OPENAI_BASE_URL=https://user:pw@host` 会被 SDK
    原样拼进连接错误消息）。

    保留可定位性：异常类名 + HTTP 状态码 + provider 名都在（分别由
    `error_type` / 既有的 `status` / 既有的 `tag` 承载），脱敏只打码凭据本身。
    """
    from runtime.audit import redact_text

    error_type = type(exc).__name__
    # 诊断代码绝不能把原故障换成新故障：异常对象的 __str__ 可能自己抛
    # （SDK 包装类里并不罕见），此时退回类名，保留可诊断性且不掩盖真因。
    try:
        raw = f"{error_type}: {exc}"
    except Exception:  # noqa: BLE001 - __str__ 抛错不能连带打挂记录路径
        raw = error_type
    message = redact_text(raw)
    # 超长错误页（HTML/整段响应体）对定位没有额外价值，只会把 detail_json 顶满。
    if len(message) > _ERROR_MESSAGE_MAX:
        message = message[:_ERROR_MESSAGE_MAX] + "…(truncated)"
    # `error` 是**读者真正在用的那个键**：`task_manager.record_provider_attempt`
    # 把它写进 provider_attempts.error 列，`/diag`（cli/commands.py）显示的
    # 正是这一列。修复前没有任何调用方写它 ⇒ 该列恒为空 ⇒ /diag 永远只显示 "-"，
    # 与「判据必须与生产端写出的值对齐」直接冲突。故此处一并写。
    return {"error_type": error_type, "error_message": message, "error": message}


def record_attempt(run_id: str, meta: dict[str, Any]) -> None:
    if not run_id:
        return
    meta.setdefault("attempt_id", uuid.uuid4().hex)
    # B7：只有「成功返回」的模型响应才构成一轮（重试/限流没有产出，也就不会产生
    # 工具调用）。工具调用的 turn_number 由此得到，避免用「尝试次数」冒充轮次。
    if str(meta.get("kind") or "").strip().lower() in _SUCCESS_ATTEMPT_KINDS:
        try:
            from runtime.runctx import current as _rc_turn

            _ctx_turn = _rc_turn()
            if _ctx_turn is not None:
                _ctx_turn.note_model_turn()
                latency = meta.get("latency_ms")
                if latency is not None:
                    _ctx_turn.model_latency_ms.append(float(latency))
        except Exception:
            pass
    # P0-8：把"这一层乘了多少倍"随每条记录一起留证。修复前 SDK 的 max_retries=2
    # 对 provider.model_attempts 完全不可见——审计看到的尝试次数只有真实 HTTP 的 1/3。
    # 现在每条记录都自带 SDK 的 HTTP 次数与 L1 的总尝试预算，乘法关系自证。
    meta.setdefault("sdk_http_attempts", 1 + _sdk_max_retries())
    meta.setdefault("provider_attempt_budget", _provider_max_total_attempts())
    _ATTEMPTS.setdefault(run_id, []).append(meta)
    _persist_append(run_id, meta)


def read_attempts(run_id: str) -> list[dict[str, Any]]:
    """Read attempt ledger without deleting the durable source."""
    mem = list(_ATTEMPTS.get(run_id, []))
    # P1-4：先合并本进程曾持久化但内存已失的记录（崩溃前写入、重启后恢复）。
    on_disk = _persist_load(run_id)
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


def ack_attempts(run_id: str) -> None:
    """Acknowledge attempts only after their durable database handoff succeeds."""
    _ATTEMPTS.pop(run_id, None)
    _persist_reset(run_id)


def take_attempts(run_id: str) -> list[dict[str, Any]]:
    """Compatibility read-and-clear API for callers that do not need a DB handoff."""
    records = read_attempts(run_id)
    ack_attempts(run_id)
    return records


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
    to = _http_timeout()
    parts = [
        f"Provider=网关(base={base})" if _env("OPENAI_BASE_URL") else "Provider=OpenAI 直连",
        f"Model={model}",
        f"Key={'已配置 ' + _env('OPENAI_API_KEY')[-4:] if key_set else '未配置'}",
        f"fallback={'已配置 ' + fb if fb else '未配置'}",
        # P0-8/P1-15：把分层声明摊在状态页上，避免"重试归属"再次变成隐性约定
        f"重试=SDK {_sdk_max_retries()} 次/单 provider {_provider_max_attempts()} 次"
        f"/合计 {_provider_max_total_attempts()} 次",
        f"超时=connect {to.connect:g}s·read {to.read:g}s·总 {_total_timeout_seconds():g}s",
    ]
    return {"provider": "openai-gateway" if _env("OPENAI_BASE_URL") else "openai",
            "model": model, "base_url": base, "api_key_set": str(key_set).lower(),
            "api_key_tail": _env("OPENAI_API_KEY")[-4:] if key_set else "",
            "fallback_model": fb or "",
            "sdk_max_retries": str(_sdk_max_retries()),
            "provider_max_attempts": str(_provider_max_attempts()),
            "provider_max_total_attempts": str(_provider_max_total_attempts()),
            "connect_timeout_seconds": f"{to.connect:g}",
            "read_timeout_seconds": f"{to.read:g}",
            "provider_total_timeout_seconds": f"{_total_timeout_seconds():g}",
            "text": " · ".join(parts)}


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


def _budget_message(limit: int, base: str = "") -> str:
    """总尝试预算耗尽时的用户可读文案。

    保留最后一次失败的具体原因（用户最需要的那句），只在后面补上"已经试了几次"，
    避免把"限流，请稍后再试"这种可执行提示替换成笼统的"服务不可用"。
    """
    tail = f"已连续尝试 {limit} 次仍未成功，已停止重试。"
    return f"{base}（{tail}）" if base else f"模型服务持续失败：{tail}"


# ── P0-8：重试归属 ──────────────────────────────────────────────────────────
def _sdk_max_retries() -> int:
    """SDK（openai 客户端）内建重试次数。默认 0。

    `openai._constants.DEFAULT_MAX_RETRIES = 2`：SDK 会自己重试 2 次，且对
    `provider.model_attempts` 完全不可见——审计看到的尝试次数只有真实 HTTP 的 1/3。
    重试是 L1 的职责（分类 / 退避 / fallback / 用户可读文案都在这里），L0 必须关掉。
    """
    try:
        return max(0, int(os.getenv("FORGE_SDK_MAX_RETRIES", "").strip() or 0))
    except ValueError:
        return 0


def _provider_max_attempts() -> int:
    """单个 provider（primary 或 fallback）最多发起的尝试次数（含首次）。默认 3。

    判据是 `local_attempts + 1 < max_attempts`：`local_attempts` 记录的是**已发起的
    重试次数**，所以"此刻已经打了 local_attempts + 1 次"，再重试一次就是 +2 次。
    旧代码写成 `local_attempts < 6`，语义上等于"最多 7 次尝试"——同一个数字在两个
    地方（代码 vs `provider_errors.MAX_RETRIES`）各说各话，这里只留一处定义。

    3 与 `provider_errors.MAX_RETRIES` 的实际语义对齐（RATE_LIMITED=2/UNAVAILABLE=2
    ⇒ 3 次尝试；NO_CHANNEL/TIMEOUT/NETWORK=1 ⇒ 2 次）。
    """
    try:
        return max(1, int(os.getenv("FORGE_PROVIDER_MAX_ATTEMPTS", "").strip() or 3))
    except ValueError:
        return 3


def _provider_max_total_attempts() -> int:
    """单次模型调用的总尝试预算（primary + fallback 合并计数）。默认 6。

    旧实现里 fallback 会把 `local_attempts` 重置为 0，所以「6」只约束单个 provider，
    合计可到 12；再乘 SDK 的 3 与格式闸的 2 就是报告里的 36 次 HTTP。现在这个值是
    **跨 provider 的硬上限**：primary 用掉几次，fallback 只剩剩余额度。
    """
    try:
        return max(2, int(os.getenv("FORGE_PROVIDER_MAX_TOTAL_ATTEMPTS", "").strip() or 6))
    except ValueError:
        return 6


# ── P1-15：分层超时 ─────────────────────────────────────────────────────────
def _http_timeout() -> httpx2.Timeout:
    """显式分层超时（connect / read / write / pool），不再吃 SDK 默认值。

    SDK 默认是 `httpx2.Timeout(timeout=600, connect=5.0)`——read 恰好等于
    `FORGE_PROVIDER_TOTAL_TIMEOUT_SECONDS`，于是「单次请求永久挂住」就能吃满整个
    provider 预算，而总墙钟只在两次尝试之间检查，等于没有兜底。这里把 read 收到
    总预算的 1/3，让一次挂死最多花掉 1/3 预算，剩下 2/3 留给重试与 fallback。
    """
    connect = _env_float("FORGE_PROVIDER_CONNECT_TIMEOUT_SECONDS", 5.0) or 5.0
    total = _total_timeout_seconds()
    default_read = max(30.0, total / 3.0) if total > 0 else 200.0
    read = _env_float("FORGE_PROVIDER_READ_TIMEOUT_SECONDS", default_read) or default_read
    write = _env_float("FORGE_PROVIDER_WRITE_TIMEOUT_SECONDS", read) or read
    pool = _env_float("FORGE_PROVIDER_POOL_TIMEOUT_SECONDS", connect) or connect
    return httpx2.Timeout(connect=connect, read=read, write=write, pool=pool)


def _build_sdk_client(*, api_key: str | None, base_url: str | None,
                      http_client: httpx2.AsyncClient | None = None) -> AsyncOpenAI:
    """构造显式配置的 AsyncOpenAI：关掉内建重试 + 分层超时。

    必须自建实例才能显式声明这两项——`agents.models.openai_provider` 的构造签名
    不接受 `max_retries`/`timeout`，走它自己的懒加载只会拿到 SDK 默认值。
    """
    kwargs: dict[str, Any] = {
        "api_key": api_key or "local-no-key",
        "base_url": base_url,
        "max_retries": _sdk_max_retries(),
        "timeout": _http_timeout(),
    }
    if http_client is not None:
        kwargs["http_client"] = http_client
    return AsyncOpenAI(**kwargs)


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
        # P0-8：次数天花板只声明一次（不依赖 retry_policy 表恰好取到什么值），
        # 且 primary + fallback 合并计数 —— 旧实现的 fallback 会重置 local_attempts，
        # 使"每个 provider 6 次"变成"合计 12 次"。
        max_attempts = _provider_max_attempts()
        max_total_attempts = _provider_max_total_attempts()
        total_attempts = 0

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
            if total_attempts >= max_total_attempts:
                # P0-8 总尝试预算：primary + fallback 合并，防止 fallback 重置计数后翻倍
                record_attempt(run_id, {
                    "kind": "attempt_budget_exhausted", "tag": tag,
                    "attempt": local_attempts, "total_attempts": total_attempts,
                    "attempt_budget": max_total_attempts, "tokens_output": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                })
                _b_exc = last_exc
                if _b_exc is None:  # pragma: no cover - 只有预算被外部改小才可能到这
                    from runtime.provider_errors import ProviderTransportError

                    _b_exc = ProviderTransportError(_budget_message(max_total_attempts))
                raise _mark(_b_exc, last_kind,
                            _budget_message(max_total_attempts, last_public), last_rid)
            attempt_start = time.monotonic()
            total_attempts += 1
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
                        **_error_fields(exc),
                    })
                    wait = retry_policy(kind, local_attempts, headers=getattr(exc, "headers", None))
                    if wait is not None and local_attempts + 1 < max_attempts:
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
                        if wait is not None and local_attempts + 1 < max_attempts:
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
                                **_error_fields(exc),
                            })
                            raise _mark(exc, kind, public, rid)
                        record_attempt(run_id, {
                            "kind": kind, "tag": tag, "attempt": local_attempts,
                            "status": getattr(exc, "status_code", None),
                            "tokens_output": False,
                            "latency_ms": round((time.monotonic() - attempt_start) * 1000, 1),
                            **_error_fields(exc),
                        })
                        wait = retry_policy(kind, local_attempts,
                                            headers=getattr(exc, "headers", None))
                        if wait is not None and local_attempts + 1 < max_attempts:
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
        # P0-8：次数天花板只声明一次，且 primary + fallback 合并计数（见 _provider_max_*）
        max_attempts = _provider_max_attempts()
        max_total_attempts = _provider_max_total_attempts()
        total_attempts = 0

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
                if total_attempts >= max_total_attempts:
                    # P0-8 总尝试预算：primary + fallback 合并，防止 fallback 重置计数后翻倍
                    record_attempt(run_id, {
                        "kind": "attempt_budget_exhausted", "tag": tag,
                        "attempt": local_attempts, "total_attempts": total_attempts,
                        "attempt_budget": max_total_attempts,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    })
                    # 保留最后一次失败的分类与原因：用户需要的是"限流"这类可执行提示，
                    # 而不是被替换成笼统的"服务不可用"。
                    last_public = _budget_message(max_total_attempts, last_public)
                    break
                attempt_start = time.monotonic()
                total_attempts += 1
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
                        **_error_fields(exc),
                    })
                    wait = retry_policy(kind, local_attempts,
                                        headers=getattr(exc, "headers", None))
                    if wait is not None and local_attempts + 1 < max_attempts:
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
        # 终态记录也带上真实异常：用户看到的 provider_internal_error 正是在这里
        # 抛出的，而它此前只记了 error_kind（分类）—— 分类相同的原因有无数种。
        record_attempt(run_id, {
            "kind": "exhausted", "error_kind": last_kind, "fallback_used": used_fallback,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            **(_error_fields(last_exc) if last_exc is not None else {}),
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
        """为回环地址的本地模型禁用系统代理；为两种地址都关闭 SDK 内建重试。

        llama.cpp 等本地 OpenAI 兼容服务常绑定在 localhost。若 Python 继承了代理
        配置，HTTP 客户端可能把这类请求送往本地代理端口，导致服务端返回 502；
        回环地址不应经过代理。远程网关仍保留原来的代理行为（沿用 SDK 的共享
        连接池）。

        P0-8/P1-15：两条分支都必须**自建** `AsyncOpenAI` —— 只有自建实例才能显式
        声明 `max_retries=0` 与分层超时。走 `agents` 的懒加载只会拿到 SDK 默认值
        （重试 2 次、read timeout 600s），那正是报告里 36 次 HTTP 的来源。
        """
        parsed = urlparse(base_url or "")
        hostname = (parsed.hostname or "").lower()
        is_loopback = hostname in {"localhost", "127.0.0.1", "::1"}
        self._owned_http_client: httpx2.AsyncClient | None = None
        if "openai_client" in kwargs:
            # 调用方自带 client：尊重之。重试/超时属于该 client 的责任，本层不改它。
            super().__init__(api_key=api_key, base_url=base_url,
                             use_responses=use_responses, **kwargs)
            return
        if is_loopback:
            self._owned_http_client = _ProtocolCheckingAsyncClient(trust_env=False)
            http_client: httpx2.AsyncClient | None = self._owned_http_client
        else:
            # P5-修复：远程网关也不能走 SDK 的 shared_http_client()（trust_env=True
            # → 读系统代理 CC Switch 127.0.0.1:50106，端口不通 → 全部 ConnectError）。
            # 但不能每次都自建：那会丢连接复用，也让"远程网关不持有 client"的约定失效。
            # 改为复用**进程级共享**的 trust_env=False 池（见 _shared_no_proxy_client）。
            http_client = _shared_no_proxy_client()
        kwargs["openai_client"] = _build_sdk_client(
            api_key=api_key, base_url=base_url, http_client=http_client)
        # openai_client 已内置 api_key/base_url（通过 _build_sdk_client 传入），
        # 不能再传给父类——OpenAIProvider 对 openai_client + api_key 同时存在会 raise UserError。
        super().__init__(use_responses=use_responses, **kwargs)

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
            # 这里刻意用裸 OpenAIProvider 而不是 ResilientProvider：后者会让 fallback
            # 自己再重试、再 fallback，两层乘法叠加（旧实现最坏 36 次 HTTP 的一半原因）。
            # client 仍自建 —— 至少在 L0 上把 SDK 的静默重试关掉。
            fb_base = cfg["base_url"]
            # 复用进程级共享池（trust_env=False，跳过系统代理），不每次新建。
            fb_http = _shared_no_proxy_client()
            provider = OpenAIProvider(
                use_responses=False,
                openai_client=_build_sdk_client(
                    api_key=cfg["api_key"], base_url=fb_base, http_client=fb_http),
            )
            self._fallback_model_cache = provider.get_model(cfg["model"])
        except Exception:
            self._fallback_model_cache = None
        return self._fallback_model_cache

    def get_model(self, model_name: str | None) -> Model:
        inner = super().get_model(model_name)
        return ResilientModel(inner, self)
