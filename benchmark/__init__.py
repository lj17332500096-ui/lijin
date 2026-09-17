"""FORGE Agent Behavior Benchmark —— 真值化评测（Phase 4）。

本包只做评测，不参与 Runtime 执行：

- ``cases``     : 50 个 Benchmark case 的结构化 *Expected Behavior*（真值）。
- ``evaluator`` : 把「一次 Run 的观测」与「期望行为」做确定性语义比对，
                  产出 Behavior Pass/Fail 与独立的 Final Run Status。
- ``report``    : 把 50 行结果聚合成权威报告，强制 PASS+FAIL=50、状态分布=50。

核心原则（Phase 4）：**Final Status ≠ Behavior Result**。
禁止用 ``status == completed`` 直接判定 Benchmark 通过。
"""

from benchmark.evaluator import (
    ExpectedBehavior,
    Observation,
    CaseResult,
    evaluate_case,
    observation_from_raw,
)
from benchmark.cases import BENCHMARK_CASES, get_case

__all__ = [
    "ExpectedBehavior",
    "Observation",
    "CaseResult",
    "evaluate_case",
    "observation_from_raw",
    "BENCHMARK_CASES",
    "get_case",
]
