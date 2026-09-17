"""Analyze Phase 37 debug JSON data."""
import json
from pathlib import Path

events = json.load(open(Path(__file__).parent / "m3_dbg_events.json", encoding="utf-8"))
hooks = json.load(open(Path(__file__).parent / "m3_dbg_hooks.json", encoding="utf-8"))

print(f"state: {events['state']}")
print(f"error: {events['error']}")
print(f"total events: {len(events['events'])}")

# Find run_tests events
print("\n=== run_tests events ===")
for i, e in enumerate(events["events"]):
    p = e["payload"]
    if p.get("tool_name") == "run_tests":
        print(f"  [{i}] blocked={p.get('blocked')} epoch={p.get('workspace_epoch')} "
              f"status={p.get('execution_status')} result={str(p.get('result_summary') or '')[:100]}")

# Find terminal events
print("\n=== terminal events ===")
for i, e in enumerate(events["events"]):
    if e["type"] == "run.terminal":
        print(f"  [{i}] {json.dumps(e['payload'], ensure_ascii=False)[:200]}")

# Find approval events
print("\n=== approval events ===")
for i, e in enumerate(events["events"]):
    if "approval" in e["type"]:
        print(f"  [{i}] {e['type']}: {json.dumps(e['payload'], ensure_ascii=False)[:150]}")

# Hooks run_tests
print("\n=== hooks run_tests ===")
for i, tc in enumerate(hooks["tool_calls"]):
    if tc.get("name") == "run_tests":
        print(f"  [{i}] result={str(tc.get('result') or '')[:100]}")

# Non-tool events
other = [e for e in events["events"] if e["type"] != "tool.invocation"]
print(f"\n=== non-tool events ({len(other)}) ===")
for e in other[:15]:
    print(f"  {e['type']}: {str(e.get('payload', {}))[:100]}")
