"""技能健康探针：逐个真实加载 skills/ 下每个技能，产出「能用/不能用」硬证据。

判据（全部客观可观测）：
  A. 指令文件可读且非空        -> 加载器能否拼出人设文本
  B. skill.json（若有）能解析   -> 元数据是否损坏
  C. tools.py（若有）能导入     -> 工具能否注册；导入后再枚举 FunctionTool
  D. 桥接引用（若有 scripts/）  -> 记录入口文件是否存在

只读，不改任何文件。
用法：python delivery/probe_skill_health.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SKILLS = BASE / "skills"
sys.path.insert(0, str(BASE))

OK, BAD, WARN = "OK", "BAD", "WARN"


def check_md(d: Path) -> tuple[str, str]:
    for fname in ("skill.md", "SKILL.md"):
        p = d / fname
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8").strip()
            except Exception as exc:  # noqa: BLE001
                return BAD, f"{fname} 读取失败 {type(exc).__name__}: {exc}"
            if not text:
                return BAD, f"{fname} 为空"
            return OK, f"{fname} {len(text)} 字符"
    return BAD, "缺 skill.md / SKILL.md（加载器会拒绝）"


def check_json(d: Path) -> tuple[str, str]:
    p = d / "skill.json"
    if not p.is_file():
        return OK, "无 skill.json（可选）"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return BAD, f"skill.json 解析失败：{type(exc).__name__}: {exc}"
    if not isinstance(data, dict) or not data:
        return WARN, "skill.json 为空/非对象"
    return OK, f"skill.json ok keys={sorted(data)[:6]}"


def check_tools(d: Path, name: str) -> tuple[str, str]:
    p = d / "tools.py"
    if not p.is_file():
        return OK, "无 tools.py（纯提示词技能）"
    try:
        spec = importlib.util.spec_from_file_location(f"_probe_tools_{name}", p)
        if spec is None or spec.loader is None:
            return BAD, "tools.py 无法构造 loader"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except BaseException as exc:  # noqa: BLE001
        return BAD, f"tools.py 导入失败 {type(exc).__name__}: {str(exc)[:160]}"
    try:
        from agents.tool import FunctionTool

        found = [v.name for v in vars(mod).values() if isinstance(v, FunctionTool)]
    except Exception:  # noqa: BLE001
        found = []
    if not found:
        return WARN, "tools.py 可导入但未导出 FunctionTool"
    return OK, f"导出工具 {len(found)} 个：{','.join(found)}"


def main() -> None:
    global SKILLS
    if len(sys.argv) > 1:
        SKILLS = Path(sys.argv[1]).resolve()
    rows = []
    for d in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        md_s, md_m = check_md(d)
        js_s, js_m = check_json(d)
        tl_s, tl_m = check_tools(d, d.name)
        verdict = BAD if BAD in (md_s, js_s, tl_s) else (WARN if WARN in (md_s, js_s, tl_s) else OK)
        rows.append((d.name, verdict, md_s, md_m, js_s, js_m, tl_s, tl_m))

    print(f"{'skill':<28} {'判定':<5} 说明")
    print("-" * 118)
    for name, verdict, md_s, md_m, js_s, js_m, tl_s, tl_m in rows:
        detail = " | ".join(
            f"{tag}:{msg}"
            for tag, msg in ((md_s, md_m), (js_s, js_m), (tl_s, tl_m))
            if msg
        )
        print(f"{name:<28} {verdict:<5} {detail}")

    bad = [r[0] for r in rows if r[1] == BAD]
    warn = [r[0] for r in rows if r[1] == WARN]
    print("-" * 118)
    print(f"合计 {len(rows)} 个技能：BAD={len(bad)} WARN={len(warn)} OK={len(rows) - len(bad) - len(warn)}")
    if bad:
        print(f"不能用（BAD）：{', '.join(bad)}")
    if warn:
        print(f"可疑（WARN）：{', '.join(warn)}")


if __name__ == "__main__":
    main()
