"""Benchmark 报告聚合：权威 50 行 + 双主指标 + 安全指标 + 稳定性。

硬约束（Phase 4）：
- 每次完整 Benchmark 必须产生恰好 N 行（N=case 数，默认 50）；
- ``behavior_pass + behavior_fail == N``；
- ``sum(status_distribution.values()) == N``；
- 缺失 case 显式记为 ``MISSING``，绝不用其它桶补齐；
- 重试 / 历史 run 必须作为独立 attempt，不并入本轮 authoritative result。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from benchmark.cases import BENCHMARK_CASES, case_ids
from benchmark.evaluator import CaseResult, is_terminal

_SAFETY_KEYS = (
    "approval_bypass",
    "readiness_bypass",
    "unauthorized_mutation",
    "readonly_mutation",
    "forbidden_tool_usage",
    "false_completion",
)


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def build_report(results: Iterable[CaseResult], *, run_label: str = "",
                 expected_ids: list[str] | None = None) -> dict[str, Any]:
    ids = expected_ids or case_ids()
    by_id = {r.case_id: r for r in results}

    rows: list[dict[str, Any]] = []
    status_counter: Counter[str] = Counter()
    exec_counter: Counter[str] = Counter()
    pass_count = 0
    fail_count = 0
    unknown_count = 0
    not_evaluable_count = 0
    missing = 0
    safety_counter: Counter[str] = Counter()
    total_tool_calls = 0
    total_turns = 0
    terminal_count = 0
    non_terminal_count = 0
    e2e_success = 0
    safety_violation_cases = 0
    runtime_p0_count = 0

    for cid in ids:
        r = by_id.get(cid)
        if r is None:
            missing += 1
            status_counter["missing"] += 1
            exec_counter["missing"] += 1
            non_terminal_count += 1
            rows.append({
                "case": cid, "behavior_result": "not_evaluable", "behavior_pass": False,
                "execution_validity": "artifact_incomplete",
                "final_status": "missing", "production_outcome": "unknown",
                "actual_behavior": "missing", "reasons": ["无本轮结果记录（MISSING）"],
            })
            continue
        status_counter[r.production_outcome or "unknown"] += 1
        exec_counter[r.execution_validity or "valid"] += 1
        if r.terminal_state:
            terminal_count += 1
        else:
            non_terminal_count += 1
        if r.behavior_result == "pass":
            pass_count += 1
        elif r.behavior_result == "unknown":
            unknown_count += 1
        elif r.behavior_result == "not_evaluable":
            not_evaluable_count += 1
        else:
            fail_count += 1
        if (r.execution_validity == "valid"
                and r.production_outcome in ("completed", "waiting_user", "waiting_approval")):
            e2e_success += 1
        if any(r.safety.get(k) is True for k in _SAFETY_KEYS):
            safety_violation_cases += 1
        if getattr(r, "runtime_p0", False):
            runtime_p0_count += 1
        for k in _SAFETY_KEYS:
            v = r.safety.get(k)
            if v is True:
                safety_counter[f"{k}.observed_violation"] += 1
            elif v is False:
                safety_counter[f"{k}.verified_safe"] += 1
            else:
                safety_counter[f"{k}.not_evaluable"] += 1
        total_tool_calls += r.tool_calls
        total_turns += r.model_turns
        rows.append(r.as_row())

    # 未在期望列表中的多余结果（例如 retry/历史 run 混入）单独列出，不并入统计
    extra = sorted(set(by_id) - set(ids))

    n = len(ids)
    completed = status_counter.get("completed", 0)
    waiting_user = status_counter.get("waiting_user", 0)
    waiting_approval = status_counter.get("waiting_approval", 0)
    failed = status_counter.get("failed", 0)
    cancelled = status_counter.get("cancelled", 0)
    timeout = status_counter.get("timeout", 0)
    provider_error = status_counter.get("provider_error", 0)
    stalled = status_counter.get("stalled", 0)
    running = sum(status_counter.get(s, 0)
                  for s in ("running", "submitted", "paused", "unknown"))
    valid_exec = exec_counter.get("valid", 0)
    infra_invalid = n - valid_exec
    evaluable = pass_count + fail_count + unknown_count
    provider_invalid = (exec_counter.get("provider_error", 0)
                        + exec_counter.get("provider_timeout", 0))
    obs_timeout = exec_counter.get("benchmark_observation_timeout", 0)

    invariants = {
        "case_count": n,
        "terminal_plus_non_terminal": terminal_count + non_terminal_count,
        "terminal_plus_non_terminal_ok": (terminal_count + non_terminal_count) == n,
        "terminal_count": terminal_count,
        "non_terminal_at_observation_count": non_terminal_count,
        "behavior_total": pass_count + fail_count + unknown_count + not_evaluable_count,
        "behavior_total_ok": (pass_count + fail_count + unknown_count
                              + not_evaluable_count) == n,
        "execution_total": valid_exec + infra_invalid,
        "execution_total_ok": (valid_exec + infra_invalid) == n,
        "status_total": sum(status_counter.values()),
        "status_total_ok": sum(status_counter.values()) == n,
        "missing": missing,
        "extra_unattributed": extra,
        "all_terminal": non_terminal_count == 0,
    }

    return {
        "run_label": run_label,
        "rows": rows,
        "summary": {
            "total_cases": n,
            "valid_executions": valid_exec,
            "infrastructure_invalid": infra_invalid,
            # 主指标（A/B/C 三维）
            "behavior_pass": pass_count,
            "behavior_fail": fail_count,
            "behavior_unknown": unknown_count,
            "behavior_not_evaluable": not_evaluable_count,
            "behavior_evaluability_rate": _rate(evaluable, n),
            "behavior_pass_rate": _rate(pass_count, n),
            "behavior_pass_rate_evaluable": _rate(pass_count, evaluable),
            "end_to_end_success_rate": _rate(e2e_success, n),
            "provider_failure_rate": _rate(provider_invalid, n),
            "observation_timeout_rate": _rate(obs_timeout, n),
            "safety_violation_rate": _rate(safety_violation_cases, n),
            "runtime_p0_violations": runtime_p0_count,
            # 辅助
            "completion_rate": _rate(completed, n),
            "waiting_user_rate": _rate(waiting_user, n),
            "waiting_approval_rate": _rate(waiting_approval, n),
            "failure_rate": _rate(failed, n),
            "timeout_rate": _rate(timeout, n),
            "provider_error_rate": _rate(provider_error, n),
            "cancel_rate": _rate(cancelled, n),
            "stalled_rate": _rate(stalled, n),
            "running_rate": _rate(running, n),
            "avg_tool_calls": round(total_tool_calls / n, 2) if n else 0.0,
            "avg_model_turns": round(total_turns / n, 2) if n else 0.0,
        },
        "execution_validity_distribution": dict(exec_counter),
        "status_distribution": dict(status_counter),
        "safety": {
            k: {
                "observed_violation": safety_counter.get(f"{k}.observed_violation", 0),
                "verified_safe": safety_counter.get(f"{k}.verified_safe", 0),
                "not_evaluable": safety_counter.get(f"{k}.not_evaluable", 0),
            }
            for k in _SAFETY_KEYS
        },
        "invariants": invariants,
    }


def build_stability(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """三轮（或更多）稳定性对比：同一 case 的 behavior_pass / final_status 是否漂移。"""
    if not reports:
        return {"runs": 0, "cases": []}
    ids = [row["case"] for row in reports[0]["rows"]]
    cases: list[dict[str, Any]] = []
    for i, cid in enumerate(ids):
        per_run_result: list[str] = []
        per_run_status: list[str] = []
        per_run_validity: list[str] = []
        for rep in reports:
            row = rep["rows"][i] if i < len(rep["rows"]) else None
            per_run_result.append(str(row.get("behavior_result") if row else "missing"))
            per_run_status.append(str(row.get("production_outcome") if row else "missing"))
            per_run_validity.append(str(row.get("execution_validity") if row else "missing"))
        stable = (len(set(per_run_result)) == 1 and len(set(per_run_status)) == 1
                  and len(set(per_run_validity)) == 1)
        cases.append({
            "case": cid,
            "behavior_result": per_run_result,
            "production_outcome": per_run_status,
            "execution_validity": per_run_validity,
            "stable": stable,
        })
    stable_count = sum(1 for c in cases if c["stable"])
    pass_rates = [r["summary"]["behavior_pass_rate"] for r in reports]
    eval_rates = [r["summary"]["behavior_evaluability_rate"] for r in reports]
    return {
        "runs": len(reports),
        "pass_rates": pass_rates,
        "evaluability_rates": eval_rates,
        "min_pass_rate": min(pass_rates),
        "max_pass_rate": max(pass_rates),
        "stable_cases": stable_count,
        "case_count": len(cases),
        "stability_rate": _rate(stable_count, len(cases)),
        "unstable_cases": [c["case"] for c in cases if not c["stable"]],
        "cases": cases,
    }
