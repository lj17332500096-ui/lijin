"""评测试训的 FORGE route 头：用 merged_test.jsonl（人工标注 test 集）。

与 eval_laya_forge.py 的区别：
- 评测文件用新的 merged_test.jsonl（224 条，来自人工标注，打破循环性）
- 支持对比 3 个 checkpoint：原始 english / 微调 forge_finetuned / 原始 HF
- 输出完整 precision/recall/混淆矩阵，不只是准确率

用法：
  .venv/Scripts/python.exe eval_route_head.py \
      --checkpoint data/laya_forge/forge_finetuned \
      --test-file data/laya_tool_intent/merged_test.jsonl \
      --out data/laya_tool_intent/eval_merged_test.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_TEST = REPO / "data" / "laya_tool_intent" / "merged_test.jsonl"
DEFAULT_CHECKPOINT = REPO / "data" / "laya_forge" / "forge_finetuned"


def _import_laya_runtime():
    try:
        from laya.common import build_sequence, render_options, QTYPES
        return build_sequence, render_options, QTYPES
    except Exception:
        sys.path.insert(0, str(REPO / "laya" / "_laya_inspect" / "laya-main"))
        from laya.common import build_sequence, render_options, QTYPES
        return build_sequence, render_options, QTYPES


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
    return option, instruction, criteria


def wilson_ci(successes: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p_hat = successes / n
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    half = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def main() -> int:
    ap = argparse.ArgumentParser(description="评测试训 route 头（merged_test）")
    ap.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    ap.add_argument("--test-file", default=str(DEFAULT_TEST))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    checkpoint = Path(args.checkpoint)
    test_file = Path(args.test_file)

    print(f"[eval] checkpoint: {checkpoint}")
    print(f"[eval] test file: {test_file}")
    if not checkpoint.exists():
        print(f"[eval] ✗ checkpoint 目录不存在: {checkpoint}")
        return 1
    if not test_file.exists():
        print(f"[eval] ✗ test 文件不存在: {test_file}")
        return 1

    # 加载模型
    import torch
    from laya.agent import Agent
    agent = Agent(str(checkpoint), device="cpu")
    print(f"[eval] 加载完成  temperature={agent.temperature}")

    cases = load_cases(test_file)
    print(f"[eval] test cases: {len(cases)}")

    # 逐条评测
    tp = fp = tn = fn = 0  # tool 为正类
    total = 0
    correct = 0
    wrong_samples = []
    confidences = []
    for c in cases:
        d = case_to_decision(c)
        if d is None:
            continue
        option, instruction, criteria = d
        qdef = {"needs_tool": {"type": "choice", "instructions": instruction, "criteria": criteria}}
        try:
            res = agent.system_one(instruction, qdef)
        except Exception:
            continue
        ans = res.get("answers", {}).get("needs_tool", {})
        pred = ans.get("choice") or ans.get("label")
        conf = ans.get("confidence")
        if conf is not None:
            confidences.append(float(conf))
        if pred == option:
            correct += 1
        else:
            wrong_samples.append({"instruction": instruction[:80], "gold": option, "pred": pred})
        # 混淆矩阵（tool 为正类）
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
        total += 1

    if total == 0:
        print("[eval] ✗ 无有效评测样本")
        return 1

    acc = correct / total
    lo, hi = wilson_ci(correct, total)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    print(f"\n[eval] ===== 结果 =====")
    print(f"  准确率: {acc:.4f} ({correct}/{total})")
    print(f"  95% CI (Wilson): [{lo:.4f}, {hi:.4f}]  半宽 ±{(hi-lo)/2*100:.1f}pp")
    print(f"  tool 类: precision={precision:.4f}  recall={recall:.4f}  F1={f1:.4f}")
    print(f"  混淆矩阵: TP={tp} FP={fp} TN={tn} FN={fn}")
    if confidences:
        print(f"  confidence: min={min(confidences):.4f} max={max(confidences):.4f} mean={sum(confidences)/len(confidences):.4f}")
    print(f"  错误样本 (前 10):")
    for w in wrong_samples[:10]:
        print(f"    gold={w['gold']:4s} pred={w['pred']:4s}  {w['instruction']}")

    # 对比基线（42.9% 是原始 HF checkpoint 在 154 条上的）
    base = 0.429
    delta = acc - base
    print(f"\n[eval] vs 42.9% 基线: {'↑' if delta >= 0 else '↓'} {abs(delta)*100:.1f}pp")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "checkpoint": str(checkpoint),
            "test_file": str(test_file),
            "total": total,
            "correct": correct,
            "accuracy": acc,
            "ci95": [lo, hi],
            "ci_halfwidth_pp": (hi - lo) / 2 * 100,
            "tool_precision": precision,
            "tool_recall": recall,
            "tool_f1": f1,
            "confusion": {"TP": tp, "FP": fp, "TN": tn, "FN": fn},
            "confidence_stats": {
                "min": min(confidences) if confidences else None,
                "max": max(confidences) if confidences else None,
                "mean": sum(confidences) / len(confidences) if confidences else None,
            },
            "delta_vs_429pp": delta,
            "wrong_samples": wrong_samples[:50],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[eval] 结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
