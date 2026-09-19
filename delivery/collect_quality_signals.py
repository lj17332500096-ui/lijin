#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段1 输入采集器：分批判跑测试套件，产出结构化质量信号。

为什么分批：本机沙箱有「批量删除护栏」，全量 pytest 可能被中途打断，
表现为失败集合每次漂移。分批跑 + 单独记录，才能拿到可回溯的干净信号。

输出：
  delivery/input/quality_signals.json  —— 结构化质量信号（阶段1 主输入）
  delivery/input/test_inventory.csv    —— 逐文件结果（阶段1 CSV 输入）

用法：
  python delivery/collect_quality_signals.py [--batch 12] [--out delivery/input]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
TESTS = ROOT / "tests"

# 与项目当前阶段（UI 已冻结）一致：这些模块默认整模块跳过
FROZEN_UI_TESTS = {
    "tests/test_webapp.py",
    "tests/test_llama_bridge.py",
    "tests/test_llama_stream_resume.py",
}

_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+)\s+passed|(?P<failed>\d+)\s+failed|(?P<skipped>\d+)\s+skipped|"
    r"(?P<errors>\d+)\s+error",
)


def _parse_counts(text: str) -> dict:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    # 只取最后几行（pytest 的 summary 行）
    tail = "\n".join(text.strip().splitlines()[-6:])
    m = re.search(r"(\d+) passed", tail)
    if m:
        counts["passed"] = int(m.group(1))
    m = re.search(r"(\d+) failed", tail)
    if m:
        counts["failed"] = int(m.group(1))
    m = re.search(r"(\d+) skipped", tail)
    if m:
        counts["skipped"] = int(m.group(1))
    m = re.search(r"(\d+) error", tail)
    if m:
        counts["errors"] = int(m.group(1))
    return counts


def _failed_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        # "FAILED tests/x.py::TestY::test_z - AssertionError: ..."
        if line.startswith("FAILED "):
            names.append(line[len("FAILED "):].split(" - ")[0].strip())
    return names


def run_batch(files: list[str], timeout: int) -> dict:
    cmd = [str(PY), "-m", "pytest", *files, "-q", "--tb=no", "-p", "no:cacheprovider"]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        rc = -9
        timed_out = True
    elapsed = round(time.time() - started, 2)
    counts = _parse_counts(out)
    return {
        "files": files,
        "returncode": rc,
        "timed_out": timed_out,
        "duration_s": elapsed,
        "counts": counts,
        "failed_names": _failed_names(out),
        "interrupted": ("KeyboardInterrupt" in out) or ("Operation not permitted" in out),
        "tail": "\n".join(out.strip().splitlines()[-4:]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="分批判跑测试并采集质量信号")
    ap.add_argument("--batch", type=int, default=12, help="每批测试文件数（默认12）")
    ap.add_argument("--timeout", type=int, default=900, help="单批超时秒数（默认900）")
    ap.add_argument("--out", default="delivery/input", help="输出目录")
    args = ap.parse_args()

    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    all_files = sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in TESTS.glob("test_*.py")
    )
    batches = [all_files[i:i + args.batch] for i in range(0, len(all_files), args.batch)]

    results = []
    for idx, files in enumerate(batches, 1):
        print(f"[batch {idx}/{len(batches)}] {len(files)} files ...", flush=True)
        r = run_batch(files, args.timeout)
        r["batch_index"] = idx
        results.append(r)
        c = r["counts"]
        print(
            f"    rc={r['returncode']} passed={c['passed']} failed={c['failed']} "
            f"skipped={c['skipped']} err={c['errors']} {r['duration_s']}s"
            + (" [INTERRUPTED]" if r["interrupted"] else "")
            + (" [TIMEOUT]" if r["timed_out"] else ""),
            flush=True,
        )
        for n in r["failed_names"]:
            print(f"      FAILED {n}", flush=True)

    totals = {
        k: sum(r["counts"][k] for r in results)
        for k in ("passed", "failed", "skipped", "errors")
    }
    failed_all = sorted({n for r in results for n in r["failed_names"]})

    payload = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "project": "my_creative_agent",
        "stage": "UI frozen / backend-only optimization",
        "runner": "pytest -q --tb=no",
        "batch_size": args.batch,
        "total_test_files": len(all_files),
        "batches": len(results),
        "totals": totals,
        "failed_tests": failed_all,
        "frozen_ui_tests": sorted(FROZEN_UI_TESTS),
        "batch_detail": results,
    }
    (out_dir / "quality_signals.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows = ["scope,files,passed,failed,skipped,errors,returncode,duration_s,interrupted"]
    for r in results:
        c = r["counts"]
        rows.append(
            f"batch_{r['batch_index']},{len(r['files'])},{c['passed']},{c['failed']},"
            f"{c['skipped']},{c['errors']},{r['returncode']},{r['duration_s']},"
            f"{'yes' if r['interrupted'] else 'no'}"
        )
    (out_dir / "test_inventory.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    print("\n=== TOTALS ===")
    print(json.dumps(totals, ensure_ascii=False))
    if failed_all:
        print("failed tests:")
        for n in failed_all:
            print("  -", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
