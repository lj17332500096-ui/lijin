"""量化 B 组实验的混淆变量：注入块本身改变了工具可用集。

背景
----
tool_router 的第 1 条规则是「查询里出现工具名 → 必选」，
而 episode 注入块会以「路径：a → b → c」的形式列出历史用过的工具名。
于是 B 组的模型看到的工具列表，比 A 组多出一批**由注入块自带名字带进来的**工具。

如果混淆成立，那么 A/B 两组的差异就不能归因于"记忆内容"——
两组跑的其实是两套不同的工具集。

用法：
    python delivery/probe_injection_confound.py
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark.cases import BENCHMARK_CASES  # noqa: E402
from runtime import tool_router as tr  # noqa: E402
from runtime.episode_recall import build_context  # noqa: E402

ALL_TOOLS = [
    "save_note", "read_note", "list_notes", "web_search",
    "get_current_datetime", "read_workspace_file", "list_workspace_files",
    "calculate", "remember", "recall_memory", "forget_memory",
    "index_workspace", "search_documents", "schedule_add", "schedule_list",
    "schedule_remove", "schedule_set_enabled", "ask_image", "deep_research",
    "fetch_github_repo", "write_code_file", "read_code_file",
    "list_code_files", "run_python", "run_tests", "codex_loop_tool",
    "sandbox_snapshot", "sandbox_rollback", "list_sandbox_snapshots",
]


def main() -> int:
    print("=" * 72)
    print("注入块对工具集的混淆量化")
    print("=" * 72)

    total_injected = 0
    changed = 0
    added_counter: collections.Counter = collections.Counter()
    removed_counter: collections.Counter = collections.Counter()
    delta_sizes: list[int] = []
    rows: list[tuple[str, int, int, list[str]]] = []

    for c in BENCHMARK_CASES:
        plain = tr.select_tool_names(c.prompt, list(ALL_TOOLS))
        block = build_context(c.prompt) or ""
        if not block:
            continue
        total_injected += 1
        inj = tr.select_tool_names(block + "\n" + c.prompt, list(ALL_TOOLS))
        ps, js = set(plain), set(inj)
        if ps != js:
            changed += 1
        added = sorted(js - ps)
        removed = sorted(ps - js)
        added_counter.update(added)
        removed_counter.update(removed)
        delta_sizes.append(len(added))
        rows.append((c.id, len(plain), len(inj), added))

    print(f"拿到注入块的题数                 = {total_injected} / {len(BENCHMARK_CASES)}")
    print(f"工具集发生变化的题数             = {changed} / {total_injected}"
          f"  ({changed / max(1, total_injected) * 100:.0f}%)")
    if delta_sizes:
        print(f"平均新增工具数                   = {sum(delta_sizes) / len(delta_sizes):.1f}")
        print(f"最多新增工具数                   = {max(delta_sizes)}")
    print()
    print("--- 被注入块『带进来』的工具排行 ---")
    for name, cnt in added_counter.most_common(12):
        print(f"  {name:<26} {cnt:>3} 题")
    print()
    print("--- 被注入块『挤出去』的工具（max_tools 截断）---")
    for name, cnt in removed_counter.most_common(8):
        print(f"  {name:<26} {cnt:>3} 题")
    print()
    print("--- 变化最大的 8 道题 ---")
    for cid, lp, li, added in sorted(rows, key=lambda r: -len(r[3]))[:8]:
        print(f"  {cid}: {lp} → {li} 工具  新增={added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
