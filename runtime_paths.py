"""Central paths for mutable runtime and evaluation output.

New generated files live under ``var/`` (or ``FORGE_RUNTIME_DIR``). Existing
root-level SQLite databases remain readable until explicitly migrated, so a
path cleanup cannot silently start the Agent with empty state.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except Exception:
    pass


_RUNTIME_ROOT_VALUE = (os.getenv("FORGE_RUNTIME_DIR") or "").strip()
RUNTIME_ROOT_CONFIGURED = bool(_RUNTIME_ROOT_VALUE)


def _configured_root() -> Path:
    raw = _RUNTIME_ROOT_VALUE
    root = Path(raw).expanduser() if raw else PROJECT_ROOT / "var"
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root


RUNTIME_ROOT = _configured_root()
STATE_DIR = RUNTIME_ROOT / "state"


LOG_DIR = RUNTIME_ROOT / "logs"
TRACE_DIR = RUNTIME_ROOT / "traces"
ARTIFACT_DIR = RUNTIME_ROOT / "artifacts"
TEMP_DIR = RUNTIME_ROOT / "tmp"
TEST_REPORT_DIR = RUNTIME_ROOT / "test-reports"


def state_db_path(filename: str, *, env_vars: tuple[str, ...] = (),
                  legacy_path: Path | None = None) -> Path:
    """Resolve a state DB path with explicit env overrides and legacy fallback.

    Existing legacy DBs are not copied or modified here. A one-time migration
    can be performed while the application is stopped; until then, continue
    using the existing DB rather than hiding its state behind a fresh DB.
    """
    for env_name in env_vars:
        raw = (os.getenv(env_name) or "").strip()
        if raw:
            path = Path(raw).expanduser()
            path = path if path.is_absolute() else PROJECT_ROOT / path
            path.parent.mkdir(parents=True, exist_ok=True)
            return path

    destination = STATE_DIR / filename
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination
    if (not RUNTIME_ROOT_CONFIGURED and legacy_path is not None
            and legacy_path.exists()):
        return legacy_path
    return destination


def generated_path(category: str, filename: str) -> Path:
    """Resolve a new cache/output path inside the runtime root."""
    destination = RUNTIME_ROOT / category / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def ensure_parent(path: str | Path) -> Path:
    """Create the destination parent for a runtime-generated file."""
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved
