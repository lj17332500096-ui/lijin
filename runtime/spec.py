"""ToolSpec：工具的统一描述与元数据目录。

纯增量层：不改变任何工具的实现与调用方式，只给工具补齐
category / risk / side_effect / destructive / idempotent 等元数据，
供未来的 Policy / Recovery / Audit / ToolRouter 使用。

内置 31+ 个工具都有显式条目；目录外（如新增 MCP/技能工具）采用保守默认值
（risk=medium、side_effect=True、idempotent=False），逼着接入前先补条目。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str = ""
    category: str = "utility"
    input_schema: dict[str, Any] = field(default_factory=dict)
    capabilities: frozenset[str] = frozenset()
    risk: str = "low"          # low / medium / high
    side_effect: bool = False  # 是否对外部世界造成持久影响
    destructive: bool = False  # 是否可能破坏/删除数据
    idempotent: bool = True    # 相同参数重复执行结果是否等价
    timeout_seconds: int = 30
    source: str = "native"     # native / mcp / skill / agent


# 工具目录：name -> (category, risk, side_effect, destructive, idempotent)
TOOL_CATALOG: dict[str, tuple[str, str, bool, bool, bool]] = {
    # 文件产出（notes）
    "save_note": ("filesystem", "low", True, False, False),
    "read_note": ("filesystem", "low", False, False, True),
    "list_notes": ("filesystem", "low", False, False, True),
    # 检索与调研
    "web_search": ("web", "low", False, False, True),
    "deep_research": ("research", "medium", True, False, False),
    "get_current_datetime": ("utility", "low", False, False, True),
    "calculate": ("math", "low", False, False, True),
    # 工作区（只读）
    "read_workspace_file": ("filesystem", "low", False, False, True),
    "list_workspace_files": ("filesystem", "low", False, False, True),
    # 记忆
    "remember": ("memory", "medium", True, False, False),
    "recall_memory": ("memory", "low", False, False, True),
    "forget_memory": ("memory", "high", True, True, False),
    # RAG
    "index_workspace": ("rag", "medium", True, False, True),
    "search_documents": ("rag", "low", False, False, True),
    # 定时任务
    "schedule_add": ("scheduler", "medium", True, False, False),
    "schedule_list": ("scheduler", "low", False, False, True),
    "schedule_remove": ("scheduler", "medium", True, True, False),
    "schedule_set_enabled": ("scheduler", "medium", True, False, False),
    # 多模态 / Office / 数据
    "ask_image": ("vision", "low", False, False, False),
    "read_office_file": ("office", "low", False, False, True),
    "read_spreadsheet": ("office", "low", False, False, True),
    "save_word_doc": ("office", "low", True, False, False),
    "save_excel_workbook": ("office", "low", True, False, False),
    "save_ppt_deck": ("office", "low", True, False, False),
    # GitHub
    "fetch_github_repo": ("github", "medium", True, False, False),
    # 代码沙箱
    "write_code_file": ("code", "medium", True, False, False),
    "read_code_file": ("code", "low", False, False, True),
    "list_code_files": ("code", "low", False, False, True),
    "run_python": ("code", "high", True, False, False),
    # 执行项目测试代码 = 有执行副作用（不是纯只读）；必须与 run_python 同等地经过
    # readiness/approval/network/filescope 边界。destructive=False（不修改源码）。
    "run_tests": ("code", "medium", True, False, True),
    "code_loop": ("code", "high", True, False, False),
    "sandbox_snapshot": ("sandbox", "low", True, False, False),
    "sandbox_rollback": ("sandbox", "high", True, True, False),
    "list_sandbox_snapshots": ("sandbox", "low", False, False, True),
    # 项目文件编辑（受控）
    "write_project_file": ("filesystem", "medium", True, False, False),
    "edit_project_file": ("filesystem", "medium", True, False, False),
    # 技能
    "scan_dependencies": ("utility", "low", False, False, True),
}


#: Phase 11：工具能力分类（复用现有 category/name 元数据，不新建 Registry）。
_CAPABILITY_BY_NAME: dict[str, str] = {
    "web_search": "EXTERNAL_FACT", "deep_research": "EXTERNAL_FACT",
    "search_documents": "DISCOVERY", "search_sources": "DISCOVERY",
    "list_workspace_files": "DISCOVERY", "list_code_files": "DISCOVERY",
    "index_workspace": "DISCOVERY", "list_notes": "DISCOVERY",
    "list_sandbox_snapshots": "DISCOVERY", "get_current_datetime": "EXTERNAL_FACT",
    "read_workspace_file": "READ", "read_code_file": "READ", "read_note": "READ",
    "read_office_file": "READ", "read_spreadsheet": "READ",
    "recall_memory": "MEMORY", "remember": "MEMORY", "forget_memory": "MEMORY",
    "write_project_file": "MUTATION", "edit_project_file": "MUTATION",
    "write_code_file": "MUTATION", "save_note": "MUTATION",
    "save_word_doc": "MUTATION", "save_excel_workbook": "MUTATION",
    "save_ppt_deck": "MUTATION", "sandbox_rollback": "MUTATION",
    # Phase 16：capability 与真实副作用对齐
    "run_tests": "VERIFICATION",
    "run_python": "CODE_EXECUTION",
    "code_loop": "MUTATION_VERIFICATION_LOOP",
    "schedule_add": "COMMUNICATION", "schedule_remove": "COMMUNICATION",
    "schedule_set_enabled": "COMMUNICATION", "schedule_list": "DISCOVERY",
}


def capability_of(name: str) -> str:
    """返回工具能力分类（DISCOVERY/READ/MUTATION/VERIFICATION/EXTERNAL_FACT/MEMORY/COMMUNICATION/OTHER）。

    Phase 22：未知外部工具（MCP/Plugin）不再因 side_effect 兜底被一律判成 MUTATION；
    语义 capability 由名称语义推断，side_effect/risk 仍由 spec_for 单独表达。
    """
    cap = _CAPABILITY_BY_NAME.get(name)
    if cap:
        return cap
    sem = semantic_capability_from_name(name)
    if sem:
        return sem
    spec = spec_for(name)
    if spec.category in ("web", "research"):
        return "EXTERNAL_FACT"
    if spec.category in ("filesystem", "rag", "code", "office"):
        return "DISCOVERY"
    return "OTHER"


_MUTATION_NAME_TERMS = (
    "create", "update", "delete", "remove", "write", "edit", "merge", "fork",
    "upload", "click", "type", "fill", "press", "drag", "drop", "evaluate",
    "set_", "add", "push", "commit", "approve", "rollback", "send", "schedule",
    "save", "build", "apply", "rename", "move", "navigate",
)
_READ_NAME_TERMS = ("read", "get", "detail", "info", "content", "load", "describe")
_DISCOVERY_NAME_TERMS = (
    "list", "search", "find", "query", "status", "snapshot", "screenshot",
    "compare", "transcript", "languages", "fetch", "audit", "trace",
    "console", "network", "pages", "tabs", "vaults", "repos", "tables",
)


def semantic_capability_from_name(name: str) -> str | None:
    """按名称语义推断未知工具 capability（不依赖 side_effect 兜底）。"""
    n = (name or "").lower()
    if not n:
        return None
    # mutation 语义优先（create/update/delete/...）
    for t in _MUTATION_NAME_TERMS:
        if t in n:
            return "MUTATION"
    for t in _READ_NAME_TERMS:
        if t in n:
            return "READ"
    for t in _DISCOVERY_NAME_TERMS:
        if t in n:
            return "DISCOVERY"
    return None


def spec_for(
    name: str,
    description: str = "",
    input_schema: dict[str, Any] | None = None,
    source: str = "native",
) -> ToolSpec:
    """按目录构造 ToolSpec；目录外工具给保守默认并标注 source。"""
    entry = TOOL_CATALOG.get(name)
    if entry is None:
        return ToolSpec(
            name=name,
            description=description,
            category="utility",
            input_schema=input_schema or {},
            capabilities=frozenset(),
            risk="medium",
            side_effect=True,
            destructive=False,
            idempotent=False,
            timeout_seconds=30,
            source=source,
        )
    category, risk, side_effect, destructive, idempotent = entry
    return ToolSpec(
        name=name,
        description=description,
        category=category,
        input_schema=input_schema or {},
        capabilities=frozenset(),
        risk=risk,
        side_effect=side_effect,
        destructive=destructive,
        idempotent=idempotent,
        timeout_seconds=30,
        source=source,
    )
