"""验证 C 组「recall_memory 零调用」的真实成因。

待判定的两个互斥假设
--------------------
H1（agent / 模型侧）：工具一直可见，是模型自己不想用 → 结论应归因于模型元认知。
H2（上下文模块侧）  ：工具压根没被派发给模型 → 结论应归因于 tool_router 的语义门。

判定方法：把 50 道评测题的 prompt 原样喂给 select_tool_names，
看 recall_memory 是否出现在返回的工具子集里。工具不在列表里 = 模型不可能调用它。

用法：
    python delivery/probe_recall_visibility.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark.cases import BENCHMARK_CASES  # noqa: E402
from runtime import tool_router as tr  # noqa: E402

# agent.py 注册的工具（节选，覆盖评测会用到的全部工具）
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

# 典型注入块的开头（模拟 B 组 prompt 前缀），用于对比"有了记忆块之后门会不会开"
INJECT_PREFIX = (
    "以下是你过去处理类似任务的历史经验（来自记忆库），仅供参考：\n"
    "- 上次做同类任务时用到了 read_workspace_file\n"
)


def main() -> int:
    print("=" * 68)
    print("recall_memory 可见性探针")
    print("=" * 68)
    print(f"router_enabled()   = {tr.router_enabled()}")
    print(f"router_fast_mode() = {tr.router_fast_mode()}")
    print(f"BASE_TOOLS         = {sorted(tr.BASE_TOOLS)}")
    print(f"recall_memory 在 BASE_TOOLS? {'recall_memory' in tr.BASE_TOOLS}")
    print(f"recall_memory 属门控集?      {'recall_memory' in tr._MEMORY_NOTE_TOOLS}")
    print()

    n = len(BENCHMARK_CASES)
    vis_plain: list[str] = []
    vis_inject: list[str] = []
    hit_plain: list[str] = []
    empty_router: list[str] = []

    for c in BENCHMARK_CASES:
        names = tr.select_tool_names(c.prompt, list(ALL_TOOLS))
        if not names:
            empty_router.append(c.id)
        if "recall_memory" in names:
            vis_plain.append(c.id)
        if tr._RECALL_INTENT.search(c.prompt or ""):
            hit_plain.append(c.id)
        names2 = tr.select_tool_names(INJECT_PREFIX + c.prompt, list(ALL_TOOLS))
        if "recall_memory" in names2:
            vis_inject.append(c.id)

    print(f"评测题总数                              = {n}")
    print(f"路由返回空工具集的题数                  = {len(empty_router)}")
    print()
    print("--- C 组场景（原样 prompt，无注入）---")
    print(f"  recall_memory 可见的题数              = {len(vis_plain)} / {n}")
    print(f"  命中 _RECALL_INTENT 语义门的题数      = {len(hit_plain)} / {n}")
    if vis_plain:
        print(f"  可见的题: {vis_plain}")
    print()
    print("--- B 组场景（prompt 前拼记忆注入块）---")
    print(f"  recall_memory 可见的题数              = {len(vis_inject)} / {n}")
    print()

    print("=" * 68)
    if not vis_plain and vis_inject:
        print("判定：H2 成立 —— 上下文模块（tool_router 语义门）的问题。")
        print("  C 组零调用不是「模型不想用」，是工具根本没派发，模型看不见。")
        print("  B 组能调用，是因为注入块自带「记忆/历史经验」等词，把语义门撞开了。")
        print("  → 报告 §5.8「模型不会自发使用记忆」的结论不成立，需修正。")
    elif vis_plain:
        print("判定：H1 成立 —— 工具一直可见，零调用应归因于模型侧。")
    else:
        print("判定：两组都不可见，需进一步排查。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
