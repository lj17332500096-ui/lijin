"""P1-B(2)：read_workspace_file per-tool cap 单测。

锁定 _DEFAULT_PER_TOOL_BUDGETS 给 read_workspace_file 设 cap=8，
can_execute_tool 在第 9 次拒绝（防 13 次 read 碎片撞 TOOL_BUDGET_TOTAL=20）。
调整 cap 用 TOOL_BUDGET_PER_TOOL=read_workspace_file=N 覆盖。
"""

import os

import pytest


def test_read_cap_default_is_8():
    """read_workspace_file 默认 per-tool cap=8（P1-B(2) 防碎片堆叠）。"""
    from runtime.runctx import _DEFAULT_PER_TOOL_BUDGETS

    assert _DEFAULT_PER_TOOL_BUDGETS.get("read_workspace_file") == 8


def test_read_cap_rejects_ninth_call():
    """第 9 次 read 被拒，前 8 次放行。"""
    from runtime.runctx import RunContext

    r = RunContext()
    for i in range(1, 9):
        ok, _ = r.can_execute_tool("read_workspace_file")
        assert ok, f"第 {i} 次 read 应放行"
    ok, key = r.can_execute_tool("read_workspace_file")
    assert not ok
    assert "read_workspace_file" in key and "8 次" in key


def test_read_cap_env_override(monkeypatch):
    """TOOL_BUDGET_PER_TOOL=read_workspace_file=3 可覆盖默认 cap=8。"""
    from runtime.runctx import RunContext

    monkeypatch.setenv("TOOL_BUDGET_PER_TOOL", "read_workspace_file=3")
    r = RunContext()
    for i in range(1, 4):
        ok, _ = r.can_execute_tool("read_workspace_file")
        assert ok, f"第 {i} 次（cap=3）应放行"
    ok, key = r.can_execute_tool("read_workspace_file")
    assert not ok
    assert "read_workspace_file" in key and "3 次" in key


def test_run_tests_not_in_per_tool_table():
    """run_tests 不在 per-tool 表，走总预算（不受 cap 限制）。"""
    from runtime.runctx import RunContext, _DEFAULT_PER_TOOL_BUDGETS

    assert "run_tests" not in _DEFAULT_PER_TOOL_BUDGETS
    r = RunContext()
    ok, _ = r.can_execute_tool("run_tests")
    assert ok  # 总预算（20）未超，放行


def test_read_cap_does_not_block_other_tools():
    """read 达 cap 不影响 list/search 等其他工具。"""
    from runtime.runctx import RunContext

    r = RunContext()
    for _ in range(8):
        r.can_execute_tool("read_workspace_file")
    # read 第 9 次被拒
    ok, _ = r.can_execute_tool("read_workspace_file")
    assert not ok
    # 但 list 仍放行
    ok2, _ = r.can_execute_tool("list_workspace_files")
    assert ok2
