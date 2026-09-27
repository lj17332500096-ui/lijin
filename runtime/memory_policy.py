"""Shared runtime switches for persistent and episodic memory."""
from __future__ import annotations

import os


def memory_enabled() -> bool:
    """Global kill switch for semantic, project, and episodic memory access."""
    return os.getenv("FORGE_MEMORY_ENABLED", "1").strip().lower() not in (
        "0", "off", "false", "no",
    )


def episode_ingest_enabled() -> bool:
    """Episode collection defaults on, but obeys the global memory kill switch."""
    if not memory_enabled():
        return False
    return os.getenv("EPISODE_INGEST", "1").strip().lower() not in (
        "0", "off", "false", "no",
    )
