"""P1-6：能力意图门去正则化 —— 双向用例。

# 为什么要"反向用例"（P1 报告 P1-6 行明确要求）

`runtime/runner.py` 在能力盘点路径上执行 `chosen.clone(tools=[])`：
把工具**清空**。这是**移除能力**的动作，误判的代价是
"一条普通任务被清空工具" -> 模型无事可做、Run 空转、用户看不到结果。

所以本文件的核心不是"能不能认出能力问句"，而是
**"普通任务绝不能被误清空"**。P0-3 说过"断言可能因被测分支不可达而静默
失去鉴别力"，这里同理：只写正向用例的门是半个门。

# 设计纪律

`looks_like_capability_query` 现为两阶段：
  阶段 1 宽召回（`_CAPABILITY_RE`，故意过召）
  阶段 2 结构确认（`confirm_capability_shape`，偏严、只做格式校验）

**禁止**在本文件或被测模块里靠"再加几个同义句"来让某个用例通过 ——
那是 AGENTS.md 分工宪法禁止的方向（业务语义判断不得用关键字/正则）。
若某条自然语言问句判不出来，正确做法是把它交给 LLM 判定层（工作流的
analyze/select_tools 节点），而不是往正则里堆词。
"""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import capability_introspection as cap


class CapabilityGatePositiveTests(unittest.TestCase):
    """正向：明确的自我盘点问句应被识别（否则用户问能力却拿到普通执行路径）。"""

    def test_recognizes_inventory_questions(self) -> None:
        cases = [
            "你能做什么？",
            "有哪些工具？",
            "把工具列表列出来",
            "what can you do",
            "你能读取我电脑中的文档吗",
        ]
        for q in cases:
            with self.subTest(q=q):
                self.assertTrue(
                    cap.looks_like_capability_query(q),
                    f"明确的盘点问句应被识别：{q!r}",
                )


class CapabilityGateMustNotOverreachTests(unittest.TestCase):
    """反向（P1-6 验收核心）：普通任务**不得**被误判为能力盘点问句。

    这些用例的共同后果：一旦误判，runner 会 `clone(tools=[])`，
    模型将失去全部工具，任务空转。
    """

    def test_ordinary_tasks_keep_their_tools(self) -> None:
        ordinary = [
            # 带明确文件目标
            "请读取工作区里的 README.md 并总结",
            "看一下 src/main.py 这个文件讲了什么",
            # 带 URL
            "帮我查一下 https://example.com 的最新价格",
            # 带代码块
            "这段代码为什么不工作：```python\nprint(1)\n```",
            # 带引号字面量
            '把 "重要客户名单" 整理成表格',
            # 盘点 + 办事（并列子句）—— 前半句像盘点，后半句要执行
            "你能做什么，并帮我把周报写出来",
            # 纯业务任务，不含任何能力词，天然安全
            "帮我把这 3 条缺陷分类",
        ]
        for q in ordinary:
            with self.subTest(q=q):
                self.assertFalse(
                    cap.looks_like_capability_query(q),
                    f"普通任务被误判为能力盘点 -> 会被 clone(tools=[]) 清空工具：{q!r}",
                )

    def test_ordinary_task_survives_full_block_pipeline(self) -> None:
        """端到端反向：普通任务**不得**拿到能力事实块。

        `capability_context_block` 是 tools=[] 的前置条件（runner 只在拿到
        非空 context 时才清空工具）。所以这一条比只测布尔函数更接近真实后果。
        """
        for q in ("请读取工作区里的 README.md 并总结",
                  "帮我查一下 https://example.com 的价格"):
            with self.subTest(q=q):
                self.assertEqual(
                    cap.capability_context_block(q, agent=None), "",
                    "普通任务被注入了能力事实块，runner 会据此清空工具",
                )

    def test_history_query_shape_still_recognized(self) -> None:
        """历史问询走既有通道，不因本次改动被误伤。"""
        self.assertTrue(cap.looks_like_history_query("我最近成功用过哪些工具"))


class ShapeConfirmationTests(unittest.TestCase):
    """阶段 2 是**结构**判定，不做业务语义判断 —— 用例要体现这个分工。"""

    def test_operand_shapes_reject(self) -> None:
        """携带可执行目标（路径/URL/代码/字面量/清单项）-> 放行工具。"""
        for q in (
            "工具清单列一下 README.md",
            "工具 https://x.com/a",
            "工具 ```x```",
            "工具 `abc`",
            "工具有 3 个",
            "1. 工具列表",
        ):
            with self.subTest(q=q):
                self.assertFalse(
                    cap.confirm_capability_shape(q),
                    f"携带可执行目标的形态不应通过结构确认：{q!r}",
                )

    def test_too_long_or_multi_sentence_rejects(self) -> None:
        """过长/分段过多 -> 更可能是多任务指令 -> 放行工具。"""
        long_q = "工具" * 60
        self.assertFalse(cap.confirm_capability_shape(long_q))
        self.assertFalse(
            cap.confirm_capability_shape("你能做什么。还有别的吗。再说说看。")
        )

    def test_plain_short_inventory_shape_accepts(self) -> None:
        for q in ("你能做什么", "有哪些工具", "把工具列表列出来"):
            with self.subTest(q=q):
                self.assertTrue(
                    cap.confirm_capability_shape(q),
                    f"短促的盘点问句应通过结构确认：{q!r}",
                )

    def test_empty_and_blank_reject(self) -> None:
        self.assertFalse(cap.confirm_capability_shape(""))
        self.assertFalse(cap.confirm_capability_shape("   \n  "))


class NoRegexStackingTests(unittest.TestCase):
    """P1-6 的"防复发"约束：判定不得退化成"堆同义句"。

    不断言"正则绝对不许存在"（宽召回阶段合法使用正则），
    只钉住两条**可观察的回归**：
      1. 不允许第二个同义句堆叠用的意图正则复活；
      2. 宽召回正则不得随改动显著变长（那是"再加同义句"的直接征兆）。
    """

    def test_no_second_synonym_stacking_regex(self) -> None:
        for gone in ("_CAPABILITY_QUERY_SHAPE",):
            self.assertFalse(
                hasattr(cap, gone),
                f"{gone} 复活 = 意图门退回双正则堆同义句的老路",
            )

    def test_wide_recall_regex_stays_small(self) -> None:
        """宽召回正则的长度上限：超过即说明有人在往里加同义句。"""
        self.assertLess(
            len(cap._CAPABILITY_RE.pattern), 400,
            "_CAPABILITY_RE 显著变长：疑似在堆同义句（P1-6 明确禁止）",
        )

    def test_shape_confirmation_is_not_regex_of_synonyms(self) -> None:
        """结构确认只认格式，不认业务词。"""
        src = cap.confirm_capability_shape.__doc__ or ""
        self.assertIn("不判断", src, "结构确认必须自我声明不做业务语义判断")


if __name__ == "__main__":
    unittest.main()
