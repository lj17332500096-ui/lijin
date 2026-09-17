import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import rag


def call_tool(tool, **kwargs) -> str:
    """调用被 @function_tool 包装的工具对象。"""
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="test_call",
            tool_arguments=input_json,
        )
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


def make_workspace(tmp: Path) -> Path:
    """在临时目录里造一个小工作区：运动笔记 + 无关菜谱。"""
    (tmp / "运动笔记.md").write_text(
        "我每天早晨跑步三十分钟，周末去健身房举铁。\n坚持锻炼对身体很好，睡得更香。",
        encoding="utf-8",
    )
    (tmp / "菜谱.md").write_text(
        "番茄炒蛋做法：先把鸡蛋打散，热锅下油，倒入蛋液翻炒。",
        encoding="utf-8",
    )
    (tmp / "README").write_text("本目录测试说明：无后缀文件也应能被检索。", encoding="utf-8")
    return tmp


class ChunkTests(unittest.TestCase):
    def test_short_text_single_chunk(self) -> None:
        self.assertEqual(rag._chunk_text("很短的一段话"), ["很短的一段话"])

    def test_long_text_splits_with_overlap(self) -> None:
        text = "甲" * 3000
        chunks = rag._chunk_text(text)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= rag.CHUNK_SIZE + 1 for c in chunks))

    def test_tokens_cover_chinese_bigrams(self) -> None:
        toks = rag._tokens("锻炼身体")
        self.assertIn("锻炼", toks)
        self.assertIn("身体", toks)


class RrfAndVectorHelperTests(unittest.TestCase):
    def test_rrf_merges_two_rankings(self) -> None:
        merged = rag._rrf_merge([3, 1, 0], [1, 2], top_k=4)
        self.assertEqual(merged, [1, 3, 2, 0])

    def test_rrf_respects_top_k(self) -> None:
        merged = rag._rrf_merge([0, 1, 2], [2, 3], top_k=2)
        self.assertEqual(len(merged), 2)

    def test_cosine_and_top_indices(self) -> None:
        scores = rag._cosine_scores([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0], [0.707, 0.707]])
        self.assertAlmostEqual(scores[0], 1.0, places=3)
        self.assertAlmostEqual(scores[1], 0.0, places=3)
        self.assertEqual(rag._top_indices(scores, 2), [0, 2])


def make_tiny_pdf(path: Path, text: str | None = None) -> None:
    """手写一个最小可用 PDF（Helvetica 文本），供 pypdf 抽取；text=None 生成无文字页。"""
    if text:
        content = ("BT /F1 20 Tf 72 720 Td (" + text.replace("(", "\\(").replace(")", "\\)") + ") Tj ET")
    else:
        content = ""
    content = content.encode("latin-1")
    stream = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        4: stream,
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in range(1, 6):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for num in range(1, 6):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    path.write_bytes(out)


class PdfIndexTests(unittest.TestCase):
    """PDF 文本层入库：pypdf 可用时才跑（用最小手写 PDF）。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="rag_pdf_test_"))
        self._orig_index = rag.INDEX_PATH
        self._orig_setting = rag.EMBED_MODEL_SETTING
        rag.EMBED_MODEL_SETTING = "off"
        rag.INDEX_PATH = self._tmp / "rag_index.json"
        rag._EMBEDDER_LOADED = False

    def tearDown(self) -> None:
        rag.EMBED_MODEL_SETTING = self._orig_setting
        rag.INDEX_PATH = self._orig_index
        rag._EMBEDDER_LOADED = False

    @unittest.skipUnless(rag._pdf_support(), "需要 pypdf")
    def test_text_pdf_indexed_with_page(self) -> None:
        make_tiny_pdf(self._tmp / "计划.pdf", "Weekly Fitness Plan: jogging and gym every morning")
        index = rag.RagIndex(self._tmp)
        index.build()
        chunk = next(c for c in index.chunks if c.get("page") == 1)
        self.assertIn("Fitness", chunk["text"])
        top = index.search("morning fitness", top_k=3)
        self.assertTrue(top and top[0]["file"].endswith(".pdf"))
        self.assertEqual(top[0].get("page"), 1)

    @unittest.skipUnless(rag._pdf_support(), "需要 pypdf")
    def test_scanned_pdf_reported_and_skipped(self) -> None:
        make_tiny_pdf(self._tmp / "扫描版.pdf", None)  # 合法 PDF，但无文字层
        index = rag.RagIndex(self._tmp)
        index.build()
        self.assertEqual(index.chunks, [])
        self.assertTrue(any("扫描版" in n for n in index.notes))


class KeywordSearchTests(unittest.TestCase):
    """纯 BM25 端到端：向量功能关闭时一切照常工作。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="rag_test_"))
        make_workspace(self._tmp)
        self._orig_root = rag.WORKSPACE_ROOT
        self._orig_index = rag.INDEX_PATH
        self._orig_setting = rag.EMBED_MODEL_SETTING
        rag.WORKSPACE_ROOT = self._tmp
        rag.INDEX_PATH = self._tmp / "rag_index.json"
        rag.EMBED_MODEL_SETTING = "off"
        rag._EMBEDDER_LOADED = False

    def tearDown(self) -> None:
        rag.WORKSPACE_ROOT = self._orig_root
        rag.INDEX_PATH = self._orig_index
        rag.EMBED_MODEL_SETTING = self._orig_setting
        rag._EMBEDDER_LOADED = False

    def test_index_and_search_keyword(self) -> None:
        out = call_tool(rag.index_workspace, directory=".")
        self.assertIn("索引完成", out)
        self.assertIn("3 个文件", out)

        found = call_tool(rag.search_documents, query="锻炼身体有什么好处", top_k=3)
        self.assertIn("运动笔记.md", found)
        self.assertIn("跑步", found)
        # 无关菜谱不应出现在关键词命中里
        self.assertNotIn("菜谱", found)

    def test_search_without_index_auto_builds(self) -> None:
        found = call_tool(rag.search_documents, query="如何做番茄炒蛋", directory=".")
        self.assertIn("菜谱.md", found)
        self.assertTrue((self._tmp / "rag_index.json").exists())

    def test_extensionless_readme_indexed(self) -> None:
        found = call_tool(rag.search_documents, query="无后缀文件", directory=".")
        self.assertIn("README", found)

    def test_no_result_hint(self) -> None:
        found = call_tool(rag.search_documents, query="量子力学入门书籍推荐", directory=".")
        self.assertIn("没有找到", found)

    def test_outside_workspace_rejected(self) -> None:
        found = call_tool(rag.search_documents, query="随便", directory="..")
        self.assertIn("只能索引工作区", found)


if __name__ == "__main__":
    unittest.main()
