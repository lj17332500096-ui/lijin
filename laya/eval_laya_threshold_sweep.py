"""Laya route head 置信度阈值扫描。

用 500 条 test 上 ep3 的逐条 (gold, pred, confidence) 画 threshold 曲线，
找"高置信度生效 + 低置信度回落"的最优折中。

用法：
  .venv/Scripts/python.exe eval_finetuned_5000.py --checkpoint data/laya_forge/forge_finetuned_5000_xpu_fixed/forge_checkpoint_ep3.pt
  （本脚本自动复用 ep3）

输出：
  - 逐条 confidence 分布
  - threshold 0.50→0.99 扫描表：每个阈值下的"生效子集准确率" + 生效数
  - 推荐阈值（F1 最高 or 准确率≥90% 的最小阈值）
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).resolve().parent.parent
LAYA_FORK = REPO / "laya" / "_laya_inspect" / "laya-main" / "laya"
DEFAULT_CKPT = REPO / "data" / "laya_forge" / "forge_finetuned_5000_xpu_fixed" / "forge_checkpoint_ep3.pt"
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
    laya = _import_laya()
    from laya.common import build_sequence, render_options, collate_items
    return build_sequence, render_options, laya.common.QTYPES, collate_items


def load_test(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        if "query" in c and "label" in c:
            q, gold = c["query"], c["label"]
        elif "state" in c:
            q_raw = c.get("state", "")
            q = q_raw.strip('"') if q_raw.startswith('"') else q_raw
            g = c.get("gold", "{}")
            g = json.loads(g) if isinstance(g, str) else g
            gold = g.get("needs_tool", {}).get("label", "text")
        else:
            continue
        out.append({"query": q, "gold": gold})
    return out


def main() -> int:
    import torch
    from laya.agent import Agent
    build_seq, render_opt, QTYPES, collate_items = _import_laya_runtime()

    print(f"[eval] 加载 checkpoint: {DEFAULT_CKPT}")
    payload = torch.load(DEFAULT_CKPT, map_location="cpu", weights_only=False)
    state_dict = payload.get("state_dict", payload)
    cfg = payload.get("cfg", {})
    english_dir = REPO / "data" / "laya_forge" / "english"
    agent = Agent(str(english_dir), device="cpu")
    model = agent.model
    model.load_state_dict(state_dict, strict=False)
    model.eval()
    tok = agent.tok

    cases = load_test(DEFAULT_TEST)
    print(f"[eval] test cases: {len(cases)}")

    # 逐条推理，收集 (gold, pred, confidence)
    results = []
    t0 = time.time()
    for c in cases:
        q, gold = c["query"], c["gold"]
        if not q:
            continue
        qdef = {"t": "choice", "ins": q, "crit": {"text": "answer directly without tools", "tool": "requires tool call"}}
        render_opt(qdef)
        raw_ids, raw_markers = build_seq(tok, q, qdef, cfg.get("max_len", 512), cfg.get("head_max_len", 256))
        if raw_ids is None:
            continue
        target = [0.0, 1.0] if gold == "tool" else [1.0, 0.0]
        raw_item = {"ids": raw_ids, "markers": raw_markers, "qtype": QTYPES["choice"], "target": target}
        b = collate_items([[raw_item]], 0)
        if b is None:
            continue
        with torch.no_grad():
            logits, _ = model(b["input_ids"], b["attention_mask"], b["marker_pos"], b["marker_mask"], b["qtype"])
            probs = torch.softmax(logits, dim=-1)[0]
            pred_idx = int(torch.argmax(probs).item())
            conf = float(probs[pred_idx].item())
        pred = "tool" if pred_idx == 1 else "text"
        results.append({"gold": gold, "pred": pred, "conf": conf, "query": q})

    elapsed = time.time() - t0
    print(f"[eval] 完成 {len(results)} cases, {elapsed:.1f}s")

    # 阈值扫描
    thresholds = [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.97, 0.99]
    print(f"\n=== 置信度阈值扫描（生效子集 = conf ≥ 阈值）===")
    print(f"{'threshold':>10}  {'生效数':>6}  {'生效准确率':>10}  {'TP':>4}  {'FP':>4}  {'TN':>4}  {'FN':>4}  {'F1(tool)':>8}")
    best = (0.0, -1.0, 0.0, 0.0)  # (thr, f1, acc, n_eff)
    for thr in thresholds:
        eff = [r for r in results if r["conf"] >= thr]
        if not eff:
            continue
        correct = sum(1 for r in eff if r["pred"] == r["gold"])
        tp = sum(1 for r in eff if r["gold"] == "tool" and r["pred"] == "tool")
        fp = sum(1 for r in eff if r["gold"] == "text" and r["pred"] == "tool")
        tn = sum(1 for r in eff if r["gold"] == "text" and r["pred"] == "text")
        fn = sum(1 for r in eff if r["gold"] == "tool" and r["pred"] == "text")
        acc = correct / len(eff)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        print(f"{thr:>10.2f}  {len(eff):>6}  {acc*100:>9.1f}%  {tp:>4}  {fp:>4}  {tn:>4}  {fn:>4}  {f1:>8.3f}")
        if f1 > best[1]:
            best = (thr, f1, acc, len(eff))

    print(f"\n[推荐] 按 tool F1 最优: threshold={best[0]:.2f}  F1={best[1]:.3f}  准确率={best[2]*100:.1f}%  生效数={best[3]}")

    # confidence 分布
    confs = [r["conf"] for r in results]
    confs.sort()
    n = len(confs)
    print(f"\n[confidence 分布] min={confs[0]:.4f}  p25={confs[n//4]:.4f}  median={confs[n//2]:.4f}  p75={confs[3*n//4]:.4f}  max={confs[-1]:.4f}")

    # 写 JSON 供后续用
    out = REPO / "data" / "laya_tool_intent" / "eval_finetuned_5000_threshold_sweep.json"
    out.write_text(json.dumps({
        "checkpoint": str(DEFAULT_CKPT),
        "n_cases": len(results),
        "recommended_threshold": best[0],
        "recommended_f1": best[1],
        "recommended_acc": best[2],
        "recommended_n_eff": best[3],
        "per_case": results[:200],  # 前 200 条供抽查
    }, indent=2), encoding="utf-8")
    print(f"\n[eval] 结果已写 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
