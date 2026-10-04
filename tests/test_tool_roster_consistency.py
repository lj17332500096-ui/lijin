"""跨模块「工具名册」一致性护栏。

背景（2026-09-19）
------------------
修旧路由表里的错名 `code_loop_tool` 时，发现同一个错名被拷贝到了
**8 个生产文件**（1 个 runtime + 7 个 benchmark），而其中 4 张"运行/验证类"名册
还漏了真实存在的 `run_tests`。

漏 `run_tests` 有真实后果：`benchmark/evaluator.py` 的判据是
「执行了不在 `tools_allowed` 里的工具 → checks['tools_allowed']=False → hard_fail」，
于是**只要模型真跑一次测试，coding 类 case 就被硬判失败**。
实测（`archive/delivery-20260919/probe_benchmark_roster_audit.py`，重放 8 个历史 run 目录）：

    臂        n=100   修正前      修正后     Δ
    A 基线            39.0%  →  43.0%    +4.0pp
    B push            36.0%  →  42.0%    +6.0pp
    C pull            38.0%  →  43.0%    +5.0pp
    （安全违规 1 / 3 / 0 不变）

**为什么原有护栏没抓到**：早期测试只守旧路由表，从不看 `benchmark/` 与其它 runtime 模块 ——
**护栏的作用域本身就是漏洞**。本文件把作用域扩到全项目的模块级名册赋值。

本文件用 AST 静态扫描，不 import（避免副作用），只认"模块级、值为字符串集合"的赋值。
三条规则：

1. **规范名册必须等于权威集合** —— `runtime/completion.py:VERIFY_TOOLS` 定义了
   `{run_tests, run_python, code_loop}`；任何叫 `VERIFY` / `RUN_TOOLS` 之类的名册
   都必须与之一致（实验变体名册显式豁免，见 `_EXPERIMENT_VARIANTS`）。
2. **不得出现已知错名/死名** —— 任何生产文件的字符串字面量都不允许是 `code_loop_tool`
   （实为 `code_loop`）、`web_search_v2`（实为 `web_search`）、`delete_task`（无此工具，
   删除语义已由 forget_memory / schedule_remove / sandbox_rollback 三个真实工具承担）。
   它们不会报错，只会让名册静默失效或误导读者。
3. **名册引用的名字必须可解释** —— 不在注册表里的名字要么是未启用技能的工具，
   要么在 `KNOWN_NON_REGISTERED` 里逐条登记理由。新增未知名字会红灯。

附带第 4 条：filescope 的权限登记表必须覆盖全部注册工具，未覆盖者只能是
`FILESCOPE_UNCOVERED_EXCEPTIONS` 里显式登记的那几个（否则严格模式下会被静默 DENY）。
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SKIP_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "logs", "runs_eval", "node_modules",
    ".workbuddy", "code_sandbox", "exports", "notes", "tests", "archive",
    "skills",  # skills 下的 tools.py 是技能自身实现，其函数名由技能白名单负责
}

# 「工具名册」的变量名形状：只有这些名字的模块级赋值才参与检查，
# 以免把 ORACLE（behavior 标签表）之类形状相似的东西误判成工具名册。
_ROSTER_NAME_RE = re.compile(
    r"^_?[A-Z_]*(?:TOOLS|CATALOG|VERIFY|RUN|MUTATION|DELETE|SEARCH|READ|WRITE|"
    r"SUPPORT|SENSITIVE)[A-Z_]*$"
)

#: 权威的"运行/验证类工具"集合（定义源：runtime/completion.py VERIFY_TOOLS）
CANONICAL_VERIFY = frozenset({"run_tests", "run_python", "code_loop"})

#: 必须与 CANONICAL_VERIFY 完全一致的名册变量名
_CANONICAL_ROSTER_NAMES = {
    "VERIFY", "VERIFY_TOOLS", "VERIFICATION_TOOLS",
    "RUN", "RUN_TOOLS", "_P9_VERIFICATION_TOOLS",
}

#: 显式豁免的实验变体名册（故意的子集，用于对照实验臂，不应被"统一"）
_EXPERIMENT_VARIANTS = {
    "E2_VERIFY": "verification-focused 臂",
    "E3_VERIFY": "verification-only 臂（故意比 E2 窄，只含 run_tests/run_python）",
}

#: 名册里引用、但不在注册表的名字 —— 逐条登记理由，避免"静默未知"
KNOWN_NON_REGISTERED = {
    # —— 未启用技能的工具（skills/*/tools.py 里有函数定义，启用后即成为注册工具）——
    "gorden_ppt_build": "技能 gorden-ppt（默认未启用）",
    "gorden_ppt_templates": "技能 gorden-ppt（默认未启用）",
    "gorden_ppt_template_intro": "技能 gorden-ppt（默认未启用）",
    "gorden_ppt_apply_custom": "技能 gorden-ppt（默认未启用）",
    "scan_dependencies": "技能 dep_doctor（默认未启用）",
    # —— harness 合成的动作名，不是工具 ——
    "questions": "澄清动作的合成名（来自 final JSON 的 kind），非注册工具",
    # —— 期望行为标签，不是工具引用 ——
    "ask_user": "behavior 标签（benchmark 的 ORACLE / ExpectedBehavior 用的键），非注册工具",
    # —— MCP 动态登记的工具：静态注册表里查不到，但生产确实挂载 ——
    # 实测（2026-10-04）：`_registered_tools()` 共 39 项，不含任何 anysearch_*。
    # 它们由 integrations/mcp_bridge.py 在 bridge 启动时经 register_non_file_tools
    # 动态登记（见 runtime/spec.py:143-144 的 EXTERNAL_FACT 分类）。
    # 故名册里引用它们属于"运行期真实存在、静态不可见"，在此登记理由。
    "anysearch_search": "MCP anysearch 动态登记（bridge 启动时），静态注册表不可见",
    "anysearch_batch_search": "MCP anysearch 动态登记（bridge 启动时），静态注册表不可见",
    "anysearch_extract": "MCP anysearch 动态登记（bridge 启动时），静态注册表不可见",
    # —— 已下线的旧搜索工具 ——
    # 实测：`web_search` 不在注册表（AnySearch 迁移后已下线），但多个名册仍在
    # 引用它。按 P1-1 的方向，这些引用应改为 anysearch_*；保留登记是为了让
    # 残留引用**显式可见**（一旦全部替换完毕，本行与上面的 anysearch_* 一并
    # 复核删除，而不是让"清理完成"变成一个没人验证的口头状态）。
    "web_search": "已下线的旧搜索工具（AnySearch 迁移产物）；名册残留引用应改为 anysearch_*",
}

#: filescope 未登记但**有意**如此的工具（由 authorize_tool 的前置分支显式处理）
FILESCOPE_UNCOVERED_EXCEPTIONS = {
    "fetch_github_repo": "authorize_tool 有专门分支显式 DENY（会把仓库写到工作区根）",
}

#: 已知死名/错名 → 正确名（无对应工具时写明替代）。生产文件出现任一即为回归。
#: 这几条都曾在多个模块间被反复拷贝，且**不会报错** —— 只会让名册静默失效或误导读者。
WRONG_NAMES = {
    "code_loop_tool": "code_loop",
    "web_search_v2": "web_search",
    "delete_task": "（无此工具；删除语义由 forget_memory / schedule_remove / sandbox_rollback 承担）",
}


def _literal_collection(node: ast.AST) -> set[str] | None:
    """把 `{...}` / `frozenset({...})` / `[...]` / `{k: v}` 解析成字符串集合。"""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set",
                                                               "list", "tuple"):
            node = node.args[0] if node.args else None
        else:
            return None
    if node is None:
        return None
    try:
        val = ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(val, dict):
        val = list(val.keys())
    if not isinstance(val, (set, frozenset, list, tuple)):
        return None
    try:
        out = set(val)
    except TypeError:
        return None
    if not out or not all(isinstance(x, str) for x in out):
        return None
    return out


def _production_files() -> list[Path]:
    return [p for p in sorted(ROOT.rglob("*.py"))
            if not any(part in _SKIP_DIRS for part in p.relative_to(ROOT).parts)]


def _parse(path: Path) -> ast.Module | None:
    """解析单个文件。

    用 `catch_warnings` 静音 `SyntaxWarning: invalid escape sequence` —— 那是被扫描文件
    里既有的 docstring 正则示例（与本次检查无关），不静音会污染测试输出。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return None


def _tool_vocabulary() -> set[str]:
    """"看起来像工具名"的词表：注册工具 ∪ 技能工具 ∪ 已登记例外 ∪ 已知错名。"""
    vocab = set(KNOWN_NON_REGISTERED) | set(WRONG_NAMES)
    vocab |= _registered_tools()
    for d in sorted((ROOT / "skills").iterdir()):
        tp = d / "tools.py"
        if not (d.is_dir() and tp.is_file()):
            continue
        tree = _parse(tp)
        if tree is None:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                vocab.add(node.name)
    return vocab


def _rosters() -> dict[str, set[str]]:
    """{"相对路径::变量名": 条目集合}。

    判定为"工具名册"的两个条件：**名字形状像** 且 **至少有一个条目落在工具词表里**。

    ⚠️ 后者是必须的：只靠名字形状会把 `sources/service.py::_NON_READY_INDEX`
    （装着 `{'failed','indexing','none'}` 的状态枚举，只因名字里有 "READY"）误判成名册。
    已知代价：**整表失效的名册（一个名字都对不上）会被漏掉** —— 由
    `CanonicalVerifyRosterTests` 按变量名做的检查兜底。
    """
    vocab = _tool_vocabulary()
    out: dict[str, set[str]] = {}
    for p in _production_files():
        tree = _parse(p)
        if tree is None:
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        for node in tree.body:
            name = value = None
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                name, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            if not name or not _ROSTER_NAME_RE.match(name):
                continue
            items = _literal_collection(value)
            if items and (items & vocab):
                out[f"{rel}::{name}"] = items
    return out


def _registered_tools() -> set[str]:
    import agent  # noqa: F401  （局部导入：避免本模块被 import 时拉起重依赖）
    from runtime.registry import discover_from_agent
    return {b.spec.name for b in discover_from_agent(agent.assistant_agent).all()}


class RosterShapeTests(unittest.TestCase):
    """先守住"扫描本身有效"，否则下面的断言可能是空转。"""

    def test_rosters_are_found(self) -> None:
        rosters = _rosters()
        self.assertGreater(len(rosters), 0,
                           "没有发现任何工具名册，扫描逻辑可能退化了")
        for must in ("benchmark/evaluator.py::RUN_TOOLS",
                     "runtime/completion.py::VERIFY_TOOLS"):
            self.assertIn(must, rosters, f"扫描漏掉了 {must}")


class CanonicalVerifyRosterTests(unittest.TestCase):
    """规范名册必须彼此一致 —— 这是本轮两类漂移的共同根源。"""

    def test_canonical_rosters_all_equal_authority(self) -> None:
        rosters = _rosters()
        checked = 0
        for key, items in sorted(rosters.items()):
            var = key.split("::", 1)[1]
            if var in _EXPERIMENT_VARIANTS:
                continue
            if var not in _CANONICAL_ROSTER_NAMES:
                continue
            checked += 1
            self.assertEqual(
                set(items), set(CANONICAL_VERIFY),
                f"{key} = {sorted(items)}，与权威集合 "
                f"{sorted(CANONICAL_VERIFY)}（runtime/completion.py:VERIFY_TOOLS）不一致。\n"
                f"漏 run_tests 的后果：coding case 真跑一次测试就被判 fail；\n"
                f"漏 code_loop 的后果：自主代码循环不计为验证证据。",
            )
        self.assertGreaterEqual(checked, 1,
                                f"只校验了 {checked} 张规范名册，名单可能已过时")

    def test_experiment_variants_are_subsets(self) -> None:
        """实验变体允许更窄，但不得出现权威集合之外的新名字。"""
        rosters = _rosters()
        for key, items in sorted(rosters.items()):
            var = key.split("::", 1)[1]
            if var not in _EXPERIMENT_VARIANTS:
                continue
            extra = set(items) - set(CANONICAL_VERIFY)
            self.assertFalse(extra, f"{key} 含权威集合外的验证工具 {sorted(extra)}")


class NoWrongToolNameTests(unittest.TestCase):
    """错名回归哨兵：任何生产文件的字符串字面量都不得是已知错名。"""

    def test_no_wrong_name_literal_anywhere(self) -> None:
        bad: list[str] = []
        for p in _production_files():
            tree = _parse(p)
            if tree is None:
                continue
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value in WRONG_NAMES):
                    bad.append(f"{rel}:{node.lineno} 出现 {node.value!r} "
                               f"（正确名应为 {WRONG_NAMES[node.value]!r}）")
        self.assertFalse(
            bad,
            "发现错名工具名（错名不会报错，只会让名册静默失效）：\n  " + "\n  ".join(bad),
        )

    def test_wrong_names_are_absent_from_rosters(self) -> None:
        for key, items in sorted(_rosters().items()):
            hit = sorted(set(items) & set(WRONG_NAMES))
            self.assertFalse(hit, f"{key} 含错名 {hit}")


class RosterEntryExplainabilityTests(unittest.TestCase):
    """名册里引用的每个名字都必须能解释 —— 否则新增未知名字会静默生效。"""

    def test_non_registered_entries_are_all_documented(self) -> None:
        registered = _registered_tools()
        undocumented: list[str] = []
        for key, items in sorted(_rosters().items()):
            for name in sorted(set(items) - registered):
                if name not in KNOWN_NON_REGISTERED:
                    undocumented.append(f"{key} 引用 {name!r}（既非注册工具、也未登记理由）")
        self.assertFalse(
            undocumented,
            "名册引用了无法解释的名字：\n  " + "\n  ".join(undocumented)
            + "\n若是新增的未启用技能工具，请补进 KNOWN_NON_REGISTERED 并写明技能名；"
              "若是错名，请改正。",
        )

    def test_documented_exceptions_still_needed(self) -> None:
        """登记过的名字若已变成注册工具，就该从例外表里删掉（防止例外表腐烂）。"""
        registered = _registered_tools()
        stale = sorted(n for n in KNOWN_NON_REGISTERED if n in registered)
        self.assertFalse(stale, f"这些名字已是注册工具，请从 KNOWN_NON_REGISTERED 移除：{stale}")


class FileScopeCoverageTests(unittest.TestCase):
    """新工具若既不在 READ/WRITE 也不在 NON_FILE 登记表 → 严格模式静默 DENY。"""

    def test_every_registered_tool_is_filescope_covered(self) -> None:
        from runtime.filescope import NON_FILE_TOOLS, READ_TOOL_ARGS, WRITE_TOOLS

        registered = _registered_tools()
        covered = set(READ_TOOL_ARGS) | set(WRITE_TOOLS) | set(NON_FILE_TOOLS)
        uncovered = sorted(registered - covered)
        expected = sorted(FILESCOPE_UNCOVERED_EXCEPTIONS)
        self.assertEqual(
            uncovered, expected,
            f"filescope 覆盖情况变化：未覆盖 {uncovered}，预期 {expected}。\n"
            f"新增工具必须显式登记（READ_TOOL_ARGS / WRITE_TOOLS / NON_FILE_TOOLS），"
            f"否则严格 Project 下会被 fail-closed 拒绝且只在运行期才暴露；\n"
            f"若确为有意不登记，请写进 FILESCOPE_UNCOVERED_EXCEPTIONS 并说明理由。",
        )


class GuardLeverConsistencyTests(unittest.TestCase):
    """护栏开关与执行预算：`.env` 的显式值必须与代码默认值一致。

    背景（2026-09-20）：这些开关原先全部隐含在代码默认值里，且口径不一
    （`FORGE_REPEAT_GUARD` 默认 on，`FORGE_REDUNDANT_GUARD` / `FORGE_COMPLETION_READY`
    默认 off），审计时无法从配置回答"到底几道防线在跑"。
    把它们显式写进 `.env` 之后，**同一件事就有了两份默认值** —— 必须由本测试守住，
    否则修完一个漂移又造出一个新的（这正是本文件通篇在防的病）。
    """

    @staticmethod
    def _read_env_file() -> dict:
        from pathlib import Path as _Path

        path = _Path(__file__).resolve().parent.parent / ".env"
        if not path.exists():
            return {}
        values: dict = {}
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
        return values

    def test_budget_values_match_code_defaults(self) -> None:
        from runtime.runctx import _DEFAULT_PER_TOOL_BUDGETS

        env = self._read_env_file()
        if not env:
            self.skipTest("无 .env（克隆/CI 环境），跳过")

        for key, code_default in (("TOOL_BUDGET_TOTAL", 20),
                                  ("TOOL_BUDGET_WEB_SEARCH", 5)):
            self.assertIn(key, env, f".env 缺少 {key}（护栏与预算应显式声明）")
            self.assertEqual(
                int(env[key]), code_default,
                f"{key} 在 .env 是 {env[key]}，代码默认是 {code_default}："
                f"改一边必须同时改另一边，或从 .env 删掉让它走默认值。",
            )

        declared: dict = {}
        for chunk in env.get("TOOL_BUDGET_PER_TOOL", "").replace(";", ",").split(","):
            name, sep, cap = chunk.partition("=")
            if sep and name.strip():
                try:
                    declared[name.strip()] = int(cap.strip())
                except ValueError:
                    self.fail(f"TOOL_BUDGET_PER_TOOL 里有非整数上限：{chunk!r}")
        self.assertTrue(declared, ".env 应显式声明 TOOL_BUDGET_PER_TOOL")
        mismatched = sorted(
            n for n, c in declared.items()
            if _DEFAULT_PER_TOOL_BUDGETS.get(n) not in (None, c)
        )
        self.assertFalse(
            mismatched,
            f"这些工具的独立预算在 .env 与 runtime/runctx.py::_DEFAULT_PER_TOOL_BUDGETS "
            f"里不一致：{mismatched}",
        )

    def test_guard_switches_are_explicit(self) -> None:
        env = self._read_env_file()
        if not env:
            self.skipTest("无 .env（克隆/CI 环境），跳过")
        for key in ("FORGE_REPEAT_GUARD", "FORGE_REDUNDANT_GUARD",
                    "FORGE_COMPLETION_READY"):
            self.assertIn(key, env, f".env 缺少护栏开关 {key}（应显式声明 on/off）")
            self.assertIn(
                env[key].strip().lower(),
                ("on", "off", "1", "0", "true", "false"),
                f"{key}={env[key]} 不是合法开关值",
            )


if __name__ == "__main__":
    unittest.main()
