"""Persistent session database path shared by CLI, TUI and API adapters."""
from runtime_paths import PROJECT_ROOT, state_db_path

SESSIONS_DB = state_db_path(
    "sessions.sqlite",
    env_vars=("FORGE_SESSIONS_DB",),
    legacy_path=PROJECT_ROOT / "sessions.sqlite",
)
