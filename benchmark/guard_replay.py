"""Phase 10：Replay 实验（确定性）——用历史决策序列验证 Guard 的安全性与收益。

不调用模型；把 phase6/runA|runB|runC 与 phase9/baseline 的 coding 工具序列
重放到 Guard 逻辑，比较 baseline vs Guard 的 executions / duplicate / suppression，
并检测 false suppression（被 suppress 的调用其结果其实与上一次不同）。

输出 phase10/guard_replay.json。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import get_case  # noqa: E402
from runtime.readiness_gate import DiscoveryTracker  # noqa: E402

RUNS = [Path("phase6/runA"), Path("phase6/runB"), Path("phase6/runC"), Path("phase9/baseline")]


def _args(t: dict) -> dict:
    a = t.get("normalized_args")
    if isinstance(a, str):
        try:
            return json.loads(a)
        except Exception:
            return {}
    return a or {}


def replay(raw: dict) -> dict | None:
    cid = raw.get("id")
    try:
        case = get_case(cid)
    except KeyError:
        return None
    if case.expected.behavior != "coding":
        return None
    tools = [t for t in (raw.get("tools") or [])
             if str(t.get("status")).lower() in ("succeeded", "executed", "ok")]
    dt = DiscoveryTracker()
    suppressed = 0
    suppressed_dup_evidence = 0
    false_suppression = 0
    # 记录每个 exact identity 上一次的 result_excerpt，用于 false suppression 检测
    last_result: dict = {}
    post_verify_suppressed = 0
    for t in tools:
        name = t.get("name") or ""
        args = _args(t)
        result = str(t.get("result_excerpt") or "")
        from runtime.readiness_gate import _P9_MUTATION_TOOLS  # noqa
        if name in _P9_MUTATION_TOOLS:
            dt.bump_epoch()
            continue
        red, _ = dt.redundant_check(name, args)
        key = dt.exact_identity(name, args)
        if red:
            suppressed += 1
            prev = last_result.get(key)
            if prev is not None and prev != result:
                false_suppression += 1  # 同 identity 但结果不同 → 可能误杀
            else:
                suppressed_dup_evidence += 1
            if dt.verification_passed:
                post_verify_suppressed += 1
        else:
            dt.mark_exact_seen(name, args)
            if key is not None:
                last_result[key] = result
            # 更新 verification 状态（供 post-verify 统计）
            if name in ("run_python", "code_loop") and ("退出码: 0" in result or "passed" in result.lower()):
                dt.verification_passed = True
    return {
        "case": cid,
        "baseline_executions": len(tools),
        "suppressed": suppressed,
        "suppressed_duplicate_evidence": suppressed_dup_evidence,
        "false_suppression": false_suppression,
        "post_verify_suppressed": post_verify_suppressed,
        "guard_executions": len(tools) - suppressed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.guard_replay")
    parser.add_argument("--out", default="phase10")
    args = parser.parse_args(argv)
    rows = []
    for d in RUNS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("T*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            r = replay(raw)
            if r:
                r["run"] = str(d)
                rows.append(r)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "guard_replay.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    base = sum(r["baseline_executions"] for r in rows)
    supp = sum(r["suppressed"] for r in rows)
    dup = sum(r["suppressed_duplicate_evidence"] for r in rows)
    false = sum(r["false_suppression"] for r in rows)
    pv = sum(r["post_verify_suppressed"] for r in rows)
    print(f"coding runs: {len(rows)}")
    print(f"baseline executions: {base} -> guard executions: {base - supp} "
          f"(suppressed {supp}, {round(supp/max(1,base),3)})")
    print(f"suppressed that were true duplicate evidence: {dup}")
    print(f"false suppression: {false}")
    print(f"post-verify discovery suppressed (Guard B scope): {pv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
