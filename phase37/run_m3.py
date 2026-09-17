"""Phase 37: M3 Production-Parity Test with Approval Lifecycle."""
import asyncio, sys, tempfile, os, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.m3_closed_loop import run_m3_case, M3_FIX, M3_PROMPT


async def main():
    prompt = M3_PROMPT.format(fix=M3_FIX.replace("\\", "/"))
    
    # Run 3 M3 cases
    results = []
    for i in range(3):
        print(f"\n{'='*60}")
        print(f"M3 Run {i}")
        print(f"{'='*60}")
        r = await run_m3_case(prompt, i, max_turns=20)
        results.append(r)
        
        print(f"state: {r['state']}")
        print(f"class: {r['failure_class']}")
        print(f"boundary: {r['repair_boundary_established']}")
        print(f"injected: {r['failure_evidence_injected']}")
        print(f"muts: {r['mutations']}")
        print(f"vers: {r['verifications']}")
        print(f"repair: {r['repair']}")
        print(f"reverify: {r['reverify']}")
        print(f"task_success: {r['task_success']}")
        print(f"loop_success: {r['repair_loop_success']}")
        print()
        
        # Full tool log (safe ASCII output)
        for t in r['tool_log']:
            name = t['tool']
            ok = t.get('ok')
            head = str(t['result_head'])[:80].encode('ascii', 'replace').decode('ascii')
            print(f"  [{name}] ok={ok} -> {head}")
    
    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        print(f"  Run {r['i']}: state={r['state']} class={r['failure_class']} "
              f"boundary={r['repair_boundary_established']} muts={r['mutations']} "
              f"vers={r['verifications']} task={r['task_success']} loop={r['repair_loop_success']}")


if __name__ == "__main__":
    asyncio.run(main())
