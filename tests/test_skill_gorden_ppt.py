# -*- coding: utf-8 -*-
"""gorden-ppt 技能内化冒烟：工具注册 / 模板清单 / 真实构建(strict) / 模式B授权边界。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.runctx import RunContext, bind as bind_ctx

SKILL = BASE / "skills" / "gorden-ppt"


def load_module():
    spec = importlib.util.spec_from_file_location("skill_gorden_tools", SKILL / "tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GordenPptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()
        cls.out_files: list[Path] = []

    def tearDown(self):
        bind_ctx(None)
        for p in self.out_files:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        self.out_files = []

    def _invoke(self, tool, **kw):
        import json as _json

        from agents.tool_context import ToolContext

        input_json = _json.dumps(kw, ensure_ascii=False)
        async def _inv():
            ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="g",
                              tool_arguments=input_json)
            r = tool.on_invoke_tool(ctx, input_json)
            if __import__("asyncio").iscoroutine(r):
                r = await r
            return str(r)
        return __import__("asyncio").run(_inv())

    def test_templates_list_contains_minimal(self):
        out = self._invoke(self.mod.gorden_ppt_templates)
        self.assertIn("minimal-business-summary", out)
        self.assertIn("report-savior", out)

    def test_intro_shows_slots(self):
        out = self._invoke(self.mod.gorden_ppt_template_intro,
                           template_slug="minimal-business-summary")
        self.assertIn("【结构】", out)
        self.assertIn("slot=", out)
        bad = self._invoke(self.mod.gorden_ppt_template_intro, template_slug="../../etc")
        self.assertIn("非法", bad)

    def test_build_strict_success_on_small_template(self):
        d = (SKILL / "templates" / "minimal-business-summary" / "detail.json")
        detail = json.loads(d.read_text(encoding="utf-8"))
        pages = detail.get("pages") or []
        self.assertTrue(pages)
        first = pages[0]
        slots = first.get("text_slots") or []
        self.assertTrue(slots)
        slot = slots[0]
        slide = first["slide_number"]
        edits = {"selected_slides": [slide], "edits": [
            {"slide": slide, "slot_id": slot["slot_id"],
             "new_text": "2026 年度工作报告",
             "expected_text": slot.get("expected_text") or slot.get("current_text"),
             }]}
        out = self._invoke(self.mod.gorden_ppt_build,
                           template_slug="minimal-business-summary",
                           edits_json=json.dumps(edits, ensure_ascii=False),
                           out_name="audit_smoke.pptx", strict=True)
        self.assertIn("构建成功", out)
        path = Path(out.splitlines()[0].split("：", 1)[1].strip())
        self.assertTrue(path.is_file())
        self.out_files.append(path)
        from pptx import Presentation
        prs = Presentation(str(path))
        self.assertGreaterEqual(len(list(prs.slides)), 1)

    def test_router_exposes_gorden_on_ppt_language(self):
        from runtime.tool_router import select_tool_names
        from agent import assistant_agent

        # gorden-ppt 技能工具不在基座 agent.tools 中（SKILLS 未配置时），
        # 模拟技能已启用场景，将 gorden 工具名加入 available 列表。
        base_names = [t.name for t in assistant_agent.tools]
        ppt_tools = ("gorden_ppt_templates", "gorden_ppt_build",
                     "gorden_ppt_apply_custom", "gorden_ppt_template_intro")
        available = base_names + list(ppt_tools)
        for q in ("帮我做一份季度汇报PPT", "用模板做一个演示文稿", "做一份PPT用内置模板",
                  "简约商务总结汇报", "做一份检验科基础培训的PPT",
                  "把刚才的大纲做成简约商务风格的PPT"):
            sel = select_tool_names(q, available)
            self.assertIn("gorden_ppt_templates", sel, q)
            self.assertIn("gorden_ppt_build", sel, q)

    def test_custom_template_requires_run_context(self):
        tmp = Path(tempfile.mkdtemp(prefix="gorden_"))
        fake = tmp / "my.pptx"
        fake.write_bytes(b"PK\x03\x04 not a real pptx but must exist")
        out = self._invoke(self.mod.gorden_ppt_apply_custom,
                           custom_template_path=str(fake),
                           edits_json='{"selected_slides":[1],"edits":[]}')
        self.assertIn("缺少项目文件范围上下文", out)  # 无 Run 上下文→拒绝
        from runtime.filescope import build_file_scope

        wl = tmp / "wl"
        wl.mkdir()
        base = tmp / "forge"
        (base / "notes").mkdir(parents=True)
        scope = build_file_scope(container_id="tk_a", session_id="proj-a",
                                 work_location_path=str(wl), base_dir=base,
                                 workspace_root=tmp, notes_dir=base / "notes")
        bind_ctx(RunContext(run_id="r", container_id="tk_a", session_id="proj-a",
                            channel="chat", memory_scope="project_only",
                            file_scope=scope))
        # 严格范围下自定义模板在工作区/wl 外 → 拒绝（FileScope）
        out2 = self._invoke(self.mod.gorden_ppt_apply_custom,
                            custom_template_path=str(fake),
                            edits_json='{"selected_slides":[1],"edits":[]}')
        self.assertIn("运行时文件边界", out2)


if __name__ == "__main__":
    unittest.main()
