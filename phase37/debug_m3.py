"""Debug Phase 37 M3 Run: write raw event data to file."""
import asyncio, sys, tempfile, os, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

    # Write raw event data to JSON
    out_path = Path(__file__).parent / "m3_dbg_events.json"
    event_data = []
    for e in events:
        event_data.append({
            "type": getattr(e, "event_type", ""),
            "payload": getattr(e, "payload", {}) or {},
        })
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"state": state, "error": err, "events": event_data}, f, ensure_ascii=False, indent=2)
    
    # Also save hooks
    hook_data = {
        "llm_calls": hooks.llm_calls,
        "tool_calls": hooks.tool_calls,
    }
    with open(Path(__file__).parent / "m3_dbg_hooks.json", "w", encoding="utf-8") as f:
        json.dump(hook_data, f, ensure_ascii=False, indent=2)
    
    print(f"state={state}, error={err}")
    print(f"total events: {len(events)}")
    print(f"total hooks tool_calls: {len(hooks.tool_calls)}")
    print(f"saved to: {out_path}")
    print(f"saved to: {Path(__file__).parent / 'm3_dbg_hooks.json'}")
    
    # Print tool invocations summary
    tool_events = [e for e in events if getattr(e, "event_type", None) == "tool.invocation"]
    print(f"\ntool.invocation events: {len(tool_events)}")
    for i, e in enumerate(tool_events):
        p = e.payload or {}
        print(f"  [{i:3d}] name={p.get('tool_name')} blocked={p.get('blocked')} "
              f"epoch={p.get('workspace_epoch')} cap={p.get('tool_capability')} "
              f"status={p.get('execution_status')}")


if __name__ == "__main__":
    asyncio.run(debug_run())
