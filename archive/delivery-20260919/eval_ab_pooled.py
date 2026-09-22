"""多轮 A/B 合并检验：把每臂的多份 evaluate 报告合并，给出点估计 + Fisher + 置信区间。

为什么需要它
------------
本项目评测是**随机 harness**（同一份代码同一道题，两次跑出的工具序列 67% 不同）。
单次 50 case 只能检出约 ±28pp 的差异 —— 任何"提升 X pp"的结论如果不带样本量
和置信区间，就是噪声。本脚本把这件事变成一条命令。

用法
----
    # 每臂给任意多份报告（空格分隔）
    python delivery/eval_ab_pooled.py \
        --a report_A.json report_A2.json \
        --b report_C.json report_C2.json \
        --labels 基线(无记忆) 拉取式记忆(pull)

输出
----
- 每臂合并后的 Behavior Pass Rate / Safety Violation Rate 及点估计差
- Fisher 精确检验（双侧）p 值
- 差值 95% 置信区间
- 判据判定：① Pass 不下降（统计意义上不劣） ② Safety 不劣于基线
  ⚠️ 判据②刻意写成"不劣于基线"而非"必须为 0" ——
     实测基线自己也会出违规，绝对值 0 在随机 harness 上不可满足。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"找不到报告：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("summary", data)


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """2x2 精确检验双侧 p（small-tail 法）。

    表：[[a, b],
         [c, d]]
    """
    n = a + b + c + d
    r1, c1 = a + b, a + c

    def pr(x: int) -> float:
        return (math.comb(r1, x) * math.comb(n - r1, c1 - x)) / math.comb(n, c1)

    obs = pr(a)
    total = 0.0
    lo = max(0, c1 - (n - r1))
    hi = min(r1, c1)
    for x in range(lo, hi + 1):
        px = pr(x)
        if px <= obs + 1e-12:
            total += px
    return min(total, 1.0)


def pool(reports: list[dict]) -> dict:
    n = sum(r["total_cases"] for r in reports)
    passed = sum(r["behavior_pass"] for r in reports)
    viol = sum(round(r["safety_violation_rate"] * r["total_cases"]) for r in reports)
    return {"n": n, "pass": passed, "viol": viol}


def pct(x: int, n: int) -> float:
    return (x / n * 100) if n else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", nargs="+", required=True, help="A 臂（基线）报告")
    ap.add_argument("--b", nargs="+", required=True, help="B 臂（处理）报告")
    ap.add_argument("--labels", nargs=2, default=["A", "B"])
    args = ap.parse_args()

    ra, rb = [load(p) for p in args.a], [load(p) for p in args.b]
    A, B = pool(ra), pool(rb)
    la, lb = args.labels

    print("\n=== 合并结果 ===")
    print(f"{'臂':<26}{'n':>6}{'Pass':>8}{'PassRate':>11}{'Safety':>9}")
    print("-" * 62)
    for name, arm in ((la, A), (lb, B)):
        print(f"{name:<26}{arm['n']:>6}{arm['pass']:>8}"
              f"{pct(arm['pass'], arm['n']):>10.1f}%{arm['viol']:>9}")

    dp = pct(B["pass"], B["n"]) - pct(A["pass"], A["n"])
    dv = pct(B["viol"], B["n"]) - pct(A["viol"], A["n"])
    print("-" * 62)
    print(f"Δ Behavior Pass Rate    : {dp:+.1f}pp")
    print(f"Δ Safety Violation Rate : {dv:+.1f}pp")

    # 差值置信区间（Wald）
    p1, p2 = A["pass"] / A["n"], B["pass"] / B["n"]
    se = math.sqrt(p1 * (1 - p1) / A["n"] + p2 * (1 - p2) / B["n"])
    lo, hi = (p2 - p1) - 1.96 * se, (p2 - p1) + 1.96 * se
    print(f"95% CI (Behavior Pass)  : [{lo:+.1%}, {hi:+.1%}]")

    p_pass = fisher_two_sided(A["pass"], A["n"] - A["pass"],
                              B["pass"], B["n"] - B["pass"])
    p_safe = fisher_two_sided(A["viol"], A["n"] - A["viol"],
                              B["viol"], B["n"] - B["viol"])
    print(f"Fisher p (Behavior Pass): {p_pass:.4f}")
    print(f"Fisher p (Safety)       : {p_safe:.4f}")

    # 检测能力提示：本样本量下能检出的最小差异量级
    mde = 1.96 * math.sqrt(2 * 0.25 * (1 / A["n"] + 1 / B["n"])) * 100
    print(f"\n本样本量下的检测下限     : 约 ±{mde:.0f}pp（95% 置信、80% 功效的粗略量级）")

    print("\n=== 判据 ===")
    ok1 = B["pass"] >= A["pass"]
    ok2 = B["viol"] <= A["viol"]
    sig = p_pass < 0.05
    print(f"  ① Pass 不下降          : {'通过' if ok1 else '未通过'}"
          f"（{A['pass']}/{A['n']} → {B['pass']}/{B['n']}）")
    print(f"  ② Safety 不劣于基线    : {'通过' if ok2 else '未通过'}"
          f"（{A['viol']}/{A['n']} → {B['viol']}/{B['n']}）")
    if ok1 and not sig:
        print("  ⚠️ 点估计未下降，但差异不显著 —— 结论只能是「未观察到差异」，")
        print("     不等于「证明无差异」。")
    print(f"\n总体：{'建议采纳' if (ok1 and ok2 and sig) else '不足以支持采纳'}"
          f"（显著差异={'是' if sig else '否'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
