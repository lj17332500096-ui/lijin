"""技能加载器：把 skills/<技能名>/ 目录挂到「全能助手」上。

每个技能是一个目录：
- skill.md         —— 指令片段（中文、自包含，可含 AgentReply/ui 示例；会被拼进人设尾部）
- tools.py（可选） —— 新增动作工具：@function_tool 修饰的函数会被自动收集注册

启用：.env 里 SKILLS=技能名1,技能名2（逗号分隔，不填 = 不启用任何技能）。
安全：技能名只允许字母/数字/_/-；某技能损坏只跳过并记录原因，不影响主 Agent。
查看状态：python -c "import skills_loader; print(skills_loader.status_text())"
"""

import importlib.util
import os
import re
from pathlib import Path

from agents import function_tool
from agents.tool import FunctionTool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
SKILLS_DIR = BASE_DIR / "skills"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_last_errors: list[str] = []
_last_loaded: list[str] = []


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
        md_path = _skill_md_path(name)
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


def _skill_md_path(name: str) -> Path | None:
    """返回技能指令文件路径：优先小写 skill.md，回退大写 SKILL.md（Linux 大小写兼容）。"""
    for fname in ("skill.md", "SKILL.md"):
        p = SKILLS_DIR / name / fname
        if p.is_file():
            return p
    return None


def collect_skill_tools() -> list[FunctionTool]:
    """收集启用技能里 tools.py 导出的 @function_tool 工具（模块级属性）。"""
    global _last_errors
    tools: list[FunctionTool] = []
    if not _last_loaded:
        skill_definitions()  # 确保 _last_loaded 与错误已刷新
    for name in list(_last_loaded):
        py_path = SKILLS_DIR / name / "tools.py"
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
