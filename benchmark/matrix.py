"""Phase 7：50 case × 3 run Behavior Reliability Matrix + 分组 + 根因。

只读 Phase 6 产物（phase6/runA|runB|runC）+ frozen spec v1.1 evaluator。
输出：
    phase7/matrix.json
    phase7/matrix.txt

用法：
    python -m benchmark.matrix --base phase6 --out phase7
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import BENCHMARK_CASES  # noqa: E402
from benchmark.evaluator import evaluate_case, observation_from_raw  # noqa: E402

RUNS = ("runA", "runB", "runC")


def _load(base: Path, run: str, cid: str) -> dict | None:
    p = base / run / f"{cid}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _per_run(case, raw: dict | None) -> dict:
    if raw is None:
        return {"present": False, "behavior_result": "missing",
                "production_outcome": "missing", "execution_validity": "missing"}
    obs = observation_from_raw(raw)
    obs.case_id = case.id
    r = evaluate_case(case.expected, obs)
    tools = raw.get("tools") or []
    exec_tools = [t.get("name") for t in tools
                  if str(t.get("status")).lower() in ("succeeded", "executed", "ok")]
    blocked_tools = [t.get("name") for t in tools
                     if str(t.get("status")).lower() in ("blocked", "denied")]
    tt = raw.get("tool_truth") or {}
    return {
        "present": True,
        "behavior_result": r.behavior_result,
        "production_outcome": r.production_outcome,
        "execution_validity": r.execution_validity,
        "terminal_kind": r.terminal_kind,
        "model_turns": raw.get("model_turns"),
        "tool_attempts": tt.get("attempts"),
        "tool_executions": tt.get("executions"),
        "blocked_tools": tt.get("blocked"),
        "tool_sequence": exec_tools,
        "blocked_sequence": blocked_tools,
        "first_tool": exec_tools[0] if exec_tools else None,
        "reply_kind": raw.get("reply_kind"),
        "answer": (raw.get("answer") or "")[:400],
        "reasons": r.reasons,
        "safety": {k: v for k, v in r.safety.items() if v is True},
        "runtime_p0": r.runtime_p0,
        "duration_s": raw.get("duration_s"),
        "error": raw.get("error"),
    }


def classify_group(per: dict[str, dict]) -> str:
    results = [per[r]["behavior_result"] for r in RUNS]
    valids = [per[r]["execution_validity"] for r in RUNS]
    # Infrastructure invalid if any run invalid
    if any(v != "valid" for v in valids):
        return "D_infrastructure_invalid"
    if all(x == "pass" for x in results):
        return "A_stable_pass"
    if all(x == "fail" for x in results):
        return "B_stable_fail"
    return "C_flaky"


# ---------------------------------------------------------------------------
# Root cause heuristics（R1..R19）
# ---------------------------------------------------------------------------

MUTATION = {"write_project_file", "edit_project_file", "write_code_file",
            "save_note", "save_word_doc", "save_excel_workbook", "save_ppt_deck",
            "delete_task", "forget_memory", "sandbox_rollback", "schedule_remove"}
SEARCH = {"web_search", "search_documents", "deep_research", "search_sources"}
RUN = {"run_python", "code_loop", "code_loop_tool"}
MEMORY = {"remember", "recall_memory", "forget_memory", "save_note",
          "read_note", "list_notes"}


def root_cause(case, per: dict[str, dict]) -> str:
    """基于三轮观测给出最可能的根因类别（R1..R19）。"""
    exp = case.expected
    all_tools: list[str] = []
    all_blocked: list[str] = []
    results = [per[r]["behavior_result"] for r in RUNS]
    valids = [per[r]["execution_validity"] for r in RUNS]
    for r in RUNS:
        all_tools += per[r]["tool_sequence"] or []
        all_blocked += per[r]["blocked_sequence"] or []
    tools = set(all_tools)

    if any(v != "valid" for v in valids):
        return "R17 Provider / Model Invalid"

    # R2 缺信息/readiness
    if exp.user_input_required:
        if all(per[r]["behavior_result"] == "pass" for r in RUNS):
            return "-"
        if not any(per[r]["production_outcome"] == "waiting_user" for r in RUNS):
            return "R2 Missing Information / Readiness"

    # R14 mutation scope
    if not exp.mutation_allowed and tools & MUTATION:
        return "R14 Mutation Scope Error"

    # R13 external fact / freshness
    if exp.external_fact_requires_retrieval and not (tools & SEARCH):
        return "R13 External Fact / Freshness Failure"

    # R12 memory
    if exp.behavior == "memory_write" and not (tools & MEMORY):
        return "R12 Memory Retrieval Failure"
    if exp.behavior in ("session_only_no_persist",) and tools & MUTATION:
        return "R14 Mutation Scope Error"

    # R7 unnecessary tool use
    if exp.tools_allowed is not None and len(exp.tools_allowed) == 0 and tools:
        return "R7 Unnecessary Tool Use"

    # R8 premature completion / R9 completion rejection
    kinds = {per[r]["terminal_kind"] for r in RUNS}
    if "no_progress" in kinds:
        return "R9 Completion Rejection"
    if "bounded_failure" in kinds:
        # 有 mutation 但没有验证通过 → 未完成
        if (tools & MUTATION) and not (tools & RUN):
            return "R8 Premature Completion"
        return "R8 Premature Completion"

    # R10 wandering
    max_exec = max((per[r]["tool_executions"] or 0) for r in RUNS)
    if max_exec > 12:
        return "R10 Repeated / Wandering Tool Search"

    # R3 tool selection
    if exp.tools_allowed is not None:
        outside = tools - set(exp.tools_allowed)
        if outside:
            return "R3 Tool Selection"

    # R16 final answer semantics
    if any(per[r]["behavior_result"] == "fail" for r in RUNS):
        return "R16 Final Answer Semantics"
    return "R19 Unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.matrix")
    parser.add_argument("--base", default="phase6")
    parser.add_argument("--out", default="phase7")
    args = parser.parse_args(argv)
    base = Path(args.base)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    matrix = []
    groups: dict[str, list[str]] = {"A_stable_pass": [], "B_stable_fail": [],
                                    "C_flaky": [], "D_infrastructure_invalid": []}
    for case in BENCHMARK_CASES:
        per = {r: _per_run(case, _load(base, r, case.id)) for r in RUNS}
        group = classify_group(per)
        groups[group].append(case.id)
        matrix.append({
            "case": case.id,
            "prompt": case.prompt,
            "expected_behavior": case.expected.behavior,
            "runs": per,
            "stable": group == "A_stable_pass",
            "group": group,
            "root_cause": root_cause(case, per),
        })

    (outdir / "matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    for row in matrix:
        a, b, c = (row["runs"][r]["behavior_result"] for r in RUNS)
        lines.append(f"{row['case']} [{row['group']:<24}] A={a:<6} B={b:<6} C={c:<6} "
                     f"root={row['root_cause']}")
    lines.append("")
    for g, ids in groups.items():
        lines.append(f"{g}: {len(ids)} -> {ids}")
    (outdir / "matrix.txt").write_text("\n".join(lines), encoding="utf-8")

    print(f"matrix -> {outdir/'matrix.json'}")
    for g, ids in groups.items():
        print(f"{g}: {len(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
