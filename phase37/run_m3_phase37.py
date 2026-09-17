"""Run M3 benchmark and collect results."""
import asyncio, sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from benchmark.m3_closed_loop import run_m3_case, M3_FIX, M3_PROMPT


async def main():
    prompt = M3_PROMPT.format(fix=M3_FIX.replace("\\", "/"))
    results = []
    for i in range(3):
        print(f"\n{'='*60}")
        print(f"Running M3 case {i}...")
        print(f"{'='*60}")
        r = await run_m3_case(prompt, i, max_turns=20)
        results.append(r)
        print(f"state={r['state']} class={r['failure_class']} "
              f"boundary={r['repair_boundary_established']} "
              f"muts={r['mutations']} vers={r['verifications']} "
              f"task={r['task_success']} loop={r['repair_loop_success']}")
        print(f"llm_calls={r['llm_calls']} tool_calls={r['tool_calls']} lat={r['latency_s']}s")
    
    # Save results
    out = {"M3": {"runs": results}}
    out_path = Path(__file__).parent / "m3_phase37_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved to {out_path}")
    
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
