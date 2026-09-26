"""
FORGE Laya 独立 test · 人工 review 回填（task #28）

流程：
  1. 你 review data/laya_tool_intent/test_annotation_review.md，
     把「你的判断」列（`_tool` / `_text`）填成 `tool` / `text`。
  2. 运行本脚本：从 .md 里解析你填的判断，按 query 对应回填到
     test_annotation_labels.jsonl 的 label_human 字段。
  3. 自动接着跑 validate_test_annotation.py 生成 test_final.jsonl（人工真值）。

规则：
  - 未填的（仍是 `_xxx` 下划线默认值）视为「沿用预标注」，按预标注保留。
  - 填了但 query 在 labels 里找不到对应 → 报告。
  - 统计 tool/text 最终分布 + 与预标注一致率，给你人工标签的真实比例。
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MD = os.path.join(REPO, "data", "laya_tool_intent", "test_annotation_review.md")
DEFAULT_LABELS = os.path.join(REPO, "data", "laya_tool_intent", "test_annotation_labels.jsonl")
DEFAULT_FINAL = os.path.join(REPO, "data", "laya_tool_intent", "test_final.jsonl")


def load_labels(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def parse_review_md(path: str) -> dict[str, str | None]:
    """从 md 表格里取 query → 你的判断。

    md 行格式：| i | query | _tool 或 tool 或 text | 预标注 |
    第四列（你的判断）是 `tool` / `text`（无下划线=已填），`_tool`/`_text`（有下划线=未填，沿用预标注）。
    返回 {query_text: your_label_or_None}
    """
    out: dict[str, str | None] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            # 跳过表头与分隔行
            if cells[0] in ("#", "") or set(cells[0]) <= set("-: "):
                continue
            query = cells[1]
            your_cell = cells[2]
            # 去下划线判断是否人工填过
            filled = your_cell.lstrip("_")
            lab = None
            if filled in ("tool", "text"):
                lab = filled  # 无论是否带下划线，都视为该值；下划线=沿用预标注=恰好也是这个值
            out[query] = lab
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="人工 review 回填 → 最终独立 test 标签")
    ap.add_argument("--md", default=DEFAULT_MD)
    ap.add_argument("--labels", default=DEFAULT_LABELS)
    ap.add_argument("--final", default=DEFAULT_FINAL)
    args = ap.parse_args()

    labels = load_labels(args.labels)
    review = parse_review_md(args.md)

    # query 归一化索引
    def norm(s):
        return re.sub(r"\s+", " ", s or "").strip()

    label_by_q = {norm(r["text"]): r for r in labels}

    filled = 0
    unmatched = 0
    for q, lab in review.items():
        key = norm(q)
        r = label_by_q.get(key)
        if r is None:
            unmatched += 1
            continue
        if lab in ("tool", "text"):
            # 无论带不带下划线，人工值即 lab（_tool→tool 视为沿用预标注=tool）
            r["label_human"] = lab
            filled += 1

    # 写回 labels（保留其余字段）
    with open(args.labels, "w", encoding="utf-8") as f:
        for r in labels:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 统计
    human = Counter(r.get("label_human", "?") for r in labels)
    # 一致率：人工 vs 预标注
    agree = sum(
        1 for r in labels
        if r.get("label_human") == (("tool" if r.get("pred_tools") else "text"))
    )
    total = len(labels)
    agree_rate = agree / total if total else 0

    print(f"回填完成：{filled} 条已写回，{unmatched} 条 query 未匹配")
    print(f"最终人工标签分布：{dict(human)}")
    print(f"与预标注一致率：{agree_rate*100:.1f}%（纠正率 {100*(1-agree_rate):.1f}%）")

    # 接着生成最终 test_final.jsonl
    known_fps = set()
    for split in ("train", "val", "test"):
        p = os.path.join(REPO, "data", "laya_tool_intent", f"{split}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as ff:
            for line in ff:
                line = line.strip()
                if line:
                    try:
                        known_fps.add(json.loads(line).get("fingerprint", ""))
                    except json.JSONDecodeError:
                        pass
    recs = [r for r in labels if r.get("fingerprint") not in known_fps]

    with open(args.final, "w", encoding="utf-8") as fo:
        for r in recs:
            lab = r.get("label_human")
            if lab not in ("tool", "text"):
                continue
            o = "tool" if lab == "tool" else "text"
            fo.write(json.dumps({
                "fingerprint": r.get("fingerprint", ""),
                "text": r.get("text", ""),
                "label_human": lab,
                "option": o,
                "question": {"id": "tool_intent", "type": "choice",
                              "instructions": r.get("text", "")},
            }, ensure_ascii=False) + "\n")

    print(f"\n✅ 最终独立 test 标签: {args.final}（{len(recs)} 条，人工真值）")
    print("下一步：train_laya_forge.py 训练后，用此 test_final.jsonl 评测；")
    print(f"         n={len(recs)}，95% CI 半宽 ≈ ±{2.58*(0.5/( (len(recs)+1))**0.5)*100:.1f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
