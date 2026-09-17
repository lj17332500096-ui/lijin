"""Message Attachment（临时附件）与 Project Source（项目来源）作用域分离测试。

覆盖：附件不入 Sources / 不跨消息长期化 / promote→Source 去重 / 引用不复制 /
多附件 / 删除未绑定附件 / 重启持久 / 产物独立 / Context 注入当前附件优先。
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.runner import AgentRuntime
from runtime.task_manager import TaskManager


class AttachmentModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="att_"))
        self.db = self._tmp / "agent.db"
        self.mgr = TaskManager(self.db)
        self.proj = self.mgr.get_or_create_container("proj-att-t1", title="附件项目")
        self.pid = self.proj["id"]

    def _store_file(self, rel: str, data: bytes) -> Path:
        p = self._tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def _add_att(self, name="A.xlsx", data=b"a1,b1\n1,2\n"):
        f = self._store_file(name, data)
        row = self.mgr.add_message_attachment(
            task_id=self.pid, display_name=name, stored_path=str(f),
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=len(data), sha256="sha-" + name,
        )
        return row

    def test_01_upload_attachment_is_not_source(self) -> None:
        att = self._add_att()
        self.assertEqual(att["attachment_scope"], "message_only")
        self.assertEqual(len(self.mgr.list_project_sources(self.pid)), 0)
        # 消息绑定后仍在附件表、不在来源
        run = self.mgr.create_task(session_id="s", goal="分析这个文件", thread_id=self.pid)
        msg = self.mgr.add_message(self.pid, "user", "分析这个文件", run_id=run.id)
        bound = self.mgr.bind_message_attachments([att["id"]], msg["id"], run.id)
        self.assertEqual(bound, 1)
        atts = self.mgr.list_message_attachments(message_id=msg["id"])
        self.assertEqual([a["display_name"] for a in atts], ["A.xlsx"])
        self.assertEqual(self.mgr.list_project_sources(self.pid), [])

    def test_03_next_message_without_attachment_is_clean(self) -> None:
        from runtime.task import TaskState as _TS

        att = self._add_att()
        run = self.mgr.create_task(session_id="s", goal="分析附件A", thread_id=self.pid)
        msg = self.mgr.add_message(self.pid, "user", "分析附件A", run_id=run.id)
        self.mgr.bind_message_attachments([att["id"]], msg["id"], run.id)
        # 产品语义（同容器单 Active Run）：前一个 Run 完成后才创建下一个
        self.mgr.transition(run.id, _TS.RUNNING, reason="test")
        self.mgr.transition(run.id, _TS.COMPLETED, reason="test")
        run2 = self.mgr.create_task(session_id="s", goal="检查产品说明", thread_id=self.pid)
        msg2 = self.mgr.add_message(self.pid, "user", "检查产品说明", run_id=run2.id)
        # 第二条消息无任何附件
        self.assertEqual(self.mgr.list_message_attachments(message_id=msg2["id"]), [])
        self.assertEqual(self.mgr.list_project_sources(self.pid), [])  # A 未升级 → 非长期来源

    def test_04_promote_attachment_to_source(self) -> None:
        f = self._store_file("B.docx", b"docx-fake")
        att = self.mgr.add_message_attachment(task_id=self.pid, display_name="B.docx", stored_path=str(f),
                                              size_bytes=9, sha256="sha-b")
        promoted = self.mgr.promote_attachment_to_source(att["id"])
        self.assertEqual(promoted["attachment_scope"], "project_source")
        self.assertTrue(promoted["promoted_to_source_id"])
        srcs = self.mgr.list_project_sources(self.pid)
        self.assertEqual([s["display_name"] for s in srcs], ["B.docx"])

    def test_05_restart_persistence(self) -> None:
        self._add_att(name="A.xlsx")
        self._store_file("src1.docx", b"doc")
        sfile = self._store_file("src1.docx", b"doc")
        src = self.mgr.add_project_source(task_id=self.pid, display_name="src1.docx", stored_path=str(sfile),
                                          size_bytes=3, sha256="sha-s")
        # “重启”
        mgr2 = TaskManager(self.db)
        atts = mgr2.list_message_attachments(task_id=self.pid)
        self.assertEqual([a["display_name"] for a in atts], ["A.xlsx"])
        self.assertEqual(len(mgr2.list_project_sources(self.pid)), 1)
        self.assertTrue(Path(atts[0]["stored_path"]).exists())

    def test_06_reference_source_no_copy(self) -> None:
        sfile = self._store_file("设计.docx", b"design")
        src = self.mgr.add_project_source(task_id=self.pid, display_name="设计.docx", stored_path=str(sfile),
                                          size_bytes=6, sha256="sha-d")
        before_files = len(list((self._tmp).rglob("*.*")))
        refs = self.mgr.create_source_reference_attachments(self.pid, [src["id"]])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["stored_path"], str(sfile))  # 引用原路径，不复制
        self.assertEqual(refs[0]["attachment_scope"], "project_source")
        self.assertEqual(len(self.mgr.list_project_sources(self.pid)), 1)  # 不重复 Source
        after_files = len(list(self._tmp.rglob("*.*")))
        self.assertEqual(before_files, after_files)  # 无新文件

    def test_07_multi_attachments_bind(self) -> None:
        a = self._add_att(name="A.xlsx")
        b = self._add_att(name="B.pdf", data=b"%PDF")
        c = self._add_att(name="C.docx", data=b"doc")
        run = self.mgr.create_task(session_id="s", goal="一起分析", thread_id=self.pid)
        msg = self.mgr.add_message(self.pid, "user", "一起分析", run_id=run.id)
        bound = self.mgr.bind_message_attachments([a["id"], b["id"], c["id"]], msg["id"], run.id)
        self.assertEqual(bound, 3)
        self.assertEqual(len(self.mgr.list_message_attachments(message_id=msg["id"])), 3)

    def test_08_delete_unbound_attachment(self) -> None:
        att = self._add_att(name="Temp.xlsx")
        path = att["stored_path"]
        row = self.mgr.delete_message_attachment(att["id"])
        self.assertIsNotNone(row)
        self.assertFalse(Path(path).exists())
        # 绑定后删除（已入历史消息）应保留文件？规范：历史消息重开附件仍在 → 不删已绑定
        att2 = self._add_att(name="Keep.xlsx")
        run = self.mgr.create_task(session_id="s", goal="x", thread_id=self.pid)
        msg = self.mgr.add_message(self.pid, "user", "x", run_id=run.id)
        self.mgr.bind_message_attachments([att2["id"]], msg["id"], run.id)
        self.assertIsNotNone(self.mgr.get_message_attachment(att2["id"]))

    def test_10_artifact_not_in_attachment_or_source(self) -> None:
        run = self.mgr.create_task(session_id="s", goal="生成报告", thread_id=self.pid)
        msg = self.mgr.add_message(self.pid, "user", "生成报告", run_id=run.id)
        art_file = self._store_file("report.xlsx", b"art")
        self.mgr.register_artifact(task_id=run.id, session_id="x", name="report.xlsx", kind="excel",
                                   storage_path=str(art_file), sha256="sha-art", size_bytes=3)
        self.assertEqual(self.mgr.list_message_attachments(message_id=msg["id"]), [])
        self.assertEqual(self.mgr.list_project_sources(self.pid), [])
        arts = self.mgr.list_project_artifacts(self.pid)
        self.assertEqual([a["name"] for a in arts], ["report.xlsx"])

    def test_11_same_file_reupload_deduped_in_source(self) -> None:
        data = b"same-content"
        f1 = self._store_file("dup1.xlsx", data)
        s1 = self.mgr.add_project_source(task_id=self.pid, display_name="dup1.xlsx", stored_path=str(f1),
                                         size_bytes=len(data), sha256="sha-dup")
        # promote 相同 sha 的附件 → 引用已有，不产生第二个副本
        f2 = self._store_file("dup2.xlsx", data)
        att = self.mgr.add_message_attachment(task_id=self.pid, display_name="dup2.xlsx", stored_path=str(f2),
                                              size_bytes=len(data), sha256="sha-dup")
        p = self.mgr.promote_attachment_to_source(att["id"])
        self.assertEqual(p["promoted_to_source_id"], s1["id"])
        self.assertEqual(len(self.mgr.list_project_sources(self.pid)), 1)
        self.assertFalse(Path(f2).exists())  # 独立副本被清理

    def test_12_promote_relations_correct(self) -> None:
        att = self._add_att(name="P.xlsx")
        p = self.mgr.promote_attachment_to_source(att["id"])
        self.assertTrue(p["promoted_to_source_id"])
        row = self.mgr.get_message_attachment(att["id"])
        self.assertEqual(row["attachment_scope"], "project_source")
        self.assertTrue(Path(row["stored_path"]).exists())


class AttachmentContextTests(unittest.TestCase):
    """当前消息附件注入 Context（run_turn resume 路径），并证明下一条消息不带入。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="attctx_"))
        self.mgr = TaskManager(self._tmp / "agent.db")
        self.proj = self.mgr.get_or_create_container("proj-ctx-a", title="附件上下文")
        self.pid = self.proj["id"]
        self.captured: list[str] = []

    def _capture_fake(self):
        async def fake(mode, message, session=None, debug=False, max_turns=20,
                       history_limit=None, agent=None, audit=None, stream_events_cb=None,
                       provider=None):
            self.captured.append(str(getattr(agent, "instructions", "") or ""))
            return '{"kind":"answer","summary":"s","content":"ok"}'
        self._patcher = patch("main.execute_turn", new=fake)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_attachment_in_context_but_not_next_message(self) -> None:
        self._capture_fake()
        rt = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        rt._ensure()
        # 消息1的 Run：先建 Run+Message+附件，再执行（resume 首次执行即带附件）
        run1 = self.mgr.create_task(session_id="s", goal="分析这个 Excel", thread_id=self.pid)
        msg1 = self.mgr.add_message(self.pid, "user", "分析这个 Excel", run_id=run1.id)
        f = self._tmp / "2026预算.xlsx"
        f.write_bytes(b"col1,col2\n")
        att = self.mgr.add_message_attachment(task_id=self.pid, display_name="2026预算.xlsx", stored_path=str(f),
                                              size_bytes=7, sha256="sha-2026")
        self.mgr.bind_message_attachments([att["id"]], msg1["id"], run1.id)
        asyncio.run(rt.run_turn("分析这个 Excel", session_id="s", task_id=run1.id, mode="sync"))
        # 消息2：无附件（自动建 Run）
        asyncio.run(rt.run_turn("检查产品说明", session_id="s", task_container_id=self.pid, mode="sync"))
        self.assertIn("本次消息附件", self.captured[0])
        self.assertIn("2026预算.xlsx", self.captured[0])
        self.assertNotIn("本次消息附件", self.captured[1])  # 第二条消息不带入附件块
        self.assertIn("当前项目", self.captured[1])  # 项目上下文仍在


if __name__ == "__main__":
    unittest.main()
