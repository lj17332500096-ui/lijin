import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import office_docs as od


class RoundTripTests(unittest.TestCase):
    """生成 → 读取回验（不依赖真实模型）。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="office_test_"))
        self._orig = od.EXPORTS_DIR
        od.EXPORTS_DIR = self._tmp / "exports"

    def tearDown(self) -> None:
        od.EXPORTS_DIR = self._orig

    @unittest.skipUnless(od.deps_ok(), "需要 python-docx")
    def test_word_markdown_roundtrip(self) -> None:
        md = (
            "# 周报\n\n## 进展\n- 完成 A 模块\n- 联调 B 服务\n\n## 数据\n"
            "| 项目 | 数量 |\n| --- | --- |\n| 缺陷 | 3 |\n| 需求 | 7 |\n\n这是普通段落。"
        )
        path = od.save_word_doc_impl("测试周报", md)
        self.assertTrue(Path(path).suffix == ".docx")
        text = "\n".join(t for _, t in od.extract_docx(Path(path)))
        self.assertIn("周报", text)
        self.assertIn("联调", text)
        self.assertIn("缺陷", text)
        self.assertIn("普通段落", text)

    @unittest.skipUnless(od.deps_ok(), "需要 openpyxl")
    def test_excel_sheets_roundtrip(self) -> None:
        sheets = json.dumps(
            [
                {"name": "支出", "headers": ["项目", "金额"], "rows": [["饮食", 800], ["交通", 150]]},
                {"name": "收入", "headers": ["来源", "金额"], "rows": [["工资", 9000]]},
            ],
            ensure_ascii=False,
        )
        path = od.save_excel_impl("账单", sheets)
        parts = od.extract_xlsx(Path(path))
        labels = [label for label, _ in parts]
        self.assertIn("工作表: 支出", labels)
        text = "\n".join(t for _, t in parts)
        self.assertIn("饮食", text)
        self.assertIn("9000", text)

    @unittest.skipUnless(od.deps_ok(), "需要 python-pptx")
    def test_ppt_roundtrip(self) -> None:
        slides = json.dumps(
            [{"title": "封面", "bullets": ["副标题甲", "副标题乙"]}, {"title": "要点", "bullets": ["a", "b"]}],
            ensure_ascii=False,
        )
        path = od.save_ppt_impl("汇报", slides)
        parts = od.extract_pptx(Path(path))
        self.assertIn("第 1 页", parts[0][0])
        text = "\n".join(t for _, t in parts)
        self.assertIn("封面", text)
        self.assertIn("副标题甲", text)

    def test_excel_bad_json_rejected(self) -> None:
        with self.assertRaises(ValueError):
            od.save_excel_impl("t", "{坏 json")

    def test_excel_too_many_sheets_rejected(self) -> None:
        sheets = json.dumps([{"name": f"s{i}", "rows": []} for i in range(od.XLSX_MAX_SHEETS + 1)])
        with self.assertRaises(ValueError):
            od.save_excel_impl("t", sheets)

    def test_ppt_too_many_slides_rejected(self) -> None:
        slides = json.dumps([{"title": f"s{i}"} for i in range(od.PPTX_MAX_SLIDES + 1)])
        with self.assertRaises(ValueError):
            od.save_ppt_impl("t", slides)


class SpreadsheetReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="office_csv_"))
        self._orig_root = None
        import tools

        self._orig_root = tools.WORKSPACE_ROOT
        tools.WORKSPACE_ROOT = self._tmp
        self._orig_od = od.WORKSPACE_ROOT
        od.WORKSPACE_ROOT = self._tmp

    def tearDown(self) -> None:
        import tools

        tools.WORKSPACE_ROOT = self._orig_root
        od.WORKSPACE_ROOT = self._orig_od

    def _write(self, name: str, data: str) -> str:
        path = self._tmp / name
        path.write_text(data, encoding="utf-8")
        return str(path)

    @unittest.skipUnless(od.deps_ok(), "需要 openpyxl")
    def test_read_csv_with_header(self) -> None:
        from agents.tool_context import ToolContext
        import asyncio

        p = self._write("t.csv", "城市,人口\n北京,2100\n上海,2400\n")
        input_json = json.dumps({"path": p}, ensure_ascii=False)
        ctx = ToolContext(context=None, tool_name="read_spreadsheet", tool_call_id="t", tool_arguments=input_json)
        result = od.read_spreadsheet.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            out = asyncio.run(result)
        else:
            out = str(result)
        self.assertIn("城市", out)
        self.assertIn("北京", out)
        self.assertIn("3 行", out)

    @unittest.skipUnless(od.deps_ok(), "需要 openpyxl")
    def test_read_xlsx_lists_and_details(self) -> None:
        from agents.tool_context import ToolContext
        import asyncio

        od.EXPORTS_DIR = self._tmp
        path = od.save_excel_impl("x", '[{"name":"明细","headers":["a","b"],"rows":[[1,2]]}]')

        def invoke(path, kwargs):
            input_json = json.dumps({"path": path, **kwargs}, ensure_ascii=False)
            ctx = ToolContext(context=None, tool_name="read_spreadsheet", tool_call_id="t", tool_arguments=input_json)
            r = od.read_spreadsheet.on_invoke_tool(ctx, input_json)
            return asyncio.run(r) if asyncio.iscoroutine(r) else str(r)

        listed = invoke(path, {})
        self.assertIn("明细", listed)
        detail = invoke(path, {"sheet": "明细"})
        self.assertIn("a", detail)
        self.assertIn("1. ", detail)
        missing = invoke(path, {"sheet": "不存在"})
        self.assertIn("找不到", missing)

    def test_office_read_guards(self) -> None:
        from agents.tool_context import ToolContext
        import asyncio

        def invoke(path):
            input_json = json.dumps({"path": path}, ensure_ascii=False)
            ctx = ToolContext(context=None, tool_name="read_office_file", tool_call_id="t", tool_arguments=input_json)
            r = od.read_office_file.on_invoke_tool(ctx, input_json)
            return asyncio.run(r) if asyncio.iscoroutine(r) else str(r)

        out = invoke(str(self._tmp / "不存在.docx"))
        self.assertIn("错误", out)
        out2 = invoke("../../Windows/win.ini")
        self.assertIn("只能读取工作区", out2)


class GuardrailOfficeSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        import guardrails

        self._g = guardrails
        self._tmp = Path(tempfile.mkdtemp(prefix="office_gr_"))
        self._orig_exports = guardrails.EXPORTS_DIR
        guardrails.EXPORTS_DIR = self._tmp
        self._orig_od = od.EXPORTS_DIR
        od.EXPORTS_DIR = self._tmp
        self._file = Path(od.save_word_doc_impl("护栏测试", "# 标题\n内容"))

    def tearDown(self) -> None:
        self._g.EXPORTS_DIR = self._orig_exports
        od.EXPORTS_DIR = self._orig_od

    def test_office_saved_file_allowed(self) -> None:
        import guardrails

        reply = json.dumps(
            {
                "kind": "answer",
                "summary": "已生成",
                "content": "word 已生成",
                "questions": [],
                "saved_file": str(self._file),
                "next_step": None,
            },
            ensure_ascii=False,
        )
        self.assertIsNone(guardrails.check_output(reply))

    def test_fake_office_file_forgiven(self) -> None:
        # 新契约：伪造 saved_file 被宽恕（丢弃+警告），不再拒绝整个回复
        import guardrails

        reply = json.dumps(
            {
                "kind": "answer",
                "summary": "x",
                "content": "x",
                "questions": [],
                "saved_file": str(self._tmp / "不存在的.docx"),
                "next_step": None,
            },
            ensure_ascii=False,
        )
        self.assertIsNone(guardrails.check_output(reply))
        from runtime.reply_parser import parse

        r = parse(reply)
        self.assertTrue(r.ok)
        self.assertIsNone(r.canonical["saved_file"])
        self.assertTrue(any("saved_file" in w for w in r.warnings))


if __name__ == "__main__":
    unittest.main()
