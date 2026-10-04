"""P3 返工项 3：能力意图门不得误伤普通任务。

# 缺陷（P3 测试工程师实测）

`tools=[]` 是**移除能力**的动作，误判代价 = 普通任务被清空工具、Run 空转。
P1-6 把判定改成"宽召回 + 结构确认"之后仍有 2 条误伤：

    '统计一下有多少个工具类'          -> tools=[]   （用户要"几个"，不是"哪些"）
    '你能做什么？顺便把 2+2 算了'     -> tools=[]   （后半句要执行，被当成纯盘点）

第二条的根因：前后两半**既无标点也无并列连词**，以"整条消息"为判定粒度
根本分不开；而 `_CAPABILITY_RE` 里的裸词 `工具` 又让"统计…工具类"过了召回。

# 修法（结构判定，不是加词）

1. 判定下沉到**子句**级：按逗号/句号切分，**任一子句不是纯盘点问句**就
   放行工具。这天然覆盖"顺便/顺手/另外/接着…"，不依赖枚举连接词 ——
   往 `_CLAUSE_JOINER_RE` 堆词正是 P1-6 刚清掉的病。
2. 新增 `_QUANTITY_ASK_RE`：数量疑问（多少/几 + 量词）是**封闭语法类**，
   答案槽位是基数而非清单，故放行工具。这是形态判定，不是同义句堆叠。

# 本文件锁住的性质

- 反向用例：含能力词但非能力问句的普通任务，**不得**被清空工具；
- 正向用例：正常盘点问句仍须被识别（修误伤不能把门关死）；
- 已知边界：`能干/做啥/干啥` 等变体**漏召回**（方向安全，不在本轮修）。
"""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import capability_introspection as cap


class OrdinaryTaskMustKeepToolsTests(unittest.TestCase):
    """**反向用例（核心）**：普通任务不得被清空工具。

    一旦误判，runner 会 `clone(tools=[])`，模型无事可做 → Run 空转。
    """

    def test_p3_reported_false_strips_are_fixed(self) -> None:
        """P3 报告的 2 条误伤。"""
        cases = [
            "统计一下有多少个工具类",
            "你能做什么？顺便把 2+2 算了",
        ]
        for q in cases:
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    f"普通任务被误判为能力盘点 -> 会被 clone(tools=[]) 清空工具：{q!r}",
                )

    def test_quantity_questions_keep_tools(self) -> None:
        """数量问句的答案槽位是基数，不是清单。"""
        for q in ("有多少个工具", "有几个插件可用", "你支持几种格式"):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    f"数量问句不应触发清空工具：{q!r}",
                )

    def test_mixed_inquiry_and_task_keeps_tools(self) -> None:
        """盘点 + 办事的混合消息（各种连接形态）都必须保留工具。"""
        for q in (
            "你能做什么？顺便把 2+2 算了",
            "有哪些工具？帮我列个清单",
            "你能做什么，帮我把周报写出来",
            "把工具列表列出来，然后再导出成 PDF",
            "你支持哪些插件，我要用其中一个",
        ):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    f"混合消息被误判为纯盘点：{q!r}",
                )

    def test_capability_words_in_ordinary_tasks_do_not_strip(self) -> None:
        """含能力关键词但并非能力问句的普通任务。"""
        for q in (
            "统计一下有多少个工具类",
            "帮我把工具类的文档整理一下",
            "这个工具能用吗"[:0] + "工具性能测试怎么做",
            "写一个工具清单管理脚本",
        ):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    f"含能力词的普通任务被误判：{q!r}",
                )

    def test_ordinary_task_survives_context_block_pipeline(self) -> None:
        """端到端：普通任务不得拿到能力事实块（tools=[] 的真实前置条件）。"""
        for q in ("统计一下有多少个工具类", "你能做什么？顺便把 2+2 算了"):
            with self.subTest(q=q):
                self.assertEqual(
                    cap.capability_context_block(q, agent=None), "",
                    "普通任务被注入能力事实块，runner 会据此清空工具",
                )

    def test_existing_operand_shapes_still_protected(self) -> None:
        """P1-6 已有的可执行目标形态不得回归。"""
        for q in (
            "工具清单列一下 README.md",
            "工具 https://x.com/a",
            "工具清单 3 个",
        ):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    f"携带可执行目标的形态被误判为纯盘点：{q!r}",
                )


class CapabilityInquiriesStillRecognisedTests(unittest.TestCase):
    """正向：正常盘点问句仍须被识别（修误伤不能把门关死）。"""

    def test_plain_inventory_questions(self) -> None:
        for q in (
            "你能做什么？",
            "有哪些工具？",
            "把工具列表列出来",
            "工具清单",
            "你支持哪些插件",
            "what can you do",
            "what tools",
        ):
            with self.subTest(q=q):
                self.assertTrue(
                    cap.looks_like_capability_query(q),
                    f"正常盘点问句被误判为普通任务：{q!r}",
                )

    def test_local_file_capability_question_preserved(self) -> None:
        """精确分支（P1-6 修过的）不得回归。"""
        self.assertTrue(
            cap.looks_like_capability_query("你可以读取我电脑中的文档吗"),
            "本地文件能力问句的精确分支失效（P1-6 回归）",
        )

    def test_multi_pure_inquiry_is_still_recognised(self) -> None:
        """两段都是盘点问句时，仍应被识别（子句级判定不误伤）。"""
        self.assertTrue(
            cap.looks_like_capability_query("你能做什么？有哪些工具？"),
            "两段皆为盘点问句却未被识别：子句级判定过严",
        )


class CapabilityRecallTests(unittest.TestCase):
    """**召回守门人**（P3 收尾新增）。

    # 为什么必须加这一类

    P3 修误伤时把接受集收得过紧，代价是 **25 条盘点语料里丢了 7 条**
    （召回 20/25 → 13/25，28% 退化）。成因：`_INQUIRY_SHAPE_RE` 只认
    "疑问词 + 中心词"的**固定语序**，于是这些全漏：
        你会什么 / 可用工具有哪些 / 把插件列出来 / 你有什么capability /
        现在支持啥功能 / 你会哪些东西 / 工具有哪些
    —— 中文疑问代词既可前置（"有什么工具"）也可后置（"工具有哪些"），
    而中心词还常被省略（"你会什么"）。

    漏召回方向是安全的（多给工具 ≠ Run 空转），但用户体验会退步：
    用户问"你会什么"不再走能力盘点，答案退回模型自行推断 ——
    正是能力自省层要消除的行为。

    教训：**收紧误伤的同时必须同时测召回**。只测"该放行的没被拦"，
    会在另一个方向静默退化 —— 我第一版 9 条自证样本恰好全含中心词，
    整类漏掉了。
    """

    def test_fresh_capability_queries_recalled(self) -> None:
        fresh = [
            "你会什么",
            "可用工具有哪些",
            "把插件列出来",
            "你有什么capability",
            "现在支持啥功能",
            "你会哪些东西",
            "工具有哪些",
        ]
        missed = [q for q in fresh if not cap.looks_like_capability_query(q)]
        self.assertEqual(
            missed, [],
            "盘点问句漏召回（用户问能力却拿不到清单）：\n  " + "\n  ".join(missed),
        )

    def test_center_word_both_orders(self) -> None:
        """中心词 + 疑问词的**两种语序**都必须成立（中文后置 / 英文前置）。"""
        for q in ("有哪些工具", "工具有哪些", "what tools", "tools available"):
            with self.subTest(q=q):
                self.assertTrue(
                    cap.looks_like_capability_query(q),
                    f"中心词与疑问词的语序未被覆盖：{q!r}",
                )

    def test_second_person_interrogative_without_center_word(self) -> None:
        """第二人称 + 疑问动词，中心词省略（"你会什么"）—— tester 建议的形态。"""
        for q in ("你会什么", "你会哪些东西", "你有什么", "你会干啥"):
            with self.subTest(q=q):
                self.assertTrue(
                    cap.looks_like_capability_query(q),
                    f"第二人称疑问形态未被覆盖：{q!r}",
                )

    def test_howto_questions_still_excluded(self) -> None:
        """实施性问句（求方法）不得被当成盘点 —— `tools=[]` 会让它答非所问。"""
        for q in (
            "MCP 怎么接入自己的服务",
            "如何安装插件",
            "怎么开发一个 MCP 服务",
            "how do I list all tools in this repo",
        ):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    f"实施性问句被当成盘点问句 -> 会被清空工具：{q!r}",
                )


class KnownRecallGapDocumentedTests(unittest.TestCase):
    """已知边界：`能干/做啥/干啥` 变体**漏召回**（方向安全，本轮不修）。

    漏召回的后果是"多给了工具"，属**安全侧**，不会造成 Run 空转；
    而修它就得往召回正则里加词 —— 那正是 P1-6 明确禁止的方向。
    正确的修法是把这类判定交给 LLM（工作流的 analyze/select_tools 节点），
    不是继续加词。故此处固化为**显式已知边界**，避免它被当成回归。
    """

    def test_synonym_variants_are_known_missed(self) -> None:
        """口语化变体仍是已知漏召回（P3 收尾实测确认未修）。

        注意与 `CapabilityRecallTests` 的分工：那里守的是"**结构**形态必须召回"
        （语序/中心词省略/英文后置），这里守的是"**词表扩展**不许悄悄进来"。
        两者都绿，才说明既没漏形态、也没退回加词路线。
        """
        for q in ("你能干", "你做啥", "你干啥"):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    "该口语变体仍是已知漏召回；若现在被识别了，请检查是否走了"
                    "往 _CAPABILITY_RE 堆词的路线（AGENTS.md 分工宪法禁止）",
                )

    def test_extreme_minimal_queries_remain_known_gap(self) -> None:
        """中心词与第二人称都缺失的极简问句：已知缺口，固化为边界。"""
        for q in ("有什么新东西", "有啥"):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    "该极简问句属已知漏召回边界；若被修复请同步更新 docstring 的边界清单",
                )

    def test_missed_recall_direction_is_safe(self) -> None:
        """漏召回 = 多给工具（安全侧），不是清空工具。"""
        from runtime.runner import AgentRuntime  # 导入即确认模块可加载

        for q in ("你能干", "你做啥"):
            with self.subTest(q=q):
                # 未被识别 -> 不注入事实块 -> runner 不会 clone(tools=[])
                self.assertEqual(cap.capability_context_block(q, agent=None), "")
        self.assertTrue(AgentRuntime)


if __name__ == "__main__":
    unittest.main()
