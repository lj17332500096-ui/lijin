"""Copy legacy runtime artifacts into var/ without deleting the originals.

Run once while all Agent, Web, daemon, and metrics processes are stopped:
    python scripts/migrate_runtime_data.py                 # preview
    python scripts/migrate_runtime_data.py --apply --i-stopped-agent
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runtime_paths import RUNTIME_ROOT  # noqa: E402

TARGET = RUNTIME_ROOT
LEGACY_ROOT = ROOT / "var" / "legacy_runtime_2026-09"


def _legacy_source(relative: str) -> Path:
    """Use the original project path, or its preserved copy after cleanup."""
    original = ROOT / relative
    if original.exists():
        return original
    preserved = LEGACY_ROOT / relative
    return preserved if preserved.exists() else original

DB_FILES = ("agent.db", "sessions.sqlite", "memory.db")
DIR_COPIES = (
    (ROOT / "traces", TARGET / "traces"),
    (_legacy_source("data/provider_attempts"), TARGET / "logs" / "provider_attempts"),
    (_legacy_source("tests/reports"), TARGET / "test-reports"),
    (_legacy_source("logs/sandbox_snapshots"), TARGET / "artifacts" / "sandbox_snapshots"),
    (_legacy_source("logs/snapshots"), TARGET / "artifacts" / "snapshots"),
)
FILE_COPIES = (
    (_legacy_source("data/rag_index.json"), TARGET / "cache" / "rag_index.json"),
)


def _display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _copy_database(source: Path, destination: Path, *, apply: bool) -> str:
    if not source.exists():
        return f"skip missing {_display(source)}"
    if destination.exists():
        return f"skip existing destination {_display(destination)}"
    if not apply:
        return f"would backup {_display(source)} -> {_display(destination)}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(destination), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        src.close()
    return f"copied database {_display(source)} -> {_display(destination)}"


def _copy_file(source: Path, destination: Path, *, apply: bool) -> list[str]:
    if not source.exists():
        return []
    if destination.exists():
        return [f"skip existing destination {_display(destination)}"]
    if not apply:
        return [f"would copy {_display(source)} -> {_display(destination)}"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return [f"copied {_display(source)} -> {_display(destination)}"]


def _copy_tree(source: Path, destination: Path, *, apply: bool,
               excluded_roots: tuple[str, ...] = ()) -> list[str]:
    if not source.exists() or not source.is_dir():
        return []
    rows: list[str] = []
    for src in source.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source)
        if rel.parts and rel.parts[0] in excluded_roots:
            continue
        dst = destination / rel
        if dst.exists():
            rows.append(f"skip existing destination {_display(dst)}")
            continue
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            rows.append(f"copied {_display(src)} -> {_display(dst)}")
        else:
            rows.append(f"would copy {_display(src)} -> {_display(dst)}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="copy files; originals are retained")
    parser.add_argument("--i-stopped-agent", action="store_true",
                        help="confirm Agent/Web/daemon/metrics processes are stopped")
    parser.add_argument("--verbose", action="store_true", help="list every copied or skipped file")
    args = parser.parse_args()
    if args.apply and not args.i_stopped_agent:
        parser.error("--apply requires --i-stopped-agent so open SQLite files are not copied live")

    actions: list[str] = []
    for filename in DB_FILES:
        actions.append(_copy_database(_legacy_source(filename), TARGET / "state" / filename,
                                      apply=args.apply))
    actions.extend(_copy_tree(_legacy_source("logs"), TARGET / "logs", apply=args.apply,
                              excluded_roots=("sandbox_snapshots", "snapshots")))
    for source, destination in DIR_COPIES:
        actions.extend(_copy_tree(source, destination, apply=args.apply))
    for source, destination in FILE_COPIES:
        actions.extend(_copy_file(source, destination, apply=args.apply))

    print(f"Migration {'applied' if args.apply else 'preview'}: {len(actions)} items.")
    if args.verbose:
        print("\n".join(actions) if actions else "No legacy runtime data found.")
    elif actions:
        print("\n".join(actions[:12]))
        if len(actions) > 12:
            print(f"... {len(actions) - 12} more items; use --verbose to list them.")
    else:
        print("No legacy runtime data found.")
    print("Original files are retained. Review the copied data before deleting any legacy path.")
    if not args.apply:
        print("Preview only. Stop all Agent processes before applying the migration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
