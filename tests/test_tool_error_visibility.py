"""护栏：工具的**业务边界错误**必须让模型看到，内部错误必须隐藏。

## 背景（10-05 CI run 37350998914 实据）

`test_rag.py::KeywordSearchTests::test_outside_workspace_rejected` 在 CI 上红：

    AssertionError: '只能索引工作区' not found in
                     'An error occurred while running the tool. Please try again.'

真因**不是代码缺陷，而是依赖版本漂移**：
- `requirements.txt` 写 `openai-agents>=0.22.0`（**开放上界**），
  CI 每次 `pip install` 装到当时最新版；
- 本地 venv 固定 0.22.0，其 `default_tool_error_function` 返回
  `... Please try again. Error: {str(error)}`（**带 detail**）；
- 新版出于安全加固（不把原始异常泄露给模型）**删掉了该后缀**。

⇒ **本地绿对新版零鉴别力**。本护栏必须**自己模拟新版**才有鉴别力。

## 三条判据（缺一不可）

① **业务错误可见**：模拟新版 SDK（无后缀默认）时，
   `ValueError("只能索引工作区...")` 的 detail 必须出现在返回串里。
② **内部错误不可见**：`EmbeddingEngineError`（含本机路径细节）不得出现。
③ **中断不可吞**：`KeyboardInterrupt` 一律判为「非业务错误」。

第 ② 条是**防过度放行**的：如果有人为了修这条红把所有异常都放行，
本护栏必须变红。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

#: 新版 SDK 的 `default_tool_error_function`（官方文档原文：刻意不暴露异常）
_NEW_SDK_DEFAULT = "An error occurred while running the tool. Please try again."


def _install_new_sdk_default() -> None:
    """把 SDK 默认错误文案替换成**新版无后缀**版本（模拟 CI）。"""
    import agents.tool as T

    T.default_tool_error_function = lambda ctx, error: _NEW_SDK_DEFAULT


def _invoke_search(directory: str) -> str:
    """直接调 `search_documents`，返回给模型看到的那个字符串。"""
    import rag
    from agents.tool_context import ToolContext

    tmp = Path(tempfile.mkdtemp(prefix="toolerr_probe_"))
    orig_root = rag.WORKSPACE_ROOT
    rag.WORKSPACE_ROOT = tmp
    try:
        tool = rag.search_documents
        ctx = ToolContext(context=None, tool_name=tool.name,
                          tool_call_id="probe", tool_arguments="{}")

        async def _run() -> str:
            r = tool.on_invoke_tool(
                ctx, json.dumps({"query": "x", "directory": directory},
                                ensure_ascii=False))
            if asyncio.iscoroutine(r):
                r = await r
            return str(r)

        return asyncio.run(_run())
    finally:
        rag.WORKSPACE_ROOT = orig_root


class UserFacingErrorClassificationTests(unittest.TestCase):
    """判据③ + 分类逻辑本身的边界（不依赖 SDK 行为，跨版本稳定）。"""

    def test_value_error_is_user_facing(self) -> None:
        from runtime.tool_errors import is_user_facing_error
        self.assertTrue(is_user_facing_error(ValueError("只能索引工作区 X 内的目录")))

    def test_embedding_engine_error_is_not_user_facing(self) -> None:
        """内部实现细节（含本机路径）**不得**给模型看。"""
        from runtime.tool_errors import is_user_facing_error

        from rag import EmbeddingEngineError
        self.assertFalse(is_user_facing_error(
            EmbeddingEngineError("未找到向量模型目录：C:/Users/secret/models")))

    def test_keyboard_interrupt_never_user_facing(self) -> None:
        """中断不是业务边界错误，绝不能被当成可展示信息吞掉。"""
        from runtime.tool_errors import is_user_facing_error
        self.assertFalse(is_user_facing_error(KeyboardInterrupt()))

    def test_arbitrary_exception_not_user_facing(self) -> None:
        from runtime.tool_errors import is_user_facing_error
        self.assertFalse(is_user_facing_error(RuntimeError("内部状态 0xdeadbeef")))

    def test_fallback_text_matches_new_sdk_default(self) -> None:
        """兜底文案必须**与 SDK 新版逐字一致**。
        不一致 ⇒ 模型会看到「我们的文案 + SDK 的文案」两份说法。"""
        from runtime.tool_errors import tool_failure_error_function
        self.assertEqual(tool_failure_error_function(None, RuntimeError("x")),
                         _NEW_SDK_DEFAULT)


class BusinessErrorVisibleUnderNewSdkTests(unittest.TestCase):
    """判据①：模拟新版 SDK 时业务错误 detail 必须可见。"""

    def setUp(self) -> None:
        _install_new_sdk_default()

    def test_boundary_error_detail_reaches_the_model(self) -> None:
        out = _invoke_search("..")
        self.assertIn(
            "只能索引工作区", out,
            "模拟新版 SDK 时业务边界错误 detail 不可见⇒ 模型无法纠正行为。"
            "（本地 0.22.0 因SDK 默认带 detail 而天然通过，对新版零鉴别力——"
            "本用例才是有鉴别力的那条。）")

    def test_error_still_signals_a_tool_failure(self) -> None:
        """放行 detail **不等于**伪装成功：仍须带 SDK 的失败前缀，
        否则 `_sdk_tool_failure_text` 会把它当正常结果记为 TOOL_EXECUTED
        （runtime/runner.py 的 Completion Gate 证据）。"""
        out = _invoke_search("..")
        self.assertTrue(
            out.startswith(_NEW_SDK_DEFAULT),
            f"应以 SDK 失败前缀开头（保持 TOOL_EXECUTED 判定不变），实际：{out[:80]!r}")


class InternalErrorStaysHiddenTests(unittest.TestCase):
    """判据②：内部错误**不得**因修① 而被顺手放行（防过度放行）。"""

    def setUp(self) -> None:
        _install_new_sdk_default()

    def test_internal_error_text_not_leaked(self) -> None:
        from runtime.tool_errors import tool_failure_error_function

        class _FakeEmbeddingError(Exception):
            pass

        _FakeEmbeddingError.__name__ = "EmbeddingEngineError"
        secret = "C:/Users/someone/private-models/bge"
        out = tool_failure_error_function(None, _FakeEmbeddingError(secret))
        self.assertNotIn(secret, out)
        self.assertNotIn("private-models", out)
        self.assertEqual(out, _NEW_SDK_DEFAULT)

    def test_only_value_error_family_passes(self) -> None:
        """白名单是**枚举**而非前缀匹配 —— 否则 `ValueErrorFoo` 之类会漏进来。"""
        from runtime.tool_errors import is_user_facing_error

        class ValueErrorLike(Exception):
            pass

        ValueErrorLike.__name__ = "ValueErrorLike"  # 名字相近但不同类
        self.assertFalse(is_user_facing_error(ValueErrorLike("x")))


class SelfCheckTests(unittest.TestCase):
    """反空转：护栏自身失效时要能发现。"""

    def test_probe_can_see_detail_at_all(self) -> None:
        """**前提自检**：装上旧版 SDK 默认时 detail 必然可见。
        若这条红了，说明 `_invoke_search` 或路径判定坏了 ⇒ 整组判据无鉴别力。"""
        import agents.tool as T

        T.default_tool_error_function = (
            lambda ctx, e: f"{_NEW_SDK_DEFAULT} Error: {str(e)}")
        try:
            out = _invoke_search("..")
        finally:
            _install_new_sdk_default()
        self.assertIn("只能索引工作区", out,
                      "探针连旧版默认都看不到 detail ⇒ 后面几条测的不是被测行为")

    def test_directory_parent_is_actually_outside_root(self) -> None:
        """`directory=".."` 必须**真的**越界，否则判据是空转。"""
        import rag
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = (root / ".." ).resolve()
            self.assertNotEqual(outside, root.resolve())
            with self.assertRaises(ValueError):
                outside.relative_to(root.resolve())


if __name__ == "__main__":
    unittest.main()