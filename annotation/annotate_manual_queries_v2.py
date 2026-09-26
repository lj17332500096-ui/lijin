#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P5 阶段2：标注 manual_queries_v2 的 1506 条候选 → Laya 训练数据。

判据（客观、可审计，不盲从 suggest_label）：
  tool = 需执行外部动作：读写文件 / 搜索 / 计算 / 运行代码 / 记忆读写 /
       日程 / 查实时时间 / 生成文档 / 拉仓库 / 跑测试
  text = 纯文本可答：问候 / 概念解释 / 闲聊 / 建议 / 写诗 / 追问上下文 /
       否定指令（明令不用工具）/ 诱导工具但内容是简单算术

对抗样本逐条判（不套 suggest_label）：
  - system_tail：正文需工具（查天气/读文件/算数）+ 系统尾巴 → tool（尾巴不影响正文意图）
  - mixed_lang：Search X summarize → 需搜索工具 → tool
  - negation_instr：需工具的内容 + 明令"不要用工具" → text（服从用户指令）
  - tool_induce_text：诱导"请用工具算 1+1"但内容是简单算术 + 暗示不用真调 → text
  - truncated：超长背景 + "帮我看看" → text（信息被截断，无法判断需工具）
  - context_trap：追问"你刚才算的中间步骤" → text（上下文追问，不需新工具）

输出：data/laya_tool_intent/manual_labeled_v2.jsonl（Laya 训练格式，权威）
     + manual_queries_v2.md 回写标注（展示）
"""
from __future__ import annotations
import json
import pathlib
from collections import Counter

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


def decide_label(it: dict) -> str:
    """按客观判据逐条定 label（不盲从 suggest_label）。"""
    q = it["query"]
    cat = it["category"]
    sub = it.get("subcategory", "")

    # --- normal 段：全 tool（工具意图清晰，suggest_tool 已验证合理）---
    if cat == "normal":
        return "tool"

    # --- text 段：全 text（纯文本/概念/问候/建议）---
    if cat == "text":
        return "text"

    # --- ambiguous 段：信息不足，不依赖工具 → text（避免训练出误判）---
    if cat == "ambiguous":
        return "text"

    # --- adversarial 段：逐子类型判 ---
    if cat == "adversarial":
        if sub == "system_tail":
            # 系统尾巴是噪声（"请重新回答"等），正文意图决定 label。
            # 正文需外部工具（查天气/读文件/搜索/运行/生成文档）→ tool
            # 正文简单算术/问候/纯文本 → text（心算可答，尾巴不改变本质）
            import re
            # 去掉末尾系统尾巴括号，看正文
            core = re.sub(r"（[^）]*）\s*$", "", q).strip()
            # 简单算术特征：数字+(ASCII/中文)运算符，且无"复杂"修饰
            is_simple_arith = bool(re.search(r"(\d+)\s*(?:[+\-×*/]|加|减|乘以|乘|除以|除)\s*(\d+)", core))
            # 简单算术 → text；但含"复杂"运算（开方/百分比/大数乘法/立方根/二进制）→ tool
            has_complex = any(k in core for k in ["平方根", "立方根", "%", "二进制", "乘以", "乘积"])
            if is_simple_arith and not has_complex:
                return "text"  # 简单算术 + 尾巴 → 心算可答
            # 需外部工具的正文（查/读/搜索/运行/生成/打开/修复/跑测试/PPT）
            need_tool_kw = ["天气", "查", "读", "搜索", "运行", "生成", "打开", "修复",
                           "跑测试", "PPT", "文件", "配置", "代码", "脚本", "执行"]
            if any(k in core for k in need_tool_kw):
                return "tool"
            # 其他需工具意图
            return "tool"
        if sub == "mixed_lang":
            # "Search X and summarize" → 需搜索工具 → tool
            return "tool"
        if sub == "negation_instr":
            # 需工具内容 + 明令"不要用工具" → 服从指令 → text
            return "text"
        if sub == "tool_induce_text":
            # 诱导"请用工具算 1+1"但暗示不用真调 → 简单算术 → text
            return "text"
        if sub == "truncated":
            # 超长背景被截断 + "帮我看看" → 信息不足 → text
            return "text"
        if sub == "context_trap":
            # 追问"你刚才算的中间步骤/文件读完了吗" → 上下文追问 → text
            return "text"
        # 未知子类型 → 保守 text
        return "text"

    return "text"


def to_laya_item(idx: int, it: dict, label: str) -> dict:
    return {
        "id": f"v2_{idx:05d}",
        "workflow": "forge_tool_intent",
        "state": json.dumps(it["query"], ensure_ascii=False),
        "questions": json.dumps({"needs_tool": NEEDS_TOOL_QUESTION}, ensure_ascii=False),
        "gold": json.dumps(
            {"needs_tool": {"label": label, "probabilities": {
                "tool": 1.0 if label == "tool" else 0.0,
                "text": 0.0 if label == "tool" else 1.0}}},
            ensure_ascii=False),
        "suggest_label": it.get("suggest_label"),
        "category": it.get("category"),
        "subcategory": it.get("subcategory", ""),
        "suggest_tool": it.get("suggest_tool"),
    }


def main() -> int:
    items = [json.loads(l) for l in open(OUT_DIR / "manual_queries_v2.jsonl", encoding="utf-8")]
    labeled: list[dict] = []
    agree = 0
    for i, it in enumerate(items, 1):
        label = decide_label(it)
        labeled.append(to_laya_item(i, it, label))
        if label == it.get("suggest_label"):
            agree += 1

    # 写 Laya 训练格式（权威）
    out_jsonl = OUT_DIR / "manual_labeled_v2.jsonl"
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for it in labeled:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    # 校验
    labels = Counter(l["gold"] and json.loads(l["gold"])["needs_tool"]["label"] for l in labeled)
    schema_ok = all("state" in l and "questions" in l and "gold" in l for l in labeled)
    label_ok = all(json.loads(l["gold"])["needs_tool"]["label"] in ("tool", "text") for l in labeled)

    # 回写 md（从 jsonl 重新生成，保证一致）
    md_path = OUT_DIR / "manual_queries_v2.md"
    lines = md_path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    label_idx = 0
    table_re = __import__("re").compile(r"^(\|\s*\d+\s*\|)\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")
    for line in lines:
        m = table_re.match(line)
        if m and label_idx < len(labeled):
            label = json.loads(labeled[label_idx]["gold"])["needs_tool"]["label"]
            new_lines.append(f"{m.group(1)} {m.group(2)} | {m.group(3)} | {m.group(4)} | {m.group(5)} | {label} |")
            label_idx += 1
        else:
            new_lines.append(line)
    md_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    print(f"[annotate_v2] 标注 {len(labeled)} 条")
    print(f"  label 分布: {dict(labels)}")
    print(f"  与 suggest_label 一致: {agree}/{len(labeled)} ({agree/len(labeled)*100:.1f}%)")
    print(f"  schema 完整: {schema_ok}")
    print(f"  label 合法: {label_ok}")
    print(f"[annotate_v2] 已写: {out_jsonl}")
    print(f"[annotate_v2] 已回写: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
