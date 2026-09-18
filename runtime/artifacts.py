"""Artifact Store：产物登记（服务器端真相，消灭"模型说保存了但文件不存在"）。

机制：
- ArtifactTracker 在任务开始前快照产物目录（notes/ exports/ 等），
  成功结束后 diff 出新文件并逐条登记（sha256/size/kind 自动计算）；
- 模型/UI 引用 artifact_id，web 按 id 下载；
- 登记发生在 Runtime 层，工具实现零改动。
"""

import hashlib
from pathlib import Path
from typing import Any

_KIND_BY_SUFFIX = {
    ".md": "markdown",
    ".docx": "word",
    ".xlsx": "excel",
    ".pptx": "ppt",
    ".csv": "csv",
    ".json": "json",
    ".txt": "text",
    ".py": "python",
    ".png": "image",
    ".jpg": "image",
    ".pdf": "pdf",
}


def kind_for(path: Path) -> str:
    return _KIND_BY_SUFFIX.get(path.suffix.lower(), path.suffix.lstrip(".") or "file")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class ArtifactTracker:
    """任务运行期产物跟踪器：快照 → diff → 登记。"""

    def __init__(self, dirs: tuple[Path, ...]):
        self.dirs = [Path(d) for d in dirs]
        self._before: set[Path] = set()

    def snapshot(self) -> None:
        self._before = set()
        for root in self.dirs:
            if not root.exists():
                continue
            self._before.update(p for p in root.rglob("*") if p.is_file())

    def new_files(self) -> list[Path]:
        """快照之后出现、且尚未登记的新文件（不落库，供 Completion Gate 取证据）。"""
        found: list[Path] = []
        for root in self.dirs:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path in self._before:
                    continue
                if path.suffix.lower() in (".tmp", ".part"):
                    continue
                found.append(path)
        return found

    def register_new(self, manager: Any, *, task_id: str | None, session_id: str | None) -> list[dict]:
        """登记快照之后出现的新文件；返回登记的 artifact dict 列表。"""
        registered: list[dict] = []
        for path in self.new_files():
            try:
                artifact = manager.register_artifact(
                    task_id=task_id,
                    session_id=session_id,
                    name=path.name,
                    kind=kind_for(path),
                    storage_path=str(path),
                    sha256=sha256_of(path),
                    size_bytes=path.stat().st_size,
                )
                self._before.add(path)  # 已登记视为快照内，避免重复
                registered.append(artifact)
            except Exception:
                continue
        return registered
