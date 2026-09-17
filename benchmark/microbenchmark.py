"""Phase 11：Local Coding Microbenchmark（真实本地模型 + 真实 Runtime + 微型 fixture）。

用法：
    python -m benchmark.microbenchmark --mode baseline --runs 1 --out phase11/baseline
    python -m benchmark.microbenchmark --mode guardB   --runs 3 --out phase11/guardB
强制本地模型（FORGE_MODEL_PREF=local）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIX = "F:/Byong-hermes/Byong-hermes/my_creative_agent/micro_fixture"

MICRO_CASES = [
    {"id": "M1", "kind": "single_file_fix_test",
     "prompt": f"修复 {FIX}/calc.py 中 add 函数结果错误的问题（应返回两数之和），"
               f"并运行 {FIX} 下的测试确认通过。",
     "expect_mutation": True, "expect_verification": True},
    {"id": "M4", "kind": "fix_then_pass",
     "prompt": f"修复 {FIX}/calc.py 的 add 函数，然后运行测试验证修改是否正确。",
     "expect_mutation": True, "expect_verification": True},
    {"id": "M6", "kind": "read_only",
     "prompt": f"只阅读 {FIX}/calc.py，解释 add 函数的作用，不要修改任何文件。",
     "expect_mutation": False, "expect_verification": False},
    {"id": "M7", "kind": "mutation_no_verify",
     "prompt": f"修改 {FIX}/calc.py 的 add 函数，让它返回两数之和。",
     "expect_mutation": True, "expect_verification": False},
    {"id": "M8", "kind": "keyword_false_positive",
     "prompt": f"修改 {FIX}/README.md 中关于测试的说明文字，让它更清晰。",
     "expect_mutation": True, "expect_verification": False},
]

CAPS = ("DISCOVERY", "READ", "MUTATION", "VERIFICATION")


def _reset_fixture() -> None:
    Path(FIX.replace("/", os.sep), "calc.py").write_text(
        "def add(a, b):\n    # BUG\n    return a - b\n\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8")


async def _auto_approve(rt, task_id: str) -> int:
    n = 0
    try:
        for ap in rt.tasks.list_pending_approvals(task_id):
            try:
                rt.tasks.decide_approval(ap["id"], "approved")
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


async def _run_case(rt, case: dict, mode: str, rep: int) -> dict:
    os.environ["FORGE_DECISION_HINT"] = "off"
    os.environ["FORGE_REDUNDANT_GUARD"] = "off"
    os.environ["FORGE_COMPLETION_READY"] = "off"  # Guard B 保持 OFF
    # Phase 13：Completion Obligation Gate 始终保持 ON（防 false completion）
    os.environ["FORGE_OBLIGATION_GATE"] = "on"
    os.environ["FORGE_OBLIGATION_FEEDBACK"] = (
        "on" if mode in ("feedback", "fact", "gate", "focus") else "off")
    os.environ["FORGE_VERIFICATION_FOCUS"] = (
        "on" if mode in ("focus",) else "off")
    _reset_fixture()
    sid = f"p11-{case['id']}-{mode}-{rep}"
    try:
        res = await rt.run_turn(case["prompt"], session_id=sid, mode="async", max_turns=16)
        for _ in range(4):
            if res.task.state.value != "waiting_approval":
                break
            if await _auto_approve(rt, res.task.id) == 0:
                break
            try:
                res = await rt.run_turn("", task_id=res.task.id, mode="async", max_turns=16)
            except Exception:
                break
    except Exception as exc:  # noqa: BLE001
        return {"case": case["id"], "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    rid = res.task.id
    events = [{"type": e.event_type, "payload": e.payload}
              for e in rt.tasks.list_events(rid, limit=800)]
    invocations = [e["payload"] for e in events if e["type"] == "tool.invocation"]
    cap_counts = {c: 0 for c in CAPS}
    max_mutation_revision = -1
    verification_epochs: list[int] = []
    for inv in invocations:
        if inv.get("blocked"):
            continue
        cap = inv.get("tool_capability")
        if cap in cap_counts:
            cap_counts[cap] += 1
        if cap == "MUTATION":
            max_mutation_revision = max(max_mutation_revision,
                                        int(inv.get("mutation_revision") or 0))
        if cap == "VERIFICATION":
            verification_epochs.append(int(inv.get("workspace_epoch") or 0))
    verified = any(inv.get("tool_capability") == "VERIFICATION"
                   and not inv.get("blocked")
                   and ("退出码: 0" in str(inv.get("result_summary") or "")
                        or "passed" in str(inv.get("result_summary") or "").lower())
                   for inv in invocations)
    latest_revision_covered = bool(verified and max_mutation_revision >= 0
                                   and any(e >= max_mutation_revision
                                           for e in verification_epochs))
    mutated = cap_counts["MUTATION"] > 0
    # First Verification Decision（mutation 后第一个非 blocked 动作的 capability）
    first_after_mutation = None
    seen_mutation = False
    for inv in invocations:
        if inv.get("blocked"):
            continue
        cap = inv.get("tool_capability")
        if cap == "MUTATION":
            seen_mutation = True
            continue
        if seen_mutation:
            first_after_mutation = {"capability": cap, "tool": inv.get("tool_name")}
            break
    _PREP_TOOLS = {"read_workspace_file", "list_workspace_files", "search_documents"}
    first_decision_correct = None
    if first_after_mutation is not None:
        fc = first_after_mutation["capability"]
        first_decision_correct = bool(
            fc == "VERIFICATION"
            or (fc in ("READ", "DISCOVERY") and first_after_mutation["tool"] in _PREP_TOOLS)
        )
    # Verification tool availability（Router 是否暴露 VERIFICATION capability）
    try:
        from runtime.tool_router import select_tool_names
        from runtime.spec import capability_of
        from agent import assistant_agent
        exposed = select_tool_names(case["prompt"],
                                    [t.name for t in assistant_agent.tools])
        available_verification = [n for n in exposed if capability_of(n) == "VERIFICATION"]
    except Exception:
        available_verification = []
    completion_kind = ""
    for e in reversed(events):
        if e["type"] == "run.terminal":
            completion_kind = str((e["payload"] or {}).get("kind") or "")
            break
    if case["expect_mutation"] and not mutated:
        behavior_pass = False
    elif case["expect_verification"] and not verified:
        behavior_pass = False
    else:
        behavior_pass = True
    return {
        "case": case["id"], "kind": case["kind"], "state": res.task.state.value,
        "terminal_kind": completion_kind, "behavior_pass": behavior_pass,
        "mutation": mutated, "verification_pass": verified,
        "verification_attempted": cap_counts["VERIFICATION"] > 0,
        "first_after_mutation": first_after_mutation,
        "first_decision_correct": first_decision_correct,
        "latest_revision_covered": latest_revision_covered,
        "available_verification_tools": available_verification,
        "verification_available": bool(available_verification),
        "capability_counts": cap_counts,
        "executions": sum(cap_counts.values()),
        "invocations": len(invocations),
        "obligation_blocked": sum(1 for e in events
                                  if e["type"] == "completion.obligation_blocked"),
        "completion_ready_forced": sum(1 for e in events if e["type"] == "completion.ready_forced"),
    }


async def run(mode: str, n: int, outdir: Path, only: list[str] | None = None) -> dict:
    import main as main_module  # noqa: F401
    from runtime.runner import AgentRuntime

    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref != "local":
        raise SystemExit(f"FORGE_MODEL_PREF={pref!r}；本轮禁止 gateway，必须为 local")
    tmp = tempfile.mkdtemp(prefix="p11_micro_")
    rt = AgentRuntime(db_path=os.path.join(tmp, "agent.db"))
    rt._ensure()
    rows = []
    cases = [c for c in MICRO_CASES if not only or c["id"] in only]
    for case in cases:
        runs = [await _run_case(rt, case, mode, i) for i in range(n)]
        rows.append({"case": case["id"], "runs": runs})
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "partial.json").write_text(
            json.dumps({"mode": mode, "n": n, "cases": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        for r in runs:
            print(f"  {r.get('case')} pass={r.get('behavior_pass')} "
                  f"exec={r.get('executions')} state={r.get('state')} "
                  f"caps={r.get('capability_counts')}", flush=True)
    return {"mode": mode, "n": n, "cases": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.microbenchmark")
    parser.add_argument("--mode",
                        choices=["baseline", "guardB", "feedback", "gate", "fact", "focus"],
                        required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--cases", default="", help="只跑指定 micro case，逗号分隔")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    outdir = Path(args.out)
    only = [c.strip() for c in args.cases.split(",") if c.strip()]
    result = asyncio.run(run(args.mode, args.runs, outdir, only))
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print(f"{args.mode} -> {outdir/'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
