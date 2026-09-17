"""Benchmark CLI。

用法：
    python -m benchmark evaluate  --runs <dir|file> [--label RUN-A] [--out report.json]
    python -m benchmark stability --a A.json --b B.json --c C.json [--out stability.json]
    python -m benchmark reconstruct --summary <summary_after.txt> [--out phase.json]

- ``evaluate``  : 把一轮 50 case 的 raw run 记录评成权威报告；
- ``stability`` : 对比 3 轮报告的稳定性；
- ``reconstruct``: 从历史 summary 文本重建真值（Phase2/3 复盘用）。
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import BENCHMARK_CASES, case_ids, get_case  # noqa: E402
from benchmark.evaluator import evaluate_case, observation_from_raw  # noqa: E402
from benchmark.report import build_report, build_stability  # noqa: E402


def _load_json_runs(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = Path(path)
    files: list[Path] = []
    if p.is_dir():
        files = sorted(p.glob("T*.json")) or sorted(p.glob("*.json"))
    elif p.is_file():
        files = [p]
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or item.get("case") or item.get("case_id") or "")
            if not cid:
                cid = f.stem
            out[cid] = item
    return out


_SUMMARY_HEADER = re.compile(r"^==\s+(T\d+)\s+state=(\S+)(.*)$")
_DICT_RE = re.compile(r"^[a-z_]+:\s*(\{.*\})\s*$")


def _parse_summary(path: str) -> dict[str, dict]:
    """解析历史 summary_after.txt（Phase2/3 原始结果文本）。"""
    out: dict[str, dict] = {}
    cur: dict | None = None
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SUMMARY_HEADER.match(raw.strip())
        if m:
            cur = {"id": m.group(1), "state": m.group(2)}
            out[m.group(1)] = cur
            continue
        if cur is None:
            continue
        line = raw.strip()
        if line.startswith("exec:"):
            payload = line.split("exec:", 1)[1].strip()
            try:
                cur["_exec"] = ast.literal_eval(payload)
            except Exception:
                cur["_exec"] = {}
        elif line.startswith("block:"):
            payload = line.split("block:", 1)[1].strip()
            try:
                cur["_blocked"] = ast.literal_eval(payload)
            except Exception:
                cur["_blocked"] = {}
        elif line.startswith("err:"):
            cur["error"] = line.split("err:", 1)[1].strip()
    return out


def _evaluate_raw(raw_by_id: dict[str, dict], label: str) -> dict:
    results = []
    for case in BENCHMARK_CASES:
        raw = raw_by_id.get(case.id)
        if raw is None:
            continue
        obs = observation_from_raw(raw)
        obs.case_id = case.id
        results.append(evaluate_case(case.expected, obs))
    return build_report(results, run_label=label)


def _cmd_evaluate(args: argparse.Namespace) -> int:
    if Path(args.runs).is_file() and args.runs.endswith(".txt"):
        raw = _parse_summary(args.runs)
    else:
        raw = _load_json_runs(args.runs)
    report = _evaluate_raw(raw, args.label or Path(args.runs).name)
    _emit(report, args.out)
    return 0


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    raw = _parse_summary(args.summary)
    report = _evaluate_raw(raw, args.label or Path(args.summary).stem)
    _emit(report, args.out)
    return 0


def _cmd_stability(args: argparse.Namespace) -> int:
    reports = []
    for p in (args.a, args.b, args.c):
        if p:
            reports.append(json.loads(Path(p).read_text(encoding="utf-8")))
    stab = build_stability(reports)
    text = json.dumps(stab, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"stability -> {args.out}")
    else:
        print(text)
    return 0


def _emit(report: dict, out: str | None) -> None:
    s = report["summary"]
    inv = report["invariants"]
    print(f"== {report['run_label']} ==")
    print(f"Total/Valid/Invalid : {s['total_cases']} / {s['valid_executions']} / "
          f"{s['infrastructure_invalid']}")
    print(f"Behavior Pass Rate : {s['behavior_pass_rate']*100:.1f}% "
          f"(pass={s['behavior_pass']} fail={s['behavior_fail']} "
          f"unknown={s['behavior_unknown']} not_eval={s['behavior_not_evaluable']})")
    print(f"Evaluability Rate  : {s['behavior_evaluability_rate']*100:.1f}%  "
          f"Pass on Evaluable={s['behavior_pass_rate_evaluable']*100:.1f}%")
    print(f"E2E Success Rate   : {s['end_to_end_success_rate']*100:.1f}%  "
          f"Provider Failure={s['provider_failure_rate']*100:.1f}%  "
          f"Obs Timeout={s['observation_timeout_rate']*100:.1f}%  "
          f"Safety Violation={s['safety_violation_rate']*100:.1f}%")
    print(f"Execution validity : {report['execution_validity_distribution']}")
    print(f"Production outcome : {report['status_distribution']}")
    print(f"Safety             : {report['safety']}")
    print(f"Invariants         : terminal+nonterminal="
          f"{inv['terminal_plus_non_terminal']}/{inv['case_count']} "
          f"behavior_total={inv['behavior_total']}/{inv['case_count']} "
          f"exec_total={inv['execution_total']}/{inv['case_count']} "
          f"status_total={inv['status_total']}/{inv['case_count']} "
          f"missing={inv['missing']}")
    if out:
        Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"report -> {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_eval = sub.add_parser("evaluate", help="评测一轮 50 case")
    p_eval.add_argument("--runs", required=True, help="raw run 目录或 summary.txt")
    p_eval.add_argument("--label", default="")
    p_eval.add_argument("--out", default="")
    p_eval.set_defaults(func=_cmd_evaluate)

    p_rec = sub.add_parser("reconstruct", help="从历史 summary 文本重建真值")
    p_rec.add_argument("--summary", required=True)
    p_rec.add_argument("--label", default="")
    p_rec.add_argument("--out", default="")
    p_rec.set_defaults(func=_cmd_reconstruct)

    p_stab = sub.add_parser("stability", help="对比三轮报告稳定性")
    p_stab.add_argument("--a", required=True)
    p_stab.add_argument("--b", default="")
    p_stab.add_argument("--c", default="")
    p_stab.add_argument("--out", default="")
    p_stab.set_defaults(func=_cmd_stability)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
