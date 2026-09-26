"""算术口径翻转：把命中算术正则的 tool 标签翻转为 text（一律 text，不分口算/执行）。

背景：用户 2026-09-25 拍板「所有命中算术正则的一律 text」，把 5000 数据里
算术类样本的标注从 tool 翻转为 text（纯口算与需执行都算 text，模型直接答）。
本脚本就地翻转 tool_routing_5000_{train,val,test}.jsonl（扁平源），
然后提示重跑 adapt_5000_to_laya.py 生成新的嵌套 565 文件。

翻转规则（正则命中即翻）：
  arith_pat = 数字 的 数字 % / 数字 运算符 数字 / 除以 / 乘以 / 加 / 减 / 百分之 / 数字 %

翻转后分布变化（原 5000 严格 2500:2500 → 新 train 偏 text，val/test 亦偏 text）。
这是**口径切换**，不是标注错误修正——必须三集同步翻转 + 重训 + 重评，保持同一把尺子。

用法：
  .venv/Scripts/python.exe flip_arith_to_text.py --apply   # 就地翻转
  .venv/Scripts/python.exe flip_arith_to_text.py --dry-run # 只统计不写
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from collections import Counter

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "data" / "laya_tool_intent"

# 与之前扫描口径完全一致的算术正则（命中即翻 text）
ARITH = re.compile(
    r"(\d+\s*的\s*\d+\s*%|\d+\s*[/\-*+]\s*\d+|\d+\s*%\s*\d*"
    r"|\d+\s*除以\s*\d+|\d+\s*乘以\s*\d+|\d+\s*加\s*\d+|\d+\s*减\s*\d+"
    r"|百分之\s*\d+|\d+\s*%)",
    re.IGNORECASE,
)


def flip_file(path: Path, apply: bool) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    stat = Counter()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        q = c.get("query", "")
        lab = c.get("label")
        is_arith = bool(ARITH.search(q)) if q else False
        if lab == "tool" and is_arith:
            c["label"] = "text"
            c["tool"] = "-"  # 翻 text 后 tool 字段无意义，清掉
            stat["flipped"] += 1
            stat["label_after"] += 1
        if lab == "text" and is_arith:
            stat["text_already"] += 1
        out.append(c)
    if apply:
        with path.open("w", encoding="utf-8") as f:
            for c in out:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
    dist_after = Counter(c.get("label") for c in out)
    stat["total"] = len(out)
    stat["dist_after"] = dict(dist_after)
    return dict(stat)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="就地写回")
    ap.add_argument("--dry-run", action="store_true", help="只统计")
    args = ap.parse_args()
    apply = args.apply
    if not (apply or args.dry_run):
        apply = False  # 默认 dry-run

    for name in ("train", "val", "test"):
        p = OUT_DIR / f"tool_routing_5000_{name}.jsonl"
        if not p.exists():
            print(f"[skip] {p.name} 不存在")
            continue
        stat = flip_file(p, apply=apply)
        mode = "APPLIED" if apply else "DRY-RUN"
        print(f"[{mode}] {p.name}: 翻转 tool→text {stat.get('flipped',0)} | "
              f"已是 text {stat.get('text_already',0)} | 总量 {stat.get('total',0)} | "
              f"翻转后分布 {stat.get('dist_after')}")

    if apply:
        print("\n[flip] 三集翻转完成。下一步：")
        print("  .venv/Scripts/python.exe adapt_5000_to_laya.py   # 重新生成嵌套 565 文件")
        print("  .venv/Scripts/python.exe train_laya_forge.py --train --device xpu --epochs 3 ...")


if __name__ == "__main__":
    main()
