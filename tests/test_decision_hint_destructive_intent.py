"""P1-B(3)：decision_hint 的 destructive 探索型 + coding 意图层两态单测。

锁定 runtime/readiness_gate.py::DiscoveryTracker.decision_hint 新增的两态：
- destructive 探索型（mutation 否 + 读多 + 低新颖连续）→ "要么确认要么动手"（治 T038）
- coding 意图层（mutation 否 + 读完目标 + 零进展）→ "读完必须动手写"（治 T023）
构造方式对齐 test_decision_hint_coding.py：直接设字段触发 saturated。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.readiness_gate import DiscoveryTracker  # noqa: E402


def _saturated_tracker(**overrides) -> DiscoveryTracker:
    """构造 saturated tracker：默认 mutation 否 + 低新颖连续=3（触发 saturated）。"""
    t = DiscoveryTracker()
    t.low_novelty_streak = 3  # 触发 saturated（>=2）
    t.unique_files_read = ["a.py", "b.py"]
    t.meaningful_progress_count = 0
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


class TestDestructiveExplorationHint:
    """destructive 探索型：读多、未改、低新颖连续 → 要么确认要么动手（T038）。"""

    def test_destructive_hint_triggered(self):
        t = _saturated_tracker()  # mutation 否 + 读 2 + 低新颖 3
        hint = t.decision_hint()
        assert hint is not None
        assert "破坏性/配置类任务不能只读不写" in hint
        assert "要么向用户确认" in hint

    def test_destructive_not_triggered_for_single_read(self):
        """读 <2 时不触发 destructive 探索型（落到 coding 意图层）。"""
        t = _saturated_tracker(unique_files_read=["a.py"])
        hint = t.decision_hint()
        assert hint is not None
        assert "破坏性/配置类任务不能只读不写" not in hint
        # 读 1 个 + 零进展 → coding 意图层
        assert "已读取目标文件但未开始修改" in hint


class TestCodingIntentHint:
    """coding 意图层：读完目标未动手写 → 读完必须动手（T023）。"""

    def test_coding_intent_hint_triggered(self):
        t = _saturated_tracker(unique_files_read=["auth.py"])  # 读 1 个未改
        hint = t.decision_hint()
        assert hint is not None
        assert "已读取目标文件但未开始修改" in hint
        assert "动手写修复" in hint


class TestSaturatedGate:
    """saturated() 不触发时 decision_hint 返回 None（原行为不变）。"""

    def test_none_when_not_saturated(self):
        t = DiscoveryTracker()
        assert t.decision_hint() is None


class TestExistingThreeStatesPreserved:
    """原 coding 三态（C 块）不被新两态覆盖。"""

    def test_verification_passed_state(self):
        t = _saturated_tracker(
            mutation_seen=True, verification_seen=True, verification_passed=True,
        )
        hint = t.decision_hint()
        assert "验证已通过" in hint

    def test_mutation_unverified_state(self):
        t = _saturated_tracker(mutation_seen=True, verification_seen=False)
        hint = t.decision_hint()
        assert "已修改但未验证" in hint
