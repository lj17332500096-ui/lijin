"""Analyze Phase 36 debug events (m3_dbg_events.json)."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

data = json.load(open(Path(__file__).parent / "m3_dbg_events.json", encoding="utf-8"))
hooks = json.load(open(Path(__file__).parent / "m3_dbg_hooks.json", encoding="utf-8"))

print(f"state: {data['state']}")
print(f"total events: {len(data['events'])}")

# Tool invocations
tool_events = [e for e in data['events'] if e['type'] == 'tool.invocation']
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

# Approval / terminal events
print(f"\n=== approval/terminal events ===")
for e in data['events']:
    t = e['type']
    if 'approval' in t or 'waiting_approval' in t or 'terminal' in t or 'obligation' in t:
        print(f"  {t}: {json.dumps(e['payload'], ensure_ascii=False)[:200]}")

# Hooks
print(f"\n=== hooks ({len(hooks['tool_calls'])} tool_calls) ===")
for i, tc in enumerate(hooks['tool_calls']):
    name = tc.get('name', '?')
    result = str(tc.get('result') or '')[:80]
    print(f"  [{i:3d}] {name:20s} -> {result}")
