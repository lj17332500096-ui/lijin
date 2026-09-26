"""
FORGE Laya 独立 test · 人工 review 后生成最终标签 + 统计

流程：
  1. 人工 review build_test_annotation.py 产出的 test_annotation_review.md，
     在 test_annotation_labels.jsonl 里把 label_human 改成 tool / text。
  2. 运行本脚本：
     - 校验每条 label_human 合法（tool/text）、fingerprint 与 565 不重叠
     - 统计人工标签分布（tool vs text）
     - 与预标注对比，统计人工纠正率（衡量 AI 预标注质量）
     - 产出 data/laya_tool_intent/test_final.jsonl（Laya 评测格式）
       —— 这是真正用于验收独立 test 的标签集（人工真值，非 FORGE 规则循环标注）
  3. 之后训练/评测用 test_final.jsonl 当独立 test（200 条，CI 半宽 ±4.5pp，可验收 >95%）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def known_fingerprints() -> set[str]:
    known: set[str] = set()
    for split in ("train", "val", "test"):
        p = os.path.join(REPO, "data", "laya_tool_intent", f"{split}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    q = json.loads(line)
                    known.add(q.get("fingerprint", ""))
                except json.JSONDecodeError:
                    continue
    return {k for k in known if k}


def main() -> int:
    ap = argparse.ArgumentParser(description="人工 review 后生成独立 test 最终标签")
    ap.add_argument("--labels", default=os.path.join(REPO, "data", "laya_tool_intent", "test_annotation_labels.jsonl"))
    ap.add_argument("--out", default=os.path.join(REPO, "data", "laya_tool_intent", "test_final.jsonl"))
    args = ap.parse_args()

    if not os.path.exists(args.labels):
        print(f"✗ 找不到标签文件 {args.labels}，请先 review build_test_annotation.py 的输出")
        return 1

    records = []
    bad = 0
    with open(args.labels, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            lab = (r.get("label_human") or "").strip().lower()
            if lab not in ("tool", "text"):
                print(f"  ✗ 第{len(records)+1}条 label_human 非法: {r.get('label_human')!r}")
                bad += 1
                continue
            records.append(r)

    if bad:
        print(f"⚠️ {bad} 条 label_human 非法，请修正后重跑")
        return 1

    # 与 565 防重叠
    known = known_fingerprints()
    overlap = [r for r in records if r.get("fingerprint") in known]
    if overlap:
        print(f"⚠️ {len(overlap)} 条与 565 重叠（fingerprint 撞），剔除")
        records = [r for r in records if r.get("fingerprint") not in known]

    # 统计
    human = Counter(r["label_human"] for r in records)
    # 预标注 vs 人工
    agree = 0
    for r in records:
        pred = (r.get("pred_tools") and "tool") or "text"
        # 预标注 label 需重建：pred_tools 非空=tool
        if pred == r["label_human"]:
            agree += 1
    agree_rate = agree / len(records) if records else 0

    print(f"最终独立 test 集：{len(records)} 条")
    print(f"  tool: {human.get('tool',0)} / text: {human.get('text',0)}")
    print(f"  人工纠正率（与 AI 预标注不一致）：{100*(1-agree_rate):.1f}%")

    # 产出 Laya 评测格式
    tool_map = "tool"
    text_map = "text"
    with open(args.out, "w", encoding="utf-8") as fo:
        for r in records:
            o = "tool" if r["label_human"] == "tool" else "text"
            fo.write(json.dumps({
                "fingerprint": r.get("fingerprint", ""),
                "text": r.get("text", ""),
                "label_human": r["label_human"],
                "option": o,
                "question": {"id": "tool_intent", "type": "choice",
                             "instructions": r.get("text", "")},
            }, ensure_ascii=False) + "\n")

    print(f"\n✅ 已写入 {args.out}（{len(records)} 条独立 test 标签，人工真值）")
    print("下一步：用 train_laya_forge.py 训练后，对 test_final.jsonl 评测，")
    print("         准确率 >95% 且 CI 半宽 ±4.5pp 通过 → Laya 真超越 FORGE 规则")
    return 0


if __name__ == "__main__":
    sys.exit(main())
