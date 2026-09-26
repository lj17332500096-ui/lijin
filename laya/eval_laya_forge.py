"""
FORGE Laya 微调模型验收评测（task #30 收尾）

用改造版 checkpoint / 微调后 checkpoint 对 154 条人工标注 test 跑二分类评测，
验证 >95% 准确率 + 95% CI 半宽（154 条 ≈ ±5.9pp）。

用法：
  # 用改造版原始 checkpoint（未微调，温度 0.3 基线）
  .venv/Scripts/python.exe eval_laya_forge.py \
      --checkpoint data/laya_forge/english \
      --test-file data/laya_tool_intent/test_final.jsonl

  # 用 FORGE 微调后 checkpoint（验证微调收益）
  .venv/Scripts/python.exe eval_laya_forge.py \
      --checkpoint data/laya_forge/forge_finetuned \
      --test-file data/laya_tool_intent/test_final.jsonl

  # 对照原始 HF checkpoint（温度 1.64/1.25/1.98，baseline 42.9%）
  .venv/Scripts/python.exe eval_laya_forge.py \
      --checkpoint C:/Users/Administrator/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/1c5edc17a7acd8701df6fc341c0d179f1c62c982 \
      --test-file data/laya_tool_intent/test_final.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_TEST = REPO / "data" / "laya_tool_intent" / "test_final.jsonl"
DEFAULT_CHECKPOINT = REPO / "data" / "laya_forge" / "english"


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
    """把 test case 拆成 Laya 二分类 decision（同 train 脚本逻辑）。"""
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
    """Wilson 95% CI（二项分布）。"""
    if n == 0:
        return 0.0, 0.0
    p_hat = successes / n
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    half = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def main() -> int:
    ap = argparse.ArgumentParser(description="FORGE Laya 微调模型验收评测")
    ap.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT),
                    help="Laya checkpoint 目录（改造版 / 微调后 / 原始 HF）")
    ap.add_argument("--test-file", default=str(DEFAULT_TEST))
    ap.add_argument("--out", default="", help="结果 JSON 输出路径（默认打印）")
    args = ap.parse_args()

    checkpoint = Path(args.checkpoint)
    test_file = Path(args.test_file)

    print(f"[eval] checkpoint: {checkpoint}")
    print(f"[eval] test file: {test_file}")
    if not checkpoint.exists():
        print(f"[eval] ✗ checkpoint 目录不存在: {checkpoint}")
        return 1
    if not test_file.exists():
        print(f"[eval] ✗ test 文件不存在（需先 review 154 条生成 test_final.jsonl）")
        return 1

    build_sequence, render_options, QTYPES = _import_laya_runtime()

    # 加载 checkpoint（用 laya Agent，走推理路径）
    import torch
    from laya.agent import Agent
    agent = Agent(str(checkpoint), device="cpu")
    print(f"[eval] 加载完成  temperature={agent.temperature}")

    # 逐条评测
    cases = load_cases(test_file)
    print(f"[eval] test cases: {len(cases)}")

    correct = 0
    total = 0
    wrong_samples = []
    confidences = []
    for c in cases:
        d = case_to_decision(c)
        if d is None:
            continue
        option, instruction, criteria = d
        # 用 Agent.system_one 跑一条
        qdef = {"needs_tool": {"type": "choice", "instructions": instruction, "criteria": criteria}}
        try:
            res = agent.system_one(instruction, qdef)
        except Exception as e:
            continue
        # 取答案
        ans = res.get("answers", {}).get("needs_tool", {})
        pred = ans.get("choice") or ans.get("label")
        prob = ans.get("probabilities", {})
        conf = ans.get("confidence")
        if conf is not None:
            confidences.append(float(conf))
        if pred == option:
            correct += 1
        else:
            wrong_samples.append({
                "instruction": instruction[:80],
                "gold": option,
                "pred": pred,
                "prob": prob,
            })
        total += 1

    if total == 0:
        print("[eval] ✗ 无有效评测样本")
        return 1

    acc = correct / total
    lo, hi = wilson_ci(correct, total)
    # 二分类准确率显著优于 50%（z 检验）
    z = (acc - 0.5) / math.sqrt(0.25 / total)
    print(f"\n[eval] ===== 结果 =====")
    print(f"  准确率: {acc:.4f} ({correct}/{total})")
    print(f"  95% CI (Wilson): [{lo:.4f}, {hi:.4f}]  半宽 ±{(hi-lo)/2*100:.1f}pp")
    print(f"  二分类显著性 (vs 50%): z={z:.2f} {'显著' if abs(z)>1.96 else '不显著'}")
    if confidences:
        print(f"  confidence 范围: [{min(confidences):.4f}, {max(confidences):.4f}]  均值={sum(confidences)/len(confidences):.4f}")
    print(f"  错误样本 (前 10):")
    for w in wrong_samples[:10]:
        print(f"    gold={w['gold']:4s} pred={w['pred']:4s}  {w['instruction']}")

    # 验收判据
    passed = acc > 0.95 and (hi - lo) / 2 * 100 < 10  # >95% 且 CI 半宽 <10pp
    print(f"\n[eval] 验收判据（>95% 且 CI 半宽 <10pp）: {'✅ 通过' if passed else '❌ 未通过'}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "checkpoint": str(checkpoint),
            "test_file": str(test_file),
            "correct": correct,
            "total": total,
            "accuracy": acc,
            "ci95": [lo, hi],
            "ci_halfwidth_pp": (hi - lo) / 2 * 100,
            "z_vs_50": z,
            "passed": passed,
            "wrong_samples": wrong_samples[:50],
            "confidence_stats": {
                "min": min(confidences) if confidences else None,
                "max": max(confidences) if confidences else None,
                "mean": sum(confidences) / len(confidences) if confidences else None,
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[eval] 结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
