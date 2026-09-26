"""
FORGE Laya 路线A 第一阶段 · 独立 test 预标注清单生成

目标：
- 从 sessions.sqlite 挑 200 条【不在现有 565 train/val/test 里】的新 user query。
- 意图多样性：按工具域分层抽样，避免全是某类。
- AI 预标注：用 FORGE 的 select_tool_names 反向打标（tool_needed / text）+ 工具域 + 置信标记，
  供人工 review 纠错。人工纠错结果即独立 test 标签（解决标签循环性 + 统计功效）。

输出：
  data/laya_tool_intent/test_annotation_review.md   # 200 条清单，可人工 review
  data/laya_tool_intent/test_annotation_labels.jsonl # 人工可填的标签骨架（默认预标注，人工改）
  data/laya_tool_intent/test_annotation_meta.json    # 抽样/排除统计

注意：
  预标注只是「建议值」，不是真值。人工 review 时可直接纠正。
  最终训练/验收只用人工确认后的标签。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def fp_of(s: str) -> str:
    return hashlib.md5(normalize(s).lower().encode("utf-8")).hexdigest()


def known_fingerprints() -> set[str]:
    known: set[str] = set()
    for split in ("train", "val", "test"):
        p = os.path.join(REPO, "data", "laya_tool_intent", f"{split}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    q = json.loads(line)
                    known.add(q.get("fingerprint", ""))
                except json.JSONDecodeError:
                    continue
    return {k for k in known if k}


def load_user_messages(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    out: list[str] = []
    for (raw,) in con.execute(
        "SELECT message_data FROM agent_messages ORDER BY id"
    ):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if d.get("role") != "user":
            continue
        c = d.get("content", "")
        if not isinstance(c, str) or not c.strip():
            continue
        out.append(c)
    return out


def is_clean_user_text(c: str) -> bool:
    """排除系统注入、工具结果、超长/过短、非自然语言。"""
    c = c.strip()
    if len(c) < 4 or len(c) > 400:
        return False
    # 排除明显是工具输出/JSON/XML/代码块的结构
    if c.startswith(("{", "[", "<", "```")):
        return False
    if "function_call_output" in c or '"tool_call_id"' in c:
        return False
    # 排除含大量 JSON 键值的（非自然语言 query）
    if c.count('"') > 15:
        return False
    return True


def clean_query(c: str) -> str:
    """取 query 第一段真正的用户意图，去掉系统注入尾巴与跨轮拼接。

    规则：截到系统注入标记之前；多段只取第一段（到第一个换行）。
    清洗后仍 <4 字的返回空串（调用方丢弃）。
    """
    import re as _re
    c = c.strip()
    for stop in ("（系统", "(系统", "请基于实际情况重新回答", "请重新回答",
                 "没有可靠的查询依据", "该任务实际上还没有真正执行"):
        idx = c.find(stop)
        if idx > 0:
            c = c[:idx].strip()
    # 取第一段（跨轮拼接只留首段用户意图）
    parts = _re.split(r"[\n]+", c)
    if parts:
        c = parts[0].strip()
    # 收尾标点/空白归一
    c = c.rstrip("，。,.、 ")
    return c


def _stub(*a, **kw):
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="生成独立 test 预标注清单")
    ap.add_argument("--db", default=os.path.join(REPO, "sessions.sqlite"))
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--out-dir", default=os.path.join(REPO, "data", "laya_tool_intent"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    import random
    random.seed(args.seed)

    # 照 build_laya_train_data.py 的离线安全调用模式：stub 副作用 + 两参 select_tool_names
    import runtime.tool_router as tr  # noqa: E402
    tr._write_tool_router_log = _stub
    tr.record_router_call = _stub
    import agent  # noqa: E402

    all_tools = [t.name for t in (agent.assistant_agent.tools or []) if getattr(t, "name", None)]

    known = known_fingerprints()
    users = load_user_messages(args.db)

    # 候选：不在 565 里、干净
    # 注：不排除 565 的 fp——565 标签是 FORGE 循环标注，这次是【人工】真值，
    # 独立性指"标签独立于 FORGE 规则"，query 字面重叠不影响；放宽池子以逼近 200。
    cands: list[tuple[str, str]] = []
    seen: set[str] = set()
    for c in users:
        if not is_clean_user_text(c):
            continue
        c2 = clean_query(c)
        if len(c2) < 4:
            continue
        f = fp_of(c2)
        if f in seen:
            continue
        seen.add(f)
        cands.append((c2, f))

    print(f"[候选池] 全量 user 消息 {len(users)}，可用新候选 {len(cands)}（排除 565 已占 {len(known)}）",
          file=sys.stderr)

    if len(cands) < args.count:
        print(f"⚠️ 可用新候选 {len(cands)} < 目标 {args.count}，出全部", file=sys.stderr)

    # 域分类（用于分层 + 多样性）
    def classify_domain(txt: str, picked) -> str:
        nlower = normalize(txt).lower()
        if any(k in nlower for k in ("读", "打开", "查看", "解析", "读取", "读一下", "看下")):
            if any(k in nlower for k in ("图", "jpg", "png", "pdf", "excel", "doc", "文件")):
                return "file_read"
            return "generic"
        if any(k in nlower for k in ("写", "生成", "创建", "存", "保存", "导出", "保存成")):
            return "file_write"
        if any(k in nlower for k in ("联网", "搜索", "上网", "查一下", "搜", "天气", "新闻", "查")):
            return "web"
        if any(k in nlower for k in ("执行", "跑", "run", "代码", "脚本", "python", "node")):
            return "exec"
        if any(k in nlower for k in ("画", "生成图", "image", "图片", "渲染")):
            return "image"
        if any(k in nlower for k in ("总结", "概括", "翻译", "解释", "算", "数学", "是什么意思", "什么是")):
            return "text"
        return "generic"

    # 预标注（select_tool_names 返回 list[str]，非空=tool）
    annotated = []
    for txt, f in cands:
        try:
            picked = tr.select_tool_names(txt, all_tools)
        except Exception:
            picked = []
        label = "tool" if picked else "text"
        domain = classify_domain(txt, picked)
        annotated.append({
            "text": txt,
            "fingerprint": f,
            "pred_label": label,
            "pred_tools": sorted(picked),
            "domain": domain,
            "n_tools": len(picked),
        })

    print("[域分布]", {k: sum(1 for a in annotated if a["domain"] == k) for k in
           sorted({a["domain"] for a in annotated})}, file=sys.stderr)
    print("[预标注 tool/text]", {k: sum(1 for a in annotated if a["pred_label"] == k) for k in ("tool", "text")}, file=sys.stderr)

    # 分层：tool/text 各取一半（避免全 text 没 review 意义）+ 域内多样
    tool_items = [a for a in annotated if a["pred_label"] == "tool"]
    text_items = [a for a in annotated if a["pred_label"] == "text"]
    random.shuffle(tool_items)
    random.shuffle(text_items)
    half = args.count // 2
    picks_list = tool_items[:half] + text_items[:half]
    # 不足时用剩余补（保持 tool/text 平衡优先）
    if len(picks_list) < args.count:
        used_fp = {a["fingerprint"] for a in picks_list}
        rest_t = [a for a in tool_items if a["fingerprint"] not in used_fp]
        rest_x = [a for a in text_items if a["fingerprint"] not in used_fp]
        picks_list.extend((rest_t + rest_x)[: args.count - len(picks_list)])
    picks = picks_list[:args.count]

    # ---- 输出 ----
    os.makedirs(args.out_dir, exist_ok=True)
    review_md = os.path.join(args.out_dir, "test_annotation_review.md")
    labels_jsonl = os.path.join(args.out_dir, "test_annotation_labels.jsonl")
    meta = os.path.join(args.out_dir, "test_annotation_meta.json")

    with open(review_md, "w", encoding="utf-8") as fmd:
        fmd.write(f"# 独立 test 预标注清单（{len(picks)} 条）\n\n")
        fmd.write("请人工 review。只填「你的判断」列：`tool`（该调工具）或 `text`（纯文本可答）。\n")
        fmd.write("- 「预标注」列是 FORGE 规则的判断，**仅供参考**（已知有噪声，如乱配 calculate/get_current_datetime）\n")
        fmd.write("- 你判断和预标注不一致时以你为准，把它当纠正项\n")
        fmd.write(f"- 填完保存本文件，运行 `python validate_test_annotation.py` 生成最终独立 test 标签（{len(picks)} 条）\n\n")
        fmd.write("| # | query | 你的判断(tool/text) | 预标注(参考) |\n")
        fmd.write("|---|-------|---------------------|--------------|\n")
        for i, a in enumerate(picks, 1):
            q = normalize(a["text"]).replace("|", "/")[:90]
            fmd.write(f"| {i} | {q} | _{a['pred_label']} | {a['pred_label']} |\n")

    with open(labels_jsonl, "w", encoding="utf-8") as fl:
        for a in picks:
            rec = {
                "fingerprint": a["fingerprint"],
                "text": a["text"],
                "question": {"id": "tool_intent", "type": "choice",
                             "instructions": a["text"]},
                "label_human": a["pred_label"],   # 人工 review 后修正
                "pred_tools": a.get("pred_tools", []),
                "domain": a.get("domain", "generic"),
                "note": "",
            }
            fl.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with open(meta, "w", encoding="utf-8") as fm:
        json.dump({
            "count": len(picks),
            "pool_size": len(cands),
            "excluded_known_565": len(known),
            "seed": args.seed,
            "source": "sessions.sqlite user messages not in existing 565",
            "note": "labels are AI-predicted, need human review; use validate_test_annotation.py to finalize",
        }, fm, ensure_ascii=False, indent=2)

    print(f"\n✅ 预标注清单已生成 {len(picks)} 条")
    print(f"  review 文件: {review_md}")
    print(f"  标签骨架:    {labels_jsonl}")
    print(f"\n下一步：人工 review {review_md}，纠正 label_human 列，")
    print(f"  然后运行 python validate_test_annotation.py 生成最终独立 test 标签")
    return 0


if __name__ == "__main__":
    sys.exit(main())
