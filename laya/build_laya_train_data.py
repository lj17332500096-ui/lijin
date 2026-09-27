#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 FORGE 真实日志 + 真实会话构造 Laya 工具意图二分类微调训练数据.

数据源 (双源合并, 去重后按 ts 8:1:1 切分):
  1. logs/tool_router.jsonl    已带 intent_class 标注 (8547 tool / 9471 ambiguous 等, 4 天 18975 行)
  2. sessions.sqlite            710 个 session / 888 条 user 消息 (16 天), 无 intent_class 标注
                                用 FORGE 真实 tool_router.select_tool_names 反向标注:
                                返回 [] → text, 非空 → tool (ground truth = 规则引擎自身输出,
                                零偏、可复现、不依赖 Laya; 副作用 _write_tool_router_log/record_router_call
                                在脚本里 stub 掉, 不污染生产日志)

设计 (对齐 Laya 微调 notebook laya_finetune_typed_decisions_2xT4_kaggle.ipynb 的数据 schema):
  每个样本 = 一个 "case":
    {id, workflow, state(JSON 字符串), questions(JSON 字符串), gold(JSON 字符串)}
  - state       = 用户 query 原文 (字符串)
  - questions   = {"needs_tool": {"type":"choice","instructions":..., "criteria":{"tool":..., "text":...}}}
  - gold        = {"needs_tool": {"label":"tool"|"text", "probabilities":{...}}}
  - workflow    = "forge_tool_intent"

二分类标签映射:
  source=tool_router.jsonl:
    tool_needed -> tool,  greeting/arith/ambiguous -> text
  source=sessions.sqlite (FORGE 规则反向标注):
    select_tool_names(query) 返回 [] -> text (is_direct_text_task 命中), 非空 -> tool

数据清洗:
  - 剔除空 query / 坏 JSON
  - 同一 query 去重: 同 query 在多源/多条记录里出现时, 取**最权威**的标注
    (tool_router.jsonl 的 intent_class 优先; sessions.sqlite 反向标注次之)
  - 按 ts 时序切分 8:1:1 (train/val/test), 同 query 不会跨 split 出现 (防泄漏)

输出 (data/laya_tool_intent/):
  train.jsonl / val.jsonl / test.jsonl   Laya 微调 case 格式
  manifest.json                          数据卡: 双源/清洗/类别分布/切分统计/语言分布
"""
import json
import sqlite3
import collections
import datetime
import pathlib
import sys
from typing import Any, Dict, List

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from runtime_paths import LOG_DIR, state_db_path  # noqa: E402

LOG = LOG_DIR / "tool_router.jsonl"
DB = state_db_path("sessions.sqlite", env_vars=("FORGE_SESSIONS_DB",),
                   legacy_path=REPO / "sessions.sqlite")
OUT = ROOT / "data" / "laya_tool_intent"
OUT.mkdir(parents=True, exist_ok=True)

# 二分类问题定义 (训练/推理时 questions 字段都用这一份, 保证 schema 一致)
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

# tool_router.jsonl 的 intent_class → 二分类 label
INTENT_TO_LABEL = {
    "tool_needed": "tool",
    "greeting": "text",
    "arith": "text",
    "ambiguous": "text",
}


# ------------------------------------------------------------------ 数据源 1: tool_router.jsonl
def load_log(path: pathlib.Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"_bad": True, "_line": i})
    return rows


# ------------------------------------------------------------------ 数据源 2: sessions.sqlite
def _build_tool_router_stub():
    """stub 掉 tool_router 的副作用 (日志 + 统计), 让 select_tool_names 可安全离线调用."""
    import runtime.tool_router as tr
    tr._write_tool_router_log = lambda *a, **k: None
    tr.record_router_call = lambda *a, **k: None
    return tr


def _load_tool_names() -> List[str]:
    """离线拿 FORGE 真实 37 工具名 (从全局 assistant_agent.tools, 不触发 guardrail)."""
    import agent as _agent_mod
    tools = getattr(_agent_mod.assistant_agent, "tools", []) or []
    return [t.name for t in tools]


def load_sessions() -> List[Dict[str, Any]]:
    """从 sessions.sqlite 读 888 条 user 消息, 用 FORGE 真实 tool_router 反向标注.

    返回: 与 tool_router.jsonl 同 schema 的 case 列表 (query/intent_class/ts/selected/hit_zero)
    """
    if not DB.exists():
        return []
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    rows = cur.execute(
        "SELECT session_id, message_data, created_at FROM agent_messages "
        "WHERE json_extract(message_data, '$.role') = 'user' ORDER BY created_at"
    ).fetchall()
    con.close()

    tr = _build_tool_router_stub()
    tool_names = _load_tool_names()
    if not tool_names:
        print(f"[WARN] 拿不到真实工具名 (assistant_agent.tools 为空), sessions 源跳过")
        return []

    out: List[Dict[str, Any]] = []
    for sid, mdata, ts in rows:
        try:
            md = json.loads(mdata) if isinstance(mdata, str) else mdata
        except (json.JSONDecodeError, TypeError):
            continue
        # user 消息的 content 可能是字符串, 也可能是 [{"type":"output_text","text":...}]
        content = md.get("content", "")
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    parts.append(c.get("text", ""))
                else:
                    parts.append(str(c))
            content = "".join(parts)
        if not isinstance(content, str) or not content.strip():
            continue
        # FORGE 真实路由反向标注: 返回 [] => text, 非空 => tool
        try:
            selected = tr.select_tool_names(content, tool_names)
        except Exception:
            continue
        intent = "tool_needed" if selected else "ambiguous"
        out.append({
            "query": content,
            "intent_class": intent,
            "ts": ts,
            "selected": selected,
            "hit_zero": (not selected),
            "_source": "sessions.sqlite",
        })
    return out


# ------------------------------------------------------------------ 合并 + 去重
def merge_sources(log_rows: List[Dict[str, Any]], sess_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """合并两源, 同 query 去重: tool_router.jsonl 的 intent_class 权威, sessions.sqlite 补充.

    返回 {query_text: case}, case 含 _source 字段标记来源.
    """
    dedup: Dict[str, Dict[str, Any]] = {}
    dropped_empty = 0
    dropped_bad = 0

    # 先收 sessions.sqlite (无 intent_class 权威标注, 但量大)
    for o in sess_rows:
        q = o.get("query")
        if not isinstance(q, str) or not q.strip():
            dropped_empty += 1
            continue
        dedup[q] = {
            "query": q,
            "intent_class": o.get("intent_class"),
            "ts": o.get("ts"),
            "selected": o.get("selected", []),
            "hit_zero": o.get("hit_zero"),
            "_source": "sessions.sqlite",
        }

    # 再收 tool_router.jsonl (同 query 覆盖 => 权威 intent_class 胜出)
    for o in log_rows:
        if o.get("_bad"):
            dropped_bad += 1
            continue
        q = o.get("query")
        if not isinstance(q, str) or not q.strip():
            dropped_empty += 1
            continue
        dedup[q] = {
            "query": q,
            "intent_class": o.get("intent_class"),
            "ts": o.get("ts"),
            "selected": o.get("selected", []),
            "hit_zero": o.get("hit_zero"),
            "_source": "tool_router.jsonl",
        }
    return dedup, {"dropped_empty": dropped_empty, "dropped_bad": dropped_bad}


def case_to_laya_item(cid: int, case: Dict[str, Any]) -> Dict[str, Any]:
    label = INTENT_TO_LABEL.get(case["intent_class"], "text")
    return {
        "id": f"f{cid:07d}",
        "workflow": "forge_tool_intent",
        "state": json.dumps(case["query"], ensure_ascii=False),
        "questions": json.dumps({"needs_tool": NEEDS_TOOL_QUESTION}, ensure_ascii=False),
        "gold": json.dumps(
            {
                "needs_tool": {
                    "label": label,
                    "probabilities": {"tool": 1.0 if label == "tool" else 0.0,
                                      "text": 0.0 if label == "tool" else 1.0},
                }
            },
            ensure_ascii=False,
        ),
        "intent_class_original": case["intent_class"],
        "selected_tools": case["selected"],
        "ts": case["ts"],
        "source": case.get("_source", "unknown"),
    }


def language_bucket(text: str) -> str:
    zh = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ratio = zh / max(1, len(text))
    if ratio > 0.5:
        return "zh_heavy"
    if ratio == 0:
        return "latin_heavy"
    return "mixed"


def main() -> None:
    log_rows = load_log(LOG)
    sess_rows = load_sessions()
    dedup, dropped = merge_sources(log_rows, sess_rows)
    cases = list(dedup.values())
    src_stat = collections.Counter(c["_source"] for c in cases)

    def sort_key(c: Dict[str, Any]):
        return (c.get("ts") or "", c["query"])

    cases.sort(key=sort_key)
    n = len(cases)
    n_test = max(1, round(n / 10))
    n_val = max(1, round(n / 10))
    train_cases = cases[: n - n_val - n_test]
    val_cases = cases[n - n_val - n_test: n - n_test]
    test_cases = cases[n - n_test:]

    splits = {"train": train_cases, "val": val_cases, "test": test_cases}
    lang_stat = {"zh_heavy": 0, "mixed": 0, "latin_heavy": 0}
    label_stat = {"tool": 0, "text": 0}
    intent_stat: Dict[str, int] = collections.Counter()
    source_split: Dict[str, Dict[str, int]] = {"train": {}, "val": {}, "test": {}}
    per_split_meta = {}

    for sname, scases in splits.items():
        items = []
        for i, c in enumerate(scases, start=1):
            items.append(case_to_laya_item(i, c))
        with open(OUT / f"{sname}.jsonl", "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        per_split_meta[sname] = len(items)
        for it in items:
            gold_parsed = json.loads(it["gold"])
            label_stat[gold_parsed["needs_tool"]["label"]] += 1
            intent_stat[it["intent_class_original"]] += 1
            lang_stat[language_bucket(it["state"].strip('"'))] += 1
            source_split[sname][it["source"]] = source_split[sname].get(it["source"], 0) + 1

    manifest = {
        "source": [
            "logs/tool_router.jsonl (schema_version=2, 已带 intent_class)",
            "sessions.sqlite (888 user 消息, FORGE tool_router 反向标注, 副作用 stub)",
        ],
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "raw_rows": {"tool_router.jsonl": len(log_rows), "sessions.sqlite": len(sess_rows)},
        "cleaned": {
            "unique_queries": len(dedup),
            "dropped_empty_query": dropped["dropped_empty"],
            "dropped_bad_json": dropped["dropped_bad"],
        },
        "source_split": src_stat,
        "label_mapping": INTENT_TO_LABEL,
        "label_distribution": label_stat,
        "tool_ratio": round(label_stat["tool"] / n, 4) if n else 0,
        "intent_class_distribution": dict(intent_stat),
        "language_distribution": lang_stat,
        "source_per_split": source_split,
        "split_sizes": per_split_meta,
        "split_ratio": "8:1:1 by ts (train/val/test), dedup before split, no query overlap across splits",
        "dedup_rule": "同 query 多源/多条记录, tool_router.jsonl 的 intent_class 权威覆盖 sessions.sqlite 反向标注",
        "schema_note": (
            "Laya fine-tune case format (state/questions/gold as JSON strings); "
            "questions=needs_tool choice with criteria {tool,text}; "
            "see notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb"
        ),
        "recommended_checkpoint": "convaiinnovations/laya-multilingual (57%+ zh_heavy)",
        "gold_target": ">=0.95 accuracy on 'needs_tool' choice; temperature refit 0.2~0.5",
    }
    with open(OUT / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("=== Laya 工具意图二分类训练数据构造完成 (双源) ===")
    print(f"raw: tool_router.jsonl={len(log_rows)}, sessions.sqlite={len(sess_rows)}")
    print(f"-> unique queries: {len(dedup)}  (dropped empty: {dropped['dropped_empty']}, bad: {dropped['dropped_bad']})")
    print(f"source split: {dict(src_stat)}")
    print(f"splits: {per_split_meta}")
    print(f"label: {label_stat}  (tool/total = {label_stat['tool']}/{n} = {label_stat['tool']/n:.1%})")
    print(f"intent_class: {dict(intent_stat)}")
    print(f"language: {lang_stat}")
    print(f"source per split: {source_split}")
    print(f"manifest: {OUT/'manifest.json'}")
    print(f"files: {sorted(p.name for p in OUT.glob('*.jsonl'))}")


if __name__ == "__main__":
    main()
