"""护栏：向量引擎缓存三全局（`_EMBEDDER`/`_EMBED_ERROR`/`_EMBEDDER_LOADED`）必须自洽。

⚠️ **为什么需要这条**（10-05 实测，`var/_probe3.py` 逐段可复现）：

`rag.py::_try_load_embedder` 曾先置 `_EMBEDDER_LOADED = True`、**再**判
`_embed_enabled()`，而 `enabled=False` 的早退分支**没清 `_EMBEDDER`** ⇒ 模块级
状态变成「标志位=True + 陈旧引擎对象」的自相矛盾态。紧接着的第二次调用命中
`_EMBEDDER_LOADED` 缓存短路（trace 里显示为 `CACHE-HIT`），把上一次那个陈旧
引擎原样交出去。

可观测后果：`EMBED_MODEL_SETTING='off'` 声明的「纯 BM25」测试**悄悄走 hybrid** ——
无关查询「量子力学入门书籍推荐」召回「菜谱.md」，且输出里出现
「（本次为关键词+向量混合检索）」。曾表现为
`pytest tests/test_sources_rag.py tests/test_rag.py` → 2 failed
（而 `pytest tests/test_rag.py` 单跑 13 passed ⇒ **单跑绿不证明隔离有效**）。

🔑 **本护栏为什么用假引擎而不是真引擎**（易踩）：
`models/bge-small-zh-v1.5/` 不入库、`onnxruntime` 不在 `requirements.txt`
⇒ **CI 上 `_embed_enabled()` 在 SETTING='' 时也返回 False、真引擎恒 None**
⇒ 任何依赖真引擎的复现都会在 CI 上静默空转（判据恒绿、零鉴别力）。
故这里注入一个**鸭子类型的假引擎**（只需 `embed_batch`），使复现在
「有模型 / 无模型」两种机器上都成立。

判据（双向）：
  ① **召回方向**：缓存态自相矛盾时，第二次调用**绝不能**把陈旧引擎交出去；
  ② **误伤方向**：陈旧引擎被清掉后，**不该**误召回无关文档（防「一律返空」
     这种假修复 —— 那会把真向量场景也一起打瞎）；
  ③ **判据自检**：假引擎确实能被交出去（否则本护栏在当前机器上已空转，
     必须报出来而不是静绿）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import rag  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402


#: 哨兵：表示「payload 里根本没有 embed_model 这个键」（区别于值为 None/空串）
_MISSING = object()


class _FakeEngine:
    """鸭子类型假向量引擎：恒返回同一向量 ⇒ 一旦被调用，检索结果就会被污染。

    只实现 `rag.py` 实际用到的 `embed_batch`，与真 `OnnxEmbedEngine` 同形。
    """

    def __init__(self, tag: str = "fake") -> None:
        self.tag = tag
        self.calls: list[int] = []      # 每次被调用的 chunk 数

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]


def _make_workspace() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="rag_isolation_"))
    (tmp / "运动笔记.md").write_text(
        "我每天早晨跑步三十分钟，周末去健身房举铁。\n坚持锻炼对身体很好，睡得更香。",
        encoding="utf-8")
    (tmp / "菜谱.md").write_text(
        "番茄炒蛋做法：先把鸡蛋打散，热锅下油，倒入蛋液翻炒。", encoding="utf-8")
    (tmp / "README").write_text(
        "本目录测试说明：无后缀文件也应能被检索。", encoding="utf-8")
    return tmp


#: 必须被「声明关向量」的测试类一起重置的三个模块级全局。
VECTOR_GLOBALS = ("_EMBEDDER", "_EMBED_ERROR", "_EMBEDDER_LOADED")

#: 行为探测用的 `EMBED_MODEL_SETTING` 值：**显式非空路径**。
#:
#: 🔑 为什么不用 `""`：`rag._embed_enabled()` 在 `SETTING=''` 时返回
#: `EMBED_DEFAULT_DIR.is_dir()`，而 `models/` 目录**不入库** ⇒ CI 上为 False
#: ⇒ 用 `""` 探测会让「前提」在 CI 上不成立、判据静默失效（正是本文件
#: 开头警告的那类空转）。显式路径分支与磁盘无关，两种机器上恒为 True。
_PROBE_SETTING = "__isolation_probe_enabled__"


def _discover_vector_off_classes(module) -> list[str]:
    """**行为探测**：找出 `module` 里所有「声明关向量」的 `TestCase` 类。

    ⚠️ 这里刻意**不用文本匹配**（例如 `getsource(setUp)` 里 grep
    `EMBED_MODEL_SETTING`）：文本判据只能问「源码里出现过这个词吗」，
    判 `rag._EMBEDDER = rag._EMBEDDER`（语义 = 完全不清）、只读不写、
    或只写在注释里，都会**照样通过**（10-06 实测，报告 §C2）。

    探测协议（对每个 `TestCase` 子类）：
      1. 预置 `EMBED_MODEL_SETTING=<显式路径>` ⇒ `_embed_enabled()` 必为 True
         （代表「本来会走向量路径」）；
      2. **真的调一次 `setUp()`**；
      3. 若之后 `_embed_enabled()` 变为 False ⇒ 这个类自己把向量关了
         ⇒ 它就是需要隔离的对象。

    只收 `cls.__module__ == module.__name__` 的类：本文件里的对照类
    （`_Probe*`）定义在 `test_rag_isolation`，不会混进对 `test_rag` 的扫描。
    """
    found = []
    for name, obj in vars(module).items():
        if not isinstance(obj, type) or not issubclass(obj, unittest.TestCase):
            continue
        if obj.__module__ != module.__name__:
            continue
        if _declares_vector_off(obj):
            found.append(name)
    return sorted(found)


def _declares_vector_off(cls: type) -> bool:
    """跑一次 `cls.setUp()`，看它是否把 `EMBED_MODEL_SETTING` 改成「关」。"""
    saved = (rag.EMBED_MODEL_SETTING, rag._EMBEDDER, rag._EMBED_ERROR,
             rag._EMBEDDER_LOADED)
    inst = None
    try:
        rag.EMBED_MODEL_SETTING = _PROBE_SETTING
        rag._EMBEDDER = None
        rag._EMBED_ERROR = None
        rag._EMBEDDER_LOADED = True
        if not rag._embed_enabled():
            # 前提不成立（显式路径分支被改坏）⇒ 宁可报错也不要静默放行，
            # 否则本护栏会在「探测前提失效」时退化成恒绿。
            raise AssertionError(
                f"探测前提不成立：EMBED_MODEL_SETTING={_PROBE_SETTING!r} 时 "
                f"_embed_enabled() 应为 True（显式路径分支与磁盘无关）")
        inst = cls("run")
        inst.setUp()
        return not rag._embed_enabled()
    finally:
        if inst is not None:
            try:
                inst.tearDown()
            except Exception:  # 探测用的清理失败不应掩盖主判据
                pass
        (rag.EMBED_MODEL_SETTING, rag._EMBEDDER, rag._EMBED_ERROR,
         rag._EMBEDDER_LOADED) = saved


def _dirty_globals_after_setup(cls: type) -> list[str]:
    """行为探针：预置脏状态 → 真调 `cls.setUp()` → 返回**仍未被重置**的全局名。

    返回空列表 = 该类做了完整隔离。这是 #1 的核心判据：它问的是
    「跑完之后状态到底干不干净」，而不是「源码里写过这个词吗」。
    """
    saved = (rag.EMBED_MODEL_SETTING, rag._EMBEDDER, rag._EMBED_ERROR,
             rag._EMBEDDER_LOADED)
    inst = None
    try:
        # 模拟上游污染：引擎已装上、缓存标记为 True（＝缓存短路态）
        rag._EMBEDDER = _FakeEngine("dirty")
        rag._EMBED_ERROR = "上一轮的脏错误"
        rag._EMBEDDER_LOADED = True
        inst = cls("run")
        inst.setUp()
        still_dirty = []
        if rag._EMBEDDER is not None:
            still_dirty.append("_EMBEDDER")
        if rag._EMBED_ERROR is not None:
            still_dirty.append("_EMBED_ERROR")
        if rag._EMBEDDER_LOADED:
            still_dirty.append("_EMBEDDER_LOADED")
        return still_dirty
    finally:
        if inst is not None:
            try:
                inst.tearDown()
            except Exception:  # 同上：清理失败不掩盖主判据
                pass
        (rag.EMBED_MODEL_SETTING, rag._EMBEDDER, rag._EMBED_ERROR,
         rag._EMBEDDER_LOADED) = saved


class _EmbedStateMixin:
    """统一保存/还原三个向量全局 —— 护栏自己也不能污染别人。"""

    def setUp(self) -> None:
        self._orig = (rag._EMBEDDER, rag._EMBED_ERROR,
                      rag._EMBEDDER_LOADED, rag.EMBED_MODEL_SETTING)

    def tearDown(self) -> None:
        (rag._EMBEDDER, rag._EMBED_ERROR,
         rag._EMBEDDER_LOADED, rag.EMBED_MODEL_SETTING) = self._orig


class StaleEngineNotLeakedTests(_EmbedStateMixin, unittest.TestCase):
    """产品侧判据：`rag._try_load_embedder` 自身的缓存自洽性。"""

    def test_enabled_false_branch_clears_stale_engine(self) -> None:
        """`enabled=False` 早退后，缓存态不得留着陈旧引擎。"""
        stale = _FakeEngine("stale")
        rag._EMBEDDER = stale
        rag._EMBEDDER_LOADED = False
        rag._EMBED_ERROR = "上一轮的旧错误"
        rag.EMBED_MODEL_SETTING = "off"
        self.assertFalse(rag._embed_enabled(), "前提不成立：off 应当关闭向量")

        engine, error = rag._try_load_embedder()

        self.assertIsNone(engine,
                          "enabled=False 时返回了陈旧引擎 —— 这就是 hybrid 的根")
        self.assertIsNone(error)
        self.assertIsNone(rag._EMBEDDER,
                          "早退分支未清 _EMBEDDER ⇒ 状态自相矛盾")
        self.assertIsNone(rag._EMBED_ERROR,
                          "早退分支未清 _EMBED_ERROR ⇒ 会把旧错误当本次结果")

    def test_second_call_after_disabled_does_not_return_stale_engine(self) -> None:
        """两次调用场景：第一次早退，第二次走缓存短路 —— 不得交出陈旧引擎。

        这是本缺陷的**实际触发序列**（`var/_probe3.py` trace 实测为
        `["RE-JUDGE(...)", "CACHE-HIT", "CACHE-HIT"]`）。
        """
        stale = _FakeEngine("stale")
        rag._EMBEDDER = stale
        rag._EMBEDDER_LOADED = False
        rag.EMBED_MODEL_SETTING = "off"

        first, _ = rag._try_load_embedder()
        second, _ = rag._try_load_embedder()      # ← 这里曾返回 stale

        self.assertIsNone(first)
        self.assertIsNone(second,
                          "缓存短路把陈旧引擎交出去了（标志位=True + 对象未清）")

    def test_failed_load_branch_also_clears_stale_engine(self) -> None:
        """「模型目录缺失」早退分支同样必须清空（不只是 `enabled=False` 那条）。"""
        stale = _FakeEngine("stale")
        rag._EMBEDDER = stale
        rag._EMBEDDER_LOADED = False
        rag._EMBED_ERROR = None
        # SETTING 指向一个**不存在的目录** ⇒ enabled=True 但 model_dir=None
        rag.EMBED_MODEL_SETTING = "definitely-no-such-model-dir"
        self.assertTrue(rag._embed_enabled(), "前提不成立：显式路径应视为已启用")

        engine, error = rag._try_load_embedder()

        self.assertIsNone(engine)
        self.assertIsNotNone(error, "模型目录缺失应给出降级说明")
        self.assertIsNone(rag._EMBEDDER,
                          "失败分支未清 _EMBEDDER ⇒ 状态自相矛盾")


class PureBm25StaysPureTests(_EmbedStateMixin, unittest.TestCase):
    """端到端判据：声明「关向量」时，检索不得走向量路径。"""

    def _search(self, query: str) -> str:
        tool = rag.search_documents
        ctx = ToolContext(context=None, tool_name=tool.name,
                          tool_call_id="t", tool_arguments="{}")
        r = tool.on_invoke_tool(
            ctx, json.dumps({"query": query, "directory": "."},
                            ensure_ascii=False))
        if asyncio.iscoroutine(r):
            r = asyncio.run(r)
        return str(r)

    def test_unrelated_query_not_recalled_after_upstream_loaded_engine(self) -> None:
        """上游装过引擎后，本测试声明关向量 ⇒ 无关查询必须召回 0 条。"""
        tmp = _make_workspace()
        orig_root, orig_index = rag.WORKSPACE_ROOT, rag.INDEX_PATH
        upstream = _FakeEngine("upstream")

        # 模拟上游污染：引擎已装上且缓存标记为 True（= CACHE-HIT 态）
        rag._EMBEDDER = upstream
        rag._EMBEDDER_LOADED = True
        rag._EMBED_ERROR = None
        rag.EMBED_MODEL_SETTING = ""

        # 本测试声明「关向量」
        rag.WORKSPACE_ROOT = tmp
        rag.INDEX_PATH = tmp / "rag_index.json"
        rag.EMBED_MODEL_SETTING = "off"
        rag._EMBEDDER = None
        rag._EMBEDDER_LOADED = False
        rag._EMBED_ERROR = None
        try:
            out = self._search("量子力学入门书籍推荐")
            self.assertEqual(
                upstream.calls, [],
                f"声明关向量却调用了上游引擎 {len(upstream.calls)} 次 ⇒ 向量路径复活")
            self.assertNotIn(
                "混合检索", out,
                "输出自称「关键词+向量混合检索」⇒ 本该是纯 BM25 的查询走了向量")
            self.assertNotIn(
                "菜谱", out,
                "无关查询召回了无关文档「菜谱.md」⇒ 判据已空转或 hybrid 未清干净")
        finally:
            rag.WORKSPACE_ROOT = orig_root
            rag.INDEX_PATH = orig_index

    def test_guard_would_detect_the_old_bug(self) -> None:
        """反空转：**不清理陈旧引擎**时，本护栏的判据必须真的红。

        若这条红了，说明上面的用例判据有鉴别力（而不是恒绿）。

        🔑 **中和的是产品侧的清空语句**（`rag.py::_try_load_embedder` 里
        `_EMBEDDER = None; _EMBED_ERROR = None`），不是测试侧的「只重置标志位」——
        因为产品已修好，只重置标志位**本来就不该**再复现缺陷（那样测的是产品修复，
        而本组守护的正是产品修复）。若改成中和测试侧，这条会恒绿而彻底空转。
        """
        tmp = _make_workspace()
        orig_root, orig_index = rag.WORKSPACE_ROOT, rag.INDEX_PATH
        upstream = _FakeEngine("upstream")

        rag._EMBEDDER = upstream
        rag._EMBEDDER_LOADED = True
        rag._EMBED_ERROR = None
        rag.EMBED_MODEL_SETTING = ""

        # 本测试声明「关向量」，且只重置标志位（测试侧保持旧写法）
        rag.WORKSPACE_ROOT = tmp
        rag.INDEX_PATH = tmp / "rag_index.json"
        rag.EMBED_MODEL_SETTING = "off"
        rag._EMBEDDER_LOADED = False
        try:
            # 🔬 中和：临时把 `_try_load_embedder` 换成**修复前**的版本
            #（先置 LOADED=True、enabled=False 早退时不清 _EMBEDDER）
            orig_try = rag._try_load_embedder

            def buggy_try():
                """精确复刻修复前的 `_try_load_embedder`（少两条清空语句）。

                ⚠️ **只复刻 `enabled=False` 这条路径**（10-06 补注，报告 §C4）：
                真实旧实现有 11 行控制流，这里只有 6 行，**故意省略**了两处：

                - `model_dir is None` 分支：真实实现会写 `_EMBED_ERROR =`长文案
                  再返回；这里直接返回短文案、**不写全局**；
                - 加载成功分支：真实实现会 `OnnxEmbedEngine()` + try/except；
                  这里恒返回 `None, "中和态"`。

                **为什么省略是安全的**：本护栏的复现路径是 `SETTING='off'` ⇒
                第 3 行 `if not _embed_enabled(): return None, None` 就返回了，
                被省略的两条分支**永不执行**。而`model_dir is None` 与加载
                成功这两条分支，由 `StaleEngineNotLeakedTests`（直调**真函数**）
                与 `HalfBakedEngineTests`（直调**真函数**）覆盖。

                ⇒ **不要因为这里"看起来简化"就放宽真函数上的断言**：
                本函数只是缺陷复现器，真相在`rag._try_load_embedder` 本身。
                """
                if rag._EMBEDDER_LOADED:
                    return rag._EMBEDDER, rag._EMBED_ERROR
                rag._EMBEDDER_LOADED = True
                if not rag._embed_enabled():
                    return None, None
                model_dir = rag._resolve_embed_model_dir()
                if model_dir is None:
                    return None, "未找到向量模型目录"
                return None, "中和态：本护栏不打算真加载模型"

            rag._try_load_embedder = buggy_try
            try:
                out = self._search("量子力学入门书籍推荐")
            finally:
                rag._try_load_embedder = orig_try

            self.assertTrue(
                upstream.calls,
                "假引擎一次都没被调用 ⇒ 本护栏在当前机器上无法复现该缺陷，"
                "判据可能已空转（请检查 _FakeEngine 是否仍与 rag.py 同形）")
            self.assertIn("混合检索", out,
                          "旧缺陷路径未复现：预期输出应自称混合检索")
            self.assertIn("菜谱", out,
                          "旧缺陷路径未复现：预期应误召回无关文档「菜谱.md」")
        finally:
            rag.WORKSPACE_ROOT = orig_root
            rag.INDEX_PATH = orig_index

    def test_product_side_alone_keeps_pure_bm25_pure(self) -> None:
        """端到端守**产品侧**：测试侧**故意不清** `_EMBEDDER`，仍必须是纯 BM25。

        🔑 **为什么必须单独加这条**（10-06，报告 §C1/§D4）：
        上面那条 `test_unrelated_query_not_recalled_after_upstream_loaded_engine`
        在**测试侧自己清了** `_EMBEDDER`（第 279行），所以它被测试侧清理
        **遮住**了 —— 实测在产品侧 bug 复现时它**仍然绿**（对产品侧零鉴别力）。
        本条**故意保留脏 `_EMBEDDER`**（模拟「只重置标志位」的调用方），
        于是产品侧 `rag.py::_try_load_embedder` 里那两行清空语句成为
        **唯一的防线** ⇒ 产品侧一旦回归，本条立刻红。

        这也正是 10-06 交叉实验的结论所要求的固化：两侧修法各自独立都能让
        测试变绿，所以**必须分别设防**，否则任何一侧的护栏都会被另一侧遮住。
        """
        tmp = _make_workspace()
        orig_root, orig_index = rag.WORKSPACE_ROOT, rag.INDEX_PATH
        upstream = _FakeEngine("upstream")

        rag._EMBEDDER = upstream          # ← 陈旧引擎**故意不清**
        rag._EMBED_ERROR = "上一轮的脏错误"  # ← 也故意不清
        rag._EMBEDDER_LOADED = False     # ← 只重置标志位（缺陷调用方的写法）
        rag.EMBED_MODEL_SETTING = "off"  # 本测试声明关向量

        rag.WORKSPACE_ROOT = tmp
        rag.INDEX_PATH = tmp / "rag_index.json"
        try:
            out = self._search("量子力学入门书籍推荐")
            self.assertEqual(
                upstream.calls, [],
                f"产品侧未清陈旧引擎：上游引擎被调用 {len(upstream.calls)} 次 "
                f"⇒ `rag.py::_try_load_embedder` 的清空语句回归了")
            self.assertNotIn(
                "混合检索", out,
                "输出自称混合检索 ⇒ 产品侧把陈旧引擎交给了调用方")
            self.assertNotIn(
                "菜谱", out,
                "无关查询召回了「菜谱.md」 ⇒ 声明关向量却走了向量路径")
        finally:
            rag.WORKSPACE_ROOT = orig_root
            rag.INDEX_PATH = orig_index


class _ProbeVectorOffGood(unittest.TestCase):
    """对照类（正确写法）：声明关向量 **且** 重置全部三个全局。

    存在的意义：让 `_dirty_globals_after_setup` 这条判据**自证有鉴别力**。
    若判据被改坏（例如退回文本匹配），本类会与 `_ProbeVectorOffBad` 一起
    被判为「通过」⇒ `test_probe_bad_class_is_detected` 变红。
    """

    def setUp(self) -> None:
        self._o = (rag.EMBED_MODEL_SETTING, rag._EMBEDDER,
                   rag._EMBED_ERROR, rag._EMBEDDER_LOADED)
        rag.EMBED_MODEL_SETTING = "off"
        rag._EMBEDDER = None
        rag._EMBED_ERROR = None
        rag._EMBEDDER_LOADED = False

    def tearDown(self) -> None:
        (rag.EMBED_MODEL_SETTING, rag._EMBEDDER,
         rag._EMBED_ERROR, rag._EMBEDDER_LOADED) = self._o

    def test_probe_placeholder(self) -> None:
        """占位：本类只作为探针素材，从不真正执行。"""


class _ProbeVectorOffBad(unittest.TestCase):
    """对照类（缺陷写法）：声明关向量，但**只**重置 `_EMBEDDER_LOADED`。

    这正是 `tests/test_rag.py` 在修复前的写法。护栏**必须**把它判为不隔离。
    """

    def setUp(self) -> None:
        self._o = (rag.EMBED_MODEL_SETTING, rag._EMBEDDER,
                   rag._EMBED_ERROR, rag._EMBEDDER_LOADED)
        rag.EMBED_MODEL_SETTING = "off"
        rag._EMBEDDER_LOADED = False      # ← 缺陷：没清 _EMBEDDER/_EMBED_ERROR

    def tearDown(self) -> None:
        (rag.EMBED_MODEL_SETTING, rag._EMBEDDER,
         rag._EMBED_ERROR, rag._EMBEDDER_LOADED) = self._o

    def test_probe_placeholder(self) -> None:
        """占位：本类只作为探针素材，从不真正执行。"""


class _ProbeNoVectorOff(unittest.TestCase):
    """对照类：完全不碰向量开关 ⇒ 不该被识别成「声明关向量」。"""

    def test_probe_placeholder(self) -> None:
        """占位：本类只作为探针素材，从不真正执行。"""


class TestSideSymmetryTests(_EmbedStateMixin, unittest.TestCase):
    """测试侧判据：所有声明「关向量」的测试类必须**真的**重置全部三个全局。

    🔑 **判据是行为，不是文本**（10-06 重写）。旧版用正则扫 `setUp`/`tearDown`
    的源码文本，只问「出现过这个词吗」，实测 5 种退化写法全部通过：
    自赋值 `rag._EMBEDDER = rag._EMBEDDER`、只读 `x = rag._EMBEDDER`、
    写在注释里、挪到 helper 方法、完全删除（报告 §C2/§D3）。
    ⇒ 那是一条恒绿判据，给的是虚假的安全感。

    现在改为：预置脏状态 → **真的调 `setUp()`** → 断言三个全局真被清掉。
    同时**自动发现**「声明关向量」的类（`_discover_vector_off_classes`），
    新增测试类自动纳入守护，不再依赖写死的类名清单（报告 §C5 洞 1）。
    """

    def test_declared_vector_off_tests_actually_isolate(self) -> None:
        """行为判据：跑一遍被守护类的 setUp，检查三个全局真的被重置了。"""
        import tests.test_rag as T

        names = _discover_vector_off_classes(T)
        # 下界自检（**可失败**，不再是结构性恒真）：报告 §D2 指出旧的
        # `assertGreaterEqual(len(checked), 2)` 在类被删时会先被
        # `assertIsNotNone` 拦下 ⇒ 永远走不到 ⇒ 恒真。现在没有
        # `assertIsNotNone` 兜底了，「一个都没发现」会真的红。
        self.assertTrue(
            names,
            "未发现任何「声明关向量」的测试类 —— 探测前提失效，或 "
            "tests/test_rag.py 的类被整体改名/删除（判据此时是空集恒真）")
        for name in names:
            with self.subTest(cls=name):
                still_dirty = _dirty_globals_after_setup(getattr(T, name))
                self.assertEqual(
                    still_dirty, [],
                    f"{name}.setUp 之后下列全局仍是脏值：{still_dirty} —— "
                    f"只重置部分全局会失去隔离（陈旧引擎会被缓存短路交出去）")

    def test_probe_good_class_passes_and_bad_class_is_detected(self) -> None:
        """判据自证：探针类必须「好类过、坏类红」。

        这是对判据本身的鉴别力证明——若 `_dirty_globals_after_setup` 退化成
        恒真，本条会红（在「坏类没被检出」处），而不是让整套护栏静默失效。
        """
        good = _dirty_globals_after_setup(_ProbeVectorOffGood)
        self.assertEqual(good, [], f"正确写法的对照类竟被判为不隔离：{good}")

        bad = _dirty_globals_after_setup(_ProbeVectorOffBad)
        self.assertIn(
            "_EMBEDDER", bad,
            "判据已空转：只重置 `_EMBEDDER_LOADED` 的缺陷写法竟被判为已隔离")
        self.assertIn(
            "_EMBED_ERROR", bad,
            "判据只覆盖了 `_EMBEDDER`、漏掉 `_EMBED_ERROR`（部分覆盖＝假覆盖）")
        self.assertNotIn(
            "_EMBEDDER_LOADED", bad,
            "对照类本该重置 `_EMBEDDER_LOADED`；若这条红了说明判据误报")

    def test_discovery_finds_probe_classes_by_behavior(self) -> None:
        """自动发现机制自证：认得「关向量」，也认得「不碰向量开关」。"""
        import tests.test_rag_isolation as self_mod

        found = _discover_vector_off_classes(self_mod)
        self.assertIn("_ProbeVectorOffGood", found,
                      "自动发现漏掉了正确写法的对照类")
        self.assertIn("_ProbeVectorOffBad", found,
                      "自动发现漏掉了缺陷写法的对照类")
        self.assertNotIn(
            "_ProbeNoVectorOff", found,
            "自动发现把「根本不碰向量开关」的类误判成「声明关向量」")


class HalfBakedEngineTests(_EmbedStateMixin, unittest.TestCase):
    """`rag._try_load_embedder` 加载失败时**不得**交出「半成品 engine」。

    ⚠️ 原有缺陷（10-06 由验证者实测发现，`git show HEAD:rag.py` 确认修改前
    同样存在 ⇒ **非本次修复引入**）：
    `engine = OnnxEmbedEngine(model_dir)` 已赋值、`engine.load()` 抛异常 ⇒
    `_EMBEDDER` 仍是 None，但 `return engine, error` 返回的是**已构造、未 load**
    的对象。调用方 `_build_vectors` 判 `if engine is None:` 会**误判为加载成功**
    ⇒ 对半成品调 `embed_batch` ⇒ **抛未捕获异常**。

    真实触发条件（常见用户环境）：装了 `models/` 目录，但 `onnxruntime` /
    `tokenizers` 缺失，或模型文件下载不完整。此时 `index_workspace` /
    `search_documents` 工具直接炸，而不是「降级为关键词检索」。
    """

    def test_load_failure_returns_none_not_half_built_engine(self) -> None:
        """`load()` 抛异常时，返回值必须是 `None`。"""
        saved_engine_cls = rag.OnnxEmbedEngine
        calls: list[str] = []

        class _ExplodingEngine:
            """构造成功、`load()` 抛异常 —— 精确复刻「半成品」那一刻。"""

            def __init__(self, model_dir) -> None:
                calls.append("constructed")

            def load(self) -> None:
                calls.append("load")
                raise ImportError("No module named 'onnxruntime'")

            def embed_batch(self, texts):  # pragma: no cover - 不应被调用
                calls.append("embed_batch")
                raise AssertionError("半成品引擎被调用 embed_batch ⇒ 会炸")

        try:
            rag.OnnxEmbedEngine = _ExplodingEngine
            # 让 enabled=True 且 model_dir 非 None ⇒ 必定走到加载分支
            rag.EMBED_MODEL_SETTING = str(BASE)
            rag._EMBEDDER = None
            rag._EMBED_ERROR = None
            rag._EMBEDDER_LOADED = False
            self.assertTrue(rag._embed_enabled(), "前提不成立：显式路径应启用向量")

            engine, error = rag._try_load_embedder()
        finally:
            rag.OnnxEmbedEngine = saved_engine_cls

        self.assertEqual(calls, ["constructed", "load"],
                         f"前提不成立：未走到 load() 分支，实际调用序列={calls}")
        self.assertIsNone(
            engine,
            "加载失败却把「已构造、未 load」的半成品交出去了 —— "
            "调用方 `if engine is None` 会误判为加载成功并对它调 embed_batch")
        self.assertIsNotNone(error, "加载失败必须给出降级说明")
        self.assertNotIn(
            "embed_batch", calls,
            "半成品引擎被调用了 embed_batch —— 这正是线上炸掉的路径")
        self.assertIsNone(rag._EMBEDDER,
                          "加载失败时全局 `_EMBEDDER` 必须保持 None")

    def test_build_with_failing_engine_degrades_instead_of_raising(self) -> None:
        """端到端：模型加载失败时 `build()` 必须降级，**不得抛未捕获异常**。

        这是 #4 的真实用户可见后果——工具调用直接炸掉，而不是「已降级为
        关键词检索」。守的是调用方 `_build_vectors` 的 `if engine is None` 分支。
        """
        saved_engine_cls = rag.OnnxEmbedEngine
        orig_index = rag.INDEX_PATH

        class _ExplodingEngine:
            def __init__(self, model_dir) -> None:
                pass

            def load(self) -> None:
                raise ImportError("No module named 'onnxruntime'")

            def embed_batch(self, texts):  # pragma: no cover - 不应被调用
                raise AssertionError("半成品引擎被调用 embed_batch ⇒ 会炸")

        tmp = _make_workspace()
        try:
            rag.OnnxEmbedEngine = _ExplodingEngine
            rag.INDEX_PATH = tmp / "rag_index.json"
            rag.EMBED_MODEL_SETTING = str(BASE)
            rag._EMBEDDER = None
            rag._EMBED_ERROR = None
            rag._EMBEDDER_LOADED = False

            index = rag.RagIndex(tmp)
            try:
                n_files, n_chunks = index.build()
            except Exception as exc:  #  noqa: BLE001 - 这里就是要抓未捕获异常
                self.fail(
                    f"模型加载失败时 build() 抛了未捕获异常 "
                    f"{type(exc).__name__}: {exc} —— 本应降级为关键词检索")
            self.assertEqual(n_chunks, 3, "关键词索引本应照常建成")
            self.assertIsNone(index.vectors, "向量应为空（关键词检索）")
            self.assertTrue(
                any("加载失败" in note for note in index.notes),
                f"notes 里应有加载失败说明，实际={index.notes}")
        finally:
            rag.OnnxEmbedEngine = saved_engine_cls
            rag.INDEX_PATH = orig_index


class StaleIndexVectorsNotRestoredTests(unittest.TestCase):
    """**磁盘索引**里的陈旧向量不得被恢复（与缓存三全局无关的独立通道）。

    ⚠️ **为什么需要这条**（10-06 实测，`var/_probe_load.py` 逐段可复现）：

    `RagIndex.load()` 曾只校验 `len(vectors) == len(chunks)` 就把向量恢复进内存，
    **完全不检查 `embed_model`** —— 而 `save()` 明明把它写进了 payload
    （`rag.py` 里 `embed_model` 只有那一处写、**零处读**，是个死字段）。
    ⇒ 模型 X 建的索引被模型 Y（或关向量的配置）读出来时，陈旧向量一直躺在
    `self.vectors` 里。

    **为什么当时没立刻误召回**（这点要说清，否则会误判严重性）：
    `_dense_rank` 里有 `engine, _ = _try_load_embedder(); if engine is None: return []`，
    配置为 off 时引擎必为 `None` ⇒ 走不到向量路径。**但那是检索期的第二道防线，
    不是加载期的。** 一旦换配置/换机器使引擎可用，这些与当前分块对不上号的
    陈旧向量就会参与排序。

    🔑 **本护栏不依赖 `models/` 目录、不依赖 onnxruntime**（只用 JSON 载荷），
    因此 CI 上同样有鉴别力（`models/` 不入库这件事在这里不构成影响）。
    """

    def setUp(self) -> None:
        self._orig_index = rag.INDEX_PATH
        self._orig_root = rag.WORKSPACE_ROOT
        self._orig_setting = rag.EMBED_MODEL_SETTING
        self._orig_embedder = rag._EMBEDDER
        self._orig_loaded = rag._EMBEDDER_LOADED
        self._tmp = _make_workspace()
        rag.WORKSPACE_ROOT = self._tmp
        rag.INDEX_PATH = self._tmp / "rag_index.json"

    def tearDown(self) -> None:
        rag.INDEX_PATH = self._orig_index
        rag.WORKSPACE_ROOT = self._orig_root
        rag.EMBED_MODEL_SETTING = self._orig_setting
        rag._EMBEDDER = self._orig_embedder
        rag._EMBEDDER_LOADED = self._orig_loaded

    def _write_index(self, *, embed_model: object, vectors: object) -> None:
        payload = {
            "version": 1,
            "root": str(self._tmp),
            "files": ["运动笔记.md"],
            "chunks": [{"text": "我每天早晨跑步三十分钟。",
                        "file": "运动笔记.md", "line": 1}],
            "pdf_support": False,
        }
        if embed_model is not _MISSING:
            payload["embed_model"] = embed_model
        payload["vectors"] = vectors
        rag.INDEX_PATH.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_vectors_discarded_when_switch_is_off(self) -> None:
        """① 开关已关闭 ⇒ 一律不恢复（声明关了就是关了，不能被磁盘内容推翻）。"""
        self._write_index(embed_model=str(BASE), vectors=[[0.1, 0.2]])
        rag.EMBED_MODEL_SETTING = "off"
        rag._EMBEDDER = None
        rag._EMBEDDER_LOADED = True
        index = rag.RagIndex.load(self._tmp)
        self.assertIsNotNone(index, "前提不成立：索引应能加载")
        self.assertIsNone(
            index.vectors,
            "向量开关已关闭却仍恢复了磁盘向量 ⇒ 声明「纯 BM25」被磁盘内容推翻")

    def test_vectors_discarded_when_model_mismatches(self) -> None:
        """③ 记录的 `embed_model` 与当前不一致 ⇒ 丢弃，交给重建。"""
        self._write_index(embed_model="some-other-model-X", vectors=[[0.1, 0.2]])
        rag.EMBED_MODEL_SETTING = str(BASE)      # 与写入时不同
        rag._EMBEDDER = None
        rag._EMBEDDER_LOADED = True
        index = rag.RagIndex.load(self._tmp)
        self.assertIsNotNone(index, "前提不成立：索引应能加载")
        self.assertIsNone(
            index.vectors,
            "模型不一致却复用了旧向量 ⇒ 排序结果无可解释性"
            "（向量与当前分块/模型对不上号）")

    def test_vectors_discarded_when_model_field_absent(self) -> None:
        """② 旧版本索引没记 `embed_model` ⇒ 保守丢弃。"""
        self._write_index(embed_model=_MISSING, vectors=[[0.1, 0.2]])
        rag.EMBED_MODEL_SETTING = str(BASE)
        rag._EMBEDDER = None
        rag._EMBEDDER_LOADED = True
        index = rag.RagIndex.load(self._tmp)
        self.assertIsNotNone(index, "前提不成立：索引应能加载")
        self.assertIsNone(
            index.vectors,
            "未记录模型的旧索引被复用了向量 ⇒ 无法判断向量来自哪个模型")

    def test_matching_model_still_usable(self) -> None:
        """反向：配置与记录**一致**时必须仍能复用，否则就是过度收紧。

        这是误伤方向的判据 —— 只丢不保会让向量检索永久失效。
        """
        rag.EMBED_MODEL_SETTING = str(BASE)
        self._write_index(embed_model=str(BASE), vectors=[[0.1, 0.2]])
        # 让 _embed_enabled() 为 True（显式非空路径分支，与磁盘无关）
        rag._EMBEDDER = None
        rag._EMBEDDER_LOADED = False
        index = rag.RagIndex.load(self._tmp)
        self.assertIsNotNone(index, "前提不成立：索引应能加载")
        self.assertIsNotNone(
            index.vectors,
            "模型一致的索引却丢弃了向量 ⇒ 判据过度收紧，"
            "会让「同一模型跨会话复用向量」永久失效")


if __name__ == "__main__":
    unittest.main()