"""Phase 33: M3 Fixture Qualification Test (deterministic, pre-model).

Proves the fixture can only be solved via a repair boundary:
  1. initial (stage-1 + stage-2 bugs)         -> tests FAIL
  2. after intended stage-1 fix (add)         -> tests STILL FAIL
  3. failure output exposes stage-2 info      -> yes
  4. after intended stage-2 fix (series_sum)  -> tests PASS

If any step fails, the fixture is NOT qualified and the model benchmark
must not run. Fixture is reproducible/deterministic/local/no-network.
"""
import subprocess
import sys
from pathlib import Path

FIX = Path(r"F:\Byong-hermes\Byong-hermes\code_sandbox\m3_fixture")

INITIAL_CALC = "def add(a, b):\n    return a - b\n"
INITIAL_SERIES = ("def series_sum(n):\n    total = 0\n"
                  "    for i in range(n):\n        total += i\n"
                  "    return total\n")

STAGE1_CALC = "def add(a, b):\n    return a + b\n"
STAGE2_SERIES = ("def series_sum(n):\n    total = 0\n"
                 "    for i in range(1, n + 1):\n        total += i\n"
                 "    return total\n")


def run_pytest() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_calc.py", "-v", "--tb=short"],
        cwd=str(FIX), capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    results = {}
    (FIX / "calc.py").write_text(INITIAL_CALC, encoding="utf-8")
    (FIX / "series.py").write_text(INITIAL_SERIES, encoding="utf-8")

    rc, out = run_pytest()
    results["initial_fail"] = rc != 0
    print(f"[1] initial: rc={rc} FAIL={rc != 0}", flush=True)

    # stage-1 fix only (add); series_sum still wrong
    (FIX / "calc.py").write_text(STAGE1_CALC, encoding="utf-8")
    rc, out = run_pytest()
    results["stage1_still_fail"] = rc != 0
    print(f"[2] after stage-1 fix: rc={rc} STILL_FAIL={rc != 0}", flush=True)

    exposes = ("series_sum" in out and "15" in out and "10" in out)
    results["failure_exposes_stage2"] = exposes
    print(f"[3] failure exposes stage-2 info: {exposes}", flush=True)

    (FIX / "series.py").write_text(STAGE2_SERIES, encoding="utf-8")
    rc, out = run_pytest()
    results["stage2_pass"] = rc == 0
    print(f"[4] after stage-2 fix: rc={rc} PASS={rc == 0}", flush=True)

    # restore initial
    (FIX / "calc.py").write_text(INITIAL_CALC, encoding="utf-8")
    (FIX / "series.py").write_text(INITIAL_SERIES, encoding="utf-8")

    ok = all(results.values())
    print("\nM3 Fixture Truth =", "PASS" if ok else "FAIL")
    print(results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
