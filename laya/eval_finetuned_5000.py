"""
用 CPU 微调后的 checkpoint 对 5000 test 做真实判别头评测。

用法：
  .venv/Scripts/python.exe eval_finetuned_5000.py --checkpoint data/laya_forge/forge_finetuned_5000_fast/forge_checkpoint.pt
  （或指定 --test-file data/laya_tool_intent/laya_5000_test.jsonl）

输出：precision / recall / F1 / 混淆矩阵 / 与 GPU 关键词基线 74.2% 的对比。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAYA_FORK = REPO / "laya" / "_laya_inspect" / "laya-main" / "laya"
DEFAULT_CKPT = REPO / "data" / "laya_forge" / "forge_finetuned_5000_fast" / "forge_checkpoint.pt"
DEFAULT_TEST = REPO / "data" / "laya_tool_intent" / "laya_5000_test.jsonl"


def _import_laya():
    try:
        import laya
        return laya
    except ImportError:
        sys.path.insert(0, str(LAYA_FORK.parent))
        import laya
        return laya


def _import_laya_runtime():
    """对齐 train_laya_forge.py：返回 (build_sequence, render_options, QTYPES, collate_items)。"""
    laya = _import_laya()
    from laya.common import build_sequence, render_options, collate_items
    return build_sequence, render_options, laya.common.QTYPES, collate_items


def load_test(path: Path) -> list[dict]:
    """test 文件是 565 格式（state/questions/gold）或 5000 格式（query/label）。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        # 判断格式
        if "query" in c and "label" in c:
            # 5000 格式
            q = c["query"]
            gold = c["label"]
        elif "state" in c:
            # 565 格式
            q_raw = c.get("state", "")
            q = q_raw.strip('"') if q_raw.startswith('"') else q_raw
            g = c.get("gold", "{}")
            g = json.loads(g) if isinstance(g, str) else g
            gold = g.get("needs_tool", {}).get("label", "text")
        else:
            continue
        out.append({"query": q, "gold": gold})
    return out


def eval_checkpoint(ckpt_path: Path, test_file: Path) -> int:
    import torch
    from laya.agent import Agent
    build_seq, render_opt, QTYPES, collate_items = _import_laya_runtime()

    print(f"[eval] 加载 checkpoint: {ckpt_path}")
    # checkpoint 结构：state_dict + cfg + ...
    payload = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    state_dict = payload.get("state_dict", payload)
    cfg = payload.get("cfg", {})
    print(f"[eval] cfg.temperature={cfg.get('temperature')}  forge_patched={cfg.get('forge_patched')}")

    # 用微调后的目录加载 Agent（tokenizer 来自原 english 目录）
    english_dir = REPO / "data" / "laya_forge" / "english"
    agent = Agent(str(english_dir), device="cpu")
    model = agent.model
    model.load_state_dict(state_dict, strict=False)
    model.eval()
    tok = agent.tok

    cases = load_test(test_file)
    print(f"[eval] test cases: {len(cases)}")
    gold_dist = {}
    for c in cases:
        gold_dist[c["gold"]] = gold_dist.get(c["gold"], 0) + 1
    print(f"[eval] gold 分布: {gold_dist}")

    tp = fp = tn = fn = 0
    total = 0
    correct = 0
    wrong_samples = []
    confidences = []
    t0 = time.time()

    for c in cases:
        q = c["query"]
        gold = c["gold"]
        if not q:
            continue
        # 构造 Laya 二分类 decision
        qdef = {"t": "choice", "ins": q, "crit": {"text": "answer directly without tools", "tool": "requires tool call"}}
        k = len(render_opt(qdef))
        raw_ids, raw_markers = build_seq(tok, q, qdef, cfg.get("max_len", 512), cfg.get("head_max_len", 256))
        if raw_ids is None:
            continue
        target_list = [0.0, 1.0] if gold == "tool" else [1.0, 0.0]
        raw_item = {"ids": raw_ids, "markers": raw_markers, "qtype": QTYPES["choice"], "target": target_list}
        b = collate_items([[raw_item]], 0)
        if b is None:
            continue
        input_ids = b["input_ids"]
        attention_mask = b["attention_mask"]
        marker_pos = b["marker_pos"]
        marker_mask = b["marker_mask"]
        qtype = b["qtype"]

        with torch.no_grad():
            logits, _ = model(input_ids, attention_mask, marker_pos, marker_mask, qtype)
            probs = torch.softmax(logits, dim=-1)[0]
            pred_idx = int(torch.argmax(probs).item())
            conf = float(probs[pred_idx].item())
        pred = "tool" if pred_idx == 1 else "text"

        total += 1
        confidences.append(conf)
        if pred == gold:
            correct += 1
        if gold == "tool" and pred == "tool":
            tp += 1
        elif gold == "text" and pred == "tool":
            fp += 1
        elif gold == "text" and pred == "text":
            tn += 1
        elif gold == "tool" and pred == "text":
            fn += 1

        if pred != gold and len(wrong_samples) < 10:
            wrong_samples.append({"query": q[:60], "gold": gold, "pred": pred, "conf": conf})

    elapsed = time.time() - t0
    acc = correct / total if total else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    print(f"\n[eval] 完成: {total} cases, {elapsed:.1f}s")
    print(f"[eval] 准确率: {acc*100:.1f}% ({correct}/{total})")
    print(f"[eval] tool 类: P={prec*100:.1f}% R={rec*100:.1f}% F1={f1*100:.1f}%")
    print(f"[eval] 混淆: TP={tp} FP={fp} TN={tn} FN={fn}")
    if confidences:
        import statistics
        print(f"[eval] 置信度: mean={statistics.mean(confidences):.4f} median={statistics.median(confidences):.4f}")

    # 对比锚点
    print(f"\n[eval] 对比锚点:")
    print(f"  GPU 关键词基线（微调前）: 74.2%")
    print(f"  CPU 微调前 30%（旧 565 口径）: 30.0%")
    print(f"  本次微调后（1000条×2epoch）: {acc*100:.1f}%")
    if wrong_samples:
        print(f"\n[eval] 前 {len(wrong_samples)} 条错误样本:")
        for ws in wrong_samples:
            print(f"  {ws['query']}")
            print(f"    gold={ws['gold']} pred={ws['pred']} conf={ws['conf']:.4f}")

    # 写结果
    out = REPO / "data" / "laya_tool_intent" / "eval_finetuned_5000.json"
    out.write_text(json.dumps({
        "checkpoint": str(ckpt_path),
        "test_file": str(test_file),
        "n_cases": total,
        "accuracy": acc,
        "precision_tool": prec,
        "recall_tool": rec,
        "f1_tool": f1,
        "confusion": {"TP": tp, "FP": fp, "TN": tn, "FN": fn},
        "mean_conf": statistics.mean(confidences) if confidences else 0,
        "elapsed_s": elapsed,
        "baseline_gpu_keyword": 0.742,
        "baseline_cpu_pre_ft": 0.300,
    }, indent=2), encoding="utf-8")
    print(f"\n[eval] 结果已写 {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", default=str(DEFAULT_CKPT))
    ap.add_argument("--test-file", default=str(DEFAULT_TEST))
    args = ap.parse_args()
    return eval_checkpoint(Path(args.checkpoint), Path(args.test_file))


if __name__ == "__main__":
    sys.exit(main())
