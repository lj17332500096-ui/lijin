"""Compare Phase 36 vs Phase 37 debug data."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# Load both debug files
f1 = Path(__file__).parent / "m3_dbg_events.json"
f2 = Path(__file__).parent / "m3_dbg_v2.json"

data1 = json.load(open(f1, encoding="utf-8"))
data2 = json.load(open(f2, encoding="utf-8"))

print("=== Phase 36 debug (m3_dbg_events) ===")
print(f"state: {data1['state']}")
tool_events1 = [e for e in data1['events'] if e['type'] == 'tool.invocation']
print(f"tool.invocation events: {len(tool_events1)}")
for i, e in enumerate(tool_events1):
    p = e['payload']
    print(f"  [{i:3d}] {p.get('tool_name','?'):20s} blocked={p.get('blocked')} cap={p.get('tool_capability')} status={p.get('execution_status')}")
print()
hooks1 = json.load(open(Path(__file__).parent / "m3_dbg_hooks.json", encoding="utf-8"))
print(f"hooks tool_calls: {len(hooks1['tool_calls'])}")
print(f"hooks llm_calls: {hooks1['llm_calls']}")
print()

print("=== Phase 37 debug (m3_dbg_v2) ===")
print(f"state: {data2['state']}")
tool_events2 = [e for e in data2['events'] if e['type'] == 'tool.invocation']
print(f"tool.invocation events: {len(tool_events2)}")
for i, e in enumerate(tool_events2):
    p = e['payload']
    print(f"  [{i:3d}] {p.get('tool_name','?'):20s} blocked={p.get('blocked')} cap={p.get('tool_capability')} status={p.get('execution_status')}")
print()
print(f"hooks tool_calls: {len(data2['hooks']['tool_calls'])}")
print(f"hooks llm_calls: {data2['hooks']['llm_calls']}")
print(f"approval_count: {data2['approval_count']}")
print()

# Check provider failures in v2
print("=== Provider failures in v2 ===")
for e in data2['events']:
    if e['type'] == 'provider.failure':
        print(f"  {e['type']}: kind={e['payload'].get('kind')} public={e['payload'].get('public')}")
    if e['type'] == 'run.terminal':
        print(f"  {e['type']}: kind={e['payload'].get('kind')} state={e['payload'].get('state')}")
