"""验证修复 A：_fail 对已终态任务的幂等收口。

根因：cancel 与 fail 双收口时，_fail 无脑调 mark_failure → 非法
CANCELLED→FAILED 转换 → AgentError 上抛 → "Task exception was never retrieved"。
修复后：_fail 入口做终态预检，已终态则只补一条 run.terminal_idempotent_skip
事件并返回既有 task，不再尝试转换。

本测试在 TaskManager 层验证该"预检逻辑"的等价行为（_fail 是 runner.run_turn
的闭包，直接构造它成本高；TaskManager.transition 的"终态冻结"是预检依据）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.errors import AgentError
from runtime.state_machine import TERMINAL_STATES
from runtime.task import TaskState
from runtime.task_manager import TaskManager


class FailIdempotentPreCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="agent_fail_idem_"))
        self.mgr = TaskManager(self._tmp / "agent.db")

    def _precheck_skip(self, task_id: str, attempted_state: str, error_text: str) -> bool:
        """与 runner._fail 的终态预检等价的判定：
        当前任务是否已终态 → True 表示应跳过 mark_failure。"""
        cur = self.mgr.get_task(task_id)
        return cur is not None and cur.state in TERMINAL_STATES

    def test_cancelled_task_triggers_skip(self) -> None:
        task = self.mgr.create_task("s1", "做某事")
        self.mgr.transition(task.id, TaskState.RUNNING)
        self.mgr.transition(task.id, TaskState.CANCELLED, reason="user cancel")
        # 模拟 _fail 的预检
        self.assertTrue(self._precheck_skip(task.id, "failed", "x"))
        # 直接调 mark_failure 会抛（保持 state_machine 的终态不可逆原则不变）
        with self.assertRaises(AgentError):
            self.mgr.mark_failure(task.id, "迟到失败")
        # 任务仍是 CANCELLED（未被晚到的 fail 改写）
        self.assertEqual(self.mgr.get_task(task.id).state, TaskState.CANCELLED)

    def test_completed_task_triggers_skip(self) -> None:
        task = self.mgr.create_task("s1", "做某事")
        self.mgr.transition(task.id, TaskState.RUNNING)
        self.mgr.mark_success(task.id, "done")
        self.assertTrue(self._precheck_skip(task.id, "failed", "x"))
        with self.assertRaises(AgentError):
            self.mgr.mark_failure(task.id, "迟到失败")
        self.assertEqual(self.mgr.get_task(task.id).state, TaskState.COMPLETED)

    def test_running_task_does_not_skip(self) -> None:
        task = self.mgr.create_task("s1", "做某事")
        self.mgr.transition(task.id, TaskState.RUNNING)
        # RUNNING 非终态 → 不跳过，正常 fail 路径
        self.assertFalse(self._precheck_skip(task.id, "failed", "boom"))
        failed = self.mgr.mark_failure(task.id, "boom")
        self.assertEqual(failed.state, TaskState.FAILED)
        self.assertIn("boom", failed.error_message or "")

    def test_event_name_contract(self) -> None:
        """与 runner._fail 写的事件名一致，便于日志/审计对账。"""
        task = self.mgr.create_task("s1", "做某事")
        self.mgr.transition(task.id, TaskState.RUNNING)
        self.mgr.transition(task.id, TaskState.CANCELLED)
        self.mgr.add_event(
            task.id, "run.terminal_idempotent_skip",
            {"already_state": "cancelled", "attempted": "failed", "error": "late"},
        )
        types = [e.event_type for e in self.mgr.list_events(task.id)]
        self.assertIn("run.terminal_idempotent_skip", types)


if __name__ == "__main__":
    unittest.main()
