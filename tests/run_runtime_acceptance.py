"""Phase 32: Run the FORGE Runtime Acceptance Suite.

Reads runtime_acceptance_suite.json and runs every listed test file,
deduplicating shared files. Exits non-zero if any test fails.

Usage:
    python tests/run_runtime_acceptance.py
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
MANIFEST = BASE / "runtime_acceptance_suite.json"


def main() -> int:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files: list[str] = []
    for cat_files in data["categories"].values():
        for f in cat_files:
            if f not in files:
                files.append(f)

    print(f"Runtime Acceptance Suite: {len(files)} files")
    for f in files:
        print(f"  {f}")

    cmd = [sys.executable, "-X", "utf8", "-m", "pytest", *files,
           "-q", "-p", "no:cacheprovider"]
    print("\nRunning...", flush=True)
    proc = subprocess.run(cmd, cwd=str(BASE))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
