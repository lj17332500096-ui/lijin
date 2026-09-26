"""固化「未微调 english 当 route 头」的保底基线。

背景（2026-09-25 P5 诊断）：
- 未微调 english checkpoint（温度 0.3）在常规子集无门槛评测 = 68.13%（tool F1 0.788）
- 但接入层 laya_router.screen() 是「高置信度才短路、否则回落」设计，0.85 阈值下
  真实生效子集准确率远低于 68%（大量回落仍走循环性规则引擎）。
- 本脚本固化两件事，供后续任何对比/消融复现：
  1. 无门槛全量评测数字（68% 基线）
  2. 当前阈值下的「生效量 / 生效准确率 / 回落量」真实曲线

产出：data/laya_tool_intent/eng_baseline_fallback.json
      （含 68% 基线 + 各阈值生效曲线，是 P5 路线对比的「真基线」）

用法：
  .venv/Scripts/python.exe fix_eng_baseline.py [--test-file ...] [--out ...]

注意：纯离线 CPU 推理，不依赖 5 epoch 训练。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_TEST = REPO / "data" / "laya_tool_intent" / "test_normal_subset.jsonl"
DEFAULT_CKPT = REPO / "data" / "laya_forge" / "english"
DEFAULT_OUT = REPO / "data" / "laya_tool_intent" / "eng_baseline_fallback.json"

THRESHOLDS = (0.95, 0.90, 0.85, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.0)


def _j(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return x
    return x


def main() -> int:
    ap = argparse.ArgumentParser(description="固化未微调 english 保底基线")
    ap.add_argument("--test-file", default=str(DEFAULT_TEST))
    ap.add_argument("--checkpoint", default=str(DEFAULT_CKPT))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    from laya.agent import Agent

    agent = Agent(args.checkpoint, device="cpu")
    print(f"[fix] checkpoint: {args.checkpoint}")
    print(f"[fix] temperature: {agent.temperature}")

    cases = [json.loads(l) for l in open(args.test_file, encoding="utf-8") if l.strip()]
    print(f"[fix] test cases: {len(cases)}")

    results: dict[str, tuple[str, str, float]] = {}
    total = correct = 0
    for c in cases:
        q = _j(c.get("state")) or ""
        g = _j(c.get("gold"))
        gold = (g or {}).get("needs_tool", {}).get("label") if isinstance(g, dict) else None
        if gold not in ("text", "tool"):
            continue
        qdef = {"needs_tool": {"type": "choice", "instructions": q,
                               "criteria": {"text": "answer directly without tools",
                                             "tool": "requires tool call"}}}
        try:
            r = agent.system_one(q, qdef)
        except Exception:
            continue
        ans = (r.get("answers") or {}).get("needs_tool", {})
        choice = ans.get("choice") or ans.get("label")
        conf = ans.get("confidence")
        if choice not in ("text", "tool") or conf is None:
            continue
        pred = "text" if choice == "text" else "tool"
        conf = float(conf)
        results[q] = (gold, pred, conf)
        total += 1
        if pred == gold:
            correct += 1

    acc = correct / total if total else 0.0
    print(f"\n[fix] ===== 无门槛全量评测（保底基线）=====")
    print(f"  准确率: {acc:.4f} ({correct}/{total})")

    # 阈值曲线
    curve = []
    for thr in THRESHOLDS:
        hit = [(g, p) for (g, p, cf) in results.values() if cf >= thr]
        fb = [1 for (g, p, cf) in results.values() if cf < thr]
        correct_hit = sum(1 for g, p in hit if g == p)
        curve.append({
            "threshold": thr,
            "effective": len(hit),
            "effective_acc": correct_hit / len(hit) if hit else 0.0,
            "fallback": len(fb),
        })
        print(f"  阈值 {thr:4.2f}  生效 {len(hit):4d}  生效准确率 "
              f"{(correct_hit/len(hit)*100 if hit else 0):5.1f}%  回落 {len(fb)}")

    out = {
        "checkpoint": str(args.checkpoint),
        "test_file": str(args.test_file),
        "temperature": agent.temperature,
        "total": total,
        "unbiased_accuracy": acc,  # 无门槛全量（68% 这个数字）
        "unbiased_correct": correct,
        "threshold_curve": curve,
        "note": ("无门槛评测 = 每个 query 都强制给判定，68% 是「评测数字」；"
                 "接入层 screen() 用阈值曲线里的 effective_acc，真实生效子集。"
                 "回落部分仍走循环性规则引擎（select_tool_names）。"),
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[fix] 基线已固化: {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
