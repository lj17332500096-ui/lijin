"""定位 B 组 13 次 recall_memory 调用的真实触发点。

上一支探针（probe_recall_visibility.py）已证明：C 组 50 题里 recall_memory 可见 0 题。
但 B 组实测有 13 次 executed 调用，分布在 8 个 case。本脚本用**真实注入块**
（episode_recall.build_context 的原样输出）复算，找出究竟是哪个词撞开了语义门。

用法：
    python delivery/probe_recall_gate.py
"""

from __future__ import annotations

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

# B 组实测出现 recall_memory 调用的 8 个 case
HIT_CASES = ["T013", "T015", "T027", "T031", "T042", "T044", "T045", "T048"]

by_id = {c.id: c for c in BENCHMARK_CASES}


def main() -> int:
    print("=" * 70)
    print("B 组 recall_memory 门控触发点定位（真实注入块）")
    print("=" * 70)

    opened = 0
    for cid in HIT_CASES:
        c = by_id.get(cid)
        if c is None:
            print(f"{cid}: 不在评测集里，跳过")
            continue
        plain = tr.select_tool_names(c.prompt, list(ALL_TOOLS))
        block = build_context(c.prompt) or ""
        if not block:
            print(f"{cid}: 注入块为空（该题无召回），跳过")
            continue
        injected = tr.select_tool_names(block + "\n" + c.prompt, list(ALL_TOOLS))
        m = tr._RECALL_INTENT.search(block)
        before = "recall_memory" in plain
        after = "recall_memory" in injected
        if after and not before:
            opened += 1
        print(f"{cid}: 原样可见={before}  注入后可见={after}  "
              f"命中词={m.group() if m else None!r}")

    print()
    print(f"注入块把门撞开的 case 数 = {opened} / {len(HIT_CASES)}")
    print()
    print("--- 全量复核：注入块对 50 题的整体影响 ---")
    n_open = 0
    for c in BENCHMARK_CASES:
        plain = tr.select_tool_names(c.prompt, list(ALL_TOOLS))
        block = build_context(c.prompt) or ""
        if not block or "recall_memory" in plain:
            continue
        inj = tr.select_tool_names(block + "\n" + c.prompt, list(ALL_TOOLS))
        if "recall_memory" in inj:
            n_open += 1
    print(f"  原本不可见、注入后可见的题数 = {n_open} / {len(BENCHMARK_CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
