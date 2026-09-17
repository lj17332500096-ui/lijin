"""skill-assets 工具：让 Agent 读取任意启用技能目录下的资源文本（只读、路径穿越防护）。"""
from __future__ import annotations

import re
from pathlib import Path

from agents import function_tool

SKILLS = Path(__file__).resolve().parents[1]  # repo/skills


def _list(root: Path) -> str:
    try:
        names = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in (".md", ".txt", ".py", ".json", ".js", ".html", ".csv")
        ]
        return "\n（该技能下的文本资源：）" + (", ".join(names[:24]) or "无")
    except Exception:
        return ""


@function_tool
def read_skill_asset(slug: str, relpath: str) -> str:
    """读取某个技能目录下的资源文件内容。

    Args:
      slug: 技能名（例如 docx / webapp-testing / theme-factory）。
      relpath: 相对该技能目录的路径（例如 references/REFERENCE.md 或 scripts/with_server.py）。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", slug or ""):
        return "无效的技能名（仅允许字母/数字/_/-）。"
    root = (SKILLS / slug).resolve()
    if not root.is_dir():
        return f"没有这个技能：{slug}。" + "可先 `ls` 已启用技能名。" if (SKILLS / slug).exists() else f"技能 {slug} 不存在。"
    rel = (relpath or "").strip()
    if not rel:
        return "请给出相对路径，例如 references/REFERENCE.md。" + _list(root)
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        return "路径越出了该技能目录，已拒绝（防路径穿越）。"
    if not target.is_file():
        return f"该技能下没有文件 {rel}。" + _list(root)
    if target.stat().st_size > 200_000:
        return "文件过大（>200KB），已拒绝读取，请改用更小的引用。"
    try:
        return target.read_text(encoding="utf-8", errors="replace")[:8000]
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{type(e).__name__}"
