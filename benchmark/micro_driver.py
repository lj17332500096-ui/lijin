"""Phase 13：本地 microbenchmark — baseline（无 fact）vs fact（Runtime Fact）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["FORGE_MODEL_PREF"] = "local"  # 禁止 gateway

from benchmark.microbenchmark import main as mb_main  # noqa: E402

CASES = "M1,M4,M6,M7,M8"


def main() -> int:
    for mode in ("baseline", "fact"):
        print(f"===== micro mode={mode} (local) =====", flush=True)
        mb_main(["--mode", mode, "--runs", "1", "--cases", CASES,
                 "--out", f"phase13/{mode}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
