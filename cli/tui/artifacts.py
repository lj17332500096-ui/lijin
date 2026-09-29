"""Safe local actions for Runtime-registered artifacts in the TUI."""

from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path
from typing import Any, Iterable


TEXT_PREVIEW_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".json"}
MAX_PREVIEW_BYTES = 512 * 1024
MAX_PREVIEW_CHARS = 24_000


def resolve_registered_artifact(
    artifact: dict[str, Any] | None,
    allowed_roots: Iterable[str | Path],
) -> Path:
    """Resolve an artifact path only when it remains inside Runtime output roots."""
    if not isinstance(artifact, dict) or not artifact.get("id"):
        raise ValueError("找不到已登记的生成文件。")
    raw_path = str(artifact.get("storage_path") or "").strip()
    if not raw_path:
        raise ValueError("生成文件没有可用的保存路径。")
    path = Path(raw_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("生成文件已不存在或不是普通文件。")

    roots: list[Path] = []
    for root in allowed_roots:
        try:
            roots.append(Path(root).expanduser().resolve(strict=False))
        except (OSError, RuntimeError):
            continue
    if not roots or not any(path == root or root in path.parents for root in roots):
        raise ValueError("出于安全保护，只能打开 Runtime 登记的产物目录中的文件。")
    return path


def read_artifact_preview(path: Path) -> tuple[str, bool]:
    """Read a bounded UTF-8 preview. Return (text, was_truncated)."""
    if path.suffix.lower() not in TEXT_PREVIEW_SUFFIXES:
        raise ValueError("这种文件类型不支持 TUI 预览。")
    with path.open("rb") as stream:
        data = stream.read(MAX_PREVIEW_BYTES + 1)
    truncated = len(data) > MAX_PREVIEW_BYTES
    data = data[:MAX_PREVIEW_BYTES]
    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_PREVIEW_CHARS:
        text = text[:MAX_PREVIEW_CHARS]
        truncated = True
    if truncated:
        text += "\n\n……预览内容已截断；完整文件仍保存在原路径。"
    return text, truncated


def open_artifact(path: Path) -> None:
    """Open a registered file with the operating system's associated app."""
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


def reveal_artifact(path: Path) -> None:
    """Open the file's parent folder, selecting the file on Windows."""
    if os.name == "nt":
        subprocess.Popen(
            ["explorer.exe", f"/select,{path}"],
            close_fds=True,
        )
    else:
        webbrowser.open(path.parent.as_uri())
