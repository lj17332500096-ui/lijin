"""量化 code_loop 错名的真实影响方向。

背景：`_EXPLICIT_ONLY_TOOLS` 是"高门槛"表 —— 在表内的工具必须命中语义才放行，
不在表内的工具 `_allowed()` 直接 return True。所以 `code_loop_tool` 这个错名
导致的**不是**"无法派发"，而是"本该受门槛保护的自主循环工具一直无门槛放行"。

本探针用 monkeypatch 复原旧行为（把 code_loop 从两张表里摘掉），
在 50 道评测题上对比工具集差异，给出真实影响面。

用法：
    ./.venv/Scripts/python.exe delivery/probe_code_loop_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    import agent
    from benchmark.cases import BENCHMARK_CASES
    from runtime import tool_router as tr
    from runtime.registry import discover_from_agent

    names = [b.spec.name for b in discover_from_agent(agent.assistant_agent).all()]

    fixed = {q: set(tr.select_tool_names(q, names)) for q in (c.prompt for c in BENCHMARK_CASES)}

    # 复原旧行为：注册名 code_loop 不在任何一张表里（= 错名状态）
    old_explicit = set(tr._EXPLICIT_ONLY_TOOLS) - {"code_loop"}
    old_gate = tuple(x for x in tr._HIGH_GATE_TERMS if x[0] != "code_loop")
    tr._EXPLICIT_ONLY_TOOLS = old_explicit
    tr._HIGH_GATE_TERMS = old_gate
    legacy = {q: set(tr.select_tool_names(q, names)) for q in (c.prompt for c in BENCHMARK_CASES)}

    # 恢复
    tr._EXPLICIT_ONLY_TOOLS = old_explicit | {"code_loop"}
    tr._HIGH_GATE_TERMS = old_gate + (("code_loop", ("修复", "代码修复", "code_loop")),)

    n_diff = 0
    recovered = []
    for q in fixed:
        if fixed[q] != legacy[q]:
            n_diff += 1
            if "code_loop" in legacy[q] and "code_loop" not in fixed[q]:
                recovered.append(q)
    total = len(fixed)
    print(f"共 {total} 题")
    print(f"修复前后工具集不同的题：{n_diff}/{total}")
    print(f"其中「旧行为放行 code_loop、修复后被门槛拦住」：{len(recovered)} 题")
    for q in recovered:
        print(f"   - {q[:52]}")
    only = [q for q in fixed if "code_loop" in fixed[q]]
    print(f"\n修复后仍能派发 code_loop 的题：{len(only)}/{total}")
    for q in only:
        print(f"   + {q[:52]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
