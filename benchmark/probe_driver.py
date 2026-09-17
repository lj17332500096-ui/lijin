"""Phase 8：Router Ablation 两变体（router / oracle）——本地模型，增量落盘。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.decision_probe import main as probe_main  # noqa: E402


def main() -> int:
    for variant in ("router", "oracle"):
        print(f"===== probe variant={variant} =====", flush=True)
        probe_main(["--variant", variant, "--n", "3", "--max-turns", "4",
                    "--provider", "local", "--out", f"phase8/probe_{variant}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
