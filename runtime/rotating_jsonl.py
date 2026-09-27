"""Small bounded JSONL appender for runtime diagnostics."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def append_jsonl(path: Path, record: dict, *, env_prefix: str, default_bytes: int = 10_000_000,
                 backups: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _guard:
        lock = _locks.setdefault(str(path.resolve()), threading.Lock())
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    encoded = line.encode("utf-8")
    try:
        max_bytes = max(1024, int(os.getenv(f"{env_prefix}_MAX_BYTES", str(default_bytes))))
    except ValueError:
        max_bytes = default_bytes
    with lock:
        if path.exists() and path.stat().st_size + len(encoded) > max_bytes:
            oldest = path.with_name(path.name + f".{backups}")
            oldest.unlink(missing_ok=True)
            for index in range(backups - 1, 0, -1):
                source = path.with_name(path.name + f".{index}")
                if source.exists():
                    source.replace(path.with_name(path.name + f".{index + 1}"))
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
