"""消融对比：高对抗 vs 低对抗 两个 route 头在同一 test 集上的表现。

用法：
  # 先分别训两个 checkpoint（高对抗 / 低对抗），再用本脚本对比
  .venv/Scripts/python.exe compare_ablation.py \
      --ckpt-high-adv data/laya_forge/forge_finetuned_highadv \
      --ckpt-low-adv data/laya_forge/forge_finetuned \
      --test-file data/laya_tool_intent/merged_test.jsonl \
      --out data/laya_tool_intent/ablation_compare.json

输出：
- 两个 checkpoint 的准确率 / P/R/F1 / 混淆矩阵
- 按对抗占比分组（test 集里 adversarial 子集 vs 常规子集）的分别表现
- 结论：低对抗是否改善了常规边界（FP 减少），代价是漏了对抗鲁棒性
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_cases(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _j(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return x
    return x


def case_to_decision(c: dict):
    q = _j(c.get("questions"))
    g = _j(c.get("gold"))
    if not isinstance(q, dict) or "needs_tool" not in q:
        return None
    qt = q["needs_tool"]
    instruction = c.get("state") or qt.get("instructions") or ""
    instruction = _j(instruction) if isinstance(instruction, str) and instruction[:1] in '"[' else instruction
    criteria = qt.get("criteria", {})
    if not isinstance(criteria, dict) or "text" not in criteria or "tool" not in criteria:
        return None
    gq = g.get("needs_tool") if isinstance(g, dict) else {}
    option = (gq or {}).get("label")
    if option not in ("text", "tool"):
        return None
    return option, instruction, criteria, c.get("source", "")


def eval_one(ckpt: Path, cases: list[dict]) -> dict:
    from laya.agent import Agent
    agent = Agent(str(ckpt), device="cpu")
    tp = fp = tn = fn = 0
    total = correct = 0
    wrong = []
    # 按 source 分组（test 集里也保留了 source 字段）
    group_stats: dict[str, dict] = {}
    for c in cases:
        d = case_to_decision(c)
        if d is None:
            continue
        option, instruction, criteria, source = d
        qdef = {"needs_tool": {"type": "choice", "instructions": instruction, "criteria": criteria}}
        try:
            res = agent.system_one(instruction, qdef)
        except Exception:
            continue
        ans = res.get("answers", {}).get("needs_tool", {})
        pred = ans.get("choice") or ans.get("label")
        total += 1
        if pred == option:
            correct += 1
        else:
            wrong.append({"instruction": instruction[:60], "gold": option, "pred": pred})
        gold_tool = (option == "tool")
        pred_tool = (pred == "tool")
        if gold_tool and pred_tool:
            tp += 1
        elif gold_tool and not pred_tool:
            fn += 1
        elif not gold_tool and pred_tool:
            fp += 1
        else:
            tn += 1
        # 按 source 分组
        g = group_stats.setdefault(source, {"total": 0, "correct": 0})
        g["total"] += 1
        if pred == option:
            g["correct"] += 1
    acc = correct / total if total else 0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "checkpoint": str(ckpt),
        "total": total,
        "accuracy": acc,
        "precision_tool": precision,
        "recall_tool": recall,
        "f1_tool": f1,
        "confusion": {"TP": tp, "FP": fp, "TN": tn, "FN": fn},
        "group_by_source": group_stats,
        "wrong_samples": wrong[:30],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt-high-adv", default="", help="高对抗占比 checkpoint")
    ap.add_argument("--ckpt-low-adv", default="", help="低对抗占比 checkpoint")
    ap.add_argument("--test-file", default=str(REPO / "data" / "laya_tool_intent" / "merged_test.jsonl"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    ck_high = Path(args.ckpt_high_adv) if args.ckpt_high_adv else None
    ck_low = Path(args.ckpt_low_adv) if args.ckpt_low_adv else None
    test_file = Path(args.test_file)
    cases = load_cases(test_file)
    print(f"[ablation] test cases: {len(cases)}")

    results = {}
    if ck_high:
        print(f"[ablation] 评测 {ck_high}")
        results["high_adv"] = eval_one(ck_high, cases)
    if ck_low:
        print(f"[ablation] 评测 {ck_low}")
        results["low_adv"] = eval_one(ck_low, cases)

    # 汇总对比表
    print("\n[ablation] ===== 对比表 =====")
    header = f"{'指标':<20s}"
    for k in results:
        header += f" {k:>12s}"
    print(header)
    for metric in ("accuracy", "precision_tool", "recall_tool", "f1_tool"):
        line = f"{metric:<20s}"
        for k in results:
            line += f" {results[k][metric]:>12.4f}"
        print(line)
    print(f"{'混淆矩阵':<20s}")
    for k in results:
        cm = results[k]["confusion"]
        print(f"  {k}: TP={cm['TP']} FP={cm['FP']} TN={cm['TN']} FN={cm['FN']}")
    print(f"{'按 source 分组':<20s}")
    for k in results:
        for src, st in results[k]["group_by_source"].items():
            print(f"  {k}/{src}: {st['correct']}/{st['total']}")

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[ablation] 结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
