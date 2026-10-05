"""S 层写重放的幂等性契约（纵深防御，非本轮主因）。

## 定位说明（重要，读代码前先看这段）

本文件曾被用来诊断「真故障路径 `task.failed` 落库 2 条」，**那个诊断是错的**。
真实根因是 `mark_failure` 与 `transition` **双写**同一条事件
（`transition` 在同事务里权威写 `task.<state>`，`mark_failure` 又`add_event`
一次），已由`tests/test_task_no_double_write.py` 锁死。

本文件锁的是**另一件事**：`_retrying_write` 的docstring 要求「传入的块必须对
同一状态可安全重放」，而 `add_event` 的 INSERT 不满足。保留 `event_key` 唯一索引
+ `INSERT OR IGNORE` 作为纵深防御 —— 若将来又有非幂等块被交给它，至少不会静默
产生重复行。

**这不是"为了让上面那个测试变绿"**：本文件在双写修复之前就已存在并通过，
它测的是重放路径，与双写路径正交。

## 为什么单语句不等于幂等

`add_event` 曾注释「单语句 + 固定 payload，重放天然幂等」。后半句不成立：
`task_events` 只有自增主键、没有业务唯一键，重放会**再插一行**而不是覆盖。
`TaskEvent.created_at` 在块外生成、块内复用，看似"固定"，但它挡不住
"这一行已经被插过一次"这件事本身。

唯一键也不能用 `(task_id, event_type, created_at)`：`created_at` 精度是**秒级**
（`utcnow_iso` 用 `timespec="seconds"`），同一秒内的合法重复事件
（两次 `task.running`）会被误去重。故用块外生成的随机 `event_key`。

## 覆盖的契约

1. 触发重试的错误必须是**可重试类**（否则测试是在测不存在的行为）——反空转。
2. **重放不得产生重复行**（核心）。
3. 不触发重试的正常路径也必须恰好一行（防止修复把事件写丢或写双）。
4. 非重试错误必须仍然立即抛，不得被"幂等去重"顺带吞掉。
"""

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.task_manager import TaskManager


def _busy() -> sqlite3.OperationalError:
    """构造一个「可重试类」的 SQLite 错误。"""
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


def _patched_connect(original, *, fail_first_commit_with=None, after=None):
    """造一个替换版 `_connect`：在第一次连接里注入指定异常。

    - `fail_first_commit_with`：块执行完（INSERT 已落库）后，由 commit 抛该异常。
      这是真实 WAL 竞争下的形态 —— 正是它触发 `_retrying_write` 的重放。
    - `after`：每次 `with` 退出后调用，用于计数。
    """
    state = {"connects": 0}

    def _wrapper(self):
        @contextlib.contextmanager
        def _cm():
            state["connects"] += 1
            n = state["connects"]
            with original(self) as conn:
                yield conn
                # 在 with 退出时（连接即将关闭前）抛可重试错。
                # 此刻 INSERT 已执行完毕。若连接是自动 commit 模式，事务已经
                # 落库；即便不是，重放时那条 INSERT 仍会再执行一次。
                # 这正是真实 WAL 竞争下的形态：写发出去了，提交/收尾阶段报BUSY。
                if fail_first_commit_with is not None and n == 1:
                    conn.commit()
                    raise fail_first_commit_with
            if after is not None:
                after(n)

        return _cm()

    _wrapper.state = state  # type: ignore[attr-defined]
    _wrapper.__name__ = "_connect"
    return _wrapper


class EventReplayIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="evtidem_"))
        self.mgr = TaskManager(str(self.tmp / "agent.db"))
        self.container = self.mgr.get_or_create_container("proj-evt")
        self.run = self.mgr.create_task(
            session_id="proj-s", goal="g", thread_id=self.container["id"]
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _count(self, event_type: str) -> int:
        return len([e for e in self.mgr.list_events(self.run.id, limit=1000)
                    if e.event_type == event_type])

    def test_replay_is_actually_reachable(self) -> None:
        """**反空转**：先证明「重放」这条路径真能被触发。

        没有这一条，一个「永远不重试」的实现也能让下一个测试变绿。
        """
        patched = _patched_connect(TaskManager._connect, fail_first_commit_with=_busy())
        with mock.patch.object(TaskManager, "_connect", patched):
            self.mgr.add_event(self.run.id, "task.failed", {"error": "boom"})

        self.assertGreaterEqual(
            patched.state["connects"], 2,
            f"只开过 {patched.state['connects']} 次连接（期望 >=2）："
            " 注入的提交期失败没有触发重放 ⇒ 下一个测试可能根本没在测重放路径。",
        )

    def test_replay_must_not_duplicate_event(self) -> None:
        """**核心契约**：一次逻辑写入 + 一次重试 == 库里仍然只有一行。"""
        patched = _patched_connect(TaskManager._connect, fail_first_commit_with=_busy())
        with mock.patch.object(TaskManager, "_connect", patched):
            self.mgr.add_event(self.run.id, "task.failed", {"error": "boom"})

        n = self._count("task.failed")
        self.assertEqual(
            n, 1,
            f"一次 add_event 落库 {n} 行（期望 1）。"
            " 重放整块时 INSERT 产生了重复行 —— `_retrying_write` 的幂等性契约"
            " 被 add_event 违反（docstring 要求「传入的块必须对同一状态可安全重放」，"
            " 而 add_event 的注释误以为「单语句 + 固定 payload」就等于幂等）。",
        )

    def test_normal_path_still_writes_exactly_one(self) -> None:
        """不触发重试的正常路径也必须恰好一行（防止修复把事件写丢或写双）。"""
        self.mgr.add_event(self.run.id, "task.result", {"summary": "ok"})
        self.assertEqual(self._count("task.result"), 1)

    def test_non_retryable_error_still_raises_immediately(self) -> None:
        """幂等去重不得顺带吞掉「立刻失败」的错误，也不得把它变成慢失败。"""
        exc = sqlite3.IntegrityError("UNIQUE constraint failed: x")
        exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT

        def _after(n: int) -> None:
            if n == 1:
                raise exc

        patched = _patched_connect(TaskManager._connect, after=_after)
        with mock.patch.object(TaskManager, "_connect", patched):
            with self.assertRaises(sqlite3.IntegrityError):
                self.mgr.add_event(self.run.id, "task.failed", {"error": "x"})

        self.assertEqual(
            patched.state["connects"], 1,
            "约束类错误不可重试，必须第一次就抛；重试会把「立刻失败」的配置问题"
            " 变成「慢 3 次后才失败」，反而更难排查。",
        )


if __name__ == "__main__":
    unittest.main()
