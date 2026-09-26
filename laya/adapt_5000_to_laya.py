"""5000 标注数据 → Laya 565 格式适配器。

把 tool_routing_5000_train.jsonl（query/label/tool 三字段，二分类严格平衡）
转成 train_laya_forge.py 能吃的 565 case 格式（state/questions/gold 嵌套），
这样 5000 数据能直接喂给现有训练脚本，不用改训练代码。

输出：data/laya_tool_intent/laya_5000_train.jsonl
  - 8:1:1 切分（train 4000 / val 500 / test 500）
  - 每条：{id, state, questions, gold, label, tool, source, subtype}
    其中 questions/gold 是 JSON 字符串（与 565 一致，train_laya_forge 的 _j 会解开）

用法：
  .venv/Scripts/python.exe adapt_5000_to_laya.py
  .venv/Scripts/python.exe train_laya_forge.py --dry-run --train-file data/laya_tool_intent/laya_5000_train.jsonl
"""
from __future__ import annotations

import json
import random
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "data" / "laya_tool_intent" / "tool_routing_5000_train.jsonl"
OUT_DIR = REPO / "data" / "laya_tool_intent"
SEED = 20260925

# 固定的 needs_tool 问题定义（与 565 case 的 questions 字段一致口径）
INSTRUCTIONS = (
    "判断这条用户消息是否需要 agent 调用外部工具才能完成 "
    "(如读写文件、搜索、运行代码、查天气、生成 PPT、记忆操作)。"
    "若纯文本/概念解释/问候/算术/模糊闲聊即可直接回答, 选 text; 需工具执行, 选 tool。"
)
CRITERIA = {
    "tool": "需要调用外部工具执行任务",
    "text": "纯文本直接回答即可, 不需要工具",
}


def _q_str(label: str, query: str) -> str:
    """构造 565 口径的 questions 字段（JSON 字符串）。"""
    return json.dumps(
        {
            "needs_tool": {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": CRITERIA,
            }
        },
        ensure_ascii=False,
    )


def _g_str(label: str) -> str:
    """构造 565 口径的 gold 字段（JSON 字符串，one-hot probabilities）。"""
    probs = {"text": 0.0, "tool": 0.0}
    probs[label] = 1.0
    return json.dumps(
        {"needs_tool": {"label": label, "probabilities": probs}},
        ensure_ascii=False,
    )


def adapt(row: dict) -> dict | None:
    query = (row.get("query") or "").strip()
    label = row.get("label")
    tool = row.get("tool", "-")
    if not query or label not in ("text", "tool"):
        return None
    return {
        "id": f"a{abs(hash(query)) % 10**8:08d}",
        "state": json.dumps(query, ensure_ascii=False),  # 565 的 state 是带引号的 JSON 字符串
        "questions": _q_str(label, query),
        "gold": _g_str(label),
        "label": label,
        "tool": tool,
        "source": "tool_routing_5000",
        "intent_class_original": "tool_needed" if label == "tool" else "direct_text",
    }


def main() -> None:
    rows = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"[adapt] 载入 {SRC.name}: {len(rows)}")
    from collections import Counter
    print(f"[adapt] label 分布: {Counter(r.get('label') for r in rows)}")

    cases = [adapt(r) for r in rows]
    cases = [c for c in cases if c is not None]
    print(f"[adapt] 有效 case: {len(cases)}")

    # 8:1:1 分层切分（按 label 分层，保证每片 tool/text 平衡）
    rng = random.Random(SEED)
    by_label: dict[str, list[dict]] = {"tool": [], "text": []}
    for c in cases:
        by_label[c["label"]].append(c)
    for v in by_label.values():
        rng.shuffle(v)

    train, val, test = [], [], []
    for label, pool in by_label.items():
        n = len(pool)
        n_tr = int(n * 0.8)
        n_va = int(n * 0.1)
        train += pool[:n_tr]
        val += pool[n_tr : n_tr + n_va]
        test += pool[n_tr + n_va:]
    rng.shuffle(train)
    rng.shuffle(val)
    rng.shuffle(test)
    print(f"[adapt] 切分: train={len(train)} val={len(val)} test={len(test)}")

    for name, data in (("train", train), ("val", val), ("test", test)):
        out = OUT_DIR / f"laya_5000_{name}.jsonl"
        with out.open("w", encoding="utf-8") as f:
            for c in data:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"[adapt] 写出 {out.name}: {len(data)}")

    print("[adapt] 完成。下一步：")
    print(f"  .venv/Scripts/python.exe train_laya_forge.py --dry-run --train-file {OUT_DIR/'laya_5000_train.jsonl'}")
    print(f"  .venv/Scripts/python.exe train_laya_forge.py --train --device cpu --epochs 5 --train-file {OUT_DIR/'laya_5000_train.jsonl'}")


if __name__ == "__main__":
    main()
