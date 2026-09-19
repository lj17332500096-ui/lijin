# -*- coding: utf-8 -*-
"""探针：用评测库的 arguments_json 精确定位 Phase 11 重复护栏为何 0 触发。

前置：delivery/probe_repeat_guard_effect.py 只能看 result_excerpt，无法还原
「目标身份」（同工具不同目录会被误并）。本探针改用评测库 tool_calls.arguments_json
+ invocation_id 与 runs_eval_baseline_router_on/T*.json 逐调用对齐，然后直接调用
runtime.readiness_gate 里**生产同款**的 repeat_target_key，得到真实重复分布。

用法：
    python delivery/probe_repeat_guard_db.py [db_path]
默认 db = %TEMP%/forge_eval_1789831111.db（= 2026-09-19 23:19~23:39 那轮 router-on）
"""
from __future__ import annotations

import glob
import io
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from runtime.readiness_gate import DiscoveryTracker  # noqa: E402

BASE = os.path.join(ROOT, "runs_eval_baseline_router_on")
DEFAULT_DB = r"C:\Users\Administrator\AppData\Local\Temp\forge_eval_1789831111.db"

# runner.py:761 的准入条件（护栏实际生效范围）
GUARD_SCOPE_READ = {"read_workspace_file", "read_code_file", "read_note",
                    "run_python", "code_loop", "run_tests", "recall_memory"}


def discovery_class(name: str) -> bool:
    from runtime.readiness_gate import is_discovery_class_tool
    return is_discovery_class_tool(name)


def main(argv: list[str]) -> int:
    db_path = argv[1] if len(argv) > 1 else DEFAULT_DB
    if not os.path.exists(db_path):
        print(f"找不到评测库：{db_path}")
        return 2

    # --- 1. 基线 case 的调用序列（含 invocation_id）---
    case_calls: dict[str, list[dict]] = {}
    for f in sorted(glob.glob(os.path.join(BASE, "T*.json"))):
        d = json.load(io.open(f, encoding="utf-8"))
        case_calls[d.get("id")] = d.get("tool_calls") or []

    inv2case: dict[str, str] = {}
    for cid, calls in case_calls.items():
        for c in calls:
            iid = c.get("invocation_id")
            if iid:
                inv2case[iid] = cid

    # --- 2. 库里的真实 arguments ---
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "select invocation_id, tool_name, arguments_json, status from tool_calls order by id"
    ).fetchall()

    def parse_args(raw):
        """解出真实工具参数。

        ⚠️ 失败 run 的参数由 runtime/runner.py:_backfill_failure_audit 兜底写入，
        形态是 {"ledger": "<真实 args 的字符串>"}（见 runner.py:2207）—— 顶层键
        只有 ledger 时须再解一层，否则所有基于参数的判定都会落空。
        """
        if not raw:
            return {}
        try:
            v = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
        if isinstance(v, dict):
            if set(v.keys()) == {"ledger"} and isinstance(v["ledger"], str):
                try:
                    inner = json.loads(v["ledger"])
                    if isinstance(inner, dict):
                        return inner
                except Exception:  # noqa: BLE001
                    return {}
            return v
        return {}

    # case -> [(name, args, status)]
    seq: dict[str, list[tuple]] = defaultdict(list)
    matched = 0
    for iid, name, raw, status in rows:
        cid = inv2case.get(iid)
        if cid is None:
            continue
        matched += 1
        seq[cid].append((name, parse_args(raw), status))

    print(f"库：{os.path.basename(db_path)}")
    print(f"tool_calls 总行 {len(rows)}，按 invocation_id 对齐到基线 {matched} 条")
    total_calls = sum(len(v) for v in case_calls.values())
    print(f"基线记录的调用数 {total_calls}")
    if matched < total_calls:
        print(f"[注意] {total_calls - matched} 条未对齐（库可能被截断或 case 未写库）")
    print()

    tracker = DiscoveryTracker()
    cap = tracker.repeat_target_cap
    print(f"护栏 cap = {cap}")
    print()

    # --- 3. 逐 case 用生产函数算真实目标重复 ---
    findings = []
    total_over = 0
    total_redundant = 0
    scope_miss = Counter()   # 不在护栏范围内的工具 -> 次数

    for cid in sorted(seq):
        calls = seq[cid]
        t = DiscoveryTracker()
        per_key: dict[str, list[int]] = defaultdict(list)
        for idx, (name, args, status) in enumerate(calls, 1):
            in_scope = discovery_class(name) or name in GUARD_SCOPE_READ
            key = t.repeat_target_key(name, args)
            if key is None:
                if not in_scope:
                    scope_miss[name] += 1
                continue
            per_key[key].append(idx)
            t.note_repeat_target(name, args)

        over = {k: v for k, v in per_key.items() if len(v) >= cap}
        # 第 cap 次之后的调用数 = 护栏若生效应拦下的次数
        redundant = sum(len(v) - cap for v in over.values())
        if over:
            total_over += len(over)
            total_redundant += redundant
            findings.append((cid, len(calls), over, redundant))

    print("=== 真实目标重复（用生产 repeat_target_key 计算）===")
    if not findings:
        print("（无目标达到 cap —— 护栏逻辑上也不该触发，需要看别的机制）")
    for cid, n, over, red in sorted(findings, key=lambda r: -r[3]):
        head = sorted(over.items(), key=lambda kv: -len(kv[1]))[:3]
        desc = "; ".join(f"{k}×{len(v)}" for k, v in head)
        print(f"  {cid} 调用{n:>3}  达标目标{len(over):>2}  应拦{red:>2}  | {desc}")
    print()

    print("=== 汇总 ===")
    print(f"达到 cap 的目标组数      : {total_over}")
    print(f"护栏若生效应拦下的调用数 : {total_redundant}")
    print(f"这些调用实际全部执行     : {total_redundant}（运行时 blocked 统计 = 0）")
    print()

    # ---- 4. 反事实：per-tool 预算（治"参数漂移"，与上面的按目标护栏互补）----
    # 按 case 还原调用序列（基线 json 的顺序 = 真实执行顺序），
    # 用生产 RunContext.can_execute_tool 重放，数出会被独立预算拦下的次数。
    from runtime.runctx import RunContext

    order: dict[str, list[tuple]] = {}
    inv2seq: dict[str, int] = {}
    for cid in sorted(case_calls):
        for c in case_calls[cid]:
            if c.get("invocation_id"):
                inv2seq[c["invocation_id"]] = len(order.setdefault(cid, []))
                order[cid].append(None)
    for iid, name, raw, status in rows:
        cid = inv2case.get(iid)
        if cid is None:
            continue
        idx = inv2seq.get(iid)
        if idx is not None:
            order[cid][idx] = (name, parse_args(raw))

    print("=== 反事实：per-tool 独立预算（本次新增，治「参数漂移」）===")
    total_budget = 0
    detail = []
    for cid in sorted(order):
        rc = RunContext(run_id=cid)
        blocked = 0
        for item in order[cid]:
            if item is None:
                continue
            ok, _reason = rc.can_execute_tool(item[0])
            if not ok:
                blocked += 1
        if blocked:
            detail.append((cid, blocked, len(order[cid])))
            total_budget += blocked
    for cid, blocked, total in sorted(detail, key=lambda r: -r[1]):
        print(f"  {cid} 会被拦 {blocked} 次（该 case 可对齐调用 {total} 次）")
    print(f"  合计会拦下 {total_budget} 次")
    print()
    print("  ⚠️ 与上面的按目标护栏**有重叠**（同一调用可能两边都命中），")
    print("     真实总拦截数须实跑确认；此处只用于判断「改动是否有效」。")
    print()
    if scope_miss:
        print("=== 不在护栏范围、目标无法解析的工具（设计缺口候选）===")
        for k, v in scope_miss.most_common():
            print(f"  {k}: {v} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
