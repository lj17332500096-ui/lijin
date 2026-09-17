"""gorden-ppt 技能工具：内置模板 PPT 构建 + 用户模板模式（模式 A / B）。

安全与边界：
- 内置模板/参考文件位于技能目录（系统资源，slug 白名单访问，不接受任意路径）；
- 自定义模板路径在「写入前」按运行期 FileScope 的只读授权校验（复用 authorize_tool）；
- 输出一律写到项目 exports/ppt/（与既有 Office 生成语义一致）；
- 本工具不联网、不执行技能目录里的更新脚本。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from agents import function_tool

SKILL_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SKILL_DIR / "templates"
SCRIPTS_DIR = SKILL_DIR / "scripts"
BASE_DIR = SKILL_DIR.parent.parent
EXPORTS_PPT_DIR = BASE_DIR / "exports" / "ppt"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff]")
MAX_EDITS_CHARS = 400_000


def _require_skill_dir() -> None:
    if not TEMPLATES_DIR.is_dir() or not (SCRIPTS_DIR / "build_pptx.py").is_file():
        raise RuntimeError("gorden-ppt 技能目录不完整：缺少 templates/ 或 scripts/build_pptx.py")


def _slug_dir(slug: str) -> Path | None:
    if not slug or not _SLUG_RE.match(slug):
        return None
    d = (TEMPLATES_DIR / slug).resolve()
    if d.parent != TEMPLATES_DIR.resolve() or not d.is_dir():
        return None
    return d


def _clean_out_name(name: str, fallback: str) -> str:
    base = _SAFE_NAME_RE.sub("_", (name or "").strip()).strip("_")[:80]
    if not base:
        base = fallback
    return base if base.endswith(".pptx") else base + ".pptx"


def _scope_allows_read(path: Path) -> tuple[bool, str]:
    """自定义模板读取授权：复用 FileScope 只读判定；无 Run 上下文视为不允许。"""
    try:
        from runtime.filescope import authorize_tool
        from runtime.runctx import current as _rc

        ctx = _rc()
        if ctx is None or ctx.file_scope is None:
            return False, "需要在一次项目对话运行中执行（缺少项目文件范围上下文）"
        ok, reason, _ = authorize_tool("read_workspace_file",
                                       {"path": str(path)}, ctx.file_scope)
        return ok, reason if not ok else ""
    except Exception as exc:  # noqa: BLE001
        return False, f"文件授权检查不可用：{exc}"


@function_tool
def gorden_ppt_templates() -> str:
    """查看 gorden-ppt 内置 PPT 模板清单（slug/名称/页数/主色/适用场景）。"""
    _require_skill_dir()
    index = TEMPLATES_DIR / "INDEX.md"
    if not index.is_file():
        return "错误：模板索引缺失。"
    text = index.read_text(encoding="utf-8")
    return text[:4000]


@function_tool
def gorden_ppt_template_intro(template_slug: str) -> str:
    """查看某个内置模板的简介与逐页 slot 容量（选模板/规划 edits 前调用）。

    template_slug 示例：minimal-business-summary、data-viz-deck、report-savior。
    """
    d = _slug_dir(template_slug or "")
    if d is None:
        return f"错误：模板不存在或 slug 非法：{template_slug!r}。可用 gorden_ppt_templates 查看清单。"
    parts: list[str] = []
    intro = d / "intro.md"
    if intro.is_file():
        parts.append("【模板简介】\n" + intro.read_text(encoding="utf-8")[:2000])
    detail = d / "detail.json"
    if detail.is_file():
        try:
            data = json.loads(detail.read_text(encoding="utf-8"))
        except Exception as exc:
            parts.append(f"detail.json 解析失败：{exc}")
            data = {}
        pages = data.get("pages") or []
        lines = [f"【结构】共 {len(pages)} 页（模板总页 {len(pages)}）"]
        total_slots = 0
        for page in pages[:40]:
            slots = page.get("text_slots") or []
            total_slots += len(slots)
            for slot in slots[:6]:
                sid = slot.get("slot_id")
                role = slot.get("role") or ""
                expected = str(slot.get("expected_text") or slot.get("current_text") or "")
                cap = slot.get("max_chars")
                lines.append(
                    f"页{page.get('slide_number')} slot={sid} role={role} "
                    f"容量={cap} 原文={expected[:60]!r}")
        lines.append(f"共 {total_slots} 个文本位（页内最多列出 6 个，其余见 detail.json）")
        parts.append("\n".join(lines))
    out = "\n\n".join(parts)
    return out[:4200]


def _build_pptx(template: Path, detail: Path | None, edits_json: str,
                out_name: str, strict: bool) -> str:
    _require_skill_dir()
    try:
        spec = json.loads(edits_json)
        if not isinstance(spec, dict):
            return "错误：edits_json 必须是对象，包含 selected_slides 与 edits 数组。"
        if not isinstance(spec.get("edits"), list) or len(edits_json) > MAX_EDITS_CHARS:
            return "错误：edits_json 格式不正确或过大。"
    except json.JSONDecodeError as exc:
        return f"错误：edits_json 不是合法 JSON：{exc}"
    if not template.is_file():
        return f"错误：模板文件不存在：{template}"
    if detail is not None and not detail.is_file():
        return f"错误：detail.json 不存在：{detail}"

    EXPORTS_PPT_DIR.mkdir(parents=True, exist_ok=True)
    safe = _clean_out_name(out_name, f"gorden_{template.stem}")
    output = EXPORTS_PPT_DIR / safe
    edits_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="gorden_edits_",
                                        dir=str(EXPORTS_PPT_DIR))
        os.close(fd)
        edits_path = Path(tmp_name)
        edits_path.write_text(edits_json, encoding="utf-8")
        cmd = [sys.executable, str(SCRIPTS_DIR / "build_pptx.py"),
               str(template), str(edits_path), str(output)]
        if detail is not None:
            cmd += ["--detail", str(detail)]
        if strict:
            cmd.append("--strict")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              cwd=str(SKILL_DIR))
        tail = (proc.stdout or "")[-1800:] + (proc.stderr or "")[-600:]
        if proc.returncode != 0 or not output.is_file():
            return (f"构建失败（exit={proc.returncode}）：\n{tail.strip() or '无输出'}"
                    + ("\n提示：常见原因是 expected_text 不匹配或容量超限，请修正 edits 后重试。" if strict else ""))
        return (f"构建成功：{output}\n"
                f"页数：{len(__import__('pptx').Presentation(str(output)).slides)}\n"
                f"输出记录（尾部）：\n{tail.strip()[:1200]}")
    except subprocess.TimeoutExpired:
        return "错误：构建超时（300s）。"
    except Exception as exc:  # noqa: BLE001
        return f"错误：{type(exc).__name__}: {exc}"
    finally:
        try:
            if edits_path and edits_path.exists():
                edits_path.unlink()
        except Exception:
            pass


@function_tool
def gorden_ppt_build(template_slug: str, edits_json: str, out_name: str = "",
                     strict: bool = True) -> str:
    """用 gorden-ppt 内置模板构建 PPT（模式 A）。

    template_slug：内置模板名（如 minimal-business-summary）。
    edits_json：JSON 字符串，形如
      {"selected_slides": [1,2,5], "edits": [
        {"slide":1, "slot_id":"title_en", "new_text":"Annual Review"},
        {"slide":2, "slot_id":"subtitle", "new_text":"..."}]}
      slot 可用信息见 gorden_ppt_template_intro；定位失败可用 address(shape_id/paragraph/run)+expected_text。
    out_name：可选输出文件名（默认自动生成）；产物保存在 exports/ppt/ 并返回真实路径。
    strict：默认 True，expected_text 不匹配会报错（推荐保持）。
    """
    d = _slug_dir(template_slug or "")
    if d is None:
        return (f"错误：模板不存在或 slug 非法：{template_slug!r}。"
                f"先用 gorden_ppt_templates 查看可用模板。")
    return _build_pptx(d / "template.pptx", d / "detail.json", edits_json,
                       out_name or f"{d.name}_{time.strftime('%Y%m%d_%H%M%S')}", strict)


@function_tool
def gorden_ppt_apply_custom(custom_template_path: str, edits_json: str,
                            out_name: str = "", strict: bool = True) -> str:
    """用用户自带的 .pptx 作为模板构建（模式 B；不会修改原文件）。

    custom_template_path：工作区/项目授权范围内的 .pptx 绝对路径（受运行时文件边界校验）。
    edits_json 与 gorden_ppt_build 相同；由于没有内置 detail.json，建议用
      address(shape_id/paragraph/run)+expected_text 精确编辑（对未知模板先做探查再改）。
    产物保存在 exports/ppt/，返回真实路径。
    """
    path = Path(custom_template_path or "").expanduser()
    if not path.is_file() or path.suffix.lower() != ".pptx":
        return "错误：custom_template_path 必须是存在的 .pptx 文件。"
    ok, reason = _scope_allows_read(path)
    if not ok:
        return f"错误：自定义模板不在当前运行允许读取的范围内：{reason}"
    return _build_pptx(path, None, edits_json,
                       out_name or f"custom_{uuid.uuid4().hex[:6]}", strict)
