"""Start and warm the configured Laya GGUF backend for the TUI lifecycle."""
from __future__ import annotations

import json
import ipaddress
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from runtime_paths import LOG_DIR

_START_LOCK = threading.Lock()
_OWNED_SERVER: subprocess.Popen | None = None


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return default if not raw else raw in {"on", "true", "1", "yes"}


def tui_laya_autostart_enabled() -> bool:
    """Only auto-start the explicitly configured GGUF backend in the TUI."""
    return (
        _flag("FORGE_LAYA", default=True)
        and os.getenv("FORGE_LAYA_BACKEND", "torch").strip().lower() == "gguf"
        and _flag("FORGE_LAYA_TUI_AUTOSTART", default=True)
    )


def _paths() -> tuple[Path, Path, Path]:
    encoder = Path(os.getenv("FORGE_LAYA_ENCODER_GGUF", "").strip()).expanduser()
    head = Path(os.getenv("FORGE_LAYA_HEAD_GGUF", "").strip()).expanduser()
    tokenizer_dir = Path(os.getenv("FORGE_LAYA_TOKENIZER_DIR", "").strip()).expanduser()
    if not str(encoder) or not encoder.is_file():
        raise FileNotFoundError(f"Laya encoder GGUF missing: {encoder}")
    if not str(head) or not head.is_file():
        raise FileNotFoundError(f"Laya head GGUF missing: {head}")
    if not str(tokenizer_dir) or not (tokenizer_dir / "tokenizer.json").is_file():
        raise FileNotFoundError(f"Laya tokenizer.json missing under: {tokenizer_dir}")
    return encoder, head, tokenizer_dir


def _service_url() -> str:
    return os.getenv("FORGE_LAYA_LLM_URL", "http://127.0.0.1:8099").strip().rstrip("/")


def _server_bind_host(hostname: str) -> str:
    """Keep auto-start local unless remote, unauthenticated binding is explicit."""
    normalized = hostname.strip().rstrip(".").lower()
    if normalized == "localhost":
        # Avoid relying on hosts-file or DNS resolution for the local-only default.
        return "127.0.0.1"
    try:
        is_loopback = ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        is_loopback = False
    if is_loopback:
        return hostname
    if not _flag("FORGE_LAYA_TUI_ALLOW_REMOTE_BIND"):
        raise RuntimeError(
            "Laya TUI 自动启动默认只允许 loopback 监听；远程绑定请显式设置 "
            "FORGE_LAYA_TUI_ALLOW_REMOTE_BIND=on。llama.cpp 服务没有认证，"
            "远程绑定会向该地址可达的网络开放接口。"
        )
    return hostname


def _server_matches(encoder: Path) -> bool:
    try:
        request = Request(_service_url() + "/props", method="GET")
        with urlopen(request, timeout=2.0) as response:
            if response.status != 200:
                return False
            props = json.loads(response.read().decode("utf-8"))
        loaded = props.get("model_path") or props.get("model_alias")
        return bool(loaded) and os.path.normcase(os.path.abspath(loaded)) == os.path.normcase(
            os.path.abspath(encoder)
        )
    except Exception:
        return False


def _start_server(encoder: Path) -> None:
    global _OWNED_SERVER
    exe_value = os.getenv("FORGE_LAYA_LLAMA_SERVER_EXE", "").strip()
    if not exe_value:
        raise RuntimeError("FORGE_LAYA_LLAMA_SERVER_EXE is not configured")
    exe = Path(exe_value).expanduser()
    if not exe.is_file():
        raise FileNotFoundError(f"llama-server.exe missing: {exe}")

    parsed = urlsplit(_service_url())
    if parsed.scheme != "http" or not parsed.hostname:
        raise RuntimeError("FORGE_LAYA_LLM_URL must be an http:// URL")
    bind_host = _server_bind_host(parsed.hostname)
    port = parsed.port or 80
    backend = os.getenv("FORGE_LAYA_LLAMA_BACKEND", "sycl").strip().lower()
    gpu_layers = os.getenv("FORGE_LAYA_LLAMA_GPU_LAYERS", "99").strip() or "99"
    env = os.environ.copy()
    env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")
    if backend == "sycl":
        env.setdefault("ZES_ENABLE_SYSMAN", "1")
        env.setdefault("ONEAPI_DEVICE_SELECTOR", "level_zero:gpu")
    elif backend == "vulkan":
        env.setdefault("GGML_VULKAN_DEVICE", "0")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = LOG_DIR / "laya-gguf-server.stdout.log"
    stderr_path = LOG_DIR / "laya-gguf-server.stderr.log"
    stdout_file = stdout_path.open("ab")
    stderr_file = stderr_path.open("ab")
    kwargs: dict = {
        "cwd": str(exe.parent),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": stdout_file,
        "stderr": stderr_file,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        _OWNED_SERVER = subprocess.Popen(
            [
                str(exe), "-m", str(encoder), "--embeddings", "--pooling", "none",
                "--ctx-size", "1024", "--n-gpu-layers", gpu_layers,
                "--host", bind_host, "--port", str(port),
            ],
            **kwargs,
        )
    finally:
        stdout_file.close()
        stderr_file.close()


def prepare_laya_for_tui() -> tuple[bool, str]:
    """Start the GGUF server if needed, then synchronously warm the Python head."""
    global _OWNED_SERVER
    if not tui_laya_autostart_enabled():
        return False, "Laya TUI 自动预热未启用"
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False, "测试环境跳过 Laya 自动启动"

    with _START_LOCK:
        encoder, _head, _tokenizer = _paths()
        if not _server_matches(encoder):
            # Do not kill or replace an unrelated service occupying the configured port.
            try:
                _start_server(encoder)
            except OSError as exc:
                raise RuntimeError(f"无法启动 llama.cpp 服务：{exc}") from exc

            try:
                timeout = max(5.0, float(os.getenv("FORGE_LAYA_START_TIMEOUT", "180")))
            except ValueError:
                timeout = 180.0
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if _OWNED_SERVER is not None and _OWNED_SERVER.poll() is not None:
                    raise RuntimeError(
                        f"llama.cpp 服务提前退出，详情见 {LOG_DIR / 'laya-gguf-server.stderr.log'}"
                    )
                if _server_matches(encoder):
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError(
                    f"等待 llama.cpp 加载模型超时；查看 {LOG_DIR / 'laya-gguf-server.stderr.log'}"
                )

        # Import/construct the singleton in a worker thread from the TUI. The .env
        # FORGE_LAYA_PRELOAD=1 setting loads the companion head and tokenizer now.
        from runtime.laya_router import laya_router

        router = laya_router()
        router._ensure()
        if router.backend != "gguf":
            detail = getattr(router, "_backend_error", None) or f"backend={router.backend}"
            raise RuntimeError(f"Laya GGUF 预热失败：{detail}")
        return True, f"Laya GGUF 已就绪（llama.cpp + {encoder.name}）"


def stop_tui_owned_laya_server() -> None:
    """Stop only the server this TUI process started; leave pre-existing servers alone."""
    global _OWNED_SERVER
    process = _OWNED_SERVER
    _OWNED_SERVER = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
