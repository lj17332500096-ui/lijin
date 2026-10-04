# -*- coding: utf-8 -*-
"""P3 独立对抗验证：意图门（P1-6 + P3 返工）是否误伤普通任务、漏召回盘点问句。

`tools=[]` 是**移除能力**的动作，两个方向的代价**不对称**：

- **误召回**（普通任务被当成盘点问句）⇒ 工具被清空 ⇒ Run 空转、用户看不到结果。
  **代价高，必须为 0。**
- **漏召回**（盘点问句没被识别）⇒ 多给工具 ⇒ 只是不注入能力事实块。
  **方向安全**，但要作为**显式已知边界**固化下来，不能默默留着让人误以为已全覆盖。

## 语料分层（关键设计）

本文件维护**三份互不重叠**的语料，刻意避免"coder 用过的样本"污染判定：

| 语料 | 用途 | 谁写的 |
|---|---|---|
| `FRESH_ORDINARY_30` | 反向用例：含召回裸词但**不是**盘点问句的祈使/任务句 | **P3 测试工程师独立构造**（coder 的 9 条样本一条都没用） |
| `FRESH_CAPABILITY_12` | 正向用例：真盘点问句必须被识别 | 同上 |
| `KNOWN_RECALL_GAP` | **已知边界**（`能干/做啥/干啥`），用 `@expectedFailure` 固化 | 同上 |

## 与 coder 的 `test_p3_capability_no_false_strip.py` 的分工

那份测的是**它自己修的 9 条样本**（自证）；本份测的是**外部语料**。
两份都保留才有意义：只有自证 = 考卷和答案一起写，只有外部语料 = 覆盖面不足。

对应报告：`04-test/测试报告.md` §6（S5）、§11（P1-2 / P2 漏召回）、P3 复验附录。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.capability_introspection import (
    confirm_capability_shape,
    looks_like_capability_query,
)

# ============================================================================
# 语料 1：普通任务（含"工具/功能/能力/插件/MCP"裸词，但不是盘点问句）
# ============================================================================
FRESH_ORDINARY_30 = [
    # --- 裸词"工具" + 祈使任务 ---
    "帮我写个工具把 CSV 转成 JSON",
    "工具坏了怎么修",
    "把这个工具的输出改成表格",
    "工具栏那个按钮怎么加",
    "给这个工具加个日志功能",
    # --- 裸词"功能" + 祈使任务 ---
    "帮我给这个函数加点新功能",
    "功能测试怎么写",
    "把这个功能删掉",
    "功能开关在哪配",
    # --- 裸词"能力" + 祈使/疑问但要执行 ---
    "帮我评估一下这个模型的能力上限",
    "这个能力怎么提升",
    "把能力值调到 100",
    "算一下他的战斗力能力值",
    # --- 裸词"插件"/"MCP" + 任务 ---
    "帮我装一个翻译插件",
    "这个插件的配置在哪",
    "MCP 怎么接入自己的服务",
    "帮我写一个 MCP server",
    # --- 盘点问句 + 执行子句（混合）---
    "你有哪些工具？帮我用 calculate 算 99*99",
    "你能做什么？顺便把 2+2 算了",
    "现在支持哪些功能？然后读一下 README",
    "你有什么插件，接着帮我搜一下 LangGraph",
    # --- 数量疑问但要操作 ---
    "统计一下有多少个工具类",
    "数一下 tools 目录有几个文件",
    "算一下 5 个插件的平均体积",
    # --- 英文 ---
    "how do I list all tools in this repo",
    "write a script that renames 3 tool files",
    "how many capabilities does this agent have",
    # --- 其它易混 ---
    "把 capabilities.yaml 读一下",
    "帮我看看 tools.py 第 3 行",
]

# ============================================================================
# 语料 2：真盘点问句（必须被识别）
# ============================================================================
FRESH_CAPABILITY_12 = [
    "现在支持哪些功能",
    "你有什么工具",
    "有哪些 MCP",
    "把工具列表列出来",
    "what tools do you have",
    "available tools",
    "你能读取我电脑中的文档吗",
    "有哪些插件",
    "你的工具名字都有啥",
    "有哪些能力",
    "把插件列出来",
    "有哪些技能",
]

# ============================================================================
# 语料 3：已知边界（漏召回，方向安全，用 expectedFailure 固化）
# ============================================================================
KNOWN_RECALL_GAP = ["你能干什么", "你都能帮我做啥", "你能帮我做什么",
                    "你都能干啥", "能帮我做哪些事"]


class NoFalseStripTests(unittest.TestCase):
    """**P1-2 的核心判据**：普通任务绝不能被清空工具。

    这条用例曾两度红：P1-2 修完（`0c4d549`）后还剩 8 条误伤
    （`MCP 怎么接入…` / `how do I list…` / `工具坏了怎么修` / `功能开关在哪配` 等，
    全部是"疑问词 + 动名词"形态）。经 `ea32679` 补修后转绿，
    故保持**无标记正向断言**作为回归守卫。

    它是本文件最重要的一条：**语料由 P3 测试工程师独立构造，coder 的自证样本
    一条都没用**。coder 自己复盘时也承认"自证的 9 条全含中心词、恰好全在安全侧"，
    这条外部语料正是那个盲区的对冲。
    """

    def test_fresh_ordinary_tasks_not_stripped(self):
        """P3 测试工程师独立构造的 30 条外部语料，coder 的样本一条都没用。"""
        wrongly = [q for q in FRESH_ORDINARY_30 if looks_like_capability_query(q)]
        self.assertEqual(
            wrongly, [],
            f"误伤 {len(wrongly)}/{len(FRESH_ORDINARY_30)} 条普通任务 -> "
            f"tools=[] 会让 Run 空转：{wrongly}")


class CapabilityRecallTests(unittest.TestCase):
    """正向：真盘点问句必须被识别（否则不注入能力事实块）。

    这条用例在 `0c4d549` 之后一度红（收窄 `_INQUIRY_SHAPE_RE` 连带丢失
    7/25 不含中心词的盘点问句召回 —— 报告 §14.7 P2-6），
    随 coder 的补修已转绿，故保持**无标记正向断言**作为回归守卫。
    """

    def test_fresh_capability_queries_recalled(self):
        missed = [q for q in FRESH_CAPABILITY_12 if not looks_like_capability_query(q)]
        self.assertEqual(
            missed, [],
            f"漏召回 {len(missed)}/{len(FRESH_CAPABILITY_12)} 条盘点问句：{missed}")


class DefectRegressionTests(unittest.TestCase):
    """P1-2 两条原始误伤的**回归守卫**（P3 返工后已转绿，标记已摘）。

    这两条曾在 `0c4d549` 之前失败、之后通过。保留它们是为了让
    "收窄接受集"这件事有长期看守 —— 后续任何把裸词加回
    `_CAPABILITY_RE` / `_INQUIRY_SHAPE_RE` 的改动都会让它们转红。
    """

    def test_counting_tool_classes_must_keep_tools(self):
        """原缺陷 a：'统计一下有多少个工具类' 是计数任务，不是盘点。

        历史成因：`_QUANTITY_ASK_RE` 缺失，`多少个` 落在盘点形态内。
        """
        self.assertFalse(
            looks_like_capability_query("统计一下有多少个工具类"),
            "计数型任务被当成能力盘点 -> tools=[] -> Run 空转")

    def test_capability_plus_task_must_keep_tools(self):
        """原缺陷 b：盘点问句 + 执行子句（顺便/顺手）必须保留工具。

        历史成因：`_CLAUSE_JOINER_RE` 缺 `顺便`，且整条粒度分不开前后两半。
        修法是**子句级判定**（任一子句非盘点即放行），不是往连接词表堆词。
        """
        for q in ("你能做什么？顺便把 2+2 算了",
                  "你能做什么？顺手算一下 2+2"):
            with self.subTest(q=q):
                self.assertFalse(
                    looks_like_capability_query(q),
                    f"{q!r} 同时承载执行意图，不得被清空工具")


class KnownRecallGapTests(unittest.TestCase):
    """**显式已知边界**：`能干/做啥/干啥` 口语变体漏召回。

    为什么留着而不是修掉：
      - 方向安全：漏召回 = 多给工具，不会 Run 空转；与误召回代价相反。
      - 修它只能往召回正则加词 —— AGENTS.md 分工宪法禁止，P1-6 提交说明
        也自我批评过"加词补不完"。正确修法在 LLM 判定层（analyze/select_tools）。

    `@expectedFailure` 的作用：若哪天它被"修好"了，pytest 会报
    `unexpectedly passing`，提醒复核是否走了加词路线。
    """

    @unittest.expectedFailure
    def test_colloquial_variants_still_missed(self):
        """`ea32679` 补修后**部分修复**：5 条里 2 条已召回，3 条仍漏。

        当前实测：
            '你能干什么'    -> True   ✅ 已修
            '你能帮我做什么' -> True   ✅ 已修
            '你都能帮我做啥' -> False  ❌ 仍漏
            '你都能干啥'     -> False  ❌ 仍漏
            '能帮我做哪些事'  -> False  ❌ 仍漏

        探针显示残留面是"**都/都帮 + 口语疑问**"的组合：
            '你都能做啥'   -> True    '你都能干啥'   -> False
            '你能做哪些事' -> True    '能帮我做哪些事' -> False
        即 `做啥` 已被形态门覆盖，但 `干啥` 未覆盖；`都能X` 覆盖而
        `都帮我X` 未覆盖。**这是词表覆盖不全，不是形态规则缺失。**

        因此本用例保持 `@expectedFailure`：**全部 5 条都召回时**才会
        报 `unexpectedly passing`，提醒复核是否走了"往 _CAPABILITY_RE
        加词"路线（AGENTS.md 分工宪法禁止，且 P1-6 已自我批评过）。
        """
        missed = [q for q in KNOWN_RECALL_GAP if not looks_like_capability_query(q)]
        self.assertEqual(
            missed, [],
            f"这 5 条口语变体已全部召回 —— 请复核是否走了'往 _CAPABILITY_RE 加词'路线：{missed}")


class ShapeBoundaryTests(unittest.TestCase):
    """阶段 2 结构确认的边界值（testing-basics §8.1 边界值）。"""

    def test_empty_and_blank(self):
        for t in ("", "   ", "\n\t", None):
            with self.subTest(t=t):
                self.assertFalse(confirm_capability_shape(t))
                self.assertFalse(looks_like_capability_query(t))

    def test_too_long_releases_tools(self):
        self.assertFalse(confirm_capability_shape("你能做什么" + "啊" * 100))

    def test_too_many_sentences_releases_tools(self):
        q = "你能做什么？你有哪些工具？还会读文件吗？"
        self.assertFalse(confirm_capability_shape(q))

    def test_operand_shape_releases_tools(self):
        for q in ("你能打开 src/a.py 吗",
                  "你的工具 https://x.com 有哪些",
                  "你能做什么 ```code```",
                  '工具列表里的 "read_workspace_file" 是什么'):
            with self.subTest(q=q):
                self.assertFalse(confirm_capability_shape(q), q)

    def test_clause_joiner_releases_tools(self):
        for q in ("你能做什么并且帮我算 1+1",
                  "你能做什么？然后帮我算 2+2",
                  "你有什么工具？接着帮我读一下 README"):
            with self.subTest(q=q):
                self.assertFalse(confirm_capability_shape(q), q)


class NeutralizationTests(unittest.TestCase):
    """**中和实验**：把两阶段判据改坏，确认误伤/漏召回面显著变化。

    鉴别力自证：若中和后指标不变，说明该阶段是空转的。
    """

    def _measure(self):
        return ([q for q in FRESH_ORDINARY_30 if looks_like_capability_query(q)],
                [q for q in FRESH_CAPABILITY_12 if looks_like_capability_query(q)])

    def test_stage2_neutralized_widens_false_strip(self):
        """阶段 2 恒 True（退化为只看宽召回）-> 误伤面必须扩大。"""
        from runtime import capability_introspection as ci
        base_strip, _ = self._measure()
        real = ci.confirm_capability_shape
        try:
            ci.confirm_capability_shape = lambda text: True
            widened, _ = self._measure()
        finally:
            ci.confirm_capability_shape = real
        print(f"[中和] 阶段2恒True -> 误伤 {len(widened)}/{len(FRESH_ORDINARY_30)}"
              f"（当前 {len(base_strip)}）")
        self.assertGreater(len(widened), len(base_strip),
                           "阶段 2 恒 True 后误伤面没扩大 -> 阶段 2 可能空转")

    def test_narrowing_shape_gate_is_load_bearing(self):
        """中和：把**收紧后的形态门**改成永不匹配 -> 召回面必须大幅缩小。

        P3 返工后判定链是**两级且为 AND**：
            宽召回 `_CAPABILITY_RE`  ->  收紧形态 `_INQUIRY_SHAPE_RE` -> tools=[]
        形态门是**收窄接受集**的那一级（收窄方向安全：宁可多给工具）。
        所以中和它（永不匹配）会让召回**下降**、误伤**归零**。
        这条断言的意义：证明这一级真的在参与判定，不是摆设。
        """
        from runtime import capability_introspection as ci
        real = getattr(ci, "_INQUIRY_SHAPE_RE", None)
        if real is None:
            self.skipTest("当前实现无 _INQUIRY_SHAPE_RE（判定链形状变了，需重评）")
        base_recall = [q for q in FRESH_CAPABILITY_12 if looks_like_capability_query(q)]
        base_strip = [q for q in FRESH_ORDINARY_30 if looks_like_capability_query(q)]
        try:
            ci._INQUIRY_SHAPE_RE = re.compile(r"(?!x)x")   # 永不匹配
            narrowed_recall = [q for q in FRESH_CAPABILITY_12
                               if looks_like_capability_query(q)]
            narrowed_strip = [q for q in FRESH_ORDINARY_30
                              if looks_like_capability_query(q)]
        finally:
            ci._INQUIRY_SHAPE_RE = real
        print(f"[中和] 形态门永不匹配 -> 召回 {len(narrowed_recall)}/{len(FRESH_CAPABILITY_12)}"
              f"（当前 {len(base_recall)}），误伤 {len(narrowed_strip)}/{len(FRESH_ORDINARY_30)}"
              f"（当前 {len(base_strip)}）")
        self.assertLess(
            len(narrowed_recall), len(base_recall),
            "形态门永不匹配后召回没下降 -> 该级判定可能没在起作用（空转）")
        self.assertEqual(
            len(narrowed_strip), 0,
            "形态门永不匹配后误伤应归零（它把非盘点形态都放行了）；"
            f"实际仍有 {narrowed_strip} -> 说明还有别的放行通道，需重评")


if __name__ == "__main__":
    unittest.main()
