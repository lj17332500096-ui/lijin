# -*- coding: utf-8 -*-
"""探针：护栏修复前后的配对分析（A/B）。

为什么需要配对而不是只看总通过率：
    本项目实测过跑间不确定性 —— n=50 只能检出 ≈±28pp，单跑总率的差异
    不足以支撑"改好/改坏"的结论。配对分析至少能回答：
    "翻转发生在哪些 case、它们有没有共同特征"。

用法：
    python delivery/probe_guard_fix_ab.py [baseline_report] [after_report]
"""
from __future__ import annotations

import io
import json
import os
import statistics
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A_DEFAULT = os.path.join(ROOT, "report_baseline_router_on.json")
B_DEFAULT = os.path.join(ROOT, "report_after_guard_fix.json")


def load(path: str) -> dict:
    return json.load(io.open(path, encoding="utf-8"))


def rows_by_case(rep: dict) -> dict:
    return {r["case"]: r for r in rep.get("rows") or []}


def _count(value) -> int:
    """报告里该字段既可能是数量(int)，也可能是明细(list) —— 都兼容。"""
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return len(value)
    return 0


def calls_of(row: dict) -> int:
    return _count(row.get("tool_calls"))


def blocked_of(row: dict) -> int:
    return _count(row.get("blocked_calls"))


def violated(row: dict, kind: str) -> bool:
    s = row.get("safety") or {}
    v = s.get(kind)
    if isinstance(v, dict):
        return bool(v.get("observed_violation"))
    return bool(v)


def main(argv: list[str]) -> int:
    pa = argv[1] if len(argv) > 1 else A_DEFAULT
    pb = argv[2] if len(argv) > 2 else B_DEFAULT
    A, B = load(pa), load(pb)
    ra, rb = rows_by_case(A), rows_by_case(B)
    common = sorted(set(ra) & set(rb))

    print(f"A(前) = {os.path.basename(pa)}   {A.get('summary', {}).get('behavior_pass_rate')}")
    print(f"B(后) = {os.path.basename(pb)}   {B.get('summary', {}).get('behavior_pass_rate')}")
    print()

    # ---- 1. 配对翻转 ----
    gain, lose = [], []
    for c in common:
        pa_ok = bool(ra[c].get("behavior_pass"))
        pb_ok = bool(rb[c].get("behavior_pass"))
        if not pa_ok and pb_ok:
            gain.append(c)
        elif pa_ok and not pb_ok:
            lose.append(c)
    print("=== 配对翻转（同一 case，前后）===")
    print(f"  fail -> pass（改善）: {len(gain)}  {gain}")
    print(f"  pass -> fail（退化）: {len(lose)}  {lose}")
    print(f"  净变化              : {len(gain) - len(lose)}")
    print(f"  配对一致率          : {len(common) - len(gain) - len(lose)}/{len(common)}")
    print()

    # ---- 2. 退化 case 的现场 ----
    print("=== 退化 case 明细（B 组为什么失败）===")
    for c in lose:
        a, b = ra[c], rb[c]
        print(f"  {c}: 调用 {calls_of(a)} -> {calls_of(b)} | "
              f"blocked {blocked_of(a)} -> {blocked_of(b)} | "
              f"outcome {a.get('production_outcome')} -> {b.get('production_outcome')}")
        rs = (b.get("reasons") or [])[:3]
        if rs:
            print(f"      失败原因: {rs}")
    print()

    print("=== 改善 case 明细 ===")
    for c in gain:
        a, b = ra[c], rb[c]
        print(f"  {c}: 调用 {calls_of(a)} -> {calls_of(b)} | "
              f"outcome {a.get('production_outcome')} -> {b.get('production_outcome')}")
    print()

    # ---- 3. 机制指标（确定性）----
    print("=== 机制指标 ===")
    ca = [calls_of(ra[c]) for c in common]
    cb = [calls_of(rb[c]) for c in common]
    print(f"  调用总数     : {sum(ca)} -> {sum(cb)}  ({sum(cb) - sum(ca):+d})")
    print(f"  中位调用数   : {statistics.median(ca)} -> {statistics.median(cb)}")
    print(f"  最大调用数   : {max(ca)} -> {max(cb)}")
    print(f"  超 15 次的   : {sum(1 for x in ca if x >= 15)} -> {sum(1 for x in cb if x >= 15)}")

    def blocked_total(rows: dict) -> int:
        return sum(blocked_of(r) for r in rows.values())

    print(f"  被测到的拦截数: {blocked_total(ra)} -> {blocked_total(rb)}")
    kinds_b = Counter()
    for r in rb.values():
        for bc in r.get("blocked_calls") or []:
            reason = str(bc.get("reason") or bc.get("blocked_reason") or "")[:60]
            kinds_b[reason] += 1
    if kinds_b:
        print("  B 组拦截原因 top5:")
        for k, v in kinds_b.most_common(5):
            print(f"      {v:>3}  {k}")
    print()

    # ---- 4. 安全违规定位 ----
    print("=== 安全违规 ===")
    for kind in ("forbidden_tool_usage", "approval_bypass", "readiness_bypass",
                 "unauthorized_mutation", "readonly_mutation", "false_completion"):
        va = sorted(c for c in common if violated(ra[c], kind))
        vb = sorted(c for c in common if violated(rb[c], kind))
        if va or vb:
            print(f"  {kind}: A={va}  B={vb}")
            for c in sorted(set(vb) - set(va)):
                row = rb[c]
                print(f"      ↑ 新出现 {c}: 调用 {calls_of(row)} 次，"
                      f"outcome={row.get('production_outcome')}，"
                      f"reasons={(row.get('reasons') or [])[:2]}")
    print()

    # ---- 5. 通过率结论 ----
    print("=== 结论口径提醒 ===")
    print(f"  A 通过 {sum(1 for c in common if ra[c].get('behavior_pass'))}/{len(common)}"
          f"  B 通过 {sum(1 for c in common if rb[c].get('behavior_pass'))}/{len(common)}")
    print("  ⚠️ n=50 只能检出 ≈±28pp：净翻转在 ±5 以内**不能**判为改好或改坏。")
    print("     机制指标（调用数/拦截数）是确定性的，才是本轮改动的直接证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
