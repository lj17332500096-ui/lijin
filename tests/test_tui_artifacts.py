"""Clickable TUI artifact actions and path confinement."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cli.tui.app import ArtifactPreviewScreen, ForgeTuiApp
from cli.tui.artifacts import read_artifact_preview, resolve_registered_artifact
from cli.tui.panels import ArtifactItem, TuiPanels
from runtime.task_manager import TaskManager


def _artifact_fixture(tmp_path: Path):
    output_dir = tmp_path / "exports"
    output_dir.mkdir()
    path = output_dir / "报告.txt"
    path.write_text("第一行\n第二行", encoding="utf-8")
    manager = TaskManager(tmp_path / "agent.db")
    task = manager.create_task(session_id="artifact-ui", goal="生成报告")
    artifact = manager.register_artifact(
        task_id=task.id,
        session_id="artifact-ui",
        name=path.name,
        kind="text",
        storage_path=str(path),
        sha256="test-sha256",
        size_bytes=path.stat().st_size,
    )
    chat = SimpleNamespace(
        session_name="artifact-ui",
        store=SimpleNamespace(mgr=manager),
        runtime=SimpleNamespace(artifact_dirs=(output_dir,)),
    )
    return chat, artifact, path


class ArtifactPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tui_artifact_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_path_is_confined_to_runtime_output_roots(self) -> None:
        chat, artifact, path = _artifact_fixture(self.tmp)
        resolved = resolve_registered_artifact(artifact, chat.runtime.artifact_dirs)
        self.assertEqual(resolved, path.resolve())

        outside = self.tmp / "private.txt"
        outside.write_text("do not open", encoding="utf-8")
        forged = {**artifact, "storage_path": str(outside)}
        with self.assertRaisesRegex(ValueError, "安全保护"):
            resolve_registered_artifact(forged, chat.runtime.artifact_dirs)

    def test_text_preview_is_bounded_and_decodes_utf8(self) -> None:
        _chat, _artifact, path = _artifact_fixture(self.tmp)
        content, truncated = read_artifact_preview(path)
        self.assertEqual(content, "第一行\n第二行")
        self.assertFalse(truncated)


class ArtifactClickInteractionTests(unittest.TestCase):
    """Use Textual Pilot to verify both visible artifact actions."""

    def test_click_view_previews_and_folder_button_uses_registered_path(self):
        async def _run():
            with tempfile.TemporaryDirectory(prefix="tui_artifact_click_") as tmp:
                chat, artifact, path = _artifact_fixture(Path(tmp))
                app = ForgeTuiApp(session_name="artifact-ui", auto_summary=False)
                app._chat_app = chat
                async with app.run_test(size=(110, 32)) as pilot:
                    panels = app.query_one(TuiPanels)
                    item = panels.msglog.add_artifact(artifact)
                    assert isinstance(item, ArtifactItem)
                    await pilot.pause()

                    await pilot.click(f"#{item.view_button_id}")
                    await pilot.pause(0.1)
                    assert isinstance(app.screen, ArtifactPreviewScreen)
                    app.screen.query_one("#artifact-preview-content")
                    assert "第一行" in app.screen.content

                    await pilot.press("escape")
                    await pilot.pause()
                    with patch("cli.tui.artifacts.reveal_artifact") as reveal:
                        await pilot.click(f"#{item.folder_button_id}")
                        await pilot.pause(0.1)
                        reveal.assert_called_once_with(path.resolve())

        asyncio.run(_run())

    def test_artifacts_command_renders_registered_files_as_actions(self):
        async def _run():
            with tempfile.TemporaryDirectory(prefix="tui_artifact_list_") as tmp:
                chat, artifact, _path = _artifact_fixture(Path(tmp))
                chat.store.artifacts = lambda session_id, limit: chat.store.mgr.list_artifacts(
                    session_id=session_id, limit=limit,
                )
                app = ForgeTuiApp(session_name="artifact-ui", auto_summary=False)
                app._chat_app = chat
                async with app.run_test(size=(110, 32)) as pilot:
                    app._handle_command("/artifacts")
                    await pilot.pause(0.1)
                    items = [item for item in app.query_one(TuiPanels).msglog._items
                             if isinstance(item, ArtifactItem)]
                    assert len(items) == 1
                    assert items[0].artifact_id == artifact["id"]

        asyncio.run(_run())
