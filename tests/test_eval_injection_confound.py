"""修法 1 回归测试：注入块不得改变工具可用集。

为什么需要这组测试
------------------
tool_router 第 1 条规则是「查询里出现工具名 → 必选」，而 episode 注入块会以
「路径：a → b → c」的形式列出历史工具名。于是注入组的工具集与基线组系统性不同
（实测 46/46 题都不同、平均多 9 个工具），A/B 差值无法归因于记忆层。

本组测试把这个机制钉死：
- router on 时，注入确实改变工具集（哨兵，记录已知现象，防止有人以为是 bug 而"顺手修好"）；
- 固定工具集后，注入前后必须完全一致（修法 1 的核心保证）。

事实源：delivery/probe_injection_confound.py、报告 §10。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.eval_runner import _apply_fixed_tools  # noqa: E402
from runtime import tool_router as tr  # noqa: E402

ALL = [
    "get_current_datetime",
    "calculate",
    "read_workspace_file",
    "list_workspace_files",
    "search_documents",
    "index_workspace",
    "recall_memory",
    "save_note",
    "web_search",
    "run_python",
    "schedule_add",
    "ask_image",
]

PROMPT = "提醒我去交资料"

#: 结构与真实注入块一致（episode_recall.render 的「路径 / 高频工具」两行）
BLOCK = (
    "【历史经验 · 仅供参考】\n"
    "路径：list_workspace_files → read_workspace_file → recall_memory\n"
    "高频工具：schedule_add×3、ask_image×2\n"
)


class _RouterEnvMixin:
    KEYS = ("TOOL_ROUTER", "TOOL_ROUTER_LOG")

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["TOOL_ROUTER_LOG"] = "off"  # 测时关日志，避免污染 logs/

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class InjectionConfoundTests(_RouterEnvMixin, unittest.TestCase):
    """注入块对工具集的影响，以及固定工具集后的消除效果。"""

    def test_router_on_injection_changes_toolset(self) -> None:
        """哨兵：router on 时，注入块里的工具名确实会改变工具集（已知现象）。"""
        os.environ["TOOL_ROUTER"] = "on"
        plain = tr.select_tool_names(PROMPT, list(ALL))
        injected = tr.select_tool_names(BLOCK + PROMPT, list(ALL))
        self.assertNotIn("recall_memory", plain)
        self.assertIn("recall_memory", injected)
        self.assertNotEqual(set(plain), set(injected))

    def test_fixed_tools_makes_toolset_query_independent(self) -> None:
        """修法 1：固定工具集后，注入前后完全一致 —— 混淆被结构性消除。"""
        os.environ["TOOL_ROUTER"] = "off"
        plain = tr.select_tool_names(PROMPT, list(ALL))
        injected = tr.select_tool_names(BLOCK + PROMPT, list(ALL))
        self.assertEqual(set(plain), set(injected))
        self.assertEqual(set(plain), set(ALL))

    def test_injection_is_still_delivered_to_model(self) -> None:
        """固定工具集只改路由，不影响注入内容本身送达模型（不要误伤注入）。"""
        os.environ["TOOL_ROUTER"] = "off"
        # A/B 的差别应只来自 message：基线不含块、注入组含块。
        self.assertIn("历史经验", BLOCK)
        self.assertNotIn("历史经验", PROMPT)


class ApplyFixedToolsTests(_RouterEnvMixin, unittest.TestCase):
    """_apply_fixed_tools 只做一件事：按需关闭 router。"""

    def test_false_is_noop(self) -> None:
        os.environ.pop("TOOL_ROUTER", None)
        self.assertFalse(_apply_fixed_tools(False))
        self.assertIsNone(os.environ.get("TOOL_ROUTER"))
        self.assertTrue(tr.router_enabled())

    def test_true_disables_router(self) -> None:
        os.environ["TOOL_ROUTER"] = "on"
        self.assertTrue(_apply_fixed_tools(True))
        self.assertEqual(os.environ["TOOL_ROUTER"], "off")
        self.assertFalse(tr.router_enabled())

    def test_false_does_not_reenable_router(self) -> None:
        """已关闭时不因传 False 而反手打开（避免调用顺序带来的意外）。"""
        os.environ["TOOL_ROUTER"] = "off"
        _apply_fixed_tools(False)
        self.assertFalse(tr.router_enabled())


if __name__ == "__main__":
    unittest.main()
