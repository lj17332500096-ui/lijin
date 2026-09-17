"""Phase 10：本地模型实验——baseline vs guardAB（Coding Set，N>=3）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 强制本地模型（禁止调用 gateway/agnes）。
os.environ["FORGE_MODEL_PREF"] = "local"

from benchmark.coding_experiment import main as ce_main  # noqa: E402

CASES = "T021,T049"


def main() -> int:
    for mode in ("baseline", "guardAB"):
        print(f"===== coding mode={mode} (local) =====", flush=True)
        ce_main(["--mode", mode, "--runs", "3", "--cases", CASES,
                 "--out", f"phase10/{mode}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
