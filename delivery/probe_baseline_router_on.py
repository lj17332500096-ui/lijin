"""拆解 router-on 基线的失败构成：区分「判据口径」与「agent 行为」。

2026-09-19 首次跑 router-on（真实配置，策略开）50-case 基线后的分析工具。

背景
----
此前所有 A/B 都是 `TOOL_ROUTER=off`（固定工具集），"真实配置下的表现"一直没有数字。
拿到 50.0% 后，第一件要回答的事是：**这 24 个 fail 该归因于谁**——
是评审判据把合法工具漏列了（改判据就能提升），还是 agent 行为本身不收敛（改判据无效）？

用法
----
    python delivery/probe_baseline_router_on.py                      # 默认读 runs_eval_baseline_router_on
    python delivery/probe_baseline_router_on.py <run_dir> <report.json>

结论（2026-09-19 那一轮）
------------------------
- 24 个 fail 里 **23 个含 tool_count 超限**，15 个同时触发 convergence 硬上限；
  只有 3 个涉及 tools_allowed/tools_forbidden，且其中 2 个（T038/T044）是**真实的越权执行**，
  另 1 个（T017）因 provider_error 归为 not_evaluable、**根本不进通过率分母**。
  → 该轮失败与"判据漏列工具"无关；改 T017/T020 的口径不会提升这一轮的数字。
- 真正的瓶颈是**尾部过度探索**：中位 10 次调用尚可，但最大 70 次，23/50 题超过各自上限。
- 死循环集中在两个"查询类"工具上：
    · `list_workspace_files` 反复调用（T038 19 次 / T014 15 次 / T013 11 次）—— **无重复调用护栏**
    · `web_search` 反复调用（T048 17 次 / T012 16 次）—— 有 `_too_repetitive` 护栏，
      但疑似因查询措辞微变而未被拦住
"""
from __future__ import annotations

import collections
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOOP_PRONE_TOOLS = ("list_workspace_files", "web_search", "read_workspace_file",
                    "search_documents", "index_workspace")


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_calls(run_dir: Path, case_id: str) -> list[str]:
    f = run_dir / f"{case_id}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text(encoding="utf-8"))
    out = []
    for c in (d.get("tool_calls") or []):
        out.append(c.get("name") if isinstance(c, dict) else str(c))
    return out


def main() -> int:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "runs_eval_baseline_router_on"
    report = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "report_baseline_router_on.json"
    if not run_dir.is_dir():
        print(f"找不到 run 目录：{run_dir}")
        return 2
    if not report.is_file():
        print(f"找不到报告：{report}\n先跑：python -m benchmark evaluate --runs {run_dir.name} --out {report.name}")
        return 2

    rep = load_report(report)
    rows = rep["rows"]
    summary = rep.get("summary", {})

    print("=" * 66)
    print(f"run = {run_dir.name}")
    print("=" * 66)
    for k in ("total", "valid", "invalid", "behavior_pass_rate", "pass", "fail",
              "unknown", "not_evaluable", "evaluability_rate",
              "pass_on_evaluable", "e2e_success_rate"):
        if k in summary:
            print(f"  {k:22s} {summary[k]}")
    print()

    # ── 1. 失败归因：判据口径 vs agent 行为 ─────────────────────
    fails = [r for r in rows if r.get("behavior_result") == "fail"]
    check_freq = collections.Counter()
    roster_fails, behavior_fails = [], []
    for r in fails:
        bad = [k for k, v in (r.get("checks") or {}).items() if v is False]
        for b in bad:
            check_freq[b] += 1
        if {"tools_allowed", "tools_forbidden"} & set(bad):
            roster_fails.append((r["case"], bad, r.get("reasons")))
        else:
            behavior_fails.append((r["case"], bad))

    print(f"=== 失败归因（共 {len(fails)} 个 fail）===")
    print("失败 check 频次:", dict(check_freq.most_common()))
    print(f"  涉及 tools_allowed/tools_forbidden 的: {len(roster_fails)} 个")
    for cid, bad, rs in roster_fails:
        print(f"    {cid} {bad}\n        {rs}")
    print(f"  纯行为类（不涉及工具名单）的: {len(behavior_fails)} 个")
    print(f"    {[c for c, _ in behavior_fails]}")
    print()

    # ── 2. 工具调用数分布：是不是尾部问题 ───────────────────────
    counts = [(r.get("tool_calls"), r["case"]) for r in rows
              if isinstance(r.get("tool_calls"), int)]
    vals = [n for n, _ in counts]
    print("=== 工具调用数分布 ===")
    print(f"  中位 {statistics.median(vals)}  均值 {statistics.mean(vals):.1f}  "
          f"最大 {max(vals)}  最小 {min(vals)}")
    print(f"  >12 次: {sum(1 for v in vals if v > 12)}/{len(vals)}")
    print(f"  ==0 次: {sum(1 for v in vals if v == 0)}/{len(vals)}")
    print(f"  尾部 Top8: {sorted(counts, reverse=True)[:8]}")
    print()

    # ── 3. 死循环画像：谁在被反复调用 ───────────────────────────
    print("=== 死循环画像（调用数 Top8 的题各调了什么）===")
    for n, cid in sorted(counts, reverse=True)[:8]:
        names = load_calls(run_dir, cid)
        if not names:
            continue
        cnt = collections.Counter(names)
        top = cnt.most_common(1)[0]
        warn = "  <<< 反复调同一工具" if top[1] >= 8 else ""
        print(f"  {cid} 共{len(names):3d}次  最高频={top[0]}×{top[1]}{warn}")
        print(f"       分布 {dict(cnt.most_common(6))}")
    print()
    print("提示：反复 CALL 同一「查询类」工具且无进展，是收敛机制该拦住的形态。")
    print("      `web_search` 有 _too_repetitive 护栏；`list_workspace_files` 目前**没有**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
