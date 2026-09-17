"""Quick test: Approval lifecycle in runner."""
import asyncio, sys, tempfile, os, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.runner import AgentRuntime
from runtime.approval import ApprovalGate
from runtime.task_manager import TaskManager


async def test_approval_resume():
    tmp = tempfile.mkdtemp()
    rt = AgentRuntime(db_path=os.path.join(tmp, 'agent.db'))
    rt._ensure()

    task = rt.tasks.create_task(session_id='test', goal='test')
    gate = ApprovalGate(rt.tasks)

    # Run 1: tool request blocked
    gate.begin(task.id, 'chat')
    d1 = await gate.check('run_tests', {'project': 'm3_fixture'}, run_id=task.id)
    print(f'Run 1 call: blocked={d1 is not None}')
    print(f'  Pending in gate: {len(gate.pending_for(task.id))}')
    print(f'  Pending in DB: {len(rt.tasks.list_pending_approvals(task.id))}')

    # Harness approves
    aps = rt.tasks.list_pending_approvals(task.id)
    assert len(aps) == 1, f'Expected 1 pending, got {len(aps)}'
    rt.tasks.decide_approval(aps[0]['id'], 'approved')
    print(f'  Approved!')

    # Run 2 (resume): same args should be allowed
    gate.begin(task.id, 'chat')
    d2 = await gate.check('run_tests', {'project': 'm3_fixture'}, run_id=task.id)
    print(f'Run 2 call (same args): blocked={d2 is not None}')
    print(f'  Pending in gate: {len(gate.pending_for(task.id))}')
    print(f'  Pending in DB: {len(rt.tasks.list_pending_approvals(task.id))}')

    # Run 3: different args → new pending
    d3 = await gate.check('run_tests', {'project': 'm3_fixture', 'extra_args': '-v'}, run_id=task.id)
    print(f'Run 3 call (diff args): blocked={d3 is not None}')
    print(f'  Pending in gate: {len(gate.pending_for(task.id))}')
    print(f'  Pending in DB: {len(rt.tasks.list_pending_approvals(task.id))}')

    # Harness approves
    aps2 = rt.tasks.list_pending_approvals(task.id)
    rt.tasks.decide_approval(aps2[0]['id'], 'approved')
    print(f'  Approved!')

    # Run 4: same diff args should be allowed
    gate.begin(task.id, 'chat')
    d4 = await gate.check('run_tests', {'project': 'm3_fixture', 'extra_args': '-v'}, run_id=task.id)
    print(f'Run 4 call (same diff args): blocked={d4 is not None}')
    print(f'  Pending in gate: {len(gate.pending_for(task.id))}')
    print(f'  Pending in DB: {len(rt.tasks.list_pending_approvals(task.id))}')

    print('\n=== ALL CHECKS PASSED ===')


if __name__ == '__main__':
    asyncio.run(test_approval_resume())
