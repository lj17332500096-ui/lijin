"""Phase 9：Coding Decision Set 离线实验（Baseline vs Decision Hint）。

直接使用 AgentRuntime.run_turn（真实 Runtime + 真实工具 + 真实 Provider），
通过环境变量 FORGE_DECISION_HINT 切换策略，无需重启 webapp。

用法：
    python -m benchmark.coding_experiment --mode baseline --runs 1 --out phase9/baseline
    python -m benchmark.coding_experiment --mode hint     --runs 1 --out phase9/hint
    （--provider gateway|local 由 FORGE_MODEL_PREF 决定）
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

from benchmark.cases import get_case  # noqa: E402
from benchmark.evaluator import evaluate_case  # noqa: E402

CODING_SET = ["T019", "T021", "T022", "T024", "T025", "T029", "T049"]
CONTROL = ["T016", "T017", "T003", "T011", "T030"]
TERMINAL = {"completed", "failed", "cancelled", "waiting_user", "waiting_approval"}


def _tool_truth(events: list) -> dict:
    started = sum(1 for e in events if e["type"] == "public.tool.started")
    completed = sum(1 for e in events if e["type"] == "public.tool.completed")
    failed = sum(1 for e in events if e["type"] == "public.tool.failed")
    blocked_types = {"tool.budget_exhausted", "tool.convergence", "tool.readiness_gate",
                     "tool.intent_gate", "tool.blocked_needs_user_input",
                     "tool.missing_required", "tool.user_constraint",
                     "tool.discovery_exhausted", "tool.discovery_hard_stopped",
                     "tool.mutation_exhausted", "tool.permission_error",
                     "tool.persistence_idempotent", "tool.network_policy",
                     "convergence.forced"}
    blocked = sum(1 for e in events if e["type"] in blocked_types)
    return {"attempts": started + blocked, "blocked": blocked, "executions": started,
            "successes": completed, "failures": failed, "budget_consumed": started}


async def _auto_approve(rt, task_id: str) -> int:
    """实验用：自动批准本 Run 的 pending approvals（模拟 webapp autoApprove）。"""
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


async def _run_case(rt, case, mode: str, rep: int = 0) -> dict:
    os.environ["FORGE_DECISION_HINT"] = "off"
    os.environ["FORGE_REDUNDANT_GUARD"] = (
        "on" if mode in ("guardA", "guardAB") else "off")
    os.environ["FORGE_COMPLETION_READY"] = (
        "on" if mode in ("guardB", "guardAB") else "off")
    session_id = f"p10-{case.id}-{mode}-{rep}"
    try:
        res = await rt.run_turn(case.prompt, session_id=session_id,
                                mode="async", max_turns=20)
        # 若暂停在审批：自动批准并 resume（最多 4 次），使实验能走到终态。
        for _ in range(4):
            if res.task.state.value != "waiting_approval":
                break
            if await _auto_approve(rt, res.task.id) == 0:
                break
            try:
                res = await rt.run_turn("", task_id=res.task.id, mode="async", max_turns=20)
            except Exception:
                break
    except Exception as exc:  # noqa: BLE001
        return {"case": case.id, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    rid = res.task.id
    tool_calls = rt.tasks.list_tool_calls(rid, limit=400)
    events = [{"type": e.event_type, "payload": e.payload}
              for e in rt.tasks.list_events(rid, limit=500)]
    truth = _tool_truth(events)
    terminal_kind = ""
    for e in reversed(events):
        if e["type"] == "run.terminal":
            terminal_kind = str((e["payload"] or {}).get("kind") or "")
            break
    # 最终 assistant 文本
    answer = ""
    cid = rt.tasks.get_run_container_id(rid)
    if cid:
        for m in reversed(rt.tasks.list_messages(cid, limit=200)):
            if m.get("run_id") == rid and m.get("role") == "assistant":
                answer = m.get("content") or ""
                break
    raw = {
        "id": case.id,
        "state": res.task.state.value,
        "observed_state_at_deadline": res.task.state.value,
        "terminal_state": res.task.state.value if res.task.state.value in TERMINAL else None,
        "terminal_kind": terminal_kind,
        "answer": answer,
        "error": res.error,
        "tool_calls_total": truth["executions"],
        "tool_truth": truth,
        "tools": [{"name": tc.get("tool_name"), "status": tc.get("status"),
                   "normalized_args": tc.get("arguments_json")} for tc in tool_calls],
    }
    from benchmark.evaluator import observation_from_raw
    obs = observation_from_raw(raw)
    obs.case_id = case.id
    r = evaluate_case(case.expected, obs)
    raw["_eval"] = {"behavior_result": r.behavior_result, "production_outcome": r.production_outcome,
                    "execution_validity": r.execution_validity, "tool_executions": truth["executions"]}
    return raw


async def run(mode: str, n: int, cases: list[str], outdir: Path) -> dict:
    import main as main_module  # noqa: F401  加载 .env
    from runtime.runner import AgentRuntime

    # 硬约束：本轮实验只允许本地模型，禁止调用 gateway/agnes。
    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref != "local":
        raise SystemExit(f"FORGE_MODEL_PREF={pref!r}；本轮禁止调用 gateway，必须为 local")

    tmp = tempfile.mkdtemp(prefix="p10_coding_")
    rt = AgentRuntime(db_path=os.path.join(tmp, "agent.db"))
    rt._ensure()
    rows = []
    for cid in cases:
        case = get_case(cid)
        runs = []
        for rep in range(n):
            runs.append(await _run_case(rt, case, mode, rep))
        rows.append({"case": cid, "runs": runs})
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "partial.json").write_text(
            json.dumps({"mode": mode, "n": n, "cases": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        last = runs[-1]
        print(f"  {cid} {last.get('_eval', {}).get('behavior_result')} "
              f"exec={last.get('_eval', {}).get('tool_executions')} "
              f"kind={last.get('terminal_kind')}", flush=True)
    return {"mode": mode, "n": n, "cases": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.coding_experiment")
    parser.add_argument("--mode",
                        choices=["baseline", "hint", "guardA", "guardB", "guardAB"],
                        required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--cases", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    cases = ([c for c in args.cases.split(",") if c]
             if args.cases else CODING_SET + CONTROL)
    outdir = Path(args.out)
    result = asyncio.run(run(args.mode, args.runs, cases, outdir))
    (outdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print(f"{args.mode} -> {outdir/'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
