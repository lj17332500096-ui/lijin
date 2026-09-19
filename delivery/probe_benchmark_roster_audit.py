"""重放评测判据：检查 benchmark 自己的工具名册是否漏项，并量化对通过率的影响。

动机（2026-09-19）：
  修 `code_loop_tool` 错名时发现同一错名还有 5 处拷贝（benchmark/ 下 5 个文件）。
  由此怀疑 benchmark 侧的工具名册也在漂移，而 `evaluator.allows_tool()` 的逻辑是
  「执行了不在 tools_allowed 里的工具 → checks['tools_allowed']=False → 直接判 fail」。
  也就是说：**名册漏一个工具 → 合法调用被记成失败**，通过率被人为压低。

本探针不猜，直接：
  1. 用 harness 自己的 `observation_from_raw` + `evaluate_case` 重放历史 run，
     先复现已知通过率（证明重放忠实）；
  2. 列出「被执行但在 allowed 集之外」的工具直方图 —— 这就是名册漏项清单；
  3. 反事实：把漏项补进名册后重算通过率，量化影响。

用法:
  ./.venv/Scripts/python.exe delivery/probe_benchmark_roster_audit.py
  ./.venv/Scripts/python.exe delivery/probe_benchmark_roster_audit.py --dirs runs_eval_A_baseline
"""

from __future__ import annotations

import argparse
import collections
import glob
import importlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_raw(run_dir: str) -> dict[str, dict]:
    out = {}
    for f in sorted(glob.glob(f"{run_dir}/T*.json")):
        cid = pathlib.Path(f).stem
        out[cid] = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    return out


def replay(run_dir: str, cases: dict) -> dict:
    """用 harness 自己的判据重放一遍，返回 {case_id: CaseResult}。"""
    from benchmark.evaluator import evaluate_case, observation_from_raw

    res = {}
    for cid, raw in load_raw(run_dir).items():
        case = cases.get(cid)
        if case is None:
            continue
        try:
            obs = observation_from_raw(raw)
            obs.case_id = cid
            res[cid] = evaluate_case(case.expected, obs)
        except Exception as exc:  # noqa: BLE001
            res[cid] = exc
    return res


def executed_of(raw: dict) -> set[str]:
    return {n for n, c in (raw.get("_exec") or {}).items() if c}


def summarize(res: dict) -> tuple[int, int, int]:
    p = sum(1 for r in res.values() if getattr(r, "behavior_result", "") == "pass")
    f = sum(1 for r in res.values() if getattr(r, "behavior_result", "") == "fail")
    u = sum(1 for r in res.values() if getattr(r, "behavior_result", "") == "unknown")
    ne = sum(1 for r in res.values() if getattr(r, "behavior_result", "") == "not_evaluable")
    return p, f, u + ne


def main() -> int:
    ap = argparse.ArgumentParser(prog="probe_benchmark_roster_audit")
    ap.add_argument("--dirs", nargs="*", default=None)
    args = ap.parse_args()

    dirs = args.dirs or sorted(
        str(p) for p in ROOT.glob("runs_eval*") if p.is_dir()
    )
    dirs = [d for d in dirs if glob.glob(f"{d}/T*.json")]
    if not dirs:
        print("没找到 runs_eval* 历史 run，无法重放")
        return 1

    from benchmark.cases import BENCHMARK_CASES
    cases = {c.id: c for c in BENCHMARK_CASES}

    print("=" * 92)
    print("benchmark 工具名册审计（重放 harness 自身判据）")
    print("=" * 92)
    print(f"case 数 {len(cases)}   重放目录 {len(dirs)}")

    # ---- 1) 忠实性校验 ----
    print("\n【1】重放忠实性（应与历史报告一致）")
    print(f"  {'目录':<26}{'pass':>6}{'fail':>6}{'unk/ne':>9}{'通过率':>9}")
    print("  " + "-" * 58)
    replayed = {}
    for d in dirs:
        res = replay(d, cases)
        replayed[d] = res
        p, f, u = summarize(res)
        tot = p + f + u
        print(f"  {pathlib.Path(d).name:<26}{p:>6}{f:>6}{u:>9}"
              f"{p / tot * 100 if tot else 0:>8.1f}%")

    # ---- 2) 名册漏项直方图 ----
    print("\n【2】被执行、但不在该 case `tools_allowed` 内的工具（名册漏项）")
    offenders: collections.Counter = collections.Counter()
    per_case: dict[str, dict[str, list[str]]] = {}
    for d in dirs:
        for cid, raw in load_raw(d).items():
            case = cases.get(cid)
            if case is None or case.expected.tools_allowed is None:
                continue
            bad = sorted(n for n in executed_of(raw)
                         if n not in case.expected.tools_allowed)
            if not bad:
                continue
            for n in bad:
                offenders[n] += 1
            per_case.setdefault(cid, {})[pathlib.Path(d).name] = bad
    if not offenders:
        print("  无")
    else:
        print(f"  {'工具名':<26}{'被误判次数':>10}   是否在注册表")
        print("  " + "-" * 56)
        try:
            import agent
            from runtime.registry import discover_from_agent
            reg = {b.spec.name for b in discover_from_agent(agent.assistant_agent).all()}
        except Exception:  # noqa: BLE001
            reg = set()
        for n, c in offenders.most_common():
            mark = "✓ 已注册（名册漏项）" if n in reg else "✗ 非注册工具（伪工具名）"
            print(f"  {n:<26}{c:>10}   {mark}")
        print()
        print("  按 case 看（哪些 case 因此被判 fail）：")
        for cid in sorted(per_case):
            tot = sum(len(v) for v in per_case[cid].values())
            print(f"    {cid}  命中 {tot} 次  例：{per_case[cid]}")

    # ---- 3) 反事实：把漏项补进名册 ----
    print("\n【3】反事实：把漏项补进 benchmark 名册后重算通过率")
    import benchmark.evaluator as ev
    from benchmark import cases as cases_mod

    patch = {"run_tests"}          # 唯一被证实的注册工具漏项
    orig_run = ev.RUN_TOOLS
    ev.RUN_TOOLS = frozenset(set(orig_run) | patch)
    importlib.reload(cases_mod)
    cases2 = {c.id: c for c in cases_mod.BENCHMARK_CASES}
    print(f"  补丁：RUN_TOOLS ∪ {sorted(patch)}   "
          f"(原 {len(orig_run)} → {len(ev.RUN_TOOLS)})")
    print()
    print(f"  {'目录':<26}{'原通过率':>10}{'补后':>9}{'Δ':>8}   翻转的 case")
    print("  " + "-" * 80)
    for d in dirs:
        r1 = {cid: r for cid, r in replayed[d].items() if cid in cases2}
        r2 = replay(d, cases2)
        p1, f1, u1 = summarize(r1)
        p2, f2, u2 = summarize(r2)
        t1, t2 = p1 + f1 + u1, p2 + f2 + u2
        flipped = sorted(cid for cid in r2
                         if getattr(replayed[d].get(cid), "behavior_result", "") != "pass"
                         and getattr(r2[cid], "behavior_result", "") == "pass")
        rate1 = p1 / t1 * 100 if t1 else 0
        rate2 = p2 / t2 * 100 if t2 else 0
        print(f"  {pathlib.Path(d).name:<26}{rate1:>9.1f}%{rate2:>8.1f}%"
              f"{rate2 - rate1:>+7.1f}pp   {flipped if flipped else '—'}")

    ev.RUN_TOOLS = orig_run

    print("\n" + "=" * 92)
    print("结论")
    print("=" * 92)
    print("  ⚠️ 若某工具「已注册、且被执行过、但不在 allowed 名册」→ 该 case 被硬判 fail。")
    print("     这类 fail 是**度量假象**，不是 Agent 行为问题；两个臂受影响方向相同，")
    print("     所以 A/B 的**差值**大体不受影响，但**绝对通过率被系统性低估**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
