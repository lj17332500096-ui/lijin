"""网页界面宿主（已冻结）：仅托管 llama-ui 前端与 OpenAI 兼容桥。

⚠️ 阶段状态：本项目已进入「只优化后端」阶段，本文件属于冻结的 UI 层。
   默认情况下 `python webapp.py` 会拒绝启动并指向 CLI 消息平台；
   仅当显式设置 FORGE_ENABLE_UI=1 或加 --enable-frozen-ui 时才真正起服务。
   冻结范围与边界约定见 ui_frozen.py；主入口请改用 `python main.py`。

本文件只保留「前端宿主」职责（不再直接对话、不推 SSE、不碰会话库）：
  · /                           → 302 跳 /llama-ui/
  · /props /v1/* /tools /slots /models/*  → llama_bridge 的 OpenAI 兼容端点
  · /llama-ui                   → _SPAStaticFiles 托管 SvelteKit 构建产物

原 63 条 /api/* 路由已于 2026-09-22 归档删除（API 接口层审计 Phase C）：
  依据 —— 生产 bundle 中 "/api/" 命中 0 次、assistant_delta/run_id/EventSource 命中均为 0、
  全仓无 HTTP 消费者、唯一测试消费者 tests/test_webapp.py 处于冻结跳过；
  它是一套前端从未调用过的并行协议面，属无人消费的死表面。
  恢复点 —— git tag archive/api-routes-pre-delete-20260922（`git show <tag>:webapp.py`）。
  审计依据 —— docs/archive/2026-09/FORGE-API-LAYER-AUDIT-2026-09-22.md §17 与 §19。

运行（已开启冻结层时）：python webapp.py          # 默认 http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import sys
import webbrowser
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ui_frozen import require_ui_enabled

# llama-ui 前端适配层（OpenAI 协议 → AgentRuntime）—— 归档后成为唯一对外协议
import llama_bridge

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765


# 这些扩展名/前缀是「真实静态资源」，它们 404 就是真缺失，不能回落成 HTML
# （否则浏览器会把 index.html 当 JS 执行，掩盖真实问题）。
_RESOURCE_SUFFIXES = (".js", ".css", ".map", ".json", ".png", ".jpg", ".jpeg",
                      ".svg", ".gif", ".ico", ".webp", ".woff", ".woff2",
                      ".ttf", ".eot", ".webmanifest")


class _SPAStaticFiles(StaticFiles):
    """SPA fallback 静态目录：仅对前端路由路径回落到 index.html，静态资源保持真 404。

    llama-ui 是单页应用且用 history 路由（bundle 内有 pushState），刷新子路径
    （如 /llama-ui/chat/xxx）时服务器没有对应文件，必须回落 index.html 交给
    前端路由，否则直接 404。

    关键设计（避免旧 bug）：
    - **不用 html=True**：Starlette 的 html 模式会在静态文件缺失时回落 404.html
      （仍返回 200），会把 `_app/xxx.js` 真 404 也变成 200，掩盖资源缺失。
    - 带资源扩展名 / `_app/` 前缀 / `apple-splash` 等静态文件 → 404 就是 404。
    - 无扩展名的「前端路由」（chat/xxx、settings 等）→ 回落 index.html。
    """

    def _is_api_like(self, path: str) -> bool:
        """判断是否为 API-like 命名空间（不得 SPA fallback 成 HTML）。

        llama-ui 前端存在大量相对请求（./props、./v1/models 等），页面挂在
        /llama-ui/ 下会被解析成 /llama-ui/props。若这些 API-like 路径被静态层
        回落到 index.html，前端按 JSON 解析会报 Unexpected token '<'。
        """
        lowered = path.lower().lstrip("/")
        if lowered.startswith("llama-ui/"):
            lowered = lowered[len("llama-ui/"):]
        if lowered.startswith(("props/", "v1/", "tools/", "slots/", "models/", "stream/")):
            return True
        return lowered in ("props", "v1", "tools", "slots", "models", "stream")

    def _is_static_resource(self, path: str, scope: dict) -> bool:
        p = path.lower()
        if p.startswith("_app/") or "apple-splash" in p or "favicon" in p or "manifest" in p:
            return True
        if any(p.endswith(suffix) for suffix in _RESOURCE_SUFFIXES):
            return True
        if self._is_api_like(p) or self._is_api_like(scope.get("path", "")):
            return True
        return False

    async def get_response(self, path, scope):
        # 目录请求（/llama-ui/ 或 /llama-ui）→ 直接返回 index.html
        if path in ("", "/", "./"):
            return await super().get_response("index.html", scope)
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and not self._is_static_resource(path, scope):
                # 普通前端路由（无资源扩展名、且不是 API-like 命名空间）→ 回落 index.html
                return await super().get_response("index.html", scope)
            # API-like 命名空间（props/...、v1/... 等）/静态资源 → 保留 404 不回落
            raise


class UiAuthMiddleware:
    """Protect the complete web/API surface when a non-loopback bind is enabled."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        app_obj = scope.get("app")
        state = getattr(app_obj, "state", None)
        if not state or not getattr(state, "require_ui_auth", False):
            await self.app(scope, receive, send)
            return
        expected = getattr(state, "ui_api_token", "")
        tokens = getattr(state, "ui_api_tokens", None) or {}
        if not tokens and expected:
            tokens = {"default": expected}
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1").strip()
        principal = ""
        supplied = ""
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
            for candidate, token in tokens.items():
                if hmac.compare_digest(supplied, token):
                    principal = candidate
        elif authorization.lower().startswith("basic "):
            try:
                credentials = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
                username, separator, password = credentials.partition(":")
                supplied = password if separator else ""
                candidate = username.strip()
                if candidate in tokens and hmac.compare_digest(supplied, tokens[candidate]):
                    principal = candidate
                elif candidate not in tokens and "default" in tokens and hmac.compare_digest(
                        supplied, tokens["default"]):
                    # Preserve legacy Basic auth where the username was cosmetic.
                    principal = "default"
            except (ValueError, UnicodeDecodeError):
                supplied = ""
        if not supplied or not principal:
            response = JSONResponse(
                {"error": {"message": "需要有效的 Bearer token", "code": "unauthorized"}},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer, Basic realm="FORGE"'},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["forge_principal"] = principal
        await self.app(scope, receive, send)


async def index_page(_request: Request) -> RedirectResponse:
    """根路径 → llama-ui 前端（OpenAI 协议，经 llama_bridge 接 AgentRuntime）。

    必须重定向到 /llama-ui/ 而不是直接返回 index.html：
    index.html 用的是**相对路径**（./_app/...  ./manifest.webmanifest），
    在 / 下会被浏览器解析成 /_app/...，而静态资源只挂在 /llama-ui 挂载点下，
    直接返回 HTML 会导致 JS/CSS 全部 404（页面白屏）。
    """
    return RedirectResponse("/llama-ui/", status_code=302)


app = Starlette(
    routes=[
        Route("/", index_page),

        # llama-ui 前端适配层（OpenAI 协议端点 + 静态资源）—— 唯一对外协议。
        # 原 /api/* 63 条已归档删除，见文件头说明与归档审计 §17。
        *llama_bridge.build_llama_ui_routes(),
        Mount("/llama-ui", app=_SPAStaticFiles(
            directory=str(BASE_DIR / "web" / "llama-ui")),
            name="llama-ui-assets"),
    ]
)
app.add_middleware(UiAuthMiddleware)

#: 非回环监听必须显式 opt-in：本进程暴露会执行工具 / 跑 agent turn 的端点。
ALLOW_NONLOCAL_ENV = "FORGE_ALLOW_NONLOCAL_UI"
UI_API_TOKEN_ENV = "FORGE_UI_API_TOKEN"
UI_API_TOKENS_ENV = "FORGE_UI_API_TOKENS"
_MIN_UI_API_TOKEN_LENGTH = 32
_TRUTHY = {"1", "true", "yes", "on", "y"}


def _is_loopback_host(host: str) -> bool:
    """监听地址是否为回环（用 ipaddress 判定，覆盖 127.0.0.0/8 整段与 ::1）。"""
    h = (host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h.lower() in ("localhost", "::1"):
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _configured_ui_tokens() -> dict[str, str]:
    """Load named remote principals; retain the single-token legacy principal."""
    tokens: dict[str, str] = {}
    raw = os.getenv(UI_API_TOKENS_ENV, "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        for name, token in parsed.items():
            principal = str(name).strip()
            secret = str(token).strip()
            if (re.fullmatch(r"[A-Za-z0-9._-]{1,64}", principal)
                    and len(secret) >= _MIN_UI_API_TOKEN_LENGTH):
                tokens[principal] = secret
    legacy = os.getenv(UI_API_TOKEN_ENV, "").strip()
    if len(legacy) >= _MIN_UI_API_TOKEN_LENGTH:
        tokens.setdefault("default", legacy)
    return tokens


def main() -> None:
    parser = argparse.ArgumentParser(description="全能助手 - 本地网页界面（已冻结，见 ui_frozen.py）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--enable-frozen-ui", action="store_true",
                        help="显式开启已冻结的网页界面（等价于 FORGE_ENABLE_UI=1）")
    args = parser.parse_args()

    # 冻结层门禁：未显式开启时拒绝启动，避免「只优化后端」阶段被 UI 意外带起来。
    if not require_ui_enabled(sys.argv[1:]):
        raise SystemExit(2)

    # 绑定断言（API 接口层审计 C6）：llama_bridge 暴露 `/tools`（直接执行工具）与
    # `/v1/chat/completions`（跑完整 agent turn，内部同样执行工具），故本进程只应监听
    # 回环地址。非回环必须显式 opt-in —— 否则"顺手 --host 0.0.0.0"就等于把工具执行面
    # 开进局域网。该断言与 llama_bridge 的 Content-Type 守卫互补：一个管"谁能连进来"，
    # 一个管"同机浏览器里的其它页面能否借道"（CSRF）。
    if not _is_loopback_host(args.host):
        if os.getenv(ALLOW_NONLOCAL_ENV, "").strip().lower() not in _TRUTHY:
            print(
                f"[UI] 拒绝以非回环地址 {args.host!r} 启动：本进程的 /tools 与 "
                "/v1/chat/completions 会执行工具与 agent turn。\n"
                f"     确实需要对外监听时，请显式设置 {ALLOW_NONLOCAL_ENV}=1"
                "（并自行在反代层加鉴权），或改用默认的 127.0.0.1。",
                file=sys.stderr,
            )
            raise SystemExit(2)
        tokens = _configured_ui_tokens()
        if not tokens:
            print(
                f"[UI] 拒绝非回环启动：必须设置 {UI_API_TOKEN_ENV}（至少 "
                f"{_MIN_UI_API_TOKEN_LENGTH} 个字符）或有效的 {UI_API_TOKENS_ENV}；"
                "网页与 API 请求都需要认证。",
                file=sys.stderr,
            )
            raise SystemExit(2)
        app.state.require_ui_auth = True
        app.state.ui_api_tokens = tokens
        app.state.ui_api_token = tokens.get("default", "")
    else:
        app.state.require_ui_auth = False
        app.state.ui_api_token = ""
        app.state.ui_api_tokens = {}

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    url = f"http://{args.host}:{args.port}"
    print(f"🌐 全能助手网页界面已启动：{url}")
    print("按 Ctrl+C 停止。会话与终端模式共用 sessions.sqlite。")
    try:
        from runtime.task_manager import auto_recover

        recovered = auto_recover()
        if recovered:
            print(f"[RUNTIME] 自动恢复 {len(recovered)} 个崩溃遗留任务（RUNNING→failed）")
    except Exception:
        pass
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
