# -*- coding: utf-8 -*-
"""探针：Phase 11「同目标重复调用护栏」在真实基线里到底拦住了什么？

背景：REPAIR_PLAN 的后续调研发现 runtime/runner.py:761 已实现
per-target 重复护栏（repeat_target_cap=3，FORGE_REPEAT_GUARD 默认 on），
但 runs_eval_baseline_router_on/ 里 T014 出现 list_workspace_files 被
调用 15 次且 15 次全部 executed（tool_truth.attempts=25, blocked=0）。

本探针只读基线产物，不改代码。回答两个问题：
  Q1 失败 case 的空转是不是「同工具 + 同结果」的重复？
  Q2 重复次数是否达到/超过 cap=3（即护栏本该拦却没拦）？

注意：基线产物未保存 arguments，因此用 result_excerpt 规范化后的指纹
作为「同一目标」的近似。指纹只用于聚合统计，不作为拦截判据。
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "runs_eval_baseline_router_on")

CAP = 3  # runtime/readiness_gate.py ProgressTracker.repeat_target_cap

# 只关心会空转的只读类工具（mutation 不在护栏范围内）
WATCH = {
    "list_workspace_files", "list_code_files", "list_notes",
    "search_documents", "search_sources", "web_search",
    "read_workspace_file", "read_code_file", "read_note",
    "index_workspace", "recall_memory", "list_sandbox_snapshots",
}

_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s，。；、）)]+")
_WS_RE = re.compile(r".*my_creative_agent[\\/]", re.I)


def normalize(text: str) -> str:
    """结果指纹。

    ⚠️ 教训：早期版本用 `<PATH>` 掩码 + 数字掩码，会把「找不到目录 A」和
    「找不到目录 B」归并成同一指纹，从而**高估**重复（实测把 19 次互不相同的
    目录列举报成同一目标 7 次）。这里只剥离 workspace 根前缀、统一分隔符，
    保留具体路径与数字 —— 对 list/read 类工具而言，路径本身就是「目标身份」。
    """
    s = (text or "").strip()
    s = _WS_RE.sub("", s)
    s = s.replace("\\", "/")
    return s[:200]


def load_cases():
    files = sorted(glob.glob(os.path.join(BASE, "T*.json")))
    out = []
    for f in files:
        try:
            out.append(json.load(io.open(f, encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 无法解析 {os.path.basename(f)}: {exc}", file=sys.stderr)
    return out


def main() -> int:
    if not os.path.isdir(BASE):
        print(f"找不到基线目录：{BASE}")
        return 2

    cases = load_cases()
    print(f"基线 case 数：{len(cases)}")
    print(f"护栏 cap = {CAP}（runtime/readiness_gate.py: repeat_target_cap）")
    print()

    total_calls = 0
    total_blocked = 0
    over_cap_examples = []      # 同目标调用 >= CAP
    wasted_calls = 0            # 同目标重复调用中的「多余」次数
    per_case_rows = []

    for d in cases:
        cid = d.get("id")
        calls = d.get("tool_calls") or []
        truth = d.get("tool_truth") or {}
        state = d.get("final_state")
        total_calls += len(calls)
        total_blocked += int(truth.get("blocked") or 0)

        # (tool, fingerprint) -> 次数
        groups = Counter()
        for c in calls:
            name = c.get("name") or "?"
            if name not in WATCH:
                continue
            groups[(name, normalize(c.get("result_excerpt") or ""))] += 1

        repeat_groups = {k: v for k, v in groups.items() if v >= 2}
        over = {k: v for k, v in groups.items() if v >= CAP}
        extra = sum(v - 1 for v in repeat_groups.values())
        wasted_calls += extra

        if over:
            over_cap_examples.append((cid, state, over, len(calls)))
        if len(calls) >= 5:
            execs = d.get("_exec") or {}
            per_case_rows.append((cid, state, len(calls), extra, execs))

    print("=== Q2：同目标重复达到 cap 的 case（护栏若生效应在第 3 次拦下）===")
    if not over_cap_examples:
        print("（无）—— 说明重复都发生在不同目标上，护栏设计覆盖不到")
    for cid, state, over, n in sorted(over_cap_examples, key=lambda r: -max(r[2].values())):
        head = sorted(over.items(), key=lambda kv: -kv[1])[:3]
        desc = "; ".join(f"{k[0]}×{v}" for k, v in head)
        print(f"  {cid} [{state}] 总调用 {n} | 同目标重复: {desc}")
    print()

    print("=== 按 case 看空转（调用数 >=5）===")
    print(f"{'case':<6}{'state':<10}{'calls':>6}{'冗余':>6}  工具分布")
    for cid, state, n, extra, execs in sorted(per_case_rows, key=lambda r: -r[3])[:20]:
        dist = ", ".join(f"{k}={v}" for k, v in
                         sorted(execs.items(), key=lambda kv: -kv[1])[:5])
        print(f"{cid:<6}{state:<10}{n:>6}{extra:>6}  {dist}")
    print()

    print("=== 汇总 ===")
    print(f"总工具调用        : {total_calls}")
    print(f"运行时 blocked 统计: {total_blocked}   （tool_truth.blocked 之和）")
    print(f"其中重复目标造成的冗余调用: {wasted_calls}")
    print(f"达到 cap 的目标组数: {sum(len(o) for _, _, o, _ in over_cap_examples)}")
    ratio = (wasted_calls / total_calls * 100) if total_calls else 0.0
    print(f"冗余占比          : {ratio:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
