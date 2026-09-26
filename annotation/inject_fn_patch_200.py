"""把 200 条 ep3 FN 对抗样本（ep3_fn_patch_200_annotated.csv）注入 5000 扁平源。

注入规则：
- 读 F:/Byong-hermes/Byong-hermes/ep3_fn_patch_200_annotated.csv（200 条）
- 每条 {query, final_label, tool} → 扁平源 {query, label, tool}
- 去重：若 query 已存在于 5000 源，跳过（避免重复计数）
- 输出：tool_routing_5000_train.jsonl（追加 200 条后 5200 行）

注入后必须重跑 adapt_5000_to_laya.py + 重训，保持同一把尺子。

用法：
  .venv/Scripts/python.exe inject_fn_patch_200.py            # 注入
  .venv/Scripts/python.exe inject_fn_patch_200.py --dry-run  # 只统计
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLAT = REPO / "data" / "laya_tool_intent" / "tool_routing_5000_train.jsonl"
CSV = Path(r"F:/Byong-hermes/Byong-hermes/ep3_fn_patch_200_annotated.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = list(csv.DictReader(CSV.open(encoding="utf-8-sig")))
    print(f"[inject] 载入 CSV: {len(rows)} 条")

    existing = set()
    flat_rows = []
    for line in FLAT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        flat_rows.append(c)
        existing.add(c.get("query", "").strip())

    injected, dup = 0, 0
    for r in rows:
        q = r.get("query", "").strip()
        lab = r.get("final_label").strip()
        tool = r.get("tool", "-").strip() or "-"
        if not q or lab not in ("text", "tool"):
            continue
        if q in existing:
            dup += 1
            continue
        flat_rows.append({"query": q, "label": lab, "tool": tool})
        injected += 1

    print(f"[inject] 注入 {injected} 条 | 去重跳过 {dup} 条 | 总量 {len(flat_rows)}")

    if not args.dry_run:
        with FLAT.open("w", encoding="utf-8") as f:
            for c in flat_rows:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"[inject] 已写 {FLAT.name}（{len(flat_rows)} 行）")
        print("下一步：")
        print(f"  .venv/Scripts/python.exe adapt_5000_to_laya.py")
        print(f"  .venv/Scripts/python.exe train_laya_forge.py --train --device xpu --epochs 3 ...")


if __name__ == "__main__":
    main()
