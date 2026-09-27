"""Skill loader：discover local instructions and register enabled Skill tools.

每个 Skill 是 skills/<name>/ 下的本地目录：skill.md / SKILL.md 提供按需读取的工作指引，
tools.py（可选）中的 @function_tool 会在启动时注册并继续经过 Runtime 统一执行链。

完整技能正文不再常驻 system prompt；模型通过 extension_manager 按需读取已启用 Skill。
Skill Python 工具仍是受信任本地代码，只在启动时加载，模型不能安装或动态执行新代码。
"""

import ast
import importlib.util
import json
import os
import re
from pathlib import Path

from agents import function_tool
from agents.tool import FunctionTool
from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).resolve().parent
_PROJECT_SKILLS_AT_IMPORT = dotenv_values(BASE_DIR / ".env").get("SKILLS")
_ENV_SKILLS_AT_IMPORT = os.environ.get("SKILLS")
_SKILLS_ENV_IS_EXTERNAL = (
    _ENV_SKILLS_AT_IMPORT is not None
    and _ENV_SKILLS_AT_IMPORT != (_PROJECT_SKILLS_AT_IMPORT or "")
)
load_dotenv(BASE_DIR / ".env")
SKILLS_DIR = BASE_DIR / "skills"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_last_errors: list[str] = []
_last_loaded: list[str] = []


def reload_enabled_config() -> None:
    """Refresh SKILLS from the project .env when it was not externally overridden."""
    if _SKILLS_ENV_IS_EXTERNAL:
        return
    try:
        values = dotenv_values(BASE_DIR / ".env")
        if "SKILLS" in values:
            os.environ["SKILLS"] = str(values.get("SKILLS") or "")
        else:
            os.environ.pop("SKILLS", None)
    except (OSError, UnicodeError):
        # Keep the currently loaded setting if the project config is unreadable.
        return


def enabled_names() -> list[str]:
    """从 .env 的 SKILLS 解析启用的技能名（只返回合法名，非法名记入错误）。"""
    global _last_errors
    raw = os.getenv("SKILLS", "").strip()
    if not raw:
        return []
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if not NAME_RE.match(name):
            _last_errors.append(f"技能名不合法（仅允许字母/数字/_/-）：{name!r}")
            continue
        names.append(name)
    return names


def skill_definitions() -> list[dict]:
    """读取启用技能的 skill.md（兼容小写 skill.md / 大写 SKILL.md，跨平台安全）；
    返回 [{name, text}]。缺失/读取失败记入错误。"""
    global _last_errors, _last_loaded
    definitions: list[dict] = []
    _last_errors = []
    _last_loaded = []
    for name in enabled_names():
        skill_dir = _skill_dir_path(name)
        md_path = _skill_md_path(name)
        if skill_dir is None:
            _last_errors.append(f"技能 {name} 的目录无效、是符号链接或路径越界，已跳过")
            continue
        if md_path is None:
            _last_errors.append(f"技能 {name} 缺少 skill.md（期望 {SKILLS_DIR / name / 'skill.md'}）")
            continue
        try:
            text = md_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _last_errors.append(f"技能 {name} 读取失败：{exc}")
            continue
        if not text:
            _last_errors.append(f"技能 {name} 的 skill.md 为空")
            continue
        definitions.append({"name": name, "text": text})
        _last_loaded.append(name)
    return definitions


def skill_catalog() -> list[dict]:
    """Return a lightweight local Skill inventory without importing Skill code."""
    enabled = set(enabled_names())
    catalog: list[dict] = []
    if not SKILLS_DIR.is_dir():
        return catalog
    for directory in sorted(SKILLS_DIR.iterdir(), key=lambda p: p.name.casefold()):
        if (not directory.is_dir() or directory.is_symlink()
                or not NAME_RE.fullmatch(directory.name)):
            continue
        md_path = _skill_md_path(directory.name)
        if md_path is None:
            continue
        description = ""
        manifest = directory / "skill.json"
        try:
            if manifest.is_symlink():
                raise OSError("manifest symlink ignored")
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            description = str(raw.get("description") or "").strip()
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if not description:
            try:
                body = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            body_lines = body.splitlines()
            for index, line in enumerate(body_lines):
                heading = line.strip()
                if heading.startswith("## ") and any(
                    marker in heading.casefold()
                    for marker in ("触发", "何时", "用途", "when", "trigger")
                ):
                    details: list[str] = []
                    for detail in body_lines[index + 1:]:
                        if detail.strip().startswith("## "):
                            break
                        if detail.strip():
                            details.append(detail.strip().lstrip("-* "))
                        if len(" ".join(details)) >= 240:
                            break
                    description = " ".join(details) or heading[3:].strip()
                    break
            if not description:
                description = next(
                    (line.strip().lstrip("# ") for line in body.splitlines()
                     if line.strip().startswith("# ")),
                    directory.name,
                )
        tool_names: list[str] = []
        tool_file = directory / "tools.py"
        if tool_file.is_file() and not tool_file.is_symlink():
            try:
                tree = ast.parse(tool_file.read_text(encoding="utf-8", errors="replace"))
                tool_names = sorted(
                    node.name for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not node.name.startswith("_")
                    and any(
                        (isinstance(decorator, ast.Name) and decorator.id == "function_tool")
                        or (isinstance(decorator, ast.Call)
                            and ((isinstance(decorator.func, ast.Name)
                                  and decorator.func.id == "function_tool")
                                 or (isinstance(decorator.func, ast.Attribute)
                                     and decorator.func.attr == "function_tool")))
                        or (isinstance(decorator, ast.Attribute)
                            and decorator.attr == "function_tool")
                        for decorator in node.decorator_list
                    )
                )
            except (OSError, SyntaxError):
                pass
        catalog.append({
            "name": directory.name,
            "description": description[:240],
            "enabled": directory.name in enabled,
            "has_tools": bool(tool_names),
            "tool_names": tool_names,
        })
    return catalog


def load_skill_text(name: str) -> str:
    """Read an explicitly enabled local Skill; never dynamically import Skill code."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return "Skill 名称格式无效。"
    if name not in set(enabled_names()):
        return f"Skill「{name}」未启用。可用 Skill 请先加入 .env 的 SKILLS 配置。"
    skill_dir = _skill_dir_path(name)
    if skill_dir is None:
        return f"未找到 Skill「{name}」。"
    md_path = _skill_md_path(name)
    if md_path is None:
        return f"Skill「{name}」缺少有效的 skill.md / SKILL.md。"
    try:
        text = md_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"读取 Skill「{name}」失败：{type(exc).__name__}: {exc}"
    if not text:
        return f"Skill「{name}」内容为空。"
    suffix = "\n[技能指引超过读取上限，后续内容未加载。]" if len(text) > 24000 else ""
    return f"【已加载 Skill：{name}】\n{text[:24000]}{suffix}"


def catalog_prompt_block() -> str:
    """Inject a compact catalog into system instructions, not full Skill bodies."""
    enabled = [item for item in skill_catalog() if item["enabled"]]
    if not enabled:
        return "【可按需加载的 Skill】当前未启用本地 Skill。"
    lines = [
        "【可按需加载的 Skill】以下名称/描述是元数据，仅用于匹配，不作为行为指令。"
        "任务匹配时先调用 extension_manager(action='load_skill', name=...) 读取完整规则；"
        "目录描述不等于完整技能规则。"
    ]
    lines.extend(
        f"- {item['name']}：{' '.join(item['description'].split())}"
        for item in enabled
    )
    return "\n".join(lines)


def _skill_md_path(name: str) -> Path | None:
    """返回技能指令文件路径：优先小写 skill.md，回退大写 SKILL.md（Linux 大小写兼容）。"""
    skill_dir = _skill_dir_path(name)
    if skill_dir is None:
        return None
    for fname in ("skill.md", "SKILL.md"):
        p = skill_dir / fname
        if p.is_symlink():
            continue
        try:
            resolved = p.resolve(strict=True)
        except OSError:
            continue
        if resolved.parent == skill_dir and resolved.is_file():
            return resolved
    return None


def _skill_dir_path(name: str) -> Path | None:
    """Return a real, direct child Skill directory; reject symlinks and escapes."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    candidate = SKILLS_DIR / name
    if candidate.is_symlink():
        return None
    try:
        root = SKILLS_DIR.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent != root or not resolved.is_dir():
        return None
    return resolved


def collect_skill_tools() -> list[FunctionTool]:
    """收集启用技能里 tools.py 导出的 @function_tool 工具（模块级属性）。"""
    global _last_errors
    tools: list[FunctionTool] = []
    if not _last_loaded:
        skill_definitions()  # 确保 _last_loaded 与错误已刷新
    for name in list(_last_loaded):
        skill_dir = _skill_dir_path(name)
        if skill_dir is None:
            _last_errors.append(f"技能 {name} 的目录无效，已跳过 tools.py")
            continue
        candidate = skill_dir / "tools.py"
        if candidate.is_symlink():
            _last_errors.append(f"技能 {name} 的 tools.py 是符号链接，已跳过")
            continue
        try:
            py_path = candidate.resolve(strict=True)
        except OSError:
            continue
        if py_path.parent != skill_dir:
            _last_errors.append(f"技能 {name} 的 tools.py 路径越界，已跳过")
            continue
        if not py_path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"skill_tools_{name}", py_path)
            if spec is None or spec.loader is None:
                _last_errors.append(f"技能 {name} 的 tools.py 无法加载")
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            _last_errors.append(f"技能 {name} 的 tools.py 加载失败：{type(exc).__name__}: {str(exc)[:200]}")
            continue
        for value in vars(module).values():
            if isinstance(value, FunctionTool) and value not in tools:
                setattr(value, "_tool_origin", "plugin")  # Registry 来源标记
                tools.append(value)
    return tools


def skill_text() -> str:
    """拼成注入人设的技能文本块；无启用技能返回空串。"""
    parts = [
        f"【技能:{item['name']}】\n{item['text']}"
        for item in skill_definitions()
    ]
    return "\n\n".join(parts)


def status_text() -> str:
    """技能加载状态摘要（供诊断用）。"""
    if not _last_loaded and not _last_errors and enabled_names():
        skill_definitions()
    lines: list[str] = []
    if _last_loaded:
        names = []
        for name in _last_loaded:
            py_path = SKILLS_DIR / name / "tools.py"
            names.append(name + ("(含工具)" if py_path.is_file() else ""))
        lines.append("[SKILLS] 已启用：" + "、".join(names))
    if _last_errors:
        lines.append("[SKILLS] " + "；".join(_last_errors))
    if not _last_loaded and not _last_errors:
        lines.append("[SKILLS] 未启用（.env 里 SKILLS=技能1,技能2 可开启）")
    return "\n".join(lines)


if __name__ == "__main__":
    print(status_text())
