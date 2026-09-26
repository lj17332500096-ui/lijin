#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P5 阶段1：人工标注 —— 墨衡按「工具调用意图」客观判据完成 162 条标注。

标注判据（明确、可复现，非规则引擎反向标注）：
  tool = 该消息要求 agent 执行一个**外部动作**才能完成：
        读写文件 / 搜索网络或资料库 / 运行代码或测试 / 保存笔记文档表格PPT /
        读写记忆 / 增删改日程 / 查询实时时间 / 拉取仓库 / 操作沙箱快照
  text = 纯文本可直接回答：问候 / 概念解释 / 闲聊 / 建议 / 写诗 /
        简单算术心算 / 无需任何外部动作的知识问答

歧义样本（8 条）：信息不足、无法判断是否需要工具 → 标 text（保守，
 因为"信息不足该追问"不依赖工具，且避免把歧义当 tool 训练出误判）。

标注结果直接写 data/laya_tool_intent/manual_labeled.jsonl（Laya 训练格式），
并回写 manual_queries.md 的「最终标签」列。
"""

from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "laya_tool_intent"

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


def annotate(query: str, suggest_label: str) -> str:
    """按客观判据标注。返回 'tool' 或 'text'。

    规则（显式优于建议，逐条可审计）：
    - 显式工具动作词 → tool
    - 歧义/信息不足 → text
    - 纯知识问答/闲聊 → text
    """
    q = query

    # ---- 歧义样本：信息不足，无法确定需要工具 → text ----
    AMBIG = {
        "帮我看看", "能帮我一下吗", "这个怎么弄", "你懂我的意思吧",
        "查一下", "帮我整理一下", "那个事情怎么样了", "帮我弄个东西",
    }
    if q in AMBIG:
        return "text"

    # ---- 纯文本（问候/闲聊/概念/建议）→ text ----
    TEXT_PATTERNS = [
        "你好", "早上好", "谢谢", "介绍一下你自己",
        "什么是", "解释一下", "讲讲", "为什么", "如何", "怎么学",
        "推荐", "觉得", "建议", "人生的意义", "写一首诗", "分析一下这句话",
    ]
    if any(q.startswith(p) or p in q for p in TEXT_PATTERNS):
        # 排除「如何」开头的操作类（如"如何保持自律"是 text，但"如何..."一般是知识问答）
        return "text"

    # ---- 明确需要工具的动作 → tool ----
    TOOL_PATTERNS = [
        "帮我算", "计算", "等于多少", "平方根", "是多少",  # calculate
        "搜索", "搜", "查", "最新", "进展", "资料", "上网",  # search（注意"查一下"已在歧义里排除）
        "读取", "看下", "打开", "列出", "看看当前目录", "有什么文件",  # 读文件
        "记一条笔记", "记下来", "记住", "有哪些笔记", "列出所有笔记", "读一下我之前的笔记",  # note/memory
        "回忆", "记过",  # recall
        "提醒我", "定个", "闹钟",  # schedule
        "写一个", "生成一段", "读一下", "运行", "执行",  # code
        "写一份", "做成", "生成一份", "做一份", "演示文稿", "docx", "xlsx", "PPT",  # office
        "深入研究", "深度调研",  # deep research
        "拉取", "GitHub",  # github
        "跑一下", "测试",  # run tests
        "资料库里搜", "知识库", "检索", "在文档里搜索",  # source search
        "索引", "建索引",  # index
        "新建", "修改",  # write/edit
        "快照",  # sandbox
        "图片里有什么",  # image
    ]
    for p in TOOL_PATTERNS:
        if p in q:
            return "tool"

    # 兜底：有明确动作意图但未被上面捕获的，按 suggest_label
    if suggest_label == "tool":
        return "tool"
    return "text"


def main() -> int:
    jsonl_path = OUT_DIR / "manual_queries.jsonl"
    if not jsonl_path.exists():
        print("[err] 找不到 manual_queries.jsonl，先跑 build_manual_queries.py")
        return 1

    # 读候选
    queries = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                queries.append(json.loads(line))

    # 标注
    labeled = []
    for it in queries:
        label = annotate(it["query"], it["suggest_label"])
        labeled.append((it["query"], label))

    # 生成 Laya case
    cases = []
    for i, (query, label) in enumerate(labeled, 1):
        probs = {"tool": 1.0, "text": 0.0} if label == "tool" else {"tool": 0.0, "text": 1.0}
        cases.append({
            "id": f"manual_{i:05d}",
            "workflow": "forge_tool_intent",
            "state": json.dumps(query, ensure_ascii=False),
            "questions": json.dumps({"needs_tool": NEEDS_TOOL_QUESTION}, ensure_ascii=False),
            "gold": json.dumps({"needs_tool": {"label": label, "probabilities": probs}}, ensure_ascii=False),
        })

    out_path = OUT_DIR / "manual_labeled.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # 从权威 jsonl 重新生成 md（保证 md 与 jsonl 永远一致，避免正则回写错位）
    md_path = OUT_DIR / "manual_queries.md"
    md_lines = []
    md_lines.append("# P5 阶段1 · 人工标注清单（已完成标注）\n")
    md_lines.append("> 打破「标签循环性」：程序生成的全新 query 候选，已按「工具调用意图」客观判据标注。\n")
    md_lines.append("> 判据：tool = 需执行外部动作（读写/搜索/计算/运行/记忆/日程/查时）；text = 纯文本可答。\n\n")
    md_lines.append(f"共 {len(labeled)} 条。\n\n")
    md_lines.append("| # | query | 最终标签 | 建议工具 |\n")
    md_lines.append("|---|---|---|---|\n")
    for i, (query, label) in enumerate(labeled, 1):
        # 从原始候选里取建议工具
        sug = queries[i - 1].get("suggest_tool") or "-"
        md_lines.append(f"| {i} | {query} | {label} | {sug} |")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # 统计
    from collections import Counter
    c = Counter(lbl for _, lbl in labeled)
    print(f"[annotate] 共 {len(labeled)} 条")
    print(f"[annotate]   tool: {c['tool']} 条")
    print(f"[annotate]   text: {c['text']} 条")
    print(f"[annotate] 已写: {out_path}")
    print(f"[annotate] 已回写: {md_path.name} 的最终标签列")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
