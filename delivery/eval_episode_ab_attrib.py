#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""episode 记忆注入 A/B 实验的归因分析器。

`eval_episode_ab.py` 只比对两份 evaluate 报告的聚合指标；本脚本多做一层：
因为 eval_runner 的注入是「按 case 是否有可召回 episode」决定的（留一法后
约 12/50 个 case 拿不到注入），这些 case 在 A/B 两组里处于**完全相同的实验条件**，
于是天然构成一个内置对照组 —— 它们的差异就是纯跑间噪声，可以拿来给
「注入组的变化」做显著性校准。

用法：
    python delivery/eval_episode_ab_attrib.py <runs_A_dir> <runs_B_dir>

输出：
    1) 注入覆盖情况
    2) 结果层(terminal state)翻转矩阵 —— 全组 / 注入组 / 内置对照组
    3) 工具序列发散率 —— 同为 evidence 的跑间不确定性测量
    4) 内置对照组的差异 = 噪声底线
    5) 结论：注入组的变化量是否超出噪声底线
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

N_EXPECTED = 50


# ---------------------------------------------------------------- loading

def _case_id(i: int) -> str:
    return f"T{i:03d}"


def load_runs(d: str) -> dict[str, dict]:
    root = Path(d)
    if not root.is_dir():
        raise SystemExit(f"找不到运行目录：{root}")
    out: dict[str, dict] = {}
    for i in range(1, N_EXPECTED + 1):
        cid = _case_id(i)
        p = root / f"{cid}.json"
        if not p.exists():
            continue
        out[cid] = json.loads(p.read_text(encoding="utf-8"))
    if not out:
        raise SystemExit(f"{root} 下没有任何 T*.json")
    return out


# ---------------------------------------------------------------- helpers

def terminal(d: dict) -> str:
    s = (d.get("final_state") or d.get("state") or "").strip()
    if s == "completed":
        return "completed"
    if s in ("waiting_user", "waiting_approval"):
        return "waiting"
    return "failed"


def kind(d: dict) -> str:
    return (d.get("terminal_kind") or "").strip() or "unknown"


def tools(d: dict) -> list[str]:
    seq: list[str] = []
    for t in (d.get("tool_calls") or []):
        seq.append(t.get("name") if isinstance(t, dict) else str(t))
    return seq


def injected_chars(d: dict) -> int:
    return len(d.get("episode_block") or "")


# ---------------------------------------------------------------- report

def _grp_report(label: str, grp: list[str], A: dict, B: dict) -> dict:
    flips = [c for c in grp if terminal(A[c]) != terminal(B[c])]
    tool_div = [c for c in grp if tools(A[c]) != tools(B[c])]
    text_same = sum(1 for c in grp if A[c].get("finalText") == B[c].get("finalText"))
    ca = Counter(terminal(A[c]) for c in grp)
    cb = Counter(terminal(B[c]) for c in grp)
    ta = sum(len(tools(A[c])) for c in grp)
    tb = sum(len(tools(B[c])) for c in grp)

    print(f"\n[{label}]  n={len(grp)}")
    print(f"  结果层翻转      : {len(flips)}/{len(grp)}"
          f"  ({len(flips) / len(grp) * 100:.0f}%)" if grp else "")
    print(f"  工具序列发散    : {len(tool_div)}/{len(grp)}"
          f"  ({len(tool_div) / len(grp) * 100:.0f}%)")
    print(f"  终稿文本完全一致: {text_same}/{len(grp)}"
          f"  ({text_same / len(grp) * 100:.0f}%)")
    print(f"  completed       : A={ca['completed']}  B={cb['completed']}"
          f"  ({cb['completed'] - ca['completed']:+d})")
    print(f"  failed          : A={ca['failed']}  B={cb['failed']}"
          f"  ({cb['failed'] - ca['failed']:+d})")
    print(f"  工具调用总数    : A={ta}  B={tb}  ({tb - ta:+d})")
    if flips:
        print("  翻转明细:")
        for c in flips:
            print(f"    {c}: {kind(A[c])} -> {kind(B[c])}"
                  f"   (inj={injected_chars(B[c])} chars)")
    return {
        "n": len(grp),
        "flips": len(flips),
        "tool_div": len(tool_div),
        "text_same": text_same,
        "completed_delta": cb["completed"] - ca["completed"],
        "failed_delta": cb["failed"] - ca["failed"],
        "tool_total_a": ta,
        "tool_total_b": tb,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    A = load_runs(argv[0])
    B = load_runs(argv[1])
    common = sorted(set(A) & set(B))
    if not common:
        raise SystemExit("两组没有重合的 case")

    inj = [c for c in common if B[c].get("episode_injected")]
    noi = [c for c in common if not B[c].get("episode_injected")]

    print("=" * 68)
    print("episode 记忆注入 —— 归因分析")
    print("=" * 68)
    print(f"重合 case        : {len(common)}")
    print(f"拿到注入 (处理组): {len(inj)}")
    print(f"无注入   (对照组): {len(noi)}  <- 两组实验条件相同，差异即纯噪声")
    if inj:
        sizes = [injected_chars(B[c]) for c in inj]
        print(f"注入块长度       : 平均 {sum(sizes) // len(sizes)} 字符，"
              f"最大 {max(sizes)}")

    all_stats = _grp_report("全组", common, A, B)
    inj_stats = _grp_report("处理组（拿到注入）", inj, A, B) if inj else {}
    noi_stats = _grp_report("对照组（无注入，纯噪声底线）", noi, A, B) if noi else {}

    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)

    if not noi_stats:
        print("  所有 case 都拿到了注入，没有内置对照组，无法估计噪声底线。")
        return 0

    # 噪声底线：对照组在完全相同条件下的翻转率 / 发散率
    noise_flip = noi_stats["flips"] / noi_stats["n"]
    noise_div = noi_stats["tool_div"] / noi_stats["n"]
    print(f"  噪声底线：结果层翻转 {noise_flip:.0%}、工具序列发散 {noise_div:.0%}")
    print("  —— 即便注入为零，同一份代码两次运行也会有这个量级的差异。")

    if inj_stats:
        print()
        print(f"  处理组 completed 变化 {inj_stats['completed_delta']:+d}"
              f" / n={inj_stats['n']}")
        print(f"  对照组 completed 变化 {noi_stats['completed_delta']:+d}"
              f" / n={noi_stats['n']}")
        d_inj = abs(inj_stats["completed_delta"])
        print()
        print("  判读：若 |处理组变化| 与对照组变化同量级（≤1~2 例），")
        print("        则本实验无法区分「注入有效」与「采样噪声」，")
        print("        需要重复实验把样本量提上去后再下结论。")
        print(f"        当前 |Δ|={d_inj}，对照组 |Δ|={abs(noi_stats['completed_delta'])}")

    # Safety：列出两组各自的 forbidden 工具执行
    print("\n  Safety 侧重点：")
    for tag, run in (("A", A), ("B", B)):
        hit = [c for c in common
               if "blocked_needs_user_input" not in str(run[c].get("blocked_reasons"))
               and run[c].get("terminal_kind") == "no_progress"]
        if hit:
            print(f"    [{tag}] no_progress（模型无行动）: {hit}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
