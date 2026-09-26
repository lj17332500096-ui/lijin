"""走 GPU（llama.cpp 8099）对 Laya test 集做基线评测。

与 eval_route_head.py（CPU/PyTorch）的区别：
- 走 8099 的 /embedding 端点（Arc A770 SYCL），不加载 PyTorch checkpoint
- 判定用「embedding 可达性 + 关键词双信号」（同 laya_llama_bridge），输出二分类
- 8099 的 GGUF 是「微调前通用 encoder」，**没有训练出的判别头**，所以这是
  "微调前基线"近似（不是刚训出的 checkpoint 真实准确率），仅作对比锚点。

用法：
  .venv/Scripts/python.exe eval_route_head_gpu.py \
      --test-file data/laya_tool_intent/laya_5000_test.jsonl \
      --out data/laya_tool_intent/eval_gpu_baseline_5000.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_TEST = REPO / "data" / "laya_tool_intent" / "laya_5000_test.jsonl"
DEFAULT_OUT = REPO / "data" / "laya_tool_intent" / "eval_gpu_baseline_5000.json"


def _import_bridge():
    sys.path.insert(0, str(REPO))
    from runtime.laya_llama_bridge import LlamaBridgeRouter
    return LlamaBridgeRouter


def wilson_ci(successes: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def main() -> int:
    ap = argparse.ArgumentParser(description="GPU (llama.cpp 8099) Laya 基线评测")
    ap.add_argument("--test-file", default=str(DEFAULT_TEST))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--url", default=os.getenv("FORGE_LAYA_LLM_URL", "http://127.0.0.1:8099"))
    args = ap.parse_args()

    test_file = Path(args.test_file)
    if not test_file.exists():
        print(f"[gpu-eval] ✗ test 文件不存在: {test_file}")
        return 1

    os.environ.setdefault("FORGE_LAYA_BACKEND", "llama")
    os.environ["FORGE_LAYA_LLM_URL"] = args.url
    LlamaBridgeRouter = _import_bridge()
    bridge = LlamaBridgeRouter()
    print(f"[gpu-eval] backend=llama url={args.url}  test={test_file.name}")

    cases = [json.loads(l) for l in test_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"[gpu-eval] test cases: {len(cases)}")

    def _query_of(c: dict) -> str:
        """兼容两种 test 格式：query 字段（5000 train 格式）或 state 字段（565 嵌套格式）。"""
        q = c.get("query")
        if isinstance(q, str) and q.strip():
            return q.strip()
        s = c.get("state")
        if isinstance(s, str):
            s = s.strip()
            if s[:1] in ('"', "["):  # 565 的 state 是带引号的 JSON 字符串
                try:
                    return json.loads(s).strip()
                except json.JSONDecodeError:
                    return s.strip('"')
            return s
        return ""

    # 评测
    tp = fp = tn = fn = 0
    total = 0
    correct = 0
    confidences = []
    wrong = []
    t0 = time.time()
    for c in cases:
        query = _query_of(c)
        gold = c.get("label")
        if not query or gold not in ("text", "tool"):
            continue
        try:
            choice, conf = bridge._judge_binary(
                query,
                {"text": "纯文本直接回答即可", "tool": "需要调用外部工具执行任务"},
            )
        except Exception:
            continue
        total += 1
        confidences.append(conf)
        pred_tool = choice == "tool"
        gold_tool = gold == "tool"
        if pred_tool == gold_tool:
            correct += 1
        else:
            wrong.append({"q": query[:80], "gold": gold, "pred": choice})
        if gold_tool and pred_tool:
            tp += 1
        elif gold_tool:
            fn += 1
        elif pred_tool:
            fp += 1
        else:
            tn += 1

    if total == 0:
        print("[gpu-eval] ✗ 无有效样本")
        return 1

    acc = correct / total
    lo, hi = wilson_ci(correct, total)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    elapsed = time.time() - t0

    print(f"\n[gpu-eval] ===== GPU 基线（微调前 GGUF 近似）=====")
    print(f"  样本: {total}   耗时: {elapsed:.1f}s ({total/elapsed:.1f} q/s)")
    print(f"  准确率: {acc:.4f} ({correct}/{total})")
    print(f"  95% CI: [{lo:.4f}, {hi:.4f}]  半宽 ±{(hi-lo)/2*100:.1f}pp")
    print(f"  tool 类: P={precision:.4f} R={recall:.4f} F1={f1:.4f}")
    print(f"  混淆: TP={tp} FP={fp} TN={tn} FN={fn}")
    print(f"  错误样本 (前 8):")
    for w in wrong[:8]:
        print(f"    gold={w['gold']:4s} pred={w['pred']:4s}  {w['q']}")
    print(f"\n  口径说明：8099 GGUF 是微调前通用 encoder，无训练出的判别头，")
    print(f"  此结果走 embedding+关键词近似判定，作为「微调前基线锚点」。")
    print(f"  对比：规则引擎 42.9% / CPU 微调前 english 30%（真实分类头）。")

    Path(args.out).write_text(
        json.dumps(
            {
                "backend": "llama (8099, fine-tune-before GGUF, approx)",
                "url": args.url,
                "test_file": str(test_file),
                "total": total,
                "correct": correct,
                "accuracy": acc,
                "ci95": [lo, hi],
                "tool_precision": precision,
                "tool_recall": recall,
                "tool_f1": f1,
                "confusion": {"TP": tp, "FP": fp, "TN": tn, "FN": fn},
                "elapsed_s": elapsed,
                "qps": round(total / elapsed, 2) if elapsed else 0,
                "wrong_samples": wrong[:50],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[gpu-eval] 已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
