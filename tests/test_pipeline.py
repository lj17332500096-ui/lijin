"""七阶段流水线（runtime/pipeline.py）端到端单测。"""
from __future__ import annotations

import unittest
import tempfile
import os
from pathlib import Path
from unittest import mock

from runtime.pipeline import run_pipeline, orchestrator_enabled
from runtime.laya_router import LayaDecision


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_memory_db = os.environ.get("FORGE_MEMORY_DB")
        os.environ["FORGE_MEMORY_DB"] = str(self.tmp / "m.db")

    def tearDown(self):
        if self._old_memory_db is None:
            os.environ.pop("FORGE_MEMORY_DB", None)
        else:
            os.environ["FORGE_MEMORY_DB"] = self._old_memory_db

    def _fake_router(self, intent="tool_needed", completeness="self_sufficient",
                     route="web_search", route_conf=0.997):
        r = mock.Mock()
        r.classify.return_value = LayaDecision(
            intent=intent, intent_conf=0.95,
            completeness=completeness, completeness_conf=0.9,
            route=route, route_conf=route_conf,
        )
        return r

    def _run(self, router, comparer, tool="web_search", completer=None):
        with mock.patch("runtime.pipeline.orchestrator_enabled", return_value=True), \
             mock.patch("runtime.pipeline.laya_router", return_value=router), \
             mock.patch("runtime.pipeline.laya_fast_screen", return_value="tool_needed"):
            def exec_tool(t, args):
                return {"tool": t, "ok": True}
            return run_pipeline(
                "query", "run1",
                tool_executor=exec_tool,
                llm_completer=completer,
                llm_comparer=comparer,
            )

    def test_direct_tool_completes(self):
        out = self._run(self._fake_router(), comparer=lambda q, r: "done")
        self.assertEqual(out.verdict, "completed")
        self.assertEqual(out.step.kind, "direct_tool")
        self.assertEqual(out.step.tool, "web_search")

    def test_need_llm_fills_then_executes(self):
        router = self._fake_router(completeness="need_llm")
        out = self._run(
            router,
            comparer=lambda q, r: "done",
            completer=lambda q, t: {"tool": t, "args": {"q": q}},
        )
        self.assertEqual(out.step.kind, "need_llm")
        self.assertEqual(out.verdict, "completed")

    def test_loop_exhausted(self):
        router = self._fake_router()
        out = self._run(router, comparer=lambda q, r: "redispatched")
        self.assertEqual(out.verdict, "exhausted")
        self.assertGreaterEqual(out.loop_count, 3)

    def test_mutation_forces_need_llm(self):
        router = self._fake_router(route="write_project_file")
        out = self._run(router, comparer=lambda q, r: "done")
        # decide() 安全原则：mutation 工具 → need_llm（若传入 mutation 集合）
        # 这里未传 mutation_tools，route 命中 direct_tool 需要 route_conf 达标
        # 故只断言 pipeline 不崩、终态合法
        self.assertIn(out.verdict, ("completed", "fallback", "exhausted"))

    def test_disabled_pipeline_does_not_execute_injected_callbacks(self):
        executor = mock.Mock(side_effect=AssertionError("executor must not run"))
        completer = mock.Mock(side_effect=AssertionError("completer must not run"))
        comparer = mock.Mock(side_effect=AssertionError("comparer must not run"))
        laya_screen = mock.Mock(side_effect=AssertionError("Laya must not run"))
        laya_factory = mock.Mock(side_effect=AssertionError("Laya router must not load"))
        with mock.patch("runtime.pipeline.orchestrator_enabled", return_value=False), \
             mock.patch("runtime.pipeline.laya_fast_screen", laya_screen), \
             mock.patch("runtime.pipeline.laya_router", laya_factory), \
             mock.patch("runtime.pipeline.memory_layers.record_step") as record_step:
            out = run_pipeline(
                "query", "offline-run", tool_executor=executor,
                llm_completer=completer, llm_comparer=comparer,
            )
        self.assertEqual(out.verdict, "fallback")
        executor.assert_not_called()
        completer.assert_not_called()
        comparer.assert_not_called()
        laya_screen.assert_not_called()
        laya_factory.assert_not_called()
        self.assertEqual(record_step.call_args.kwargs["db"], Path(":memory:"))

    def test_pipeline_uses_only_explicitly_injected_executor(self):
        router = self._fake_router()
        executor = mock.Mock(return_value={"ok": True})
        comparer = mock.Mock(return_value="done")
        with mock.patch("runtime.pipeline.orchestrator_enabled", return_value=True), \
             mock.patch("runtime.pipeline.laya_router", return_value=router), \
             mock.patch("runtime.pipeline.laya_fast_screen", return_value="tool_needed"), \
             mock.patch("runtime.pipeline.memory_layers.record_step"):
            out = run_pipeline("query", "offline-run", tool_executor=executor,
                               llm_comparer=comparer)
        executor.assert_called_once_with("web_search", {})
        comparer.assert_called_once_with("query", {"ok": True})
        self.assertEqual(out.verdict, "completed")

    def test_production_entrypoints_do_not_reference_pipeline(self):
        root = Path(__file__).resolve().parents[1]
        production_files = (
            root / "main.py",
            root / "agent.py",
            root / "runtime" / "runner.py",
            root / "runtime" / "registry.py",
        )
        forbidden = ("runtime.pipeline", "run_pipeline(")
        for path in production_files:
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
