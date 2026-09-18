"""诊断与任务管理入口：python -m runtime [选项]

--tools       逐个列出工具元数据
--tasks       列出最近任务（可用 --session <名> / --state <状态> / --limit N 过滤）
--task <id>   查看任务详情与事件流
--cancel <id> 取消一个未结束任务
（无参数）     打印工具汇总
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.errors import AgentError
from runtime.task import TaskState
from runtime.task_manager import TaskManager
from runtime.runner import AgentRuntime


def _arg_after(name: str, default: str = "") -> str:
    if name in sys.argv:
        idx = sys.argv.index(name)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return default


def main() -> int:
    base = Path(__file__).resolve().parent.parent
    if "--snapshot" in sys.argv or "--restore" in sys.argv:
        from runtime.snapshot import (create_snapshot, list_snapshots,
                                      restore_snapshot)

        agent_db = base / "agent.db"
        sessions_db = base / "sessions.sqlite"
        snap_dir = base / "logs" / "snapshots"
        if "--snapshot" in sys.argv:
            snap = create_snapshot(agent_db, sessions_db, snap_dir)
            print(f"快照已创建：{snap.root}")
            print(f"  id={snap.snapshot_id} hash(agent)={snap.manifest['agent_db_hash'][:12]}…")
            return 0
        target = _arg_after("--restore")
        snapshot = None
        if target:
            cand = Path(target)
            if cand.exists() and cand.is_dir():
                try:
                    manifest = json.loads((cand / "manifest.json").read_text(encoding="utf-8"))
                    from runtime.snapshot import Snapshot

                    snapshot = Snapshot(root=cand, snapshot_id=manifest.get("snapshot_id", cand.name),
                                        manifest=manifest)
                except Exception as exc:
                    print(f"快照目录解析失败：{exc}")
                    return 1
            else:
                for s in list_snapshots(snap_dir):
                    if s.snapshot_id == target or s.snapshot_id.endswith(target):
                        snapshot = s
                        break
        if snapshot is None:
            print("用法：--restore <snapshot_id|快照目录>；可用 --snapshot 先创建")
            return 1
        result = restore_snapshot(snapshot, agent_db, sessions_db, snap_dir)
        if result.get("ok"):
            print(f"恢复完成（pre-restore: {result.get('pre_restore')}）")
            print(f"一致性: ok={result['consistency']['ok']} "
                  f"orphans={len(result['consistency'].get('orphan_sessions', []))}")
            return 0
        print(f"恢复失败：{result.get('error')}")
        return 1

    runtime = AgentRuntime()

    if "--tools" in sys.argv:
        for binding in runtime.list_tools():
            spec = binding.spec
            flags = []
            if spec.side_effect:
                flags.append("副作用")
            if spec.destructive:
                flags.append("破坏性")
            if not spec.idempotent:
                flags.append("非幂等")
            tag = "、".join(flags) if flags else "只读/纯计算"
            print(
                f"{spec.name:<24} {spec.category:<10} risk={spec.risk:<6} "
                f"source={spec.source:<6} {tag}"
            )
        return 0

    tasks = TaskManager(runtime.db_path)

    if "--schedules" in sys.argv:
        for row in tasks.list_schedules():
            state = "启用" if row["enabled"] else "停用"
            print(
                f"{row['id']} [{state}] {row['name']} ｜ {row['schedule']} ｜ "
                f"下次 {row['next_run']} ｜ 上次 {row['last_run'] or '—'}"
            )
        return 0
    if "--runs" in sys.argv:
        sid = _arg_after("--runs") or None
        for run in tasks.list_schedule_runs(schedule_id=sid):
            print(
                f"{run['schedule_id']} @ {run['fire_at']} [{run['status']}] "
                f"task={run['task_id'] or '—'} {run['detail'] or ''}"[:160]
            )
        return 0

    if "--recover" in sys.argv:
        recovered = tasks.recover_stale_tasks()
        if recovered:
            print(f"已恢复 {len(recovered)} 个陈旧任务（RUNNING 超时 → failed）：")
            for task_id in recovered:
                print(f"  {task_id}")
        else:
            print("没有需要恢复的陈旧任务。")
        return 0

    if "--tasks" in sys.argv:
        session_id = _arg_after("--session") or None
        state = _arg_after("--state") or None
        if state:
            try:
                TaskState(state)
            except ValueError:
                print(f"未知状态：{state}（可用 {[s.value for s in TaskState]}）")
                return 2
        limit = int(_arg_after("--limit", "20") or 20)
        for task in tasks.list_tasks(session_id=session_id, state=state, limit=limit):
            usage = task.usage
            print(
                f"{task.id} [{task.state.value}] {task.goal[:60]} "
                f"| session={task.session_id} turns={usage.turns} tool={usage.tool_calls}"
            )
        return 0

    task_id = _arg_after("--task") or _arg_after("--cancel")
    if task_id:
        task = tasks.get_task(task_id)
        if task is None:
            print(f"task not found: {task_id}")
            return 1
        if "--cancel" in sys.argv:
            try:
                task = tasks.transition(task.id, TaskState.CANCELLED, reason="cli cancel")
                print(f"已取消：{task.id}（状态 {task.state.value}）")
            except AgentError as exc:
                print(f"取消失败：{exc}")
                return 1
            return 0
        print(f"id: {task.id}")
        print(f"session: {task.session_id} | agent: {task.agent_name}")
        print(f"state: {task.state.value} | goal: {task.goal}")
        print(
            f"usage: turns={task.usage.turns} tool_calls={task.usage.tool_calls} "
            f"failures={task.usage.failures}"
        )
        if task.error_message:
            print(f"error: {task.error_message[:200]}")
        if task.metadata:
            print(f"metadata: {str(task.metadata)[:200]}")
        latest = tasks.read_latest_checkpoint(task.id)
        if latest:
            print(f"checkpoint: v{latest['schema_version']} @ {latest['created_at']}")
            snap = latest["snapshot"]
            if snap.get("final_summary"):
                print(f"  final_summary: {snap['final_summary'][:200]}")
            if snap.get("error"):
                print(f"  error: {snap['error'][:200]}")
        print("events:")
        for event in tasks.list_events(task.id):
            print(f"  [{event.created_at}] {event.event_type} {str(event.payload)[:80]}")
        return 0

    print(runtime.summary())
    print("\n用法：python -m runtime --tasks / --task <id> / --cancel <id> / --recover / --schedules / --runs [<id>] / --tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
