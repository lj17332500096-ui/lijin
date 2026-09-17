"""Phase 8：coding 阶段化 + task-relevant 执行比 + Completion rejection 归因。

只读 phase6/runA|runB|runC（frozen v1.1 产物）与 phase7/runA|runB。
输出 phase8/behavior_analysis.json。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import BENCHMARK_CASES, get_case  # noqa: E402

DISCOVERY = {"list_workspace_files", "list_code_files", "index_workspace",
             "search_documents", "search_sources", "web_search", "list_notes"}
READ = {"read_workspace_file", "read_code_file", "read_note", "recall_memory",
        "read_office_file", "read_spreadsheet"}
MUTATION = {"write_project_file", "edit_project_file", "write_code_file",
            "save_note", "save_word_doc", "save_excel_workbook", "save_ppt_deck"}
VERIFY = {"run_python", "code_loop", "code_loop_tool"}
CODING_RELEVANT = DISCOVERY | READ | MUTATION | VERIFY


def _stage(name: str) -> str:
    if name in VERIFY:
        return "VERIFICATION"
    if name in MUTATION:
        return "MUTATION"
    if name in READ:
        return "READ"
    if name in DISCOVERY:
        return "DISCOVERY"
    return "DRIFT"


def _load(d: Path) -> list[dict]:
    out = []
    for p in sorted(d.glob("T*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def analyze(run_dirs: list[Path]) -> dict:
    coding_stage = defaultdict(Counter)
    task_relevant = {}
    completion = []
    primary = Counter()
    for d in run_dirs:
        for raw in _load(d):
            cid = raw.get("id") or ""
            if not cid:
                continue
            try:
                case = get_case(cid)
            except KeyError:
                continue
            tools = [t.get("name") for t in (raw.get("tools") or [])
                     if str(t.get("status")).lower() in ("succeeded", "executed", "ok")]
            tt = raw.get("tool_truth") or {}
            execs = tt.get("executions") or len(tools)
            relevant = sum(1 for t in tools if t in CODING_RELEVANT)
            drift = len(tools) - relevant
            if case.expected.behavior == "coding":
                coding_stage[cid]["executions"] += execs
                for t in tools:
                    coding_stage[cid][_stage(t)] += 1
                # redundant = 连续同名工具（粗粒度）
                redundant = sum(1 for i in range(1, len(tools)) if tools[i] == tools[i - 1])
                coding_stage[cid]["REDUNDANT"] += redundant
                task_relevant[cid] = {
                    "executions": len(tools),
                    "relevant": relevant,
                    "drift": drift,
                    "ratio": round(relevant / len(tools), 3) if tools else None,
                }
            kind = raw.get("terminal_kind") or ""
            if case.expected.behavior == "coding" and (
                    kind in ("no_progress", "bounded_failure")
                    or (raw.get("final_status") == "completed" and execs > (case.expected.max_tool_calls or 0))):
                has_mut = any(t in MUTATION for t in tools)
                has_verify = any(t in VERIFY for t in tools)
                if has_mut and not has_verify:
                    cat = "missing_verification"
                elif execs > (case.expected.max_tool_calls or 0):
                    cat = "tool_overuse_only"
                elif has_mut and has_verify:
                    cat = "unfinished_or_semantics"
                else:
                    cat = "other"
                completion.append({"case": cid, "kind": kind, "exec": execs, "cat": cat,
                                   "has_mutation": has_mut, "has_verification": has_verify})
                primary[cat] += 1

    return {
        "coding_stage": {k: dict(v) for k, v in coding_stage.items()},
        "task_relevant": task_relevant,
        "completion_rejection": completion,
        "completion_categories": dict(primary),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.behavior_analysis")
    parser.add_argument("--out", default="phase8")
    args = parser.parse_args(argv)
    dirs = [Path("phase6/runA"), Path("phase6/runB"), Path("phase6/runC")]
    result = analyze([d for d in dirs if d.is_dir()])
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "behavior_analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 汇总
    tot = Counter()
    for cid, c in result["coding_stage"].items():
        for k, v in c.items():
            tot[k] += v
    print("coding stage totals:", dict(tot))
    ratios = [v["ratio"] for v in result["task_relevant"].values() if v["ratio"] is not None]
    if ratios:
        print("task-relevant ratio avg:", round(sum(ratios) / len(ratios), 3))
    print("completion categories:", result["completion_categories"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
