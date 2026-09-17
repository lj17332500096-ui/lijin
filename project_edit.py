"""项目文件编辑：让 Agent 能改用户项目里的真实代码/文档（受控写）。

启用条件：.env 里 ALLOW_PROJECT_EDIT=true（显式授权，默认关闭）。

安全设计：
- 只允许修改 项目目录（BASE_DIR，即 my_creative_agent）内的文件；
- 拒绝名单：.env*、apikey、memory.json、sessions.sqlite、tasks.json、rag_index.json、
  .venv / __pycache__ / .git / logs / traces / data / models 等目录；
- 后缀白名单（.py/.md/.txt/.json/.yaml/.yml/.html/.js/.css/.bat 等），单文件 ≤600KB，拒绝二进制；
- 内容里出现 API Key/密钥格式 → 拒绝写入（防把凭据写进文件）；
- 覆盖/改写前自动备份到 logs/backups/，便于回滚；
- edit 默认要求 old 唯一匹配，出现多次须显式 replace_all，杜绝误改。
"""

import difflib
import os
import uuid
from datetime import datetime
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv

from runtime.key_patterns import has_key

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

ALLOWED_SUFFIXES = {
    ".py", ".md", ".markdown", ".txt", ".rst",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".js", ".ts", ".tsx", ".jsx",
    ".bat", ".csv", ".sql",
}
PROTECTED_NAMES = {
    ".env", ".env.example", "apikey.txt", "memory.json",
    "sessions.sqlite", "tasks.json", "rag_index.json",
}
PROTECTED_DIRS = {".venv", "__pycache__", ".git", "logs", "traces", "data", "models"}
MAX_FILE_BYTES = 600 * 1024


def _backup_dir() -> Path:
    return BASE_DIR / "logs" / "backups"

def _project_edit_enabled() -> bool:
    return os.getenv("ALLOW_PROJECT_EDIT", "").strip().lower() == "true"


def _active_write_root() -> Path:
    """运行期写入根：严格 Project 绑定 WorkLocation 时 = WorkLocation（读写授权根）；
    其余语境（legacy 个人会话/未绑定）保持 BASE_DIR。"""
    try:
        from runtime.runctx import current as _rc

        ctx = _rc()
        fs = getattr(ctx, "file_scope", None) if ctx else None
        if (fs is not None and getattr(fs, "strict", False)
                and fs.work_location is not None and not getattr(fs, "read_only", False)):
            return fs.work_location
    except Exception:
        pass
    return BASE_DIR


def _resolve_target(path_str: str) -> Path | None:
    """把相对路径解析到当前写入根内；越界/受保护返回 None。

    写入根 = 绑定 WorkLocation 时的工作位置，否则为项目目录（BASE_DIR）。
    """
    if not path_str or path_str.strip() in ("", "."):
        return None
    root = _active_write_root()
    target = Path(path_str).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    for part in target.parts:
        if part in PROTECTED_DIRS:
            return None
    if target.name in PROTECTED_NAMES or target.name.startswith(".env") \
            or target.name.startswith("memory.json") or target.name.endswith(".migrated.bak"):
        return None
    return target


def _validate_content(content: str) -> str | None:
    if len(content) > MAX_FILE_BYTES:
        return f"文件超过 {MAX_FILE_BYTES // 1024}KB 上限"
    if "\x00" in content:
        return "内容含二进制字节，拒绝写入"
    if has_key(content):
        return "内容疑似包含 API Key/密钥，拒绝写入（凭据应留在 .env）"
    return None


def _backup(path: Path) -> None:
    if not path.exists():
        return
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # uuid 后缀防同一秒内多次备份同文件导致覆盖
    backup = backup_dir / f"{stamp}_{uuid.uuid4().hex[:6]}_{path.name}"
    backup.write_bytes(path.read_bytes())


def _denied_message(why: str) -> str:
    return (
        f"{why}\n"
        "项目文件编辑受控：.env 里 ALLOW_PROJECT_EDIT=true 才可写；"
        "且只允许项目目录内、白名单后缀、非敏感文件。"
    )


def _rel_display(target: Path) -> str:
    """显示用相对路径：以当前写入根为基准；越界时回退为绝对路径。

    Phase 33 修复：绑定 WorkLocation（写入根 ≠ BASE_DIR）时，
    target.relative_to(BASE_DIR) 会抛 ValueError 导致写入工具崩溃。
    """
    try:
        return target.relative_to(_active_write_root()).as_posix()
    except ValueError:
        try:
            return target.relative_to(BASE_DIR).as_posix()
        except ValueError:
            return target.as_posix()


@function_tool
def write_project_file(path: str, content: str) -> str:
    """在项目目录内新建或覆盖一个代码/文档文件（真实修改文件！）。
    path 是相对项目根（如 agent.py、tests/test_x.py）的相对路径；
    content 是完整文件内容。受控安全：.env/密钥类文件不可写、写前自动备份、
    内容含 API Key 格式会拒绝。写完后请在回复中给出建议的验证命令。"""
    if not _project_edit_enabled():
        return _denied_message("项目文件写入未开启（安全默认）")
    target = _resolve_target(path)
    if target is None:
        return _denied_message(f"路径不合法或属于受保护文件：{path}")
    if target.suffix.lower() not in ALLOWED_SUFFIXES:
        return f"错误：后缀 {target.suffix or '(无)'} 不在白名单内（{sorted(ALLOWED_SUFFIXES)}）"
    error = _validate_content(content)
    if error:
        return f"错误：{error}"
    existed = target.exists()
    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    action = "覆盖" if existed else "新建"
    rel = _rel_display(target)
    return f"已{action}项目文件 {rel}（{len(content)} 字符，原文件已备份到 logs/backups/）。"


@function_tool
def edit_project_file(
    path: str, old_string: str, new_string: str, replace_all: bool = False, diff: bool = True
) -> str:
    """在项目目录内的文件里做精确文本替换（真实修改文件！）。
    path 是相对项目根的路径；old_string 必须唯一（出现多次会报错，需要时把
    replace_all 设为 true 全部替换）。修改前自动备份。修改后请建议验证命令。
    diff=true（默认）会在结果里附上统一 diff 变更（- 旧行 / + 新行），
    便于确认与向用户展示改了什么；不需要时传 diff=false。"""
    if not _project_edit_enabled():
        return _denied_message("项目文件编辑未开启（安全默认）")
    target = _resolve_target(path)
    if target is None:
        return _denied_message(f"路径不合法或属于受保护文件：{path}")
    if not target.exists():
        return f"错误：文件不存在：{target}（先用 write_project_file 创建）"
    if target.suffix.lower() not in ALLOWED_SUFFIXES:
        return f"错误：后缀 {target.suffix or '(无)'} 不在白名单内"
    if not old_string or old_string == new_string:
        return "错误：old_string 不能为空，且不能与 new_string 相同"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"错误：读取失败（{type(exc).__name__}: {exc}）。二进制文件不支持编辑。"
    count = text.count(old_string)
    if count == 0:
        return f"错误：在 {target.name} 里没有找到要替换的内容（注意区分全半角/换行）。"
    if count > 1 and not replace_all:
        return f"错误：内容出现 {count} 次，为避免误改需要显式处理——把 replace_all 设为 true 全部替换，或扩大 old_string 使其唯一。"
    if replace_all:
        new_text = text.replace(old_string, new_string)
    else:
        new_text = text.replace(old_string, new_string, 1)
    error = _validate_content(new_text)
    if error:
        return f"错误：{error}"
    _backup(target)
    target.write_text(new_text, encoding="utf-8")
    rel = _rel_display(target)
    verb = "替换全部" if replace_all and count > 1 else "替换"
    summary = (
        f"已在 {rel} 里{verb} {count} 处匹配（原文件已备份到 logs/backups/）。"
        f"如需验证可运行：pytest / ruff check 相关文件"
    )
    if diff:
        diff_text = _unified_diff(text, new_text, rel)
        if diff_text:
            return f"{summary}\n[Diff 变更]（- 旧行 / + 新行）\n{diff_text}"
    return summary


def _unified_diff(old_text: str, new_text: str, label: str, max_lines: int = 60) -> str:
    """生成统一 diff 文本（difflib），过长时截断。"""
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            n=3,
            lineterm="",
        )
    )
    if not lines:
        return ""
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"...（diff 过长，已截断为 {max_lines} 行，可用 read_workspace_file 查看完整内容）"]
    return "\n".join(lines)
