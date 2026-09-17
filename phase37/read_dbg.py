"""Read and display Phase 37 debug data."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

events = json.load(open(Path(__file__).parent / "m3_dbg_events.json", encoding="utf-8"))
hooks = json.load(open(Path(__file__).parent / "m3_dbg_hooks.json", encoding="utf-8"))

print(f"state: {events['state']}")
print(f"error: {events['error']}")
print(f"total events: {len(events['events'])}")
print(f"llm_calls (hooks): {hooks['llm_calls']}")
print(f"tool_calls (hooks): {len(hooks['tool_calls'])}")

tool_events = [e for e in events['events'] if e['type'] == 'tool.invocation']
print(f"\ntool.invocation events: {len(tool_events)}")
for i, e in enumerate(tool_events):
    p = e['payload']
    name = p.get('tool_name', '?')
    blocked = p.get('blocked', False)
    epoch = p.get('workspace_epoch', 0)
    cap = p.get('tool_capability', '?')
    status = p.get('execution_status', '?')
    result = str(p.get('result_summary') or '')[:60]
    print(f"  [{i:3d}] {name:20s} blocked={blocked} epoch={epoch} cap={cap} status={status} result={result}")

print(f"\n=== HOOKS TOOL CALLS ===")
for i, tc in enumerate(hooks['tool_calls']):
    name = tc.get('name', '?')
    result = str(tc.get('result') or '')[:80]
    print(f"  [{i:3d}] {name:20s} -> {result}")
