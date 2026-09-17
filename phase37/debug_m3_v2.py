"""Phase 37: Write M3 debug data to file for analysis."""
import asyncio, sys, tempfile, os, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from benchmark.m3_closed_loop import reset_m3_fixture, M3_FIX, M3_PROMPT, M3Hooks, _RunnerProxy
import main as main_module
from runtime.runner import AgentRuntime


async def debug_run():
    prompt = M3_PROMPT.format(fix=M3_FIX.replace("\\", "/"))
    reset_m3_fixture()
    tmp = tempfile.mkdtemp(prefix="m3_dbg_")
    rt = AgentRuntime(db_path=os.path.join(tmp, "agent.db"))
    rt._ensure()

    os.environ["FORGE_OBLIGATION_GATE"] = "on"
    os.environ["FORGE_OBLIGATION_FEEDBACK"] = "on"
    os.environ["FORGE_REDUNDANT_GUARD"] = "off"
    os.environ["FORGE_COMPLETION_READY"] = "on"
    os.environ["FORGE_VERIFICATION_FOCUS"] = "off"
    os.environ["FORGE_DECISION_HINT"] = "off"

    sid = "proj-m3-dbg"
    try:
        wl = rt.tasks.create_work_location("m3-wl-dbg", local_path=M3_FIX)
        container = rt.tasks.get_or_create_container(sid)
        rt.tasks.update_project(container["id"], work_location_id=wl["id"])
    except Exception:
        pass

    hooks = M3Hooks()
    real_runner = main_module.Runner
    main_module.Runner = _RunnerProxy(real_runner, hooks)

    import time
    t0 = time.monotonic()
    state = "error"
    err = None
    try:
        res = await rt.run_turn(prompt, session_id=sid, mode="async", max_turns=20)
        approval_count = 0
        for turn_num in range(15):
            if res.task.state.value != "waiting_approval":
                break
            approved = 0
            for ap in rt.tasks.list_pending_approvals(res.task.id):
                tool = str(ap.get("tool_name") or "")
                decision = "denied" if tool in ("run_python", "code_loop") else "approved"
                try:
                    rt.tasks.decide_approval(ap["id"], decision)
                    approved += 1
                    approval_count += 1
                except Exception:
                    pass
            if approved == 0:
                break
            res = await rt.run_turn(prompt, task_id=res.task.id, mode="async", max_turns=20)
        state = res.task.state.value
        events = list(rt.tasks.list_events(res.task.id, limit=4000))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        err = f"{type(exc).__name__}: {str(exc)[:160]}"
        events = []
    finally:
        main_module.Runner = real_runner

    # Save all data
    out = {
        "state": state,
        "error": err,
        "approval_count": approval_count,
        "events": [{"type": getattr(e, "event_type", ""), "payload": getattr(e, "payload", {}) or {}} for e in events],
        "hooks": {"llm_calls": hooks.llm_calls, "tool_calls": hooks.tool_calls},
    }
    out_path = Path(__file__).parent / "m3_dbg_v2.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"state={state}, error={err}, approvals={approval_count}, events={len(events)}, hooks={len(hooks.tool_calls)}")
    print(f"saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(debug_run())
