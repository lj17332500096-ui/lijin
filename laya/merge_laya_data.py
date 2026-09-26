"""合并 3 源 Laya 训练数据（现有 565 + 阶段1 162 + 阶段2 1506 = 2233）。

去重策略：
- 现有 565（train/val/test）= 规则引擎反向标注，有循环性
- 人工 1668（manual_labeled + manual_labeled_v2）= 打破循环性的主力
- 按 query 去重（人工标注优先，现有降权）
- 去重后 8:1:1 随机切分（固定 seed，可复现）

输出：
- merged_train.jsonl / merged_val.jsonl / merged_test.jsonl
- 每条保留 source 字段（existing/manual_v1/manual_v2），方便消融
"""
from __future__ import annotations
import json
import random
import sys
from pathlib import Path
from collections import Counter

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "data" / "laya_tool_intent"
SEED = 42


def load(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def get_query(c: dict) -> str:
    s = c.get("state", "")
    if isinstance(s, str):
        s = s.strip()
        # 去掉可能的外层引号（Laya state 是 JSON 字符串）
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
    return s


def label(c: dict) -> str:
    return json.loads(c["gold"])["needs_tool"]["label"]


def main() -> int:
    # 1. 加载 3 源
    existing = []
    for sp in ("train", "val", "test"):
        existing.extend(load(OUT_DIR / f"{sp}.jsonl"))
    manual_v1 = load(OUT_DIR / "manual_labeled.jsonl")
    manual_v2 = load(OUT_DIR / "manual_labeled_v2.jsonl")

    print(f"[merge] 现有 565: {len(existing)}")
    print(f"[merge] 人工 v1: {len(manual_v1)}")
    print(f"[merge] 人工 v2: {len(manual_v2)}")

    # 2. 打 source 标记
    for c in existing:
        c["source"] = "existing"
    for c in manual_v1:
        c["source"] = "manual_v1"
    for c in manual_v2:
        c["source"] = "manual_v2"

    # 3. 按 query 去重（人工优先，同 query 保留人工版）
    import hashlib
    all_items = manual_v1 + manual_v2 + existing
    seen: dict[str, dict] = {}
    for c in all_items:
        q = get_query(c)
        if not q:
            continue
        # 用 query 的 hash 做唯一 id（避免 3 源 id 冲突）
        c["id"] = f"{c['source']}::{hashlib.md5(q.encode('utf-8')).hexdigest()[:12]}"
        if q in seen:
            # 保留 source 优先级高的（manual_v2 > manual_v1 > existing）
            prio = {"manual_v2": 0, "manual_v1": 1, "existing": 2}
            if prio.get(c["source"], 9) < prio.get(seen[q]["source"], 9):
                seen[q] = c
        else:
            seen[q] = c

    deduped = list(seen.values())
    print(f"[merge] 去重后: {len(deduped)}（原 {len(all_items)}）")
    print(f"  source 分布: {dict(Counter(c['source'] for c in deduped))}")
    print(f"  label 分布: {dict(Counter(label(c) for c in deduped))}")

    # 4. 8:1:1 随机切分（固定 seed）
    random.seed(SEED)
    random.shuffle(deduped)
    n = len(deduped)
    n_train = int(n * 0.8)
    n_val = int(n * 0.1)
    train = deduped[:n_train]
    val = deduped[n_train:n_train + n_val]
    test = deduped[n_train + n_val:]

    print(f"[merge] 切分: train={len(train)} val={len(val)} test={len(test)}")
    print(f"  train source: {dict(Counter(c['source'] for c in train))}")
    print(f"  train label:  {dict(Counter(label(c) for c in train))}")

    # 5. 落盘
    for name, items in [("merged_train", train), ("merged_val", val), ("merged_test", test)]:
        p = OUT_DIR / f"{name}.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for c in items:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"[merge] 已写 {p.name} ({len(items)})")

    # 6. 校验：id 唯一、label 合法
    all_ids = [c.get("id", "") for c in deduped]
    print(f"[merge] id 唯一: {len(set(all_ids)) == len(all_ids)}")
    bad = [c for c in deduped if label(c) not in ("tool", "text")]
    print(f"[merge] label 非法: {len(bad)} 条")
    if bad:
        print(f"  样例: {bad[0].get('state', '')[:60]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
