"""工具冗余对账探针：注册表 × 路由规则 × 生产调用 三方比对。

回答三个问题：
  1. 注册表里有哪些工具、来自哪里；
  2. 哪些工具"存在但从不派发 / 从不被调用"；
  3. 路由规则里有没有指向"已不存在工具"的死条目。

只读：数据库以 mode=ro 打开，不写任何状态。

用法：
    ./.venv/Scripts/python.exe delivery/probe_tool_redundancy.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "agent.db"


def registry_names() -> dict[str, str]:
    import agent
    from runtime.registry import discover_from_agent
    reg = discover_from_agent(agent.assistant_agent)
    return {b.spec.name: getattr(b.spec, "source", "native") for b in reg.all()}


def router_referenced() -> dict[str, set[str]]:
    """直接读 tool_router 模块里的真实对象，不做源码文本正则猜测。

    返回 {表名: 该表引用的工具名集合}。只用模块真实属性，
    避免把 MCP 关键词同义表（键是 issue/repo 之类）误当成工具名。
    """
    from runtime import tool_router as tr

    tables: dict[str, set[str]] = {}
    for attr in ("TOOL_TERMS",):
        obj = getattr(tr, attr, None)
        if isinstance(obj, dict):
            tables[attr] = set(obj.keys())
    for attr in (
        "BASE_TOOLS", "_CODING_SUPPORT", "_WEB_SUPPORT", "_MEMORY_NOTE_SUPPORT",
        "_DOC_READ_SUPPORT", "_ANALYSIS_SUPPORT", "_ARITH_SUPPORT",
        "_MEMORY_NOTE_TOOLS", "_EXPLICIT_ONLY_TOOLS", "_CAPABILITY_EXPLORE_TOOLS",
        "_WRITE_TOOLS_SET",
    ):
        obj = getattr(tr, attr, None)
        if isinstance(obj, (set, frozenset)):
            tables[attr] = set(obj)
    return tables


def prod_calls() -> dict[str, tuple[int, str, str]]:
    """tool_name -> (调用次数, 最后一次时间, 最后一次状态)。"""
    if not DB.exists():
        return {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("select tool_name, count(*), max(created_at) from tool_calls group by tool_name")
        out = {n: (c, t or "", "") for n, c, t in cur.fetchall()}
        cur.execute(
            "select tool_name, status from tool_calls t1 where created_at = "
            "(select max(created_at) from tool_calls t2 where t2.tool_name = t1.tool_name)",
        )
        for n, st in cur.fetchall():
            if n in out:
                out[n] = (out[n][0], out[n][1], st or "")
        return out
    finally:
        con.close()


# final JSON 的 kind 字段曾被误记为工具名，这里显式排除
NOT_A_TOOL = {"answer", "questions", "done", "note", "kind", "ui"}


def main() -> int:
    from runtime import tool_router as tr

    reg = registry_names()
    tables = router_referenced()
    ref_all: set[str] = set().union(*tables.values()) if tables else set()
    dead_refs = ref_all - set(reg)
    calls = prod_calls()
    called = {n for n in calls if n not in NOT_A_TOOL}

    print("=" * 68)
    print(f"注册表：{len(reg)} 个工具    TOOL_ROUTER={'off' if not tr.router_enabled() else 'on'}")
    src_cnt: dict[str, int] = {}
    for s in reg.values():
        src_cnt[s] = src_cnt.get(s, 0) + 1
    print(f"来源分布：{src_cnt}")
    print("=" * 68)

    never_called = sorted(set(reg) - called)
    print(f"\n【A】注册了但生产从未调用（{len(never_called)} 个）")
    for n in never_called:
        print(f"   {n}")
    ghost = sorted(called - set(reg))
    print(f"\n【B】生产调用过但现已不在注册表（{len(ghost)} 个，历史残留）")
    for n in ghost:
        c, t, st = calls[n]
        print(f"   {n:<26} 调用{c:>4} 次  最后 {t[:19]}  {st}")

    print(f"\n【C】路由表引用但注册表里不存在（{len(dead_refs)} 个，死引用）")
    for n in sorted(dead_refs):
        where = [t for t, s in tables.items() if n in s]
        print(f"   {n:<26} 出现于 {', '.join(where)}")
    print("\n【C2】各路由表规模")
    for t, s in tables.items():
        miss = sorted(s - set(reg))
        print(f"   {t:<24} 引用 {len(s):>3} 个，其中 {len(miss)} 个不存在")
        if miss:
            print(f"        不存在：{', '.join(miss)}")

    print("\n【D】生产调用频次（降序，前 20）")
    for n, (c, t, _st) in sorted(
        ((k, v) for k, v in calls.items() if k not in NOT_A_TOOL),
        key=lambda kv: -kv[1][0],
    )[:20]:
        tag = "" if n in reg else "  ← 已下线"
        print(f"   {n:<26}{c:>5} 次  最后 {t[:19]}{tag}")

    print("\n【E】命名同族（可能是同功能多入口，需人工判定）")
    families: dict[str, list[str]] = {}
    for n in sorted(reg):
        fam = n.split("_")[0]
        families.setdefault(fam, []).append(n)
    for fam, items in sorted(families.items()):
        if len(items) > 1:
            print(f"   {fam:<10} -> {', '.join(items)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
