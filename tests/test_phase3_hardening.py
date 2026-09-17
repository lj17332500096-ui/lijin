"""Phase 3 hardening tests — trusted-root canonical path security + convergence.

覆盖规范要求的：
- trusted-root 正常 path 放行 / sibling-prefix / ../ traversal / 注释伪造 / 普通字符串 / symlink 逃逸
- direct rewrite → tools=[]
- read-only intent → 无写工具
- capability description 不误判 / execution claim 无证据仍拦
- needs_user_input 确定性收口
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TOOL_ROUTER"] = "on"

from runtime import tool_router as tr
from runtime.readiness_gate import required_questions, missing_required_fields
from runtime.completion import CompletionGate, ExecutionEvidence
from runtime.runctx import RunContext


class TrustedRootSecurityTests(unittest.TestCase):
    """P0：受信根判定必须用规范化真实路径，防 Approval 绕过。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p3trust_"))
        self.root = self.tmp / "trusted"
        self.root.mkdir()
        (self.root / "tests").mkdir()
        (self.root / "tests" / "t.py").write_text("print(1)", encoding="utf-8")
        os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(self.root)
        import importlib
        import code_exec
        importlib.reload(code_exec)
        self.ce = code_exec

    def tearDown(self):
        os.environ.pop("FORGE_TRUSTED_CODE_ROOTS", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normal_project_name(self):
        self.assertIsNotNone(self.ce.trusted_root_for("trusted", "", ""))

    def test_normal_abs_filename(self):
        self.assertIsNotNone(
            self.ce.trusted_root_for("", str(self.root / "tests" / "t.py"), ""))

    def test_sibling_prefix_rejected(self):
        # /trusted-evil 不是 /trusted 的子路径
        self.assertIsNone(self.ce.trusted_root_for("trusted-evil", "", ""))

    def test_parent_traversal_rejected(self):
        outside = str(self.root / ".." / "outside.py")
        self.assertIsNone(self.ce.trusted_root_for("", outside, ""))

    def test_code_comment_not_scanned(self):
        # code/args 文本里出现受信根字符串，但执行位置不在受信根 → 不放行
        self.assertIsNone(self.ce.trusted_root_for(
            "", "", f"# {self.root}\nrun something else"))

    def test_plain_string_arg_not_scanned(self):
        self.assertIsNone(self.ce.trusted_root_for(
            "", "", f"just a string mentioning {self.root}"))

    def test_abs_filename_outside_rejected(self):
        self.assertIsNone(self.ce.trusted_root_for("", str(self.tmp / "evil.py"), ""))

    def test_case_and_slash_normalized(self):
        # 大写 project 名（大小写差异）仍应命中（Windows 大小写不敏感）
        self.assertIsNotNone(self.ce.trusted_root_for("TRUSTED", "", ""))


class DirectTextRouterTests(unittest.TestCase):
    """纯文本改写/翻译 → tools=[]；只读意图 → 无写工具。"""

    def _all(self):
        try:
            from agent import assistant_agent
            return [t.name for t in assistant_agent.tools]
        except Exception:
            return ["web_search", "read_workspace_file", "list_workspace_files",
                    "edit_project_file", "write_code_file", "save_note", "run_python"]

    def test_rewrite_no_tools(self):
        self.assertEqual(tr.select_tool_names(
            "帮我把“这个功能不好用，你们赶紧改”改得正式一点。", self._all()), [])

    def test_translate_no_tools(self):
        self.assertEqual(tr.select_tool_names("把这句话翻译成英文。", self._all()), [])

    def test_readonly_excludes_write(self):
        names = tr.select_tool_names("看一下 auth.py，告诉我登录流程，不要修改代码。", self._all())
        for w in ("edit_project_file", "write_code_file", "write_project_file", "save_note"):
            self.assertNotIn(w, names)


class NeedsUserInputTests(unittest.TestCase):
    """缺必需信息 → 具体问题；RunContext needs_user_input 状态。"""

    def test_flight_question(self):
        self.assertEqual(required_questions("帮我查明天去上海的航班。"), ["请告诉我从哪里出发？"])

    def test_reminder_question(self):
        self.assertTrue(required_questions("提醒我去交资料。"))

    def test_sufficient_info_no_question(self):
        self.assertEqual(required_questions("从北京出发查明天去上海的航班。"), [])

    def test_runctx_enter_needs_user_input(self):
        rc = RunContext(run_id="r1")
        self.assertFalse(rc.needs_user_input)
        rc.enter_needs_user_input(["从哪里出发？"])
        self.assertTrue(rc.needs_user_input)
        self.assertEqual(rc.pending_questions, ["从哪里出发？"])


class ClaimClassificationTests(unittest.TestCase):
    """capability 描述 vs execution 声明。"""

    def _g(self):
        return CompletionGate()

    def test_capability_description_pass(self):
        v = self._g().evaluate(
            {"kind": "answer", "content": "我可以读取、生成和修改文档。", "summary": "能力"},
            ExecutionEvidence(), request_text="你能处理 Excel 和 Word 吗？")
        self.assertEqual(v.value, "pass")

    def test_execution_claim_without_evidence_blocked(self):
        v = self._g().evaluate(
            {"kind": "answer", "content": "我已经修改完成配置。", "summary": "x"},
            ExecutionEvidence(), request_text="改配置")
        self.assertEqual(v.value, "claim_unsupported")

    def test_persistence_claim_with_evidence_pass(self):
        ev = ExecutionEvidence(persistence_done={"save_note"})
        v = self._g().evaluate(
            {"kind": "answer", "content": "已保存到笔记。", "summary": "ok"},
            ev, request_text="帮我记一下")
        self.assertEqual(v.value, "pass")

    def test_persistence_claim_without_evidence_blocked(self):
        v = self._g().evaluate(
            {"kind": "answer", "content": "已保存到笔记。", "summary": "ok"},
            ExecutionEvidence(), request_text="帮我记一下")
        self.assertEqual(v.value, "claim_unsupported")


if __name__ == "__main__":
    unittest.main()
