"""P0-4 S 层连接韧性测试。

要锁死的三件事（每条都对应本轮实际踩到的坑）：
1. 按 db_path 串行化连接生命周期 —— 治本，缺了它只加重试**无效**（实测 3 次
   退避耗尽后仍抛同一错）；
2. 可重试错误分类 —— 只放行 BUSY/LOCKED/READONLY，权限/约束类必须立即抛；
3. 事务失败必须回滚 —— 否则残留事务状态会污染同一 TaskManager 的后续连接。
"""

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.task_manager import (
    _CONNECT_RETRIES,
    _DB_LOCKS,
    _RETRYABLE_ERRNOS,
    _WRITE_RETRIES,
    TaskManager,
    _db_lock_for,
    _is_retryable_sqlite_error,
)


class RetryClassificationTests(unittest.TestCase):
    """错误分类：放行该放行的，挡住不该重试的。"""

    def test_busy_and_locked_are_retryable(self) -> None:
        for name in ("SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_READONLY"):
            with self.subTest(name=name):
                self.assertIn(getattr(sqlite3, name), _RETRYABLE_ERRNOS)

    def test_integrity_and_operational_misc_are_not_retryable(self) -> None:
        """唯一约束失败重试无意义：必须立刻抛，不能被拖 3 次。"""
        exc = sqlite3.IntegrityError("UNIQUE constraint failed: tasks.id")
        self.assertFalse(_is_retryable_sqlite_error(exc))

    def test_no_such_table_is_not_retryable(self) -> None:
        exc = sqlite3.OperationalError("no such table: tasks")
        self.assertFalse(_is_retryable_sqlite_error(exc))

    def test_readonly_database_is_retryable(self) -> None:
        """本轮实测到的真实故障形态（Windows + WAL 并发）。"""
        exc = sqlite3.OperationalError("attempt to write a readonly database")
        self.assertTrue(_is_retryable_sqlite_error(exc))

    def test_database_is_locked_is_retryable(self) -> None:
        self.assertTrue(
            _is_retryable_sqlite_error(sqlite3.OperationalError("database is locked"))
        )

    def test_budget_is_bounded(self) -> None:
        """重试预算是有界的：不能让一次 Run 被无限拖住。"""
        self.assertLessEqual(_CONNECT_RETRIES, 3)
        self.assertLessEqual(_WRITE_RETRIES, 3)
        self.assertGreaterEqual(_CONNECT_RETRIES, 2)


class DbLockTests(unittest.TestCase):
    """按 db_path 的锁：同路径复用、异路径互不影响、且可重入。"""

    def test_same_path_returns_same_lock(self) -> None:
        a = _db_lock_for(Path("var/state/x.db"))
        b = _db_lock_for(Path("var/state/x.db"))
        self.assertIs(a, b, "同一 db_path 必须复用同一把锁，否则等于没锁")

    def test_different_paths_get_different_locks(self) -> None:
        a = _db_lock_for(Path("var/state/a.db"))
        b = _db_lock_for(Path("var/state/b.db"))
        self.assertIsNot(a, b, "不同 DB 不应互相阻塞")

    def test_lock_is_reentrant(self) -> None:
        """可重入：同线程嵌套获取不得自锁（否则历史顺序复用型调用会死锁）。"""
        lock = _db_lock_for(Path("var/state/reentrant.db"))
        acquired = []

        def worker() -> None:
            with lock:
                with lock:  # 第二次获取：RLock 允许
                    acquired.append(True)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "RLock 嵌套获取被自锁阻塞")
        self.assertEqual(acquired, [True])

    def test_lock_actually_serializes_across_threads(self) -> None:
        """并发进入必须互斥（这是治本的那一层，必须有测试钉住）。"""
        lock = _db_lock_for(Path("var/state/serialize.db"))
        inside = []
        peak = []
        guard = threading.Lock()

        def worker() -> None:
            with lock:
                with guard:
                    inside.append(1)
                    peak.append(len(inside))
                # 持锁一小段时间，让竞争者有机会进入
                threading.Event().wait(0.02)
                with guard:
                    inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "线程未完成：锁未正确串行化")
        self.assertEqual(max(peak), 1, "锁失效：多线程同时进入了临界区")


class RollbackOnFailureTests(unittest.TestCase):
    """块内失败后不得留下可见的半截事务。

    注：这里**不能**断言"没有残留行"来验证显式 `rollback()` ——
    实测中和掉 rollback 后该断言仍然通过，因为 Python sqlite3 在 DML 前隐式
    BEGIN，而 IntegrityError 路径上的 `conn.close()` 本身就会回滚未提交事务。
    那种断言是 test vacuity（P0-3 同类问题），故改为断言**可观测差异**：
    失败之后同一个 TaskManager 必须仍能正常读写（连接未被留在坏状态）。
    """

    def test_manager_usable_after_failed_block(self) -> None:
        tm = TaskManager(db_path=str(Path(tempfile.mkdtemp(prefix="rb_")) / "agent.db"))
        # 先落 "ok1"，制造一个必然撞主键的块（首条插入 ok2，第二条重插 ok1）
        tm._retrying_write(
            lambda conn: conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?,?,?)", ("ok1", "n", "t")
            )
        )

        def _violates_pk(conn):
            conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?,?,?)", ("ok2", "n", "t")
            )
            conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?,?,?)", ("ok1", "n", "t")
            )

        with self.assertRaises(sqlite3.IntegrityError):
            tm._retrying_write(_violates_pk)

        # 关键断言：失败之后连接必须回到可用状态（未提交事务不得泄漏到后续操作）
        tm._retrying_write(
            lambda conn: conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?,?,?)", ("after", "n", "t")
            )
        )
        ids = [p["id"] for p in tm.list_projects()]
        self.assertIn("after", ids, "失败后 TaskManager 必须仍可写入")
        self.assertNotIn(
            "ok2", ids, "块内首条 INSERT 不得残留（未提交事务泄漏）"
        )


class _FlakyConn:
    """模拟连接打开被 WAL 降级：前 N 次失败，之后成功。"""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.row_factory = sqlite3.Row
        self.closed = False

    def execute(self, *a, **k):
        if self.closed:
            raise sqlite3.ProgrammingError("Connection closed")
        return self._real.execute(*a, **k)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        self.closed = True
        self._real.close()


class WriteRetryTests(unittest.TestCase):
    """_retrying_write 必须真的重放整块，而不是只重试连接。"""

    def test_replays_whole_block_on_retryable_error(self) -> None:
        tm = TaskManager(db_path=str(Path(tempfile.mkdtemp(prefix="wr_")) / "agent.db"))
        pass  # TaskManager.__init__ 已完成建库/迁移
        calls = {"n": 0}
        real_connect = sqlite3.connect

        def flaky_connect(*a, **k):
            calls["n"] += 1
            conn = real_connect(*a, **k)
            if calls["n"] <= 2:  # 前两次连接一打开就降级
                proxy = _FlakyConn(conn)
                def boom(*_a, **_k):
                    raise sqlite3.OperationalError("attempt to write a readonly database")
                proxy.execute = boom
                return proxy
            return conn

        sqlite3.connect = flaky_connect
        try:
            tm._retrying_write(
                lambda conn: conn.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?,?,?)", ("r1", "n", "t")
                )
            )
        finally:
            sqlite3.connect = real_connect

        self.assertGreaterEqual(calls["n"], 3, "未在重试：连接降级后直接放弃了")
        self.assertEqual(
            [p["id"] for p in tm.list_projects()],
            ["r1"],
            "重放后应恰好写入一行（不得重复）",
        )

    def test_does_not_retry_permanent_error(self) -> None:
        tm = TaskManager(db_path=str(Path(tempfile.mkdtemp(prefix="wn_")) / "agent.db"))
        pass  # TaskManager.__init__ 已完成建库/迁移
        attempts = {"n": 0}

        def bad(conn):
            attempts["n"] += 1
            conn.execute("INSERT INTO projects (id, name, created_at) VALUES (?,?,?) (nope)")

        with self.assertRaises(sqlite3.Error):
            tm._retrying_write(bad)
        self.assertEqual(attempts["n"], 1, "永久性错误不应被重试（每次都是白等）")


class ConcurrentWritesTests(unittest.TestCase):
    """真并发写：同一 DB 上多线程写不应出现 readonly / 丢失。"""

    def test_parallel_writes_all_land(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="cw_"))
        tm = TaskManager(db_path=str(tmp / "agent.db"))
        pass  # TaskManager.__init__ 已完成建库/迁移
        errors: list[BaseException] = []
        barrier = threading.Barrier(6)

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                for j in range(4):
                    tm.add_event(f"task_{i}", "test.event", {"i": i, "j": j})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive())

        self.assertEqual(errors, [], f"并发写出现异常：{errors[:3]}")
        total = 0
        for i in range(6):
            total += len(tm.list_events(f"task_{i}", limit=100))
        self.assertEqual(total, 24, "并发写有丢失")


if __name__ == "__main__":
    unittest.main()
