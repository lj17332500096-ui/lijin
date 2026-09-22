"""工具清单探针：从真实 ToolRegistry 拉出全部工具、来源、schema 规模。

用途：回答"现在有多少个工具、分别是什么、有没有冗余"这类问题，
避免凭记忆报数字。只读，不改任何状态。

用法：
    ./.venv/Scripts/python.exe delivery/probe_tool_inventory.py
    ./.venv/Scripts/python.exe delivery/probe_tool_inventory.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def collect() -> dict:
    import agent  # noqa: F401  —— 导入即触发 skills_loader 收集
    from runtime.registry import discover_from_agent

    reg = discover_from_agent(agent.assistant_agent)
    rows = []
    for b in reg.all():
        spec = b.spec
        schema = getattr(spec, "input_schema", None) or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        rows.append({
            "name": spec.name,
            "source": getattr(spec, "source", "native"),
            "desc_len": len(getattr(spec, "description", "") or ""),
            "params": list(props.keys()),
            "n_required": len(required),
        })
    rows.sort(key=lambda r: (r["source"], r["name"]))
    return {"total": len(rows), "tools": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="probe_tool_inventory")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = ap.parse_args(argv)

    from runtime import tool_router as tr

    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    by_source: dict[str, list[dict]] = {}
    for r in data["tools"]:
        by_source.setdefault(r["source"], []).append(r)

    print(f"总计 {data['total']} 个工具；"
          f"TOOL_ROUTER={'off' if not tr.router_enabled() else 'on'}\n")
    for src in ("native", "skill", "mcp"):
        items = by_source.get(src, [])
        if not items:
            continue
        print(f"===== 来源 {src}（{len(items)} 个）=====")
        for r in items:
            mark = " [BASE]" if r["name"] in tr.BASE_TOOLS else ""
            print(f"  {r['name']:<28} 参数{len(r['params']):>2} 必填{r['n_required']:>2} "
                  f"描述{r['desc_len']:>4}字{mark}")
        print()

    # 路由可见性：50 道评测题里各工具被派发的次数
    try:
        from benchmark.cases import BENCHMARK_CASES
        all_names = [r["name"] for r in data["tools"]]
        hits: dict[str, int] = {n: 0 for n in all_names}
        n_tools_per_case = []
        for c in BENCHMARK_CASES:
            sel = tr.select_tool_names(c.prompt, all_names)
            n_tools_per_case.append(len(sel))
            for n in sel:
                hits[n] = hits.get(n, 0) + 1
        print("===== 在 50 道评测题上的路由可见性 =====")
        print(f"每道题平均派发 {sum(n_tools_per_case)/max(1,len(n_tools_per_case)):.1f} 个工具 "
              f"（min {min(n_tools_per_case)}, max {max(n_tools_per_case)}）\n")
        for n, cnt in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0])):
            rate = cnt / max(1, len(BENCHMARK_CASES)) * 100
            print(f"  {n:<28} 可见 {cnt:>2}/50 ({rate:>3.0f}%)")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 路由可见性统计失败：{exc!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
