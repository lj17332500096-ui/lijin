"""Phase 9：独占 Primary Stage + Evidence Novelty + Coding Efficiency Scorecard。

规则（Phase 9 §二/§三/§五）：
- 每个 execution 只有一个 primary_stage（Σ primary_stage == tool_executions）；
- 其它维度进入 secondary_flags；
- Evidence Novelty 基于确定性指纹：hash(tool + normalized_args + result_excerpt)；
- Redundant 基于 target + args + result fingerprint，而非连续同名。

只读 phase6/runA|runB|runC（v1.1 产物）与 phase7/runA|runB。输出 phase9/evidence_analysis.json。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import get_case  # noqa: E402

DISCOVERY = {"list_workspace_files", "list_code_files", "index_workspace",
             "search_documents", "search_sources", "web_search", "list_notes",
             "list_sandbox_snapshots"}
READ = {"read_workspace_file", "read_code_file", "read_note", "recall_memory",
        "read_office_file", "read_spreadsheet"}
MUTATION = {"write_project_file", "edit_project_file", "write_code_file",
            "save_note", "save_word_doc", "save_excel_workbook", "save_ppt_deck"}
VERIFY = {"run_python", "code_loop", "code_loop_tool"}

STAGES = ("DISCOVERY", "READ", "MUTATION", "VERIFICATION", "OTHER")


def primary_stage(name: str) -> str:
    if name in VERIFY:
        return "VERIFICATION"
    if name in MUTATION:
        return "MUTATION"
    if name in READ:
        return "READ"
    if name in DISCOVERY:
        return "DISCOVERY"
    return "OTHER"


def _fp(tool: str, args: object, result: object) -> str:
    payload = json.dumps([tool, args, str(result)[:300]], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:16]


def _target(name: str, args: dict) -> str:
    for k in ("path", "file", "filename", "directory", "project"):
        if k in args and args[k]:
            return str(args[k])
    return ""


def _verification_passed(result: str) -> bool:
    t = str(result or "")
    return ("退出码: 0" in t) or ("退出码：0" in t) or ("✅ 通过" in t) or ("passed" in t.lower())


def analyze_run(raw: dict) -> dict | None:
    cid = raw.get("id")
    try:
        case = get_case(cid)
    except KeyError:
        return None
    if case.expected.behavior != "coding":
        return None
    tools = [t for t in (raw.get("tools") or [])
             if str(t.get("status")).lower() in ("succeeded", "executed", "ok")]
    seen_fp: set[str] = set()
    read_targets: dict[str, str] = {}
    seen_queries: set[str] = set()
    stage_counter: Counter = Counter()
    novel = dup = 0
    unique_files = set()
    repeated_files = 0
    unique_queries = 0
    repeated_queries = 0
    mutation_count = verify_count = 0
    post_mut_disc = post_verify_disc = 0
    premature_mutation = False
    mut_seen = verify_pass_seen = False
    read_before_mutation = 0
    mutation_stage_flag = False
    for t in tools:
        name = t.get("name") or ""
        args = t.get("normalized_args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        args = args or {}
        result = t.get("result_excerpt") or ""
        stage = primary_stage(name)
        stage_counter[stage] += 1
        fp = _fp(name, args, result)
        if stage == "MUTATION":
            mutation_count += 1
            mutation_stage_flag = True
            if not read_before_mutation:
                premature_mutation = True
        elif stage == "VERIFICATION":
            verify_count += 1
            if _verification_passed(result):
                verify_pass_seen = True
        if mutation_stage_flag and stage in ("DISCOVERY", "READ"):
            post_mut_disc += 1
        if verify_pass_seen and stage in ("DISCOVERY", "READ"):
            post_verify_disc += 1
        if stage in ("READ",):
            tgt = _target(name, args)
            if tgt:
                unique_files.add(tgt)
                if tgt in read_targets and read_targets[tgt] == fp:
                    repeated_files += 1
                read_targets[tgt] = fp
            read_before_mutation += 1
        if stage in ("DISCOVERY",):
            q = _target(name, args) or json.dumps(args, ensure_ascii=False)[:60]
            if q in seen_queries:
                repeated_queries += 1
            else:
                seen_queries.add(q)
                unique_queries += 1
        if fp in seen_fp:
            dup += 1
        else:
            seen_fp.add(fp)
            novel += 1
    total = len(tools)
    if total == 0:
        return None
    novel_or_change = novel  # STATE_CHANGE 已计入 novel（首次 mutation 指纹为新）
    return {
        "case": cid,
        "executions": total,
        "primary_stage": dict(stage_counter),
        "novel_evidence": novel,
        "duplicate_evidence": dup,
        "evidence_novelty_rate": round(novel / total, 3),
        "unique_files_read": len(unique_files),
        "repeated_files_read": repeated_files,
        "unique_queries": unique_queries,
        "repeated_queries": repeated_queries,
        "mutation_count": mutation_count,
        "verification_count": verify_count,
        "post_mutation_discovery": post_mut_disc,
        "post_verification_discovery": post_verify_disc,
        "premature_mutation": premature_mutation,
        "execution_efficiency": round(novel_or_change / total, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.evidence_analysis")
    parser.add_argument("--out", default="phase9")
    args = parser.parse_args(argv)
    dirs = [Path("phase6/runA"), Path("phase6/runB"), Path("phase6/runC"),
            Path("phase7/runA"), Path("phase7/runB")]
    rows = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("T*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            r = analyze_run(raw)
            if r:
                r["run"] = d.parent.name + "/" + d.name
                rows.append(r)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "evidence_analysis.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    tot_stage = Counter()
    tot = {"executions": 0, "novel": 0, "dup": 0, "post_mut": 0, "post_verify": 0,
           "mutation": 0, "verification": 0, "premature": 0}
    for r in rows:
        for k, v in r["primary_stage"].items():
            tot_stage[k] += v
        tot["executions"] += r["executions"]
        tot["novel"] += r["novel_evidence"]
        tot["dup"] += r["duplicate_evidence"]
        tot["post_mut"] += r["post_mutation_discovery"]
        tot["post_verify"] += r["post_verification_discovery"]
        tot["mutation"] += r["mutation_count"]
        tot["verification"] += r["verification_count"]
        tot["premature"] += 1 if r["premature_mutation"] else 0
    print("coding runs:", len(rows))
    print("Σ primary_stage:", sum(tot_stage.values()), "== executions", tot["executions"])
    print("primary_stage:", dict(tot_stage))
    print("novel/dup:", tot["novel"], "/", tot["dup"],
          "novelty_rate", round(tot["novel"] / max(1, tot["executions"]), 3))
    print("post_mutation_discovery:", tot["post_mut"], "post_verification_discovery:", tot["post_verify"])
    print("mutation:", tot["mutation"], "verification:", tot["verification"],
          "premature_mutation_runs:", tot["premature"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
