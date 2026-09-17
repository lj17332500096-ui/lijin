"""Office 文档支持：读取 docx/xlsx/pptx 文字 + 生成 Word/Excel/PPT + 结构化表格读取。

读取：
- read_office_file —— docx/xlsx/pptx 全文转文字；
- read_spreadsheet —— csv/xlsx 按“表头/行列/多工作表”读取；
- rag 索引自动覆盖三种 Office 格式（带 部分/页 标注）。

生成（全部写入 项目/exports/ 目录）：
- save_word_doc —— Markdown（标题/列表/表格）转 Word；
- save_excel_workbook —— JSON sheets 转 Excel；
- save_ppt_deck —— JSON slides 转 PPT。

安全：文件均落在 项目/exports/，路径强校验；读取只读工作区内文件；
旧二进制格式 .doc/.xls（非 OOXML）不支持，会如实报错。
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or BASE_DIR.parent).resolve()
EXPORTS_DIR = BASE_DIR / "exports"

OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_CELL_CHARS = 200
#: 生成类工具上限（防模型传超大内容致内存/磁盘耗尽）
WORD_MAX_CHARS = 200_000
PPTX_MAX_BULLETS = 12
PPTX_MAX_SLIDES = 24

# 导出上限
XLSX_MAX_SHEETS = 5
XLSX_MAX_ROWS = 2000
XLSX_MAX_COLS = 60

_DEPS_OK = None


def deps_ok() -> bool:
    """三个生成/解析库是否都可用。"""
    global _DEPS_OK
    if _DEPS_OK is None:
        try:
            import docx  # noqa: F401
            import openpyxl  # noqa: F401
            import pptx  # noqa: F401

            _DEPS_OK = True
        except ImportError:
            _DEPS_OK = False
    return _DEPS_OK


def _resolve_under_workspace(path_str: str) -> Path:
    from tools import _is_protected, _resolve_under_root

    target = _resolve_under_root(path_str)
    if target is None:
        raise ValueError("只能读取工作区内的文件")
    if _is_protected(target):
        raise ValueError("出于安全考虑，这个文件不允许读取")
    if not target.exists() or not target.is_file():
        raise ValueError(f"找不到文件：{target}")
    if target.stat().st_size > MAX_READ_BYTES:
        raise ValueError(f"文件超过 {MAX_READ_BYTES // (1024 * 1024)}MB，暂不支持整读")
    return target


# ---------------------------------------------------------------------------
# 读取：docx / xlsx / pptx → 文本
# ---------------------------------------------------------------------------


def _cell_str(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…"


def extract_docx(path: Path) -> list[tuple[str, str]]:
    """docx → [(part, text)]；按 body 顺序段落与表格。"""
    from docx import Document

    doc = Document(str(path))
    parts: list[tuple[str, str]] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(("", text))
    for table in doc.tables:
        lines = []
        for row in table.rows:
            lines.append(" | ".join(_cell_str(c.text) for c in row.cells))
        if lines:
            parts.append(("表格", "\n".join(lines)))
    return parts


def extract_xlsx(path: Path) -> list[tuple[str, str]]:
    """xlsx → [(工作表名, 文本行)]；保护性限制读取行列。"""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts: list[tuple[str, str]] = []
    try:
        for ws in wb.worksheets:
            lines = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= XLSX_MAX_ROWS:
                    lines.append(f"……（超过 {XLSX_MAX_ROWS} 行，其余略）")
                    break
                cells = [_cell_str(v) for v in (row or [])[:XLSX_MAX_COLS]]
                if any(cells):
                    lines.append(" | ".join(cells))
            if lines:
                parts.append((f"工作表: {ws.title}", "\n".join(lines)))
    finally:
        wb.close()
    return parts


def extract_pptx(path: Path) -> list[tuple[str, str]]:
    """pptx → [(第 N 页, 文字)]（含备注）。"""
    from pptx import Presentation

    prs = Presentation(str(path))
    parts: list[tuple[str, str]] = []
    for i, slide in enumerate(prs.slides, 1):
        lines = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if text:
                    lines.append(text)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"[备注] {notes}")
        if lines:
            parts.append((f"第 {i} 页", "\n".join(lines)))
    return parts


def extract_sections(path: Path) -> list[tuple[str, str]]:
    """按扩展名抽取 (部分标注, 文本) 列表；供 read_office_file 与 RAG 共用。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".xlsx":
        return extract_xlsx(path)
    if suffix == ".pptx":
        return extract_pptx(path)
    raise ValueError(f"不支持的 Office 格式：{suffix}")


# ---------------------------------------------------------------------------
# 生成：Word / Excel / PPT
# ---------------------------------------------------------------------------


def _md_to_docx(doc, md_text: str) -> None:
    """轻量 Markdown → docx：标题/列表/编号/表格/普通段落。"""
    lines = (md_text or "").splitlines()
    i = 0
    table_buffer: list[list[str]] = []
    in_code = False
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            i += 1
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not any(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                table_buffer.append(cells)
                if i + 1 < len(lines) and re.fullmatch(
                    r"\s*\|?[\s:|-]+\|?\s*", lines[i + 1]
                ) and "-" in lines[i + 1]:
                    i += 2
                    continue
                i += 1
                continue
            i += 1
            continue
        if table_buffer:
            tbl = doc.add_table(rows=len(table_buffer), cols=max(len(r) for r in table_buffer))
            tbl.style = "Table Grid"
            for ri, row in enumerate(table_buffer):
                for ci, cell in enumerate(row):
                    tbl.cell(ri, ci).text = cell
            table_buffer = []
            continue

        if stripped.startswith("###"):
            doc.add_heading(stripped.lstrip("# "), level=3)
        elif stripped.startswith("##"):
            doc.add_heading(stripped.lstrip("# "), level=2)
        elif stripped.startswith("#"):
            doc.add_heading(stripped.lstrip("# "), level=1)
        elif re.match(r"^\s*[-*]\s+", stripped):
            doc.add_paragraph(stripped[1:].strip(), style="List Bullet")
        elif re.match(r"^\s*\d+[.、]\s+", stripped):
            doc.add_paragraph(re.sub(r"^\s*\d+[.、]\s+", "", stripped), style="List Number")
        elif stripped:
            doc.add_paragraph(stripped)
        i += 1
    if table_buffer:
        tbl = doc.add_table(rows=len(table_buffer), cols=max(len(r) for r in table_buffer))
        tbl.style = "Table Grid"
        for ri, row in enumerate(table_buffer):
            for ci, cell in enumerate(row):
                tbl.cell(ri, ci).text = cell


def save_word_doc_impl(title: str, markdown: str) -> str:
    from docx import Document

    title = (title or "").strip()[:80]
    if not title:
        raise ValueError("标题不能为空")
    if len(markdown or "") > WORD_MAX_CHARS:
        raise ValueError(f"正文超过 {WORD_MAX_CHARS} 字符上限")
    doc = Document()
    doc.add_heading(title, level=0)
    _md_to_docx(doc, markdown)
    exports_dir = EXPORTS_DIR / "word"
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title)[:50]
    path = exports_dir / f"{stamp}_{safe}.docx"
    doc.save(str(path))
    return str(path)


def save_excel_impl(title: str, sheets_json: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    try:
        sheets = json.loads(sheets_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"sheets_json 不是合法 JSON：{exc}")
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("sheets_json 需要是数组，至少一个工作表")
    if len(sheets) > XLSX_MAX_SHEETS:
        raise ValueError(f"工作表数量超过上限（{XLSX_MAX_SHEETS} 个）")
    wb = Workbook()
    wb.remove(wb.active)
    for i, sheet in enumerate(sheets):
        if not isinstance(sheet, dict):
            raise ValueError("每个工作表需要是 {name, headers?, rows?} 对象")
        ws = wb.create_sheet(title=str(sheet.get("name") or f"Sheet{i + 1}")[:31])
        headers = sheet.get("headers") or []
        rows = sheet.get("rows") or []
        if not isinstance(headers, list) or not isinstance(rows, list):
            raise ValueError("headers/rows 必须是数组")
        if len(rows) > XLSX_MAX_ROWS:
            raise ValueError(f"工作表 {ws.title} 超过 {XLSX_MAX_ROWS} 行上限")
        start_row = 1
        if headers:
            for ci, cell in enumerate(headers[:XLSX_MAX_COLS]):
                c = ws.cell(row=1, column=ci + 1, value=str(cell))
                c.font = Font(bold=True)
            start_row = 2
        for ri, row in enumerate(rows[:XLSX_MAX_ROWS]):
            if not isinstance(row, (list, tuple)):
                raise ValueError("每行必须是数组")
            for ci, cell in enumerate(row[:XLSX_MAX_COLS]):
                ws.cell(row=ri + start_row, column=ci + 1, value=cell)
    title_clean = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title or "工作簿")[:50]
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = EXPORTS_DIR / "excel" / f"{stamp}_{title_clean}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return str(path)


def save_ppt_impl(title: str, slides_json: str) -> str:
    from pptx import Presentation
    from pptx.util import Pt

    try:
        slides = json.loads(slides_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"slides_json 不是合法 JSON：{exc}")
    if not isinstance(slides, list) or not slides:
        raise ValueError("slides_json 需要是数组，至少一页")
    if len(slides) > PPTX_MAX_SLIDES:
        raise ValueError(f"页数超过上限（{PPTX_MAX_SLIDES} 页）")
    prs = Presentation()
    layout = prs.slide_layouts[1]  # 标题+内容
    for i, slide in enumerate(slides):
        if not isinstance(slide, dict):
            raise ValueError("每页需要是 {title, bullets?} 对象")
        s = prs.slides.add_slide(layout)
        stitle = str(slide.get("title") or f"第 {i + 1} 页")[:120]
        s.shapes.title.text = stitle
        body = s.placeholders[1].text_frame
        body.text = ""
        bullets = slide.get("bullets") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        if not isinstance(bullets, list):
            raise ValueError("bullets 必须是数组或字符串")
        for j, item in enumerate(bullets[:12]):
            line = str(item)
            p = body.paragraphs[0] if j == 0 else body.add_paragraph()
            p.text = line[:300]
            p.level = 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title or "演示文稿")[:50]
    path = EXPORTS_DIR / "ppt" / f"{stamp}_{safe}.pptx"
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return str(path)


# ---------------------------------------------------------------------------
# 对外 function_tool
# ---------------------------------------------------------------------------


@function_tool
def read_office_file(path: str, max_chars: int = 20000) -> str:
    """读取 Office 文档（.docx Word / .xlsx Excel / .pptx PPT）的文字内容。
    path 是工作区内的文件路径；返回带结构标注（表格/工作表/页码）的纯文本。"""
    if not deps_ok():
        return "错误：缺少 python-docx/openpyxl/python-pptx，请先 pip install python-docx openpyxl python-pptx。"
    try:
        target = _resolve_under_workspace(path)
    except ValueError as exc:
        return f"错误：{exc}"
    try:
        sections = extract_sections(target)
    except Exception as exc:
        return f"读取失败（{type(exc).__name__}: {str(exc)[:200]}）。注意 .doc/.xls 老格式不支持。"
    limit = max(500, min(int(max_chars), 60000))
    lines = [f"文件: {target}"]
    total = 0
    truncated = False
    for label, text in sections:
        head = f"\n[ {label} ]\n{text}" if label else f"\n{text}"
        if total + len(head) > limit:
            lines.append("\n……（内容较长，仅显示前一部分）")
            truncated = True
            break
        lines.append(head)
        total += len(head)
    from runtime.trust import tag

    return tag("Office 文档", str(target), "\n".join(lines), max_len=24000)


@function_tool
def read_spreadsheet(path: str, sheet: str = "", max_rows: int = 120) -> str:
    """结构化读取表格文件（.xlsx / .csv）：看表头、行列数和每行内容。
    sheet 指定 Excel 工作表名（不填列出全部工作表名称和行列数）；max_rows 最多展开的行数。"""
    if not deps_ok():
        return "错误：缺少 openpyxl，请先 pip install openpyxl。"
    try:
        target = _resolve_under_workspace(path)
    except ValueError as exc:
        return f"错误：{exc}"
    suffix = target.suffix.lower()
    limit = max(5, min(int(max_rows), 1000))

    def render(name: str, rows: list[list], truncated: bool) -> str:
        lines = [f"工作表: {name}（{len(rows)} 行内已展开）"]
        if not rows:
            lines.append("（无数据行）")
            return "\n".join(lines)
        for ri, row in enumerate(rows[:limit]):
            cells = [_cell_str(v)[:60] for v in row]
            lines.append(f"{ri + 1}. " + " | ".join(cells) if cells else "")
        if truncated or len(rows) > limit:
            lines.append(f"……（共 {len(rows)} 行，仅显示前 {limit} 行）")
        return "\n".join(lines)

    if suffix == ".xlsx":
        import openpyxl

        try:
            wb = openpyxl.load_workbook(str(target), read_only=True, data_only=True)
        except Exception as exc:
            return f"打开失败：{type(exc).__name__}: {str(exc)[:200]}"
        out = []
        try:
            if sheet:
                if sheet not in wb.sheetnames:
                    return f"找不到工作表 {sheet}，可用工作表：{wb.sheetnames}"
                ws = wb[sheet]
                rows = [list(r) for r in ws.iter_rows(values_only=True) if any(r)]
                out.append(render(sheet, rows, False))
            else:
                out.append(f"文件: {target}\n共 {len(wb.sheetnames)} 个工作表：")
                for ws in wb.worksheets:
                    count = sum(1 for r in ws.iter_rows(values_only=True) if any(r))
                    out.append(f"- {ws.title}（约 {count} 行数据）")
                out.append("\n想看具体内容请指定 sheet=工作表名。")
        finally:
            wb.close()
        from runtime.trust import tag

        return tag("表格文件(xlsx)", str(target), "\n".join(out), max_len=24000)

    if suffix == ".csv":
        import csv as csv_lib
        import io

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return f"读取失败：{exc}"
        text = raw.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv_lib.Sniffer().sniff(text[:4096], delimiters=",;\t")
            delimiter = dialect.delimiter
        except csv_lib.Error:
            delimiter = ","
        reader = csv_lib.reader(io.StringIO(text))
        try:
            rows = [list(r) for r in reader]
        except csv_lib.Error as exc:
            return f"CSV 解析失败：{exc}"
        rows = [r for r in rows if any(_cell_str(c) for c in r)]
        from runtime.trust import tag

        return tag(
            "表格文件(csv)",
            str(target),
            "\n".join(
                [
                    f"文件: {target}（分隔符 {repr(delimiter)}，共 {len(rows)} 行）",
                    render("CSV", rows, False),
                ]
            ),
            max_len=24000,
        )
    return "错误：仅支持 .xlsx 或 .csv 文件。"


@function_tool
def save_word_doc(title: str, markdown: str) -> str:
    """把 Markdown 内容生成 Word 文档（.docx）保存到 项目/exports/word/。
    支持 #/##/### 标题、- 列表、1. 编号、| 管道表格、普通段落。
    title 是文档标题；markdown 是正文。返回保存路径。"""
    try:
        path = save_word_doc_impl(title, markdown)
    except Exception as exc:
        return f"生成失败：{type(exc).__name__}: {str(exc)[:250]}"
    return f"已生成 Word 文档：{path}\n（可再用 read_office_file 回读校验内容）"


@function_tool
def save_excel_workbook(title: str, sheets_json: str) -> str:
    """把 JSON 描述的多个工作表生成 Excel（.xlsx）保存到 项目/exports/excel/。
    sheets_json 形如：
    [{"name":"支出","headers":["项目","金额"],"rows":[["饮食",800],["交通",150]]}]
    支持多工作表；单元格可用数字/字符串/布尔。返回保存路径。"""
    try:
        path = save_excel_impl(title, sheets_json)
    except Exception as exc:
        return f"生成失败：{type(exc).__name__}: {str(exc)[:250]}"
    return f"已生成 Excel：{path}\n（可再用 read_spreadsheet 回读校验）"


@function_tool
def save_ppt_deck(title: str, slides_json: str) -> str:
    """把 JSON 描述的幻灯片生成 PPT（.pptx）保存到 项目/exports/ppt/。
    slides_json 形如：
    [{"title":"封面","bullets":["副标题1","副标题2"]},{"title":"要点","bullets":["a","b","c"]}]
    每页最多 12 条要点、每页 ≤300 字符。返回保存路径。"""
    try:
        path = save_ppt_impl(title, slides_json)
    except Exception as exc:
        return f"生成失败：{type(exc).__name__}: {str(exc)[:250]}"
    return f"已生成 PPT：{path}\n（可再用 read_office_file 回读校验）"
