import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import code_exec as code_exec_mod
import runtime.sandbox_snapshot as ss


class SandboxSnapshotTests(unittest.TestCase):
    """沙箱快照/回滚：完全离线（临时目录替换沙箱与快照根）。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="ssnap_"))
        code_exec_mod.SANDBOX_ROOT = self._tmp / "code_sandbox"
        ss.SANDBOX_ROOT = code_exec_mod.SANDBOX_ROOT
        ss.SNAPSHOT_ROOT = self._tmp / "logs" / "sandbox_snapshots"
        (ss.SANDBOX_ROOT / "demo").mkdir(parents=True)

    def _write(self, rel: str, content: str) -> None:
        p = ss.SANDBOX_ROOT / "demo" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def test_snapshot_then_rollback_restores(self) -> None:
        self._write("a.py", "x = 1\n")
        self._write("sub/b.py", "y = 2\n")
        snap = ss.snapshot_impl("demo", note="动手前")
        self.assertIn("已拍快照", snap)
        self.assertIn("2 个文件", snap)
        self._write("a.py", "x = 999\n")
        self._write("c.py", "new\n")
        (ss.SANDBOX_ROOT / "demo" / "sub" / "b.py").unlink()
        roll = ss.rollback_impl("demo")
        self.assertIn("已回滚", roll)
        self.assertIn("恢复", roll)
        self.assertEqual((ss.SANDBOX_ROOT / "demo" / "a.py").read_text(encoding="utf-8"), "x = 1\n")
        self.assertEqual((ss.SANDBOX_ROOT / "demo" / "sub" / "b.py").read_text(encoding="utf-8"), "y = 2\n")
        self.assertFalse((ss.SANDBOX_ROOT / "demo" / "c.py").exists())

    def test_dry_run_changes_nothing(self) -> None:
        self._write("a.py", "x = 1\n")
        ss.snapshot_impl("demo")
        self._write("a.py", "x = 2\n")
        out = ss.rollback_impl("demo", dry_run=True)
        self.assertIn("dry_run", out)
        self.assertIn("修改", out)
        self.assertEqual((ss.SANDBOX_ROOT / "demo" / "a.py").read_text(encoding="utf-8"), "x = 2\n")

    def test_list_and_prune_to_max(self) -> None:
        self._write("a.py", "x = 1\n")
        for _ in range(ss.MAX_SNAPSHOTS + 2):
            ss.snapshot_impl("demo")
        listing = ss.list_snapshots_impl("demo")
        entries = [p for p in (ss.SNAPSHOT_ROOT / "demo").iterdir() if p.is_dir()]
        self.assertEqual(len(entries), ss.MAX_SNAPSHOTS)
        self.assertIn("个文件", listing)

    def test_rollback_by_id_and_bad_id_rejected(self) -> None:
        self._write("a.py", "v1\n")
        ss.snapshot_impl("demo")
        ids = [p.name for p in (ss.SNAPSHOT_ROOT / "demo").iterdir() if p.is_dir()]
        self._write("a.py", "v2\n")
        out = ss.rollback_impl("demo", snapshot_id=ids[0])
        self.assertIn("已回滚", out)
        self.assertEqual((ss.SANDBOX_ROOT / "demo" / "a.py").read_text(encoding="utf-8"), "v1\n")
        bad = ss.rollback_impl("demo", snapshot_id="../../evil")
        self.assertIn("不合法", bad)

    def test_errors_when_nothing(self) -> None:
        self.assertIn("不存在", ss.snapshot_impl("nope"))
        self.assertIn("没有可用的快照", ss.rollback_impl("demo"))
        self.assertIn("还没有快照", ss.list_snapshots_impl("demo"))
        (ss.SANDBOX_ROOT / "demo" / "a.py").write_text("x\n", encoding="utf-8")
        ss.snapshot_impl("demo")
        self.assertIn("个文件", ss.list_snapshots_impl("demo"))

    def test_skips_pycache_and_big_files(self) -> None:
        self._write("a.py", "x = 1\n")
        self._write("__pycache__/a.cpython.pyc", "bin")
        self._write("big.py", "z" * (ss.MAX_FILE_BYTES + 1))
        out = ss.snapshot_impl("demo")
        self.assertIn("1 个文件", out)


if __name__ == "__main__":
    unittest.main()
