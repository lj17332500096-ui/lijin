#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P5 阶段1：人工标注回填 —— 把 manual_queries.md 里的标注回填成训练数据。

流程：
  1. 你打开 data/laya_tool_intent/manual_queries.md
  2. 在「最终标签」列填 tool 或 text（歧义的可填 text 或留空跳过）
  3. 运行本脚本：解析标注 → 生成 Laya 训练 case（复用 build_laya_train_data.py 的 schema）

输出：
  data/laya_tool_intent/manual_labeled.jsonl   已标注的 case（Laya 训练格式）

用法：
  .venv/Scripts/python.exe collect_manual_labels.py
"""

from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "laya_tool_intent"

# 与 build_laya_train_data.py 完全一致，保证 schema 对齐
NEEDS_TOOL_QUESTION = {
    "type": "choice",
    "instructions": (
        "判断这条用户消息是否需要 agent 调用外部工具才能完成 "
        "(如读写文件、搜索、运行代码、查天气、生成 PPT、记忆操作)。"
        "若纯文本/概念解释/问候/算术/模糊闲聊即可直接回答, 选 text; 需工具执行, 选 tool。"
    ),
    "criteria": {
        "tool": "需要调用外部工具执行任务",
        "text": "纯文本直接回答即可, 不需要工具",
    },
}


def parse_md(path: pathlib.Path) -> list[tuple[str, str]]:
    """解析 markdown 标注清单，返回 [(query, label), ...]，label ∈ {tool, text}。"""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        # 匹配表格行：| # | query | 建议标签 | 建议工具 | 最终标签 |
        m = re.match(r"^\|\s*\d+\s*\|\s*(.+?)\s*\|\s*(\w+)\s*\|\s*[^|]*\|\s*(tool|text)?\s*\|", line)
        if not m:
            continue
        query = m.group(1).strip()
        label = m.group(3)
        if label in ("tool", "text"):
            rows.append((query, label))
    return rows


def main() -> int:
    md_path = OUT_DIR / "manual_queries.md"
    if not md_path.exists():
        print(f"[err] 找不到 {md_path}，请先运行 build_manual_queries.py")
        return 1

    labeled = parse_md(md_path)
    if not labeled:
        print("[err] 未解析到任何标注。请在 manual_queries.md 的「最终标签」列填 tool/text 后重试。")
        return 1

    # 生成 Laya 训练 case
    cases = []
    for i, (query, label) in enumerate(labeled, 1):
        probs = {"tool": 1.0, "text": 0.0} if label == "tool" else {"tool": 0.0, "text": 1.0}
        cases.append({
            "id": f"manual_{i:05d}",
            "workflow": "forge_tool_intent",
            "state": json.dumps(query, ensure_ascii=False),
            "questions": json.dumps({"needs_tool": NEEDS_TOOL_QUESTION}, ensure_ascii=False),
            "gold": json.dumps({
                "needs_tool": {"label": label, "probabilities": probs},
            }, ensure_ascii=False),
        })

    out_path = OUT_DIR / "manual_labeled.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # 统计
    from collections import Counter
    c = Counter(lbl for _, lbl in labeled)
    print(f"[collect] 解析到 {len(labeled)} 条标注")
    print(f"[collect]   tool: {c['tool']} 条")
    print(f"[collect]   text: {c['text']} 条")
    print(f"[collect] 已写: {out_path}")
    print(f"\n[下一步] 这 {len(labeled)} 条是纯人工 ground-truth，可与现有 565 条合并后训练。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
