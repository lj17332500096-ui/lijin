import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agents.tool_context import ToolContext

import tools


def call_tool(tool, **kwargs) -> str:
    """调用被 @function_tool 包装的工具对象。"""
    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="test_call",
            tool_arguments=input_json,
        )
        result = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    return asyncio.run(_invoke())


class CalculateToolTests(unittest.TestCase):
    def test_basic_arithmetic(self) -> None:
        self.assertIn("= 14", call_tool(tools.calculate, expression="2 + 3 * 4"))

    def test_functions_and_constants(self) -> None:
        self.assertIn("= 4", call_tool(tools.calculate, expression="sqrt(16)"))
        self.assertIn("= 2", call_tool(tools.calculate, expression="round(2.4)"))

    def test_code_injection_rejected(self) -> None:
        out = call_tool(tools.calculate, expression="__import__('os').system('dir')")
        self.assertNotIn("执行成功", out)
        self.assertTrue("无法计算" in out or "错误" in out or "不支持" in out)

    def test_deep_nesting_rejected(self) -> None:
        # 左结合的深层二叉表达式会触发递归深度上限
        expr = "+".join(["1"] * 200)
        out = call_tool(tools.calculate, expression=expr)
        self.assertNotIn("= 200", out)
        self.assertIn("无法计算", out)


class WorkspaceFileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._last_repeat_calls.clear()

    # 这组用例需要「工作区内的项目子目录」。**不要硬编码目录名** ——
    # 本地 WORKSPACE_ROOT 下的子目录恰为 `my_creative_agent`，而 GitHub runner
    # 上 actions/checkout 的 clone 目录是**仓库名**（本仓库 = `lijin`），
    # 该子目录会变成 `lijin`。硬编码会让这些用例在 runner 上必然失败，
    # 且报错极具误导性：「找不到目录 <tmp>/my_creative_agent」——
    # 看起来像工具坏了，实则是测试把自己的仓库名写死了。
    #
    # 实测（2026-10-05）同一份代码仅因副本目录名不同就翻转：
    #   副本目录名=my_creative_agent → 3 passed
    #   副本目录名=lijin→ 3 failed
    # **workflow 修不了这个**（.env / 建目录都救不了），只能改测试。
    #
    # 正确来源：项目根相对工作区根的**相对路径**（`_resolve_under_root` 就是
    # 按 WORKSPACE_ROOT 解析的），不是 WORKSPACE_ROOT.name ——
    # 后者是项目根的**父目录**名（本地恰好也叫 Byong-hermes，误导性极强）。
    @property
    def _root_name(self) -> str:
        rel = tools.BASE_DIR.relative_to(tools.WORKSPACE_ROOT)
        return rel.as_posix()

    def test_list_hides_env_and_venv(self) -> None:
        out = call_tool(tools.list_workspace_files, directory=self._root_name)
        self.assertIn("目录:", out)
        self.assertNotIn("\n.env", out)
        self.assertNotIn(".venv", out)

    def test_path_escape_rejected(self) -> None:
        out = call_tool(tools.read_workspace_file, path="../../Windows/win.ini")
        self.assertIn("只能读取工作区", out)

    def test_dotenv_rejected(self) -> None:
        out = call_tool(tools.read_workspace_file, path=f"{self._root_name}/.env")
        self.assertIn("不允许读取", out)

    def test_missing_file_reports_error(self) -> None:
        out = call_tool(
            tools.read_workspace_file, path=f"{self._root_name}/不存在_xyz.md")
        self.assertIn("找不到", out)

    def test_reads_text_file(self) -> None:
        out = call_tool(tools.read_workspace_file,
                        path=f"{self._root_name}/README.md", max_chars=300)
        self.assertIn("文件:", out)
        self.assertIn("全能助手", out)

    def test_repeat_list_call_reminds_model(self) -> None:
        first = call_tool(tools.list_workspace_files, directory=self._root_name)
        second = call_tool(tools.list_workspace_files, directory=self._root_name)
        self.assertIn("目录:", first)
        self.assertIn("提醒", second)
        # 不同参数不受影响
        other = call_tool(tools.list_workspace_files,
                          directory=f"{self._root_name}/tests")
        self.assertIn("目录:", other)


class LongTermMemoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="agent_mem_test_"))
        self._orig_file = tools._MEMORY_FILE
        tools._MEMORY_FILE = self._tmp / "memory.json"

    def tearDown(self) -> None:
        tools._MEMORY_FILE = self._orig_file

    def test_remember_recall_forget(self) -> None:
        out = call_tool(tools.remember, text="用户偏好简约风格的设计", tags="用户偏好")
        self.assertIn("已记住", out)

        recalled = call_tool(tools.recall_memory, keyword="简约")
        self.assertIn("简约", recalled)
        self.assertIn("mem_", recalled)

        import re

        match = re.search(r"mem_[0-9a-z]+", recalled)
        self.assertIsNotNone(match)
        deleted = call_tool(tools.forget_memory, entry_id=match.group(0))
        self.assertIn("已删除", deleted)

        recalled_after = call_tool(tools.recall_memory, keyword="简约")
        self.assertTrue(
            "没有找到" in recalled_after or "还没有任何内容" in recalled_after,
            recalled_after,
        )


class RepeatGuardRunIsolationTests(unittest.TestCase):
    """D4：重复调用护栏按 Run 隔离（审计 Phase D 验收）。

    旧实现用进程级 _last_repeat_calls，跨 Run 污染：
    一个 Run 里调用过 web_search("foo")，下一个 Run 的第一次调用会被误判为重复。
    新实现优先取 runtime.runctx.current()（RunContext），无 Run 时回退进程级。
    """

    def setUp(self) -> None:
        tools._last_repeat_calls.clear()

    def tearDown(self) -> None:
        tools._last_repeat_calls.clear()

    def test_repeat_call_isolated_per_run(self) -> None:
        from runtime.runctx import RunContext
        r1 = RunContext(run_id="run-1")
        self.assertFalse(r1.note_repeat_call("web_search", "foo"))
        self.assertTrue(r1.note_repeat_call("web_search", "foo"))
        # 新 Run 的第一次同参调用不能被旧 Run 污染（旧实现此处为 True）
        r2 = RunContext(run_id="run-2")
        self.assertFalse(r2.note_repeat_call("web_search", "foo"))
        self.assertEqual(len(r1.repeat_calls), 1)
        self.assertEqual(len(r2.repeat_calls), 1)

    def test_window_expires(self) -> None:
        from runtime.runctx import RunContext
        r = RunContext(run_id="run-3")
        self.assertFalse(r.note_repeat_call("read_workspace_file", "a.py", now=1000.0))
        self.assertTrue(r.note_repeat_call("read_workspace_file", "a.py", now=1005.0))
        self.assertFalse(r.note_repeat_call("read_workspace_file", "a.py", now=1020.0))

    def test_fallback_to_process_level_without_run(self) -> None:
        # 无 RunContext（测试/脚本直调）时走进程级回退，行为与旧实现一致
        self.assertFalse(tools._too_repetitive("web_search", "z"))
        self.assertTrue(tools._too_repetitive("web_search", "z"))
        self.assertIn("web_search", tools._last_repeat_calls)


if __name__ == "__main__":
    unittest.main()
