"""FORGE_TOKEN_BUDGET 演练结果分析脚本。

用法（等 ③ 演练跑完 runs_eval_budget20k/ 目录后）：
    python -m benchmark.analyze_budget runs_eval_budget20k

输出：
- 哪些 case 触底（terminal_kind == "token_budget"）
- 各 case 的 input_tokens + output_tokens 合计 vs budget
- 归档 delivery/baselines/2026-09-19-token-budget-20k.json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

def main() -> int:
    if len(sys.argv) < 2:
        print(f"用法: python -m benchmark.analyze_budget <runs_dir>")
        return 1
    runs_dir = Path(sys.argv[1])
    if not runs_dir.exists():
        print(f"目录不存在: {runs_dir}")
        return 1

    all_rows: list[dict] = []
    for f in sorted(runs_dir.glob("*.json")):
        if f.name == "manifest.json":
            continue
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "id" in row:
            all_rows.append(row)

    if not all_rows:
        print(f"未找到 case 记录: {runs_dir}")
        return 1

    budget_hit: list[dict] = []
    others: list[dict] = []
    for row in all_rows:
        tk = row.get("terminal_kind", "")
        if tk == "token_budget":
            budget_hit.append(row)
        else:
            others.append(row)

    print(f"\n=== FORGE_TOKEN_BUDGET 演练结果（{runs_dir}）===")
    print(f"总 case 数: {len(all_rows)}")
    print(f"触底 (token_budget): {len(budget_hit)} / {len(all_rows)}")
    print(f"未触底: {len(others)}")
    if budget_hit:
        print("\n触底 case 列表:")
        for row in sorted(budget_hit, key=lambda r: r.get("id", "")):
            cid = row.get("id", "?")
            state = row.get("state", "?")
            dur = row.get("duration_s", "?")
            print(f"  {cid}: state={state}, t={dur}s")

    # 归档
    arch_dir = Path("delivery/baselines")
    arch_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    arch_path = arch_dir / f"{stamp}-token-budget-20k.json"
    arch = {
        "label": "FORGE_TOKEN_BUDGET=20000 演练",
        "runs_dir": str(runs_dir),
        "n_total": len(all_rows),
        "n_budget_hit": len(budget_hit),
        "budget_hit_ids": [r.get("id", "?") for r in sorted(budget_hit, key=lambda r: r.get("id", ""))],
        "state_summary": {},
        "rows": [
            {
                "id": r.get("id"),
                "state": r.get("state"),
                "terminal_kind": r.get("terminal_kind"),
                "duration_s": r.get("duration_s"),
                "model_turns": r.get("model_turns", 0),
                "tool_calls": len(r.get("tool_calls") or []),
            }
            for r in all_rows
        ],
    }
    for r in all_rows:
        st = r.get("state", "unknown")
        arch["state_summary"][st] = arch["state_summary"].get(st, 0) + 1
    arch_path.write_text(json.dumps(arch, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n归档: {arch_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
