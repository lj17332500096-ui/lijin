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

                    # ⚠️ 这里等的是「**组件已挂载**」而不是「屏已弹出」
                    # （2026-10-05 负载压测实据，8 次里2 次红）。
                    # 先按「等屏弹出」写过一版，**负载下仍红** ⇒ 真因是
                    # `textual.css.query.NoMatches: No nodes match
                    # '#artifact-preview-content'` —— 屏 push 成功但子树还在
                    # compose 中，`query_one` 抛 `NoMatches`。
                    # ⇒ 必须轮询「组件能取到」这个条件本身。
                    #
                    # 为什么不用固定 `pilot.pause(0.1)`：那只是把「概率红」
                    # 变成「概率红但慢」，机器一慢又挂（同文件下方那条测试的注释
                    # 已记录过这个教训）。统一上限 5s（50 × 0.1s）。
                    await pilot.click(f"#{item.view_button_id}")
                    widget = None
                    for _ in range(50):
                        await pilot.pause(0.1)
                        if isinstance(app.screen, ArtifactPreviewScreen):
                            try:
                                widget = app.screen.query_one(
                                    "#artifact-preview-content")
                                break
                            except Exception:
                                continue          # 子树还没 compose 完，继续等
                    self.assertIsInstance(
                        app.screen, ArtifactPreviewScreen,
                        "5s 内预览屏没弹出——点击未生效或 push_screen 未完成。",
                    )
                    self.assertIsNotNone(
                        widget,
                        "5s 内预览屏弹出了但 '#artifact-preview-content' 始终取不到"
                        "——屏 push 成功但子树未 compose 完（NoMatches）。",
                    )
                    preview = app.screen
                    for _ in range(50):
                        await pilot.pause(0.1)
                        if "第一行" in getattr(preview, "content", ""):
                            break
                    self.assertIn(
                        "第一行", preview.content,
                        "5s 内预览内容没出现——组件已挂载但内容未填充（另一类故障）。",
                    )

                    await pilot.press("escape")
                    for _ in range(50):
                        await pilot.pause(0.1)
                        if not isinstance(app.screen, ArtifactPreviewScreen):
                            break
                    self.assertNotIsInstance(
                        app.screen, ArtifactPreviewScreen,
                        "5s 内 escape 没退回主屏——后续点击会被屏挡住。",
                    )

                    with patch("cli.tui.artifacts.reveal_artifact") as reveal:
                        await pilot.click(f"#{item.folder_button_id}")
                        for _ in range(50):
                            await pilot.pause(0.1)
                            if reveal.called:
                                break
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
                    # **`pilot.pause(0.1)` 在负载下不够**（2026-10-05 实测，
                    # 3 个并发压测进程在跑时 6 次里 3 次红，单跑/低负载必绿）。
                    # 失败形态是 `assert 0 == 1`（items 为空）—— 命令还没
                    # 被处理完就去查列表。
                    #
                    # 为什么不能只加大 0.1：那是把「概率红」变成「概率红但慢」，
                    # 机器一慢又挂。正确做法是**等到条件成立**。
                    # `pilot.pause()` 无参走 `wait_for_idle(0)`（确定性），但它等的是
                    # 「app 当前空闲」，对「后台 worker 里的命令尚未完成」无感知 ——
                    # 实测正是 `pilot.pause()` 后面立刻查 items 仍得到空列表。
                    # 所以这里显式轮询直到 artifact 出现，上限 5s。
                    items: list[ArtifactItem] = []
                    for _ in range(50):          # 50 × 0.1s = 5s 上限
                        await pilot.pause(0.1)
                        items = [i for i in app.query_one(TuiPanels).msglog._items
                                 if isinstance(i, ArtifactItem)]
                        if items:
                            break
                    self.assertEqual(
                        len(items), 1,
                        "/artifacts 命令 5s 内没把 artifact 渲染成 action —— "
                        "若 items 为空说明命令处理未完成（不是渲染缺失）",
                    )
                    assert items[0].artifact_id == artifact["id"]

        asyncio.run(_run())
