"""Run-scoped 文件授权边界（P1-B）：WorkLocation 从假边界变成真边界。

设计：
- 每个 Run 只允许访问「当前 Run 的文件范围」：本 Project 的 FORGE_DATA
  （forge_data/projects/<container>/）、本 Project 绑定的 WorkLocation、共享产物目录
  （notes/exports/materials/summaries/logs）、代码沙箱（code_sandbox）。
- 无 WorkLocation 的显式 Project（session 前缀 proj-*）：只允许本 Project 数据与共享
  产物目录；不得把 BASE_DIR/WORKSPACE_ROOT 当作默认用户目录乱读。
- Legacy 会话（personal / sess-*，个人助手语境）：保持既有 WORKSPACE_ROOT/BASE_DIR
  语义（原有工具级硬边界继续生效），避免破坏个人助理既有能力。
- 授权判定在工具执行前（runner 统一包装层）完成：拒绝 = 工具不执行，返回运行时错误文本；
  project_id/scope 一律来自 Runtime 注入的 RunContext，不信任模型自报。
- 路径按「解析后的绝对路径」判定：兼容 ../、绝对路径、盘符、UNC、大小写、junction/symlink
  （resolve 后比较），并继续沿用工具层原有 .env/敏感后缀/二进制拒绝。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class FileScope:
    """一轮 Run 的文件授权范围。roots 全部为解析后的绝对路径。"""

    strict: bool = False                 # True=Project 级严格范围；False=legacy 个人助手范围
    work_location: Path | None = None    # 绑定的 WorkLocation（授权根）
    project_data_root: Path | None = None  # 本 Project 的 forge_data/projects/<cid>
    shared_roots: tuple[Path, ...] = ()  # notes/exports/materials/summaries/logs 等共享产物目录
    sandbox_root: Path | None = None     # code_sandbox（隔离执行目录）
    legacy_read_root: Path | None = None  # legacy 个人会话的 WORKSPACE_ROOT
    legacy_write_root: Path | None = None  # legacy 个人会话的 BASE_DIR（项目编辑根）
    read_only: bool = False              # 无 WorkLocation 的严格 Project：默认只读（除数据目录内工具）

    def allowed_read_roots(self) -> list[Path]:
        if not self.strict:
            return [r for r in (self.legacy_read_root,) if r]
        roots = list(self.shared_roots) + [r for r in (self.project_data_root, self.sandbox_root, self.work_location) if r]
        return roots

    def allowed_write_roots(self) -> list[Path]:
        if not self.strict:
            return [r for r in (self.legacy_write_root,) if r]
        # 严格范围下：只允许写入 WorkLocation；无 WorkLocation 时默认不可写（只读模式）
        return [r for r in (self.work_location,) if r] if not self.read_only else []


def _norm(p: Path) -> Path:
    try:
        return Path(os.path.normcase(os.path.realpath(p)))
    except Exception:
        return Path(os.path.normcase(str(p)))


def _within(path: Path, roots: list[Path]) -> bool:
    p = _norm(path)
    for root in roots:
        try:
            r = _norm(root)
            if p == r or r in p.parents:
                return True
        except Exception:
            continue
    return False


def build_file_scope(
    *,
    container_id: str | None,
    session_id: str | None,
    work_location_path: str | None,
    base_dir: Path,
    workspace_root: Path,
    notes_dir: Path,
) -> FileScope:
    """由 Runtime 注入的容器/项目信息构造 FileScope。"""
    is_project_like = bool(session_id and str(session_id).startswith("proj-"))
    has_wl = bool(work_location_path and str(work_location_path).strip())

    shared = [p for p in (base_dir / "notes", base_dir / "exports", base_dir / "materials",
                          base_dir / "summaries", base_dir / "logs") if p]
    scope = FileScope(
        strict=is_project_like,
        work_location=_norm(Path(work_location_path)) if has_wl else None,
        project_data_root=_norm(base_dir / "forge_data" / "projects" / container_id) if container_id else None,
        shared_roots=tuple(_norm(p) for p in shared),
        sandbox_root=_norm(workspace_root / "code_sandbox"),
        legacy_read_root=_norm(workspace_root),
        legacy_write_root=_norm(base_dir),
        read_only=(is_project_like and not has_wl),
    )
    return scope


# ---------------------------------------------------------------------------
# 参数提取与授权
# ---------------------------------------------------------------------------

#: 只读文件工具 → 参数里代表目标路径的 key（相对路径基于 legacy WORKSPACE_ROOT 解析）
READ_TOOL_ARGS: dict[str, str] = {
    "read_workspace_file": "path",
    "list_workspace_files": "directory",
    "read_office_file": "path",
    "read_spreadsheet": "path",
    "ask_image": "image_path",
    "index_workspace": "directory",    # P2：RAG 索引目录纳入 Run 文件边界
    "search_documents": "directory",   # P2：RAG 检索目录纳入 Run 文件边界
}

#: 写入型工具（参数根 = BASE_DIR 或 WorkLocation，由 project_edit 运行期根决定）
WRITE_TOOLS = {"write_project_file", "edit_project_file"}

#: 明确登记为“不接触 Run 文件系统边界”的生产工具（自身有沙箱/白名单/项目作用域约束）。
#: 新增工具若既不在此登记、也不在 READ/WRITE/工具层自检名单 → 严格 Project 下默认 DENY
#: （fail closed：未登记权限策略的工具不得进入生产执行）。
NON_FILE_TOOLS = {
    "save_note", "read_note", "list_notes",
    "web_search", "get_current_datetime", "calculate",
    "remember", "recall_memory", "forget_memory",
    "schedule_add", "schedule_list", "schedule_remove", "schedule_set_enabled",
    "deep_research", "search_sources", "scan_dependencies",
    "write_code_file", "read_code_file", "list_code_files",
    "run_python", "code_loop", "run_tests",
    "sandbox_snapshot", "sandbox_rollback", "list_sandbox_snapshots",
    "save_word_doc", "save_excel_workbook", "save_ppt_deck",
    "gorden_ppt_templates", "gorden_ppt_template_intro",
    "gorden_ppt_build", "gorden_ppt_apply_custom",
}


def register_non_file_tools(names: set[str] | list[str] | tuple[str, ...]) -> None:
    """把外部工具（如 MCP 策略工具）登记为“不接触 Run 文件边界”的工具。

    仅用于接入方已显式声明的策略工具；登记前这些工具在严格 Project 下会因
    “tool policy not registered”被 fail-closed 拒绝。登记不代表绕过 FileScope，
    只是声明该工具不接收/改写本 Run 文件路径参数（其自身的本地文件行为由接入方策略负责）。
    """
    NON_FILE_TOOLS.update(names)

#: 这些目录的共享产物子路径在相对路径语境下仍按旧根（WORKSPACE_ROOT 子树）解析
_PREFIX_ROOT_HINTS = ("code_sandbox", "forge_data", "notes", "exports", "materials", "summaries", "logs")


def _resolve_candidate(value: str, scope: FileScope) -> Path | None:
    """把工具参数解析成候选绝对路径（解析失败返回 None，由工具层兜底报错）。"""
    raw = str(value).strip().strip("\"'")
    if not raw:
        return None
    cand = Path(raw)
    if not cand.is_absolute():
        # 相对路径：严格范围优先相对 WorkLocation；带共享前缀的相对路径按旧根解析
        if scope.strict and not _starts_with_hint(raw):
            base = scope.work_location or scope.legacy_read_root or scope.legacy_write_root
        else:
            base = scope.legacy_read_root or scope.legacy_write_root
        cand = base / cand if base else cand
    return cand


def _starts_with_hint(raw: str) -> bool:
    first = re.split(r"[/\\]", raw, maxsplit=1)[0]
    return first in _PREFIX_ROOT_HINTS


def _describe_scope(scope: FileScope) -> str:
    if not scope.strict:
        return "个人会话工作区"
    parts = []
    if scope.work_location:
        parts.append(f"工作位置 {scope.work_location}")
    if scope.project_data_root:
        parts.append(f"本项目的资料目录 {scope.project_data_root}")
    if scope.read_only and not scope.work_location:
        parts.append("（未绑定工作位置，默认只读，不能写本地目录）")
    return "；".join(parts) or "Project 范围"


def authorize_tool(tool_name: str, arguments: dict[str, Any], scope: FileScope | None) -> tuple[bool, str, dict[str, Any]]:
    """判定 (是否放行, 拒绝原因, 是否应改写后的参数)。scope=None 表示未启用严格边界。"""
    if scope is None or not scope.strict:
        return True, "", arguments
    if tool_name == "fetch_github_repo":
        # P2：抓取固定写入 WORKSPACE_ROOT/github_repos（工作区根），严格 Project 不授予该落地权
        return False, (
            "运行时文件边界：fetch_github_repo 会把仓库写入工作区根目录下的固定位置，"
            "当前 Project（严格文件范围）不允许直接写入工作区根。"
            "请使用个人会话抓取，或先在工作位置目录内自备仓库文件后读取。"
        ), arguments
    if tool_name in ("index_workspace", "search_documents"):
        raw = arguments.get("directory")
        if raw is None or not str(raw).strip() or str(raw).strip() in (".", "./"):
            return False, (
                "运行时文件边界：RAG 需要明确指定在本 Project 授权目录内的检索/索引目录；"
                "不允许以整个工作区为默认范围。请指定工作位置或其子目录。"
            ), arguments
    if tool_name in WRITE_TOOLS:
        if scope.read_only:
            return False, (
                "运行时文件边界：当前 Project 未绑定工作位置，默认只读，不能修改本地文件。"
                "如需写文件，请在项目设置中先绑定工作位置（WorkLocation）。"
            ), arguments
        path_arg = arguments.get("path")
        if not path_arg:
            return True, "", arguments
        cand = _resolve_candidate(str(path_arg), scope)
        if cand is None or not _within(cand, scope.allowed_write_roots()):
            return False, (
                f"运行时文件边界：目标 {path_arg} 不在本 Project 授权的工作位置 "
                f"{scope.work_location} 内。请只操作当前工作位置中的文件。"
            ), arguments
        # 交给工具时使用绝对路径（工具运行期根 = WorkLocation）
        args = dict(arguments)
        args["path"] = str(_norm(cand))
        return True, "", args

    arg_key = READ_TOOL_ARGS.get(tool_name)
    if not arg_key:
        # P1 fail-closed：严格 Project 下，未登记的文件工具/未知新工具不得默认放行
        if tool_name not in WRITE_TOOLS and tool_name not in NON_FILE_TOOLS \
                and tool_name != "fetch_github_repo":
            return False, (
                f"工具权限未登记：{tool_name} 没有运行期文件权限策略（tool policy not registered），"
                "默认禁止在项目模式执行。请联系管理员登记后重试。"
            ), arguments
        return True, "", arguments  # 已登记的非文件入口工具由各自边界负责
    raw = arguments.get(arg_key)
    if raw is None or not str(raw).strip():
        return True, "", arguments  # 目录类工具缺省当前目录：交给工具自身行为
    cand = _resolve_candidate(str(raw), scope)
    if cand is None:
        return True, "", arguments
    if not _within(cand, scope.allowed_read_roots()):
        return False, (
            f"运行时文件边界：{tool_name} 的目标 {raw} 不在当前 Run 允许范围（{_describe_scope(scope)}）内，"
            "已拒绝读取。"
        ), arguments
    # 绝对化，避免工具按自身根二次解析出错
    args = dict(arguments)
    args[arg_key] = str(_norm(cand))
    return True, "", args
