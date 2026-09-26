#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P5 阶段1：query 生成器 —— 打破「标签循环性」的高质量 ground-truth 来源。

背景（为什么需要本脚本）：
  现有 565 条训练数据的标签全部来自 FORGE 规则引擎反向标注（tool_router.jsonl 的
  intent_class + sessions.sqlite 的 select_tool_names 输出），这是「标签循环性」——
  Laya 学到的只会是"复刻规则引擎"，而非"理解用户意图"。且实测基线 42.9% < 随机 50%。

本脚本的价值：
  程序化生成**全新的、多样化的**用户 query 候选（不依赖规则引擎、不依赖历史日志），
  每条候选附上「建议标签 + 建议工具 + 理由」，供人工 review 后形成 ground-truth。

输出：
  data/laya_tool_intent/manual_queries.md        人类可读标注清单（你在这里填最终标签）
  data/laya_tool_intent/manual_queries.jsonl     结构化候选（后续训练管线消费）

用法：
  .venv/Scripts/python.exe build_manual_queries.py
  .venv/Scripts/python.exe build_manual_queries.py --count 400   # 控制生成规模
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "laya_tool_intent"

# ---------------------------------------------------------------------------
# 工具 → 生成模板。每个工具给一组中文自然 query 模板 + 参数变体。
# label: tool（需工具） / text（纯文本可答）
# 注意：这是「建议标签」，最终以人工 review 为准。
# ---------------------------------------------------------------------------

TOOL_TEMPLATES: dict[str, list[str]] = {
    "calculate": [
        "帮我算 {a} {op} {b}",
        "计算 {a} 乘以 {b} 的结果",
        "{a} 加 {b} 减 {c} 等于多少",
        "求 {a} 的平方根",
        "{num} 的 {pct}% 是多少",
    ],
    "web_search": [
        "搜索一下「{topic}」的最新消息",
        "帮我查 {topic} 的相关资料",
        "现在 {topic} 的最新进展是什么",
        "上网搜 {topic}",
    ],
    "read_workspace_file": [
        "读取工作区里的 {file} 文件内容",
        "帮我看下 {file} 里写了什么",
        "打开 {file} 并总结要点",
    ],
    "list_workspace_files": [
        "列出工作区有哪些文件",
        "看看当前目录下有什么",
    ],
    "get_current_datetime": [
        "现在几点了",
        "今天是星期几",
        "告诉我当前日期时间",
    ],
    "save_note": [
        "帮我记一条笔记：{content}",
        "把这个记下来：{content}",
    ],
    "list_notes": [
        "我有哪些笔记",
        "列出所有笔记",
    ],
    "read_note": [
        "读一下我之前的笔记",
    ],
    "remember": [
        "记住：{fact}",
        "帮我记住 {fact}",
    ],
    "recall_memory": [
        "我之前让你记过什么",
        "回忆一下我的偏好",
    ],
    "schedule_add": [
        "提醒我 {time} 做 {task}",
        "定个 {time} 的闹钟，要做 {task}",
    ],
    "write_code_file": [
        "写一个 {lang} 的 {func} 函数",
        "帮我生成一段 {func} 代码",
    ],
    "read_code_file": [
        "读一下 {project} 里的 {file} 代码",
    ],
    "run_python": [
        "运行这段 Python：{code}",
        "帮我执行这个脚本",
    ],
    "save_word_doc": [
        "帮我写一份关于 {topic} 的 Word 文档",
        "生成一份 {topic} 的 docx",
    ],
    "save_excel_workbook": [
        "把 {data} 做成 Excel 表格",
        "生成一份 {data} 的 xlsx",
    ],
    "save_ppt_deck": [
        "做一份 {topic} 的 PPT",
        "帮我生成 {topic} 的演示文稿",
    ],
    "read_spreadsheet": [
        "读一下 {file} 这个表格",
    ],
    "read_office_file": [
        "打开 {file} 这个文档",
    ],
    "deep_research": [
        "深入研究一下 {topic}",
        "帮我做一个 {topic} 的深度调研",
    ],
    "fetch_github_repo": [
        "拉取 {repo} 这个仓库",
        "看下 GitHub 上的 {repo}",
    ],
    "run_tests": [
        "跑一下项目的测试",
        "运行单元测试",
    ],
    "search_sources": [
        "在资料库里搜 {topic}",
        "检索知识库里的 {topic}",
    ],
    "search_documents": [
        "在文档里搜索 {keyword}",
    ],
    "index_workspace": [
        "索引一下工作区",
        "给工作区建索引",
    ],
    "write_project_file": [
        "新建一个 {file} 文件，内容是 {content}",
    ],
    "edit_project_file": [
        "修改 {file} 里的 {part}",
    ],
    "sandbox_snapshot": [
        "给当前沙箱打个快照",
    ],
    "ask_image": [
        "看看这张图片里有什么",
    ],
}

# 每个工具的模板参数池（用中文常见值，避免生成重复）
TEMPLATE_ARGS: dict[str, Any] = {
    "a": [1234, 3.14, 789, 4567, 88, 1024],
    "b": [56, 2.5, 321, 19, 7, 512],
    "c": [10, 100, 5, 33],
    "num": [200, 1000, 5000, 800],
    "pct": [15, 30, 7.5, 45],
    "op": ["加", "减", "乘以", "除以"],
    "topic": ["人工智能", "新能源汽车", "房价走势", "量子计算", "霸州市", "水培大麦苗", "Web开发", "机器学习"],
    "file": ["README.md", "notes.md", "config.json", "报告.docx", "数据.xlsx", "main.py"],
    "content": ["明天的会议安排", "一个重要的灵感", "采购清单：牛奶、鸡蛋、面包"],
    "fact": ["我喜欢用 Python", "我的手机号尾号是 1234", "下次会议在周三"],
    "time": ["明天上午 9 点", "后天下午 3 点", "下周一早上"],
    "task": ["开会", "交报告", "打电话给客户", "取快递"],
    "lang": ["Python", "JavaScript", "Java"],
    "func": ["快速排序", "二叉树遍历", "字符串反转", "斐波那契"],
    "project": ["my_creative_agent", "novel-creator", "shengshui_desktop"],
    "code": ["print('hello')", "1+2", "sorted([3,1,2])"],
    "data": ["销售数据", "库存清单", "员工名单"],
    "repo": ["deepseek-ai/awesome-deepseek", "openai/gpt-4", "vllm-project/vllm"],
    "keyword": ["架构", "审批", "记忆", "评测"],
    "part": ["第三段", "函数名", "标题", "配置项"],
}

# 纯文本/负面样本（这些是 text 类，用于校准"不需要工具"的判别）
TEXT_QUERIES: list[str] = [
    "你好",
    "早上好",
    "谢谢你的帮助",
    "什么是机器学习？",
    "解释一下相对论",
    "给我讲讲中国古代史",
    "为什么天空是蓝色的",
    "什么是 REST API",
    "你觉得明天会下雨吗",
    "推荐几本好看的小说",
    "如何保持自律",
    "介绍一下你自己",
    "这个周末有什么建议",
    "帮我分析一下这句话的意思",
    "什么是量子纠缠",
    "讲讲三体的故事",
    "如何学好英语",
    "人生的意义是什么",
    "给我写一首诗",
    "什么是区块链",
]

# 歧义样本（既可能是 tool 也可能是 text，用于测试判别鲁棒性）
AMBIGUOUS_QUERIES: list[str] = [
    "帮我看看",
    "能帮我一下吗",
    "这个怎么弄",
    "你懂我的意思吧",
    "查一下",
    "帮我整理一下",
    "那个事情怎么样了",
    "帮我弄个东西",
]


def _render(template: str, args: dict[str, Any]) -> str:
    """渲染模板（用参数池里的值替换占位符）。"""
    out = template
    for k, v in args.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def generate_tool_queries() -> list[dict[str, Any]]:
    """基于工具模板生成 query 候选。每条含建议工具 + 建议标签。"""
    results: list[dict[str, Any]] = []
    for tool, templates in TOOL_TEMPLATES.items():
        for tpl in templates:
            # 找出模板里用到的占位符
            keys = [k for k in TEMPLATE_ARGS if "{" + k + "}" in tpl]
            # 每条模板生成多个变体（取参数池前几个值）
            variants = 3
            for vi in range(variants):
                args = {}
                for k in keys:
                    pool = TEMPLATE_ARGS[k]
                    args[k] = pool[(vi + hash(tool) % len(pool)) % len(pool)]
                query = _render(tpl, args)
                results.append({
                    "query": query,
                    "suggest_tool": tool,
                    "suggest_label": "tool",
                    "source": "template",
                })
    return results


def generate_text_queries() -> list[dict[str, Any]]:
    return [
        {"query": q, "suggest_tool": None, "suggest_label": "text", "source": "template"}
        for q in TEXT_QUERIES
    ]


def generate_ambiguous_queries() -> list[dict[str, Any]]:
    return [
        {"query": q, "suggest_tool": None, "suggest_label": "ambiguous", "source": "template"}
        for q in AMBIGUOUS_QUERIES
    ]


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        q = it["query"]
        if q in seen:
            continue
        seen.add(q)
        out.append(it)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="P5 阶段1 query 生成器")
    ap.add_argument("--count", type=int, default=0, help="生成目标规模（0=全部生成）")
    args = ap.parse_args()

    tool_q = generate_tool_queries()
    text_q = generate_text_queries()
    amb_q = generate_ambiguous_queries()

    all_q = dedupe(tool_q + text_q + amb_q)
    if args.count > 0:
        all_q = all_q[: args.count]

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. JSONL（结构化，供后续训练管线消费）
    jsonl_path = OUT_DIR / "manual_queries.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for it in all_q:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    # 2. Markdown（人工标注清单）
    md_path = OUT_DIR / "manual_queries.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# P5 阶段1 · 人工标注清单\n\n")
        f.write("> 打破「标签循环性」：以下是程序生成的全新 query 候选，"
                "请逐条在「最终标签」列填写 `tool` 或 `text`（歧义的可填 `text` 或跳过）。\n")
        f.write("> 「建议标签」是生成器的参考，**不作为 ground-truth**，以你的判断为准。\n\n")
        f.write(f"共 {len(all_q)} 条候选。\n\n")
        f.write("| # | query | 建议标签 | 建议工具 | 最终标签（填 tool/text） |\n")
        f.write("|---|---|---|---|---|\n")
        for i, it in enumerate(all_q, 1):
            tool = it["suggest_tool"] or "-"
            f.write(f"| {i} | {it['query']} | {it['suggest_label']} | {tool} |  |\n")

    # 统计
    from collections import Counter
    c = Counter(it["suggest_label"] for it in all_q)
    print(f"[gen] 工具类 query: {c['tool']} 条")
    print(f"[gen] 纯文本 query: {c['text']} 条")
    print(f"[gen] 歧义 query: {c['ambiguous']} 条")
    print(f"[gen] 去重后共 {len(all_q)} 条")
    print(f"[gen] 已写: {jsonl_path}")
    print(f"[gen] 已写: {md_path}")
    print(f"\n[下一步] 打开 {md_path.name}，逐条填「最终标签」列，"
          f"然后运行 collect_manual_labels.py 回填成训练数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
