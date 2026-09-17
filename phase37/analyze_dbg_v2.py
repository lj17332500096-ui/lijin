"""Analyze Phase 37 debug v2 data."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

data = json.load(open(Path(__file__).parent / "m3_dbg_v2.json", encoding="utf-8"))

print(f"state: {data['state']}")
print(f"error: {data['error']}")
print(f"approvals: {data['approval_count']}")
print(f"total events: {len(data['events'])}")
print(f"hooks tool_calls: {len(data['hooks']['tool_calls'])}")
print(f"hooks llm_calls: {data['hooks']['llm_calls']}")

# All events
print(f"\n=== ALL EVENTS ===")
for i, e in enumerate(data['events']):
    etype = e['type']
    payload = e.get('payload', {})
    preview = json.dumps(payload, ensure_ascii=False)[:100] if payload else ''
    print(f"  [{i:3d}] {etype}: {preview}")

# Tool events
tool_events = [e for e in data['events'] if e['type'] == 'tool.invocation']
print(f"\n=== TOOL INVOCATION EVENTS ({len(tool_events)}) ===")
for i, e in enumerate(tool_events):
    p = e['payload']
    name = p.get('tool_name', '?')
    blocked = p.get('blocked', False)
    epoch = p.get('workspace_epoch', 0)
    cap = p.get('tool_capability', '?')
    status = p.get('execution_status', '?')
    result = str(p.get('result_summary') or '')[:60]
    print(f"  [{i:3d}] {name:20s} blocked={blocked} epoch={epoch} cap={cap} status={status} result={result}")

# Approval events
print(f"\n=== APPROVAL/TERMINAL EVENTS ===")
for e in data['events']:
    if 'approval' in e['type'] or 'waiting_approval' in e['type'] or e['type'] == 'run.terminal':
        print(f"  {e['type']}: {json.dumps(e['payload'], ensure_ascii=False)[:200]}")
