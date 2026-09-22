"""表脱节普查：找出全项目所有"把工具名写第二遍"的地方，并逐表判定状态。

背景（2026-09-19）：
  同一天内发现两例"两张表对不上"，但处置方式相反 ——
    ① code_loop_tool 错名  → 该「补」（把正确名字接进门槛集）
    ② gorden 4 条门槛词条  → 该「删」（旧设计遗留，且当前宽松行为被既有回归依赖）
  本探针不为判定"该补还是该删"给答案，只负责把**所有候选**列出来，
  因为实践两次都是「先改错方向、再被外部检查拦下」——所以先普查，再让检查说话。

用法:
  ./.venv/Scripts/python.exe delivery/probe_table_desync.py            # 全项目普查
  ./.venv/Scripts/python.exe delivery/probe_table_desync.py --detail   # 附每表逐条
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "logs", "runs_eval",
             "node_modules", ".workbuddy", "code_sandbox", "exports", "notes"}


# ---------------------------------------------------------------- 数据源

def registry_names() -> set[str]:
    import agent
    from runtime.registry import discover_from_agent
    return {b.spec.name for b in discover_from_agent(agent.assistant_agent).all()}


def skill_tool_names() -> dict[str, str]:
    """静态扫描 skills/*/tools.py 的函数名（不 import，避免副作用）。"""
    out: dict[str, str] = {}
    for d in sorted((ROOT / "skills").iterdir()):
        tp = d / "tools.py"
        if not (d.is_dir() and tp.is_file()):
            continue
        try:
            tree = ast.parse(tp.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = d.name
    return out


def term_table_keys() -> set[str]:
    from runtime import tool_router as tr
    return set(tr.TOOL_TERMS)


# ---------------------------------------------------------------- 扫描

def python_files() -> list[pathlib.Path]:
    files = []
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        files.append(p)
    return files


def scan_literals(files, vocab: set[str]) -> dict[str, dict[str, list[int]]]:
    """返回 {相对路径: {工具名: [行号...]}}，只统计**字符串字面量**里的精确匹配。

    刻意只认完整字符串字面量（'web_search' 而非 "web_search 工具"），
    因为这里要找的是"表"——声明式的名字清单，而不是散落在提示词里的提及。
    """
    pat = re.compile(r"""^["']([a-z][a-z0-9_]{2,})["']$""")
    hits: dict[str, dict[str, list[int]]] = {}
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        found: dict[str, list[int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                m = pat.match(repr(node.value).replace("\\'", "'").replace('\\"', '"'))
                name = node.value
                if m and name in vocab:
                    found.setdefault(name, []).append(node.lineno)
        if found:
            hits[str(p.relative_to(ROOT)).replace("\\", "/")] = found
    return hits


# ---------------------------------------------------------------- 判定

def classify(files_hits, reg: set[str], skill: dict[str, str], terms: set[str]):
    """给每个 (文件, 表) 打状态标签。

    只对**工具路由相关的表**做状态判定 —— 其它文件里出现工具名多为正常引用
    （如 broker 的审批策略按名字分组），需要人读语义，探针不越权下结论。
    """
    from runtime import tool_router as tr

    rows = []
    for f, found in sorted(files_hits.items()):
        names = set(found)
        unknown = names - reg
        rows.append({
            "file": f,
            "lines": sum(len(v) for v in found.values()) // max(1, len(found)) or 0,
            "names": names,
            "dead": sorted(unknown - set(skill)),
            "pending": sorted(unknown & set(skill)),
            "match": len(names & reg),
        })
    return rows


def router_tables() -> list[tuple[str, object]]:
    """tool_router 里所有"装工具名的容器"（模块级）。"""
    from runtime import tool_router as tr
    out = []
    for name, val in vars(tr).items():
        if name.startswith("__") or callable(val) or isinstance(val, type):
            continue
        if isinstance(val, (set, frozenset, list, tuple)):
            strs = [x for x in val if isinstance(x, str)]
            if strs and all(re.fullmatch(r"[a-z][a-z0-9_]{2,}", s) for s in strs):
                out.append((name, val))
        elif isinstance(val, dict):
            ks = [k for k in val if isinstance(k, str)]
            if ks and all(re.fullmatch(r"[a-z][a-z0-9_]{2,}", k) for k in ks):
                out.append((name, val))
    return out


def router_regexes():
    from runtime import tool_router as tr
    return [(n, v) for n, v in vars(tr).items()
            if hasattr(v, "pattern") and not callable(v)]


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(prog="probe_table_desync")
    ap.add_argument("--detail", action="store_true", help="逐条列出每表条目")
    args = ap.parse_args()

    reg = registry_names()
    skill = skill_tool_names()
    terms = term_table_keys()
    vocab = reg | set(skill) | terms

    print("=" * 88)
    print("表脱节普查")
    print("=" * 88)
    print(f"注册表工具            {len(reg)}")
    print(f"技能工具(静态扫描)    {len(skill)}  {sorted(set(skill.values()))}")
    print(f"TOOL_TERMS 条目       {len(terms)}")
    print(f"普查词表              {len(vocab)}")

    # ---- 1) 全项目字符串字面量里的工具名 ----
    hits = scan_literals(python_files(), vocab)
    print(f"\n【1】硬编码工具名的文件（共 {len(hits)} 个）")
    print(f"  {'文件':<52}{'不同工具名':>10}{'引用行':>8}")
    print("  " + "-" * 70)
    for f, found in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        lines = sum(len(v) for v in found.values())
        flag = ""
        dead = set(found) - reg - set(skill)
        if dead:
            flag = f"  ⚠️ 死引用 {len(dead)}"
        print(f"  {f:<52}{len(found):>10}{lines:>8}{flag}")

    # ---- 2) tool_router 内所有表的一致性 ----
    print(f"\n【2】tool_router 模块级表（共 {len(router_tables())} 张）")
    print(f"  {'表名':<26}{'条目':>5}{'⊆注册表':>8}{'死引用':>7}{'待启用':>7}   判定")
    print("  " + "-" * 84)
    dead_any = []
    for name, val in sorted(router_tables()):
        items = set(val) if not isinstance(val, dict) else set(val)
        in_reg = items & reg
        dead = sorted(items - reg - set(skill))
        pending = sorted((items - reg) & set(skill))
        verdict = "ok" if not dead else f"⚠️ {len(dead)} 个死引用"
        if dead:
            dead_any.append((name, dead))
        print(f"  {name:<26}{len(items):>5}{len(in_reg):>8}{len(dead):>7}"
              f"{len(pending):>7}   {verdict}")
        if args.detail:
            for d in dead:
                print(f"      ✗ {d}")
            for p in pending:
                print(f"      · {p}  (技能 {skill[p]}，未启用)")

    # ---- 3) 门槛表双向 ----
    from runtime import tool_router as tr
    hg = {k for k, _ in tr._HIGH_GATE_TERMS}
    ex = set(tr._EXPLICIT_ONLY_TOOLS)
    print("\n【3】门槛两张表双向对账")
    print(f"  _HIGH_GATE_TERMS     {len(hg)} 条")
    print(f"  _EXPLICIT_ONLY_TOOLS {len(ex)} 条")
    print(f"  只在词条表（词条永不求值 → 死数据）: {sorted(hg - ex) or '无 ✓'}")
    print(f"  只在生效集（_high_gate_hit 兜底 True → 同样没门槛）: {sorted(ex - hg) or '无 ✓'}")

    # ---- 4) 正则里提到的工具名 ----
    print("\n【4】正则中出现的工具名（意图族补齐路径）")
    for n, v in sorted(router_regexes()):
        found = sorted({t for t in vocab if t in v.pattern})
        if found:
            print(f"  {n:<28} 提到 {found}")

    print("\n" + "=" * 88)
    print("结论")
    print("=" * 88)
    if dead_any:
        print("  仍需处置的死引用：")
        for name, dead in dead_any:
            print(f"    {name}: {dead}")
    else:
        print("  tool_router 内无死引用 ✓")
    print("  ⚠️ 本探针只列证据，不给「补 or 删」的答案 ——")
    print("     该判定必须靠「改一下，看谁会红」取得，不能靠读表推断（两次都栽在这）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
