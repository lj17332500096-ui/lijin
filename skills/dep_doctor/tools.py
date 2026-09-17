"""依赖体检工具：只读扫描 requirements*.txt / pyproject.toml，报告依赖健康问题。

检测项：未锁定版本（无版本运算符）、重复依赖（同名多次出现）、文件数量。
不安装、不联网、不进入 .venv。
"""

import re
from pathlib import Path

from agents import function_tool

from tools import _resolve_under_root

_TARGET_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-prod.txt", "pyproject.toml")
_SEARCH_DEPTH = 3
_SKIP_DIR_PARTS = {".venv", ".git", "__pycache__", "node_modules", ".idea", "models", "code_sandbox", "github_repos", "data", "logs"}


def _parse_requirements_line(raw: str) -> str | None:
    line = raw.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "--")):
        return None
    line = line.split(";", 1)[0].strip()  # 去掉环境标记
    line = re.sub(r"\[[^\]]*\]", "", line).strip()  # 去掉 extras
    return line or None


def _analyze_manifest(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name == "pyproject.toml":
        specs = []
        in_deps = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("["):
                in_deps = line in ("[project]", "[tool.poetry.dependencies]", "[tool.pdm.dev-dependencies]")
                continue
            if not in_deps or not line:
                continue
            m = re.match(r"^(?:[a-zA-Z0-9_.-]+\s*=\s*)?[\"']?([A-Za-z0-9_.-]+(?:\s*[<>=!~]=?\s*[^\"'#;]+)?)", line)
            if m and not line.startswith(("#", "python")):
                specs.append(m.group(1).strip())
    else:
        specs = [s for s in (_parse_requirements_line(raw) for raw in text.splitlines()) if s]
    return _classify(specs)


def _classify(specs: list[str]) -> dict:
    pinned: list[str] = []
    unpinned: list[str] = []
    ranged: list[str] = []
    seen: dict[str, list[str]] = {}
    for spec in specs:
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", spec)
        if not match:
            continue
        name, operator = match.group(1), match.group(2).strip()
        norm = name.lower().replace("_", "-")
        seen.setdefault(norm, []).append(spec)
        if not operator:
            unpinned.append(spec)
        elif "==" in operator:
            pinned.append(spec)
        else:
            ranged.append(spec)
    duplicates = {name: specs_list for name, specs_list in seen.items() if len(set(specs_list)) > 1}
    return {
        "total": len(specs),
        "pinned": pinned,
        "unpinned": unpinned,
        "ranged": ranged,
        "duplicates": duplicates,
    }


def _fmt_report(manifests: dict[Path, dict]) -> str:
    if not manifests:
        return "没有找到 requirements*.txt 或 pyproject.toml（可指定 directory 指向项目目录）。"
    lines = []
    total_unpinned = 0
    for path, stats in manifests.items():
        lines.append(f"文件: {path}")
        lines.append(
            f"共 {stats['total']} 条：已锁定 == {len(stats['pinned'])} 条，"
            f"区间 ~ {len(stats['ranged'])} 条，未锁定 {len(stats['unpinned'])} 条"
        )
        if stats["unpinned"]:
            total_unpinned += len(stats["unpinned"])
            shown = "、".join(stats["unpinned"][:8])
            lines.append(f"  未锁定示例：{shown}" + ("…" if len(stats["unpinned"]) > 8 else ""))
        if stats["duplicates"]:
            for name, dup in stats["duplicates"].items():
                lines.append(f"  重复依赖 {name}：{' / '.join(dup)}")
        lines.append("")
    verdict = (
        f"发现 {total_unpinned} 条未锁定依赖"
        if total_unpinned
        else "依赖全部已锁定或明确限定版本"
    )
    return "\n".join(lines + [f"结论：{verdict}。建议对未锁定项用 == 固定版本以保证可复现（是否要我改由你决定）。"])


@function_tool
def scan_dependencies(directory: str = ".") -> str:
    """只读扫描一个目录树里的 requirements*.txt 与 pyproject.toml，报告依赖健康问题。
    directory 是工作区内的目录（默认工作区根）；报告未锁定版本、重复依赖与数量。
    只解析不安装；不会进入 .venv 等目录。"""
    try:
        root = _resolve_under_root(directory)
    except ValueError:
        root = None
    if root is None:
        return "错误：只能读取工作区内的目录。"
    if not root.is_dir():
        return f"错误：{root} 不是目录。"
    manifests: dict[Path, dict] = {}
    for path in sorted(root.rglob("*")):
        try:
            rel_depth = len(path.relative_to(root).parts)
        except ValueError:
            continue
        if rel_depth > _SEARCH_DEPTH:
            continue
        if any(part in _SKIP_DIR_PARTS for part in path.parts):
            continue
        if path.is_file() and path.name in _TARGET_FILES:
            try:
                manifests[path] = _analyze_manifest(path)
            except OSError:
                continue
    try:
        return _fmt_report(manifests)
    except Exception as exc:
        return f"扫描失败：{type(exc).__name__}: {str(exc)[:200]}"
