import asyncio
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.artifacts import ArtifactTracker, kind_for, sha256_of
from runtime.task import TaskState
from runtime.task_manager import TaskManager


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="artifacts_"))
        self.manager = TaskManager(self._tmp / "agent.db")
        self.task = self.manager.create_task("s1", "产出文档")

    def _write(self, relative: str, content: bytes) -> Path:
        path = self._tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_register_get_list(self) -> None:
        path = self._write("notes/a.md", "你好".encode("utf-8"))
        art = self.manager.register_artifact(
            task_id=self.task.id,
            session_id=self.task.session_id,
            name="a.md",
            kind=kind_for(path),
            storage_path=str(path),
            sha256=sha256_of(path),
            size_bytes=path.stat().st_size,
        )
        self.assertTrue(art["id"].startswith("art_"))
        fetched = self.manager.get_artifact(art["id"])
        self.assertEqual(fetched["name"], "a.md")
        self.assertEqual(fetched["kind"], "markdown")
        listed = self.manager.list_artifacts(task_id=self.task.id)
        self.assertEqual([a["id"] for a in listed], [art["id"]])

    def test_kind_mapping(self) -> None:
        self.assertEqual(kind_for(Path("x.docx")), "word")
        self.assertEqual(kind_for(Path("x.xlsx")), "excel")
        self.assertEqual(kind_for(Path("x.pptx")), "ppt")
        self.assertEqual(kind_for(Path("x.unknown")), "unknown")

    def test_tracker_diff_registers_only_new(self) -> None:
        tracker = ArtifactTracker((self._tmp / "notes",))
        tracker.snapshot()
        first = self._write("notes/one.md", b"one")
        registered = tracker.register_new(self.manager, task_id=self.task.id, session_id="s1")
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0]["name"], "one.md")
        # 再登记一次不重复
        again = tracker.register_new(self.manager, task_id=self.task.id, session_id="s1")
        self.assertEqual(again, [])
        # 快照后新增第二个文件才登记
        tracker.snapshot()
        self._write("notes/two.txt", b"two")
        registered2 = tracker.register_new(self.manager, task_id=self.task.id, session_id="s1")
        self.assertEqual([a["name"] for a in registered2], ["two.txt"])


class RuntimeArtifactIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="artifact_run_"))
        self.out_dir = self._tmp / "notes"
        self.out_dir.mkdir()
        import main as main_module

        self._main = main_module
        self._orig = main_module.execute_turn
        from runtime.runner import AgentRuntime

        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime.artifact_dirs = (self.out_dir,)

    def tearDown(self) -> None:
        self._main.execute_turn = self._orig

    def _install_fake(self, make_file: bool):
        async def fake_execute_turn(
            mode, message, session=None, debug=False, max_turns=20, history_limit=None, agent=None, audit=None,
            stream_events_cb=None,
        ):
            if make_file:
                (self.out_dir / "报告.md").write_text("# 报告", encoding="utf-8")
            return '{"kind":"note","summary":"ok","content":"x"}'

        self._main.execute_turn = fake_execute_turn

    def test_success_registers_artifacts_in_result(self) -> None:
        self._install_fake(make_file=True)
        result = asyncio.run(self.runtime.run_turn("写份报告", session_id="unit"))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.artifacts), 1)
        art = result.artifacts[0]
        self.assertEqual(art["name"], "报告.md")
        self.assertTrue(art["id"].startswith("art_"))
        restored = self.runtime.tasks.get_artifact(art["id"])
        self.assertEqual(restored["session_id"], "unit")

    def test_no_new_files_no_artifacts(self) -> None:
        self._install_fake(make_file=False)
        result = asyncio.run(self.runtime.run_turn("随便", session_id="unit"))
        self.assertEqual(result.artifacts, [])

    def test_failure_registers_nothing(self) -> None:
        async def fake_fail(mode, message, session=None, debug=False, max_turns=20, history_limit=None, agent=None, audit=None, stream_events_cb=None):
            raise RuntimeError("中途挂了")

        self._main.execute_turn = fake_fail
        (self.out_dir / "半成品.md").write_text("partial", encoding="utf-8")
        result = asyncio.run(self.runtime.run_turn("慢产出", session_id="unit"))
        self.assertFalse(result.ok)
        self.assertEqual(result.task.state, TaskState.FAILED)
        self.assertEqual(self.runtime.tasks.list_artifacts(task_id=result.task.id), [])


if __name__ == "__main__":
    unittest.main()

