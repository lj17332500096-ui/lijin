"""Phase 7：Target Set + Control Set 连续 3 轮（A/B/C）。

用法：
    python -m benchmark.target_runner --out phase7 --runs 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONTROL = ["T001", "T002", "T003", "T004", "T005", "T006", "T010", "T011", "T030", "T031"]
TARGET = [
    "T007", "T008", "T009", "T013", "T014", "T017", "T018", "T019", "T020",
    "T021", "T022", "T023", "T024", "T025", "T026", "T027", "T028", "T029",
    "T033", "T034", "T036", "T038", "T039", "T042", "T043", "T044", "T045", "T049",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.target_runner")
    parser.add_argument("--out", default="phase7")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=150)
    args = parser.parse_args(argv)
    from benchmark.live_runner import main as lr_main

    only = ",".join(CONTROL + TARGET)
    for i in range(args.runs):
        tag = chr(ord("A") + i)
        outdir = str(Path(args.out) / f"run{tag}")
        print(f"===== Phase7 target run {tag} ({len(CONTROL+TARGET)} cases) =====", flush=True)
        lr_main(["--out", outdir, "--only", only, "--run-id", f"P7{tag}",
                 "--timeout", str(args.timeout)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
