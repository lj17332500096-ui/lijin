# -*- coding: utf-8 -*-
"""探针：定位"工具调用审计缺口"——两条记录链为什么对不上。

背景（2026-09-20）：router-on 基线报告 `tool_truth.blocked = 0`，
而评测库 `tool_calls` 表里有 43 条 `status='blocked'`（收敛拦截 / 审批 / 次数上限）。
两个数字各自"正确"，却指向完全不同的结论 —— 必须查清谁在记、记的是什么。

本探针只读，输出四条对账：
  1. 基线（source = task_events.tool.invocation，执行层）状态分布
  2. 审计表（source = runtime/audit.py 解析 LLM 响应）状态分布
  3. 用 invocation_id 求交，量化"执行了但没进审计"的条数
  4. 审计表里 blocked 的真实原因分类

用法：
    python delivery/probe_audit_gap.py [db_path]
"""
from __future__ import annotations

import collections
import glob
import io
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "runs_eval_baseline_router_on")
DEFAULT_DB = r"C:\Users\Administrator\AppData\Local\Temp\forge_eval_1789831111.db"

# 审计表 status 的来源是"工具输出文本"，按文本特征归类拦截原因
BLOCK_SIGNATURES = [
    ("收敛拦截 CONVERGENCE", re.compile(r"CONVERGENCE")),
    ("硬终止 TERMINALIZE", re.compile(r"TERMINALIZE")),
    ("单 Run 执行上限", re.compile(r"达到单 Run 执行上限")),
    ("工具自身次数上限", re.compile(r"达到本任务上限")),
    ("权限审批", re.compile(r"需要审批")),
    ("重复调用护栏", re.compile(r"重复调用护栏")),
    ("冗余证据守卫", re.compile(r"已有证据")),
]


def classify(text: str) -> str:
    for label, pat in BLOCK_SIGNATURES:
        if pat.search(text or ""):
            return label
    return "（未归类）"


def main(argv: list[str]) -> int:
    db_path = argv[1] if len(argv) > 1 else DEFAULT_DB
    if not os.path.exists(db_path):
        print(f"找不到评测库：{db_path}")
        return 2

    # ---- 1. 基线（执行层口径）----
    base_ids: list[str] = []
    case_state: dict[str, str] = {}
    case_ncalls: dict[str, int] = {}
    empty_cases: list[str] = []
    st_base = collections.Counter()
    for f in sorted(glob.glob(os.path.join(BASE, "T*.json"))):
        d = json.load(io.open(f, encoding="utf-8"))
        cid = d.get("id")
        calls = d.get("tool_calls") or []
        case_state[cid] = d.get("final_state")
        case_ncalls[cid] = len(calls)
        if not calls:
            empty_cases.append(cid)
        for c in calls:
            st_base[c.get("status")] += 1
            if c.get("invocation_id"):
                base_ids.append(c["invocation_id"])

    print(f"库：{os.path.basename(db_path)}")
    print()
    print("=== ① 基线（task_events.tool.invocation，执行层）===")
    print(f"  case 数 {len(case_state)}，其中 tool_calls 为空 {len(empty_cases)} 个 "
          f"（纯回答/提问类，非缺口）")
    print(f"  调用总数 {sum(case_ncalls.values())}，status 分布 {dict(st_base)}")
    print()

    # ---- 2. 审计表 ----
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "select invocation_id, tool_name, status, result_excerpt from tool_calls"
    ).fetchall()
    st_audit = collections.Counter(r[2] for r in rows)
    print("=== ② 审计表 tool_calls（runtime/audit.py 解析 LLM 响应）===")
    print(f"  总行数 {len(rows)}，status 分布 {dict(st_audit)}")
    null_inv = sum(1 for r in rows if not r[0])
    print(f"  invocation_id 为空 {null_inv} 行（无法与基线对齐）")
    print()

    # ---- 3. 求交 ----
    audit_ids = {r[0] for r in rows if r[0]}
    base_set = set(base_ids)
    inter = base_set & audit_ids
    only_base = base_set - audit_ids
    only_audit = audit_ids - base_set
    print("=== ③ 用 invocation_id 对账 ===")
    print(f"  两边都有（精确对齐）  : {len(inter)}")
    print(f"  基线有、审计无        : {len(only_base)}   ← 执行了却没进审计表")
    print(f"  审计有、基线无        : {len(only_audit)}   ← 应为被拦截的调用（未进入执行）")
    print()
    over = sum(case_ncalls.values()) - len(inter)
    print(f"  → 基线执行 {sum(case_ncalls.values())} 次，其中 {over} 次在审计表里找不到")

    # 按 case 看"有执行但审计零记录"
    audit_by_case: dict[str, int] = collections.Counter()
    inv2case: dict[str, str] = {}
    for f in sorted(glob.glob(os.path.join(BASE, "T*.json"))):
        d = json.load(io.open(f, encoding="utf-8"))
        for c in d.get("tool_calls") or []:
            if c.get("invocation_id"):
                inv2case[c["invocation_id"]] = d["id"]
    for iid, *_ in rows:
        cid = inv2case.get(iid)
        if cid:
            audit_by_case[cid] += 1
    missing = [(cid, case_ncalls[cid], case_state[cid])
               for cid in case_ncalls
               if case_ncalls[cid] > 0 and audit_by_case.get(cid, 0) == 0]
    print(f"  有执行但审计零记录的 case：{len(missing)} 个")
    for cid, n, state in missing:
        print(f"    {cid}（执行 {n} 次，终态 {state}）")
    print()

    # ---- 4. 拦截原因分类 ----
    print("=== ④ 审计表 blocked 的真实原因 ===")
    cnt = collections.Counter()
    for iid, name, status, excerpt in rows:
        if status == "blocked":
            cnt[classify(excerpt)] += 1
    for k, v in cnt.most_common():
        print(f"  {k:<24} {v}")
    print()
    print("结论：审计表的 blocked 是**真拦截**，基线 blocked=0 是因为")
    print("      被拦的调用不产生 tool.invocation 事件 → 执行层口径看不见它们。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
