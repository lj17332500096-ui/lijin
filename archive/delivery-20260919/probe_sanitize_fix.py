"""验证修复方案：注入块脱敏后，工具集能否完全恢复成与基线一致。

混淆的成因
----------
tool_router 规则 1「查询里出现工具名 → 必选」会被注入块里的
「路径：list_workspace_files → read_workspace_file」撞中，
导致注入组的工具集与基线组系统性不同（实测 46/46 题都不同，平均 +9 个工具）。

修复思路
--------
在评测层拼接前，把注入块里的**工具名**替换成中文动作词（保留语义、去掉字面量）。
若脱敏后 select_tool_names(sanitized + prompt) == select_tool_names(prompt)，
则混淆被彻底消除，A/B 才可比。

用法：
    python delivery/probe_sanitize_fix.py
"""

from __future__ import annotations

import re
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

#: 工具名 → 中文动作词（保留"做了什么"的语义，去掉可被路由命中的字面量）
ALIAS = {
    "read_workspace_file": "读取文件",
    "list_workspace_files": "列举文件",
    "search_documents": "检索文档",
    "index_workspace": "建立索引",
    "web_search": "联网检索",
    "get_current_datetime": "取当前时间",
    "run_python": "执行脚本",
    "run_tests": "运行测试",
    "write_code_file": "写代码文件",
    "read_code_file": "读代码文件",
    "list_code_files": "列举代码文件",
    "edit_project_file": "编辑项目文件",
    "write_project_file": "写项目文件",
    "recall_memory": "查历史记忆",
    "remember": "写入记忆",
    "forget_memory": "删除记忆",
    "save_note": "保存笔记",
    "read_note": "读笔记",
    "list_notes": "列笔记",
    "schedule_add": "新建日程",
    "schedule_list": "列日程",
    "schedule_remove": "删日程",
    "schedule_set_enabled": "开关日程",
    "ask_image": "看图",
    "deep_research": "深度调研",
    "fetch_github_repo": "抓取仓库",
    "codex_loop_tool": "代码修复循环",
    "sandbox_snapshot": "建快照",
    "sandbox_rollback": "回滚快照",
    "list_sandbox_snapshots": "列快照",
}

_TOOLNAME_RE = re.compile(
    "|".join(re.escape(n) for n in sorted(ALIAS, key=len, reverse=True))
)


def sanitize(block: str) -> str:
    """把注入块里的工具名替换为中文动作词。"""
    return _TOOLNAME_RE.sub(lambda m: ALIAS[m.group()], block)


def main() -> int:
    print("=" * 72)
    print("注入块脱敏修复验证")
    print("=" * 72)

    n_block = 0
    fixed_same = 0
    still_diff = []
    leftover_names = 0

    for c in BENCHMARK_CASES:
        block = build_context(c.prompt) or ""
        if not block:
            continue
        n_block += 1
        plain = set(tr.select_tool_names(c.prompt, list(ALL_TOOLS)))
        raw = set(tr.select_tool_names(block + "\n" + c.prompt, list(ALL_TOOLS)))
        clean_block = sanitize(block)
        if any(t in clean_block for t in ALL_TOOLS):
            leftover_names += 1
        clean = set(tr.select_tool_names(clean_block + "\n" + c.prompt, list(ALL_TOOLS)))
        if clean == plain:
            fixed_same += 1
        else:
            still_diff.append((c.id, sorted(plain - clean), sorted(clean - plain)))

    print(f"有注入块的题数                     = {n_block}")
    print(f"脱敏后工具集与基线**完全一致**的题 = {fixed_same} / {n_block}"
          f"  ({fixed_same / max(1, n_block) * 100:.0f}%)")
    print(f"脱敏后仍残留工具名的题             = {leftover_names}")
    print()
    if still_diff:
        print("--- 仍不一致的题（前 8）---")
        for cid, lost, gained in still_diff[:8]:
            print(f"  {cid}: 少={lost}  多={gained}")
    print()
    print("--- 脱敏效果示例 ---")
    for c in BENCHMARK_CASES:
        block = build_context(c.prompt) or ""
        if block and "→" in block:
            print(f"原块片段: {block[block.find('路径'):block.find('路径') + 60]}")
            sb = sanitize(block)
            print(f"脱敏片段: {sb[sb.find('路径'):sb.find('路径') + 60]}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
