"""路由表与工具注册表的一致性约束（2026-09-19 工具清单对账后新增）。

背景 —— 两张表之间原本没有任何校验，于是：

1. `_EXPLICIT_ONLY_TOOLS` 是**高门槛表**，语义容易记反：
   **在表内**的工具要命中语义才放行；**不在表内**的 `_allowed()` 直接 `return True`。
   所以表里出现**错名**（`code_loop_tool` vs 实际注册名 `code_loop`）的后果，
   不是"工具派发不出去"，而是"本该受门槛保护的自主循环重工具被无条件放行"。
   实测：50 道评测题里有 12 题曾被盲塞 `code_loop`，包括
   "帮我记一下：正式环境数据库不能直接执行 destructive migration"
   和"介绍你能做什么…再告诉我北京天气"这类完全无关的查询。

2. `TOOL_TERMS` / `_HIGH_GATE_TERMS` 里若有指向**不存在工具**的条目，就是死引用 ——
   永不生效，且没有任何报警。下线工具或改动技能启用列表都会静默留下新的死引用。

3. 两张**门槛**表之间也会脱节。`_HIGH_GATE_TERMS` 的词条只在工具名**同时出现在
   `_EXPLICIT_ONLY_TOOLS`** 里时才会被求值（`_high_gate_hit` 只有一个调用点且有前置条件）。
   只写词条、忘了加名字，该工具就等于**没有门槛**、被无条件放行；
   反过来只加名字、没写词条，`_high_gate_hit` 会兜底 `return True`，同样是没门槛。
   2026-09-19 实测：gorden 系列 4 条词条长期处于第一种状态。但要注意辨别成因——
   它们属于**设计变更后未清理的遗留**（该族改走 `_GORDEN_PPT_INTENT` 意图族补齐，
   因为出现过 "Tool not found" 真实故障），因此处置是**删词条**，
   而不是"把名字补进生效集"（那会掐掉补齐路径）。

本文件把关系钉死：**路由表引用的每个名字，必须 ∈「注册表工具 ∪ 技能工具定义」。**
技能工具的定义从 `skills/*/tools.py` 的函数签名静态扫描得到（不执行模块，避免副作用），
所以"技能未启用"时这些名字仍算已知 —— 死引用只允许指向**已声明存在**的技能工具。
"""
import os
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agent import assistant_agent  # noqa: E402
from runtime import tool_router as tr  # noqa: E402

#: 参与一致性校验的路由表（名字 → 被引用的工具名集合）
TABLES: dict[str, set[str]] = {}
for _attr in ("TOOL_TERMS",):
    _obj = getattr(tr, _attr, None)
    if isinstance(_obj, dict):
        TABLES[_attr] = set(_obj.keys())
for _attr in (
    "BASE_TOOLS",
    "_CODING_SUPPORT",
    "_WEB_SUPPORT",
    "_MEMORY_NOTE_SUPPORT",
    "_DOC_READ_SUPPORT",
    "_ANALYSIS_SUPPORT",
    "_ARITH_SUPPORT",
    "_MEMORY_NOTE_TOOLS",
    "_EXPLICIT_ONLY_TOOLS",
    "_CAPABILITY_EXPLORE_TOOLS",
    "_WRITE_TOOLS_SET",
):
    _obj = getattr(tr, _attr, None)
    if isinstance(_obj, (set, frozenset)):
        TABLES[_attr] = set(_obj)
TABLES["_HIGH_GATE_TERMS"] = {name for name, _terms in tr._HIGH_GATE_TERMS}


def skill_tool_names() -> set[str]:
    """静态扫描 skills/*/tools.py 的顶层函数名（公开名，跳过下划线开头）。

    静态扫描而非 import：技能模块可能依赖运行时环境或被禁用，import 会引入副作用。
    """
    found: set[str] = set()
    for py in sorted((BASE / "skills").glob("*/tools.py")):
        text = py.read_text(encoding="utf-8", errors="replace")
        found.update(re.findall(r"^def ([a-z][a-z0-9_]*)\(", text, re.M))
    return found


REGISTERED = {t.name for t in assistant_agent.tools}
SKILL_TOOLS = skill_tool_names()
KNOWN = REGISTERED | SKILL_TOOLS


class RouterRegistryConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        """必须显式把 TOOL_ROUTER 固定为 on。

        `router_enabled()` 每次调用都读 `os.environ`（无模块级缓存），
        `TOOL_ROUTER=off` 时 `select_tool_names` 直接返回 `list(available)`、
        **返回值与 query 无关**。全量跑时其它测试会把它改成 off 且未必恢复 ——
        实测若不固定，本文件的行为断言会在全量回归里失败、单独跑却通过。
        """
        self._saved_router = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "on"

    def tearDown(self) -> None:
        if self._saved_router is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._saved_router

    def test_skill_scan_is_not_empty(self) -> None:
        """保证扫描本身有效 —— 否则下面的断言会因为 KNOWN 退化而变成空断言。"""
        self.assertIn("scan_dependencies", SKILL_TOOLS)
        self.assertIn("gorden_ppt_build", SKILL_TOOLS)
        self.assertTrue(SKILL_TOOLS, "技能工具扫描结果为空，一致性断言将失去意义")

    def test_registry_and_router_overlap(self) -> None:
        """路由表至少要真的认识大部分注册工具，防止两边彻底脱节。"""
        overlap = REGISTERED & TABLES["TOOL_TERMS"]
        self.assertGreaterEqual(
            len(overlap), 20,
            f"TOOL_TERMS 只认识 {len(overlap)} 个注册工具，路由表与注册表可能已脱节",
        )

    def test_router_tables_only_reference_known_tools(self) -> None:
        """核心断言：路由表引用的名字必须存在于注册表或技能工具定义中。"""
        problems: list[str] = []
        for table, refs in sorted(TABLES.items()):
            unknown = sorted(refs - KNOWN)
            for name in unknown:
                problems.append(f"  {table} 引用了不存在的工具 {name!r}")
        self.assertFalse(
            problems,
            "路由表存在死引用（下线工具/改技能启用列表时未同步）：\n" + "\n".join(problems),
        )

    def test_explicit_only_tools_are_effective_names(self) -> None:
        """门槛表里的名字必须精确匹配注册名 —— 或至少是已声明的技能工具名。

        这条专门守 `code_loop_tool` 那类**错名**：错名不会报错，只会让门槛静默失效。
        允许出现未启用技能的工具名（如 `scan_dependencies`）—— 技能启用后它就是注册工具；
        但**不在 KNOWN 里**的名字（既非注册工具、也非任何 skills/*/tools.py 的函数）
        一定是错名或已删工具的残留，必须失败。
        """
        for name in sorted(tr._EXPLICIT_ONLY_TOOLS):
            self.assertIn(
                name, KNOWN,
                f"_EXPLICIT_ONLY_TOOLS 里的 {name!r} 既不是注册工具、也不是任何技能定义的工具 —— "
                f"错名会让门槛对该工具静默失效（被无条件放行）",
            )

    def test_code_loop_gate_is_active(self) -> None:
        """回归哨兵：钉住 2026-09-19 的 P0 修复，防止错名再次出现。"""
        self.assertIn("code_loop", REGISTERED)
        self.assertIn("code_loop", tr._EXPLICIT_ONLY_TOOLS)
        self.assertNotIn("code_loop_tool", tr._EXPLICIT_ONLY_TOOLS)
        self.assertTrue(tr._high_gate_hit("code_loop", "帮我修复这段代码的 bug"))
        self.assertFalse(tr._high_gate_hit("code_loop", "帮我记一下明天开会"))

    def test_high_gate_terms_are_effective(self) -> None:
        """`_HIGH_GATE_TERMS` 的键必须在 `_EXPLICIT_ONLY_TOOLS` 内，双向都不能有缺口。

        依据：`_high_gate_hit()` 全项目**只有一个调用点**（`select_tool_names._allowed`），
        且前置条件是 `if name in _EXPLICIT_ONLY_TOOLS`。由此产生两种"写了但没接上"：

        - 词条表有、生效集没有 → 该词条**永不被求值**（死数据）。
          2026-09-19 实测：gorden 系列 4 条长期处于此状态，后果是它们被
          `_allowed()` 无条件放行 —— 纯文字的"帮我写个季度总结"被塞整套 PPT 工具
          （工具数 4→8）。与 P0 的 `code_loop_tool` 错名是**同一类**病。
        - 生效集有、词条表没有 → `_high_gate_hit()` 走到末尾 `return True` 兜底，
          等于该工具**实际没有门槛**（只在被点名时才拦住）。
        """
        gate_keys = {name for name, _terms in tr._HIGH_GATE_TERMS}
        active = set(tr._EXPLICIT_ONLY_TOOLS)

        never_evaluated = sorted(gate_keys - active)
        self.assertFalse(
            never_evaluated,
            f"_HIGH_GATE_TERMS 里这些词条永远不会被求值"
            f"（不在 _EXPLICIT_ONLY_TOOLS 内，等于死数据）：{never_evaluated}",
        )

        no_terms = sorted(active - gate_keys)
        self.assertFalse(
            no_terms,
            f"_EXPLICIT_ONLY_TOOLS 里这些工具没有门槛词条，"
            f"_high_gate_hit() 会兜底 return True（等于不设门槛）：{no_terms}",
        )

    def test_gorden_tools_are_intent_family_driven(self) -> None:
        """gorden 族走「意图族补齐」而非高门槛 —— 有意设计，并记录它的代价。

        背景（**真实故障**）：任务 goal 不含"套模板 / 做PPT"等精确词时，若只靠门槛
        词条判断，工具会被裁出 16 窗口，模型随后按技能指令调用即报
        "Tool ... not found in agent"。所以它们只由 `_GORDEN_PPT_INTENT` 补齐，
        同源回归见 tests/test_tool_router.py::test_natural_ppt_phrasing_keeps_gorden_tools。

        ⚠️ 第二段断言的是**当前已知代价，不是优点**：不含任何 PPT 字面词的
        "帮我写个季度总结"也会带上这 4 个工具（工具数 4→8）。
        若将来决定收紧口径，这段会变红 —— 那是**预期内的行为变更**：
        届时必须同步修改上面那条 test_tool_router.py 的回归，并确认不会
        重新引入 Tool-not-found。**不要只改一边。**
        """
        pool = sorted(REGISTERED | {n for n in SKILL_TOOLS if n.startswith("gorden_")})
        for query in ("简约商务总结汇报", "帮我做个PPT",
                      "用公司模板做汇报", "把这份总结做成PPT"):
            names = tr.select_tool_names(query, pool)
            self.assertTrue(
                any(n.startswith("gorden_") for n in names),
                f"{query!r} 应能派发 gorden 工具（意图族补齐），当前结果：{names}",
            )
        names = tr.select_tool_names("帮我写个季度总结", pool)
        self.assertTrue(
            any(n.startswith("gorden_") for n in names),
            "意图族补齐的行为变了？请同步更新 test_tool_router.py 的同源回归测试，"
            "并确认不会重新引入 Tool-not-found 故障。",
        )

    def test_dead_refs_point_to_existing_skill_dirs(self) -> None:
        """当前唯一的合法死引用来源 = 未启用的技能。

        若某天技能目录被删、路由表却没清理，这条会失败并指明要清哪几行。
        """
        dead = sorted((set().union(*TABLES.values()) if TABLES else set()) - REGISTERED)
        if not dead:
            return
        for name in dead:
            self.assertIn(
                name, SKILL_TOOLS,
                f"{name!r} 既不在注册表、也不在任何 skills/*/tools.py 里 —— "
                f"这是纯死引用，应从 TOOL_TERMS / _HIGH_GATE_TERMS 中删除",
            )

    def test_local_source_search_not_in_web_family(self) -> None:
        """`search_sources`（本地参考资料）不得留在联网补齐族里。

        2026-09-19 对账结论：它是本地检索，与"联网查实时信息"是两件事。
        留在 `_WEB_SUPPORT` 会让"北京天气/航班/股价"这类纯联网查询凭空多带
        一个本地资料检索工具 —— 与 BASE_TOOLS 注释里"web_search 不常驻"
        完全同类的噪音。实测 50 题里 11 题被这样捎带，采纳率 1/11。
        """
        self.assertNotIn(
            "search_sources", tr._WEB_SUPPORT,
            "search_sources 又被加回 _WEB_SUPPORT 了 —— 本地资料检索不属于联网族",
        )

    def test_pure_web_query_does_not_dispatch_local_source_search(self) -> None:
        """纯联网查询不应派发本地参考资料检索（回归哨兵）。"""
        for query in ("北京现在天气怎么样？", "帮我查明天去上海的航班。"):
            names = tr.select_tool_names(query, sorted(REGISTERED))
            self.assertNotIn(
                "search_sources", names,
                f"纯联网查询 {query!r} 不该派发 search_sources（会空转：库当前为空）",
            )

    def test_search_documents_stays_available_for_web_queries(self) -> None:
        """对照组：`search_documents` 必须留在联网族。

        它与 `search_sources` 暴露位置相同、但库里有 2MB 索引（非空），
        实测被调用 355 次。把两者一起摘掉会丢掉真实可用的检索能力 ——
        这条防止有人看到上面的改动后"顺手统一"。
        """
        self.assertIn("search_documents", tr._WEB_SUPPORT)


if __name__ == "__main__":
    unittest.main()
