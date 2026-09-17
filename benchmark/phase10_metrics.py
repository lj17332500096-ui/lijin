"""Phase 10：Post-Mutation / Post-Verify 归一化指标 + Premature Mutation 修正。

从 phase6/runA|runB|runC 与 phase9/baseline 的 coding 序列计算：
- runs_with_mutation / runs_with_verification_pass
- avg / median / P95 post_mutation_discovery、post_verify_discovery（per run）
- justified vs unjustified post-verify exploration（approximate）
- premature mutation（修正：mutation 目标未被读取过才计）
输出 phase10/metrics.json。
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import get_case  # noqa: E402
from benchmark.evidence_analysis import (  # noqa: E402
    MUTATION, READ, VERIFY, DISCOVERY, primary_stage, _target, _verification_passed,
)

RUNS = [Path("phase6/runA"), Path("phase6/runB"), Path("phase6/runC"), Path("phase9/baseline")]


def _p95(vals: list[float]) -> float:
    if not vals:
        return 0.0
    o = sorted(vals)
    return o[min(len(o) - 1, int(round(0.95 * (len(o) - 1))))]


def analyze(raw: dict) -> dict | None:
    cid = raw.get("id")
    try:
        case = get_case(cid)
    except KeyError:
        return None
    if case.expected.behavior != "coding":
        return None
    tools = [t for t in (raw.get("tools") or [])
             if str(t.get("status")).lower() in ("succeeded", "executed", "ok")]
    mut_seen = False
    verify_pass = False
    post_mut = 0
    post_verify = 0
    post_verify_unjustified = 0
    read_targets: set[str] = set()
    premature = False
    for t in tools:
        name = t.get("name") or ""
        args = t.get("normalized_args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        args = args or {}
        result = str(t.get("result_excerpt") or "")
        stage = primary_stage(name)
        if stage == "MUTATION":
            tgt = _target(name, args)
            if tgt and tgt not in read_targets:
                premature = True
            mut_seen = True
            continue
        if stage == "VERIFICATION":
            if _verification_passed(result):
                verify_pass = True
            continue
        if stage in ("READ", "DISCOVERY"):
            if mut_seen:
                post_mut += 1
            if verify_pass:
                post_verify += 1
                # unjustified：verification 通过后没有新的失败/错误上下文
                if not ("fail" in result.lower() or "error" in result.lower()
                        or "traceback" in result.lower()):
                    post_verify_unjustified += 1
            tgt = _target(name, args)
            if tgt and stage == "READ":
                read_targets.add(tgt)
    return {
        "case": cid, "mutation": mut_seen, "verify_pass": verify_pass,
        "post_mutation_discovery": post_mut,
        "post_verify_discovery": post_verify,
        "post_verify_unjustified": post_verify_unjustified,
        "premature_mutation": premature,
    }


def main() -> int:
    rows = []
    for d in RUNS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("T*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            r = analyze(raw)
            if r:
                r["run"] = str(d)
                rows.append(r)
    mut_runs = [r for r in rows if r["mutation"]]
    ver_runs = [r for r in rows if r["verify_pass"]]
    pm = [r["post_mutation_discovery"] for r in mut_runs]
    pv = [r["post_verify_discovery"] for r in ver_runs]
    pvu = [r["post_verify_unjustified"] for r in ver_runs]
    result = {
        "coding_runs": len(rows),
        "runs_with_mutation": len(mut_runs),
        "runs_with_verification_pass": len(ver_runs),
        "premature_mutation_runs": sum(1 for r in rows if r["premature_mutation"]),
        "post_mutation": {
            "total": sum(pm),
            "avg_per_run": round(statistics.mean(pm), 2) if pm else 0,
            "median": statistics.median(pm) if pm else 0,
            "p95": _p95(pm),
        },
        "post_verify": {
            "total": sum(pv),
            "avg_per_run": round(statistics.mean(pv), 2) if pv else 0,
            "median": statistics.median(pv) if pv else 0,
            "p95": _p95(pv),
            "unjustified_total": sum(pvu),
            "unjustified_avg_per_run": round(statistics.mean(pvu), 2) if pvu else 0,
        },
    }
    out = Path("phase10")
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
