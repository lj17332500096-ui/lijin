# -*- coding: utf-8 -*-
"""收尾清理是纯副作用：任何异常（含 SystemExit）都不得终止服务。

真实故障背景（本项目实际发生过）：
本地 8765 服务启动约 27 分钟后整个进程突然退出，堆栈为

    runner._close_run -> provider_gateway.take_attempts
        -> _persist_reset -> Path.unlink -> SystemExit(1)

即运行环境的安全护栏在删除文件时直接抛 SystemExit，而 `_persist_reset`
当时只吞了 Exception（SystemExit 继承自 BaseException，不受 Exception 覆盖），
异常一路冒泡到 uvicorn 顶层，直接把 Web 服务打死。

结论：清理路径必须能吞掉 SystemExit；同时保留 KeyboardInterrupt，保证
Ctrl+C 语义不受影响。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import runtime.provider_gateway as pg  # noqa: E402


class CleanupSwallowsSystemExitTests(unittest.TestCase):
    """清理路径必须静默失败，不得让 SystemExit 冒泡。"""

    def setUp(self) -> None:
        # 使用独立数据目录，避免污染真实 data/provider_attempts。
        self._tmp = Path(BASE) / "data" / "provider_attempts_cleanup_tmp"
        self._env = mock.patch.dict(
            os.environ,
            {"FORGE_DATA_DIR": str(self._tmp), "FORGE_PROVIDER_ATTEMPTS_PERSIST": "on"},
        )
        self._env.start()
        pg._BASDIR = None  # 让 _persist_path 按新 env 重建
        self.addCleanup(self._env.stop)
        self.addCleanup(self._rmtree_tmp)

    def _rmtree_tmp(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)
        pg._BASDIR = None

    def test_persist_reset_swallows_systemexit(self) -> None:
        run_id = "cleanup::systemexit"
        path = pg._persist_path(run_id)
        self.assertIsNotNone(path)
        path.write_text("{\"kind\": \"ok\"}\n", encoding="utf-8")

        # 模拟安全护栏：删文件时直接抛 SystemExit
        with mock.patch.object(Path, "unlink", side_effect=SystemExit(1)):
            pg._persist_reset(run_id)  # 不得抛出

    def test_take_attempts_survives_systemexit_on_unlink(self) -> None:
        """复刻真实崩溃栈：take_attempts -> _persist_reset -> unlink 抛 SystemExit。"""
        run_id = "cleanup::take_attempts"
        pg.record_attempt(run_id, {"kind": "ok", "latency_ms": 12, "model": "agnes-x"})
        self.assertTrue(pg._persist_path(run_id).exists())

        with mock.patch.object(Path, "unlink", side_effect=SystemExit(1)):
            attempts = pg.take_attempts(run_id)  # 不得抛出

        # 内存记录仍须完整返回：清理失败不能影响审计数据可用性。
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["model"], "agnes-x")

    def test_persist_reset_still_propagates_keyboard_interrupt(self) -> None:
        """Ctrl+C 语义必须保留：KeyboardInterrupt 不能被吞掉。"""
        run_id = "cleanup::sigint"
        path = pg._persist_path(run_id)
        path.write_text("{}", encoding="utf-8")

        with mock.patch.object(Path, "unlink", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                pg._persist_reset(run_id)


if __name__ == "__main__":
    unittest.main()
