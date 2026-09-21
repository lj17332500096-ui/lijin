"""P1-B-C：coding 不收敛专项——decision_hint 三态特化 + CONVERGENCE 分支拼接。

背景：T038/T023/T024/T049 在 CONVERGENCE 分支被拦下时只拿到通用
CONVERGENCE_REACHED_TEXT（"不要再探索、直接作答"），但 coding 场景下模型真正需要
的是**下一步该干什么**——已修改未验证→run_tests / 验证未通过→修失败点 /
验证已通过→收口。decision_hint 早已实现（readiness_gate.py:1073）但从未被 runner
调用（孤儿功能）。本测试锁定三态提示 + saturated 才非 None 的行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.readiness_gate import DiscoveryTracker  # noqa: E402


def _make_saturated_tracker() -> DiscoveryTracker:
    """构造一个"已 saturated"的 tracker：post_mutation_discovery=3。"""
    t = DiscoveryTracker()
    t.mutation_seen = True
    t.post_mutation_discovery = 3  # 触发 saturated
    return t


def test_hint_none_when_not_saturated():
    t = DiscoveryTracker()
    assert t.decision_hint() is None, "未 saturated 时 decision_hint 应为 None"


def test_hint_pass_when_verification_passed():
    """验证已通过 → 直接收口，不要再改代码（治 T038/T049）。"""
    t = _make_saturated_tracker()
    t.verification_seen = True
    t.verification_passed = True
    hint = t.decision_hint()
    assert hint is not None
    assert "收口" in hint or "不要再探索或改代码" in hint, f"应提示收口：{hint!r}"
    assert "验证已通过" in hint


def test_hint_fix_when_verification_failed():
    """已修改且验证未通过 → 修失败点，不要盲目探索（治 T023/T024）。"""
    t = _make_saturated_tracker()
    t.verification_seen = True
    t.verification_passed = False
    hint = t.decision_hint()
    assert hint is not None
    assert "修" in hint and "失败点" in hint, f"应提示修失败点：{hint!r}"
    assert "盲目" not in hint or "不再探索" in hint


def test_hint_run_when_mutated_not_verified():
    """已修改未验证 → 下一步 run_tests（治 coding 不收敛根因）。"""
    t = _make_saturated_tracker()
    t.mutation_seen = True
    t.verification_seen = False
    t.verification_passed = False
    hint = t.decision_hint()
    assert hint is not None
    assert "run_tests" in hint, f"应提示运行验证：{hint!r}"
    assert "已修改但未验证" in hint


def test_hint_explore_when_no_mutation():
    """无 mutation 场景 → 原行为（继续探索需明确未解决问题）。"""
    t = _make_saturated_tracker()
    t.mutation_seen = False
    t.verification_seen = False
    t.verification_passed = False
    hint = t.decision_hint()
    assert hint is not None
    assert "继续探索需要明确的未解决问题" in hint, f"应保留原行为：{hint!r}"


def test_hint_header_contains_state():
    """三态提示前都要带【执行状态】事实行（让模型看到当前 mutation/verification 状态）。"""
    for seen, passed in [(False, False), (True, False), (True, True)]:
        t = _make_saturated_tracker()
        t.mutation_seen = True
        t.verification_seen = seen
        t.verification_passed = passed
        hint = t.decision_hint()
        assert hint is not None
        assert "【执行状态】" in hint, f"缺少事实头：{hint!r}"
        assert "已修改：是" in hint, f"mutation 状态应标记：{hint!r}"


def test_hint_reflects_state_line_values():
    """验证状态行的措辞随 verification_passed/seen 切换。"""
    t1 = _make_saturated_tracker()
    t1.mutation_seen = True
    t1.verification_seen = True
    t1.verification_passed = True
    assert "已验证：通过" in t1.decision_hint()

    t2 = _make_saturated_tracker()
    t2.mutation_seen = True
    t2.verification_seen = True
    t2.verification_passed = False
    assert "已验证：已运行" in t2.decision_hint()

    t3 = _make_saturated_tracker()
    t3.mutation_seen = True
    t3.verification_seen = False
    t3.verification_passed = False
    assert "已验证：否" in t3.decision_hint()
