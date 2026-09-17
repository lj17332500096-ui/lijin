"""Sources 解析与结构化切分（Parser + Chunker 合并实现，避免机械拆文件）。

支持：.md/.txt/.py/.js/.ts/.tsx/.jsx/.json/.yaml/.yml/.toml/.pdf/.docx
原则：
- 结构感知切分：Markdown 按标题段；代码按 class/def/函数边界；JSON/YAML/TOML 按顶层键；
  标题/段落与正文同 chunk；PDF/DOCX 按 Heading/Paragraph（分页号尽力保留，不伪造）；
- 不做“每 N 字符硬切”；token 估算用保守近似（CJK 每字 1、其他按 ~3.5 字符/token），
  结构边界优先级高于严格 token 上限（允许 1.5 倍溢出，不截断函数体）。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TARGET_TOKENS = 380
MAX_TOKENS = 900


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf")
    other = len(text) - cjk
    return cjk + max(1, round(other / 3.5))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Section:
    text: str
    chunk_type: str = "text"
    heading: str | None = None
    section_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None


# ---------------------------------------------------------------------------
# 文本/结构化解析
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_text_file(path: Path, language: str) -> list[Section]:
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ValueError("binary")
    text = raw.decode("utf-8", errors="replace")
    if language in ("md", "markdown", "txt", "text"):
        return _md_sections(text)
    if language in ("json",):
        return _json_sections(text)
    if language in ("yaml", "yml", "toml"):
        return _kv_sections(text, comment="#")
    return _code_sections(text)


def _md_sections(text: str) -> list[Section]:
    lines = text.splitlines()
    out: list[Section] = []
    cur_title: str | None = None
    cur_path: list[str] = []
    body: list[str] = []
    start = 0
    stack_depth = 0

    def flush(end: int) -> None:
        nonlocal body, start
        body_text = "\n".join(body).strip()
        if body_text:
            out.append(Section(text=body_text, chunk_type="markdown",
                               heading=cur_title,
                               section_path=" / ".join(cur_path) if cur_path else None,
                               start_line=start + 1, end_line=end))
        body = []

    for idx, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            flush(idx - 1)
            depth = len(m.group(1))
            title = m.group(2).strip()
            cur_path = cur_path[:depth - 1] + [title]
            cur_title = title
            stack_depth = depth
            start = idx  # 标题行本身也属于该 Section（保留结构标题文本）
            body = [line]
            continue
        body.append(line)
    flush(len(lines))
    return out or ([Section(text=text.strip(), chunk_type="markdown")] if text.strip() else [])


def _json_sections(text: str) -> list[Section]:
    try:
        data = json.loads(text)
    except Exception:
        # 无法解析的 JSON 降级为文本
        return [Section(text=text.strip(), chunk_type="json")]
    out: list[Section] = []
    if isinstance(data, dict):
        for key, value in data.items():
            rendered = json.dumps({key: value}, ensure_ascii=False, indent=2)
            out.append(Section(text=rendered, chunk_type="json", heading=str(key),
                               section_path=str(key)))
        return out
    return [Section(text=json.dumps(data, ensure_ascii=False, indent=2), chunk_type="json")]


def _kv_sections(text: str, comment: str) -> list[Section]:
    """YAML/TOML：按顶层键分块（不支持嵌套展开，只做顶层分组）。"""
    top = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-\.]*)\s*:\s*(.*)$")
    sections: list[Section] = []
    cur: list[tuple[str, str]] = []
    cur_key: str | None = None
    start_line = 1
    for i, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(comment) or not line.strip():
            continue
        m = top.match(line)
        if m and not line.startswith((" ", "\t")):  # 顶层键（顶格）
            if cur:
                sections.append(Section(
                    text="\n".join(f"{k}: {v}" if v else k for k, v in cur),
                    chunk_type="config", heading=cur_key,
                    section_path=cur_key, start_line=start_line, end_line=i - 1))
            cur_key = m.group(1)
            cur = [(m.group(1), m.group(2))] if m.group(2) else [(m.group(1), "")]
            start_line = i
            continue
        if cur_key is not None and line.startswith((" ", "\t")):
            cur.append((cur_key, line.strip()))
    if cur:
        sections.append(Section(
            text="\n".join(f"{k}: {v}" if v else k for k, v in cur),
            chunk_type="config", heading=cur_key, section_path=cur_key,
            start_line=start_line, end_line=len(text.splitlines())))
    return sections or ([Section(text=text.strip(), chunk_type="config")] if text.strip() else [])


#: 代码结构边界（无 AST 依赖的第一版：行级边界）
_CODE_BOUNDARY = re.compile(
    r"^(?P<indent>[ \t]*)(?P<body>"
    r"(?:async\s+def|def|class|function|export\s+(?:default\s+)?(?:function|const|class)|"
    r"const\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_][\w]*)\s*=>|"
    r"public|private|protected|static|@.*\bdecorator))"
)
_DECORATOR = re.compile(r"^[ \t]*@[\w\.]+")


def _code_sections(text: str) -> list[Section]:
    lines = text.splitlines()
    sections: list[Section] = []
    start = 0
    current_header: str | None = None
    buf: list[str] = []

    def flush(end: int) -> None:
        nonlocal buf, start
        content = "\n".join(buf).rstrip()
        if content.strip():
            sections.append(Section(text=content, chunk_type="code",
                                    heading=current_header,
                                    start_line=start + 1, end_line=end))
        buf = []
        start = end + 1

    for i, line in enumerate(lines):
        m = _CODE_BOUNDARY.match(line)
        if m:
            flush(i - 1)
            current_header = line.strip()[:200]
            start = i
            buf.append(line)
            continue
        if _DECORATOR.match(line):
            # 装饰器归入随后的结构体
            buf.append(line)
            continue
        buf.append(line)
    flush(len(lines))
    return sections or ([Section(text=text.strip(), chunk_type="code")] if text.strip() else [])


# ---------------------------------------------------------------------------
# PDF / DOCX
# ---------------------------------------------------------------------------

def parse_pdf(path: Path) -> list[Section]:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"pypdf 不可用: {exc}")
    reader = PdfReader(str(path))
    sections: list[Section] = []
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            page_text = (page.extract_text() or "").strip()
        except Exception:
            page_text = ""
        if not page_text:
            continue
        for sec in _md_sections(page_text):
            sec.page = page_no
            sections.append(sec)
    if not sections:
        raise ValueError("text_unavailable")  # 扫描版 PDF：无文本层，明确标记
    return sections


def parse_docx(path: Path) -> list[Section]:
    try:
        import docx  # python-docx
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"python-docx 不可用: {exc}")
    document = docx.Document(str(path))
    sections: list[Section] = []
    current: list[str] = []
    current_heading: str | None = None
    start = 1

    def flush(end: int) -> None:
        nonlocal current, start
        body = "\n".join(current).strip()
        if body:
            sections.append(Section(text=body, chunk_type="docx",
                                    heading=current_heading,
                                    start_line=start, end_line=end))
        current = []
        start = end + 1

    for idx, para in enumerate(document.paragraphs, start=1):
        style = (para.style.name or "").lower() if para.style else ""
        text = para.text.strip()
        if not text:
            continue
        if style.startswith("heading") or style in ("title", "subtitle"):
            flush(idx - 1)
            current_heading = text[:200]
            start = idx
            current = []
        else:
            if not current:
                start = idx
            current.append(text)
    flush(len(document.paragraphs) or 1)
    return sections or ([Section(text="", chunk_type="docx")] if False else [])


# ---------------------------------------------------------------------------
# Chunker（把 Sections 合并为 <= 目标 token 的 chunk，结构优先）
# ---------------------------------------------------------------------------

def chunk_sections(title: str, sections: list[Section],
                   target: int = TARGET_TOKENS, maximum: int = MAX_TOKENS) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    buffer: list[str] = []
    buffer_tokens = 0
    buffer_meta: Section | None = None
    start_line: int | None = None

    def flush(end_line: int | None) -> None:
        nonlocal buffer, buffer_tokens, buffer_meta, start_line
        text = "\n\n".join(buffer).strip()
        if text:
            chunks.append({
                "title": title,
                "content": text,
                "chunk_type": buffer_meta.chunk_type if buffer_meta else "text",
                "heading": buffer_meta.heading if buffer_meta else None,
                "section_path": buffer_meta.section_path if buffer_meta else None,
                "start_line": start_line,
                "end_line": end_line,
                "page": buffer_meta.page if buffer_meta else None,
                "token_count": estimate_tokens(text),
                "content_hash": content_hash(text),
            })
        buffer = []
        buffer_tokens = 0
        buffer_meta = None
        start_line = None

    for sec in sections:
        tokens = estimate_tokens(sec.text)
        if buffer and buffer_tokens + tokens > target and len(buffer) > 0:
            # 下一段放不下 → 收掉当前 buffer（保持整段完整性，允许小幅超上限）
            flush(sec.start_line - 1 if sec.start_line else None)
        if buffer_meta is None:
            buffer_meta = sec
            start_line = sec.start_line
        buffer.append(sec.text)
        buffer_tokens += tokens
        if buffer_tokens >= maximum:
            flush(sec.end_line)
    flush(None)
    return chunks


def parse_and_chunk(path: Path, title: str, mime_or_ext: str) -> list[dict[str, Any]]:
    """按扩展名/mime 解析并结构化切分；失败抛 ValueError(reason)。"""
    ext = (Path(title).suffix or mime_or_ext or "").lower().lstrip(".")
    if ext in ("pdf",):
        sections = parse_pdf(path)
    elif ext in ("docx", "word"):
        sections = parse_docx(path)
    elif ext in ("py", "js", "ts", "tsx", "jsx", "c", "cpp", "h", "java", "go", "rs"):
        sections = _code_sections(path.read_text(encoding="utf-8", errors="replace"))
    elif ext in ("json",):
        sections = _json_sections(path.read_text(encoding="utf-8", errors="replace"))
    elif ext in ("yaml", "yml", "toml", "ini", "cfg"):
        sections = _kv_sections(path.read_text(encoding="utf-8", errors="replace"))
    elif ext in ("md", "markdown", "txt", "text", "rst"):
        sections = parse_text_file(path, "md")
    else:
        sections = parse_text_file(path, "txt")
    return chunk_sections(title, sections)
