# -*- coding: utf-8 -*-
"""模型选择（本地/远程）接线测试：后台配置决定模型，历史项目 model_pref 不得覆盖后台。"""
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
import runtime.execution as runtime_execution
from tests.workflow_model_stub import workflow_model_stub

import main as main_module
from runtime.approval import ApprovalGate
from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager


class ModelSelectHarness:
    def __init__(self, testcase, *, local_env):
        self.tmp = Path(tempfile.mkdtemp(prefix="model_sel_"))
        self.manager = TaskManager(str(self.tmp / "agent.db"))
        self.runtime = AgentRuntime(db_path=str(self.tmp / "agent.db"))
        self.runtime._initialized = True
        self.runtime._tools_patched = True
        self.runtime.tasks = self.manager
        self.runtime.approval = ApprovalGate(self.manager)
        self.runtime.broker = object()
        self.testcase = testcase
        self.captured = {}
        self._orig_exec = runtime_execution.execute_turn
        self._saved_env = {k: os.environ.get(k) for k in (
            "FORGE_LOCAL_MODEL_NAME", "FORGE_LOCAL_MODEL_BASE_URL",
            "FORGE_LOCAL_MODEL_API_KEY", "FORGE_MODEL_PREF")}
        # 测试必须从无本地模型配置的状态开始，避免读取开发者 .env 后把
        # “默认远程”场景误判为已配置本地模型。
        for k in ("FORGE_LOCAL_MODEL_NAME", "FORGE_LOCAL_MODEL_BASE_URL",
                  "FORGE_LOCAL_MODEL_API_KEY", "FORGE_MODEL_PREF"):
            os.environ.pop(k, None)
        for k, v in local_env.items():
            os.environ[k] = v

        async def fake_execute(mode, message, session=None, debug=False, max_turns=20,
                               history_limit=None, agent=None, audit=None,
                               stream_events_cb=None, **kwargs):
            self.captured["agent_model"] = getattr(agent, "model", None)
            self.captured["provider"] = kwargs.get("provider")
            return json.dumps({"kind": "answer", "summary": "s", "content": "ok",
                               "questions": [], "saved_file": None, "next_step": None},
                              ensure_ascii=False)

        runtime_execution.execute_turn = workflow_model_stub(fake_execute)
    def close(self):
        runtime_execution.execute_turn = self._orig_exec
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run(self, message, container_id=None, **kw):
        return asyncio.run(self.runtime.run_turn(
            message, session_id="s1", task_container_id=container_id, mode="sync", **kw))


class ModelSelectTests(unittest.TestCase):
    def tearDown(self):
        # 防御：确保本地变量被清掉
        for k in ("FORGE_LOCAL_MODEL_NAME", "FORGE_LOCAL_MODEL_BASE_URL",
                  "FORGE_LOCAL_MODEL_API_KEY", "FORGE_MODEL_PREF"):
            os.environ.pop(k, None)

    def _assert_gateway_provider(self, h):
        """网关路径应显式传**网关侧** Provider。

        2026-09-26 契约变更：旧实现网关路径不传 provider（由 execute_turn 内部取默认），
        代价是 TUI 里切模型/改网关配置后必须重启才生效。现在统一显式传网关 Provider
        （带 env 变化自动重建的缓存），所以这里断言"传的是网关 Provider"，
        而不是旧断言"provider 为 None"。
        """
        from runtime.provider_gateway import ResilientProvider

        gw = h.captured.get("provider")
        self.assertIsInstance(gw, ResilientProvider,
                              "网关路径应显式传网关 Provider（支持热切换）")
        self.assertNotIn("127.0.0.1", str(getattr(gw, "base_url", "") or ""),
                         "回落网关时不得传本地回环 Provider")

    def test_default_remains_gateway(self):
        h = ModelSelectHarness(self, local_env={})
        try:
            pid = h.manager.get_or_create_container("proj-default")["id"]
            res = h.run("1+1", container_id=pid)
            self.assertTrue(res.ok)
            self._assert_gateway_provider(h)  # 默认走网关 Provider
            self.assertEqual(h.captured["agent_model"], os.getenv("AGENT_MODEL"))
        finally:
            h.close()

    def test_stale_project_local_cannot_override_backend_gateway(self):
        h = ModelSelectHarness(self, local_env={
            "FORGE_LOCAL_MODEL_NAME": "Ornith-Local",
            "FORGE_LOCAL_MODEL_BASE_URL": "http://127.0.0.1:9/v1",  # 仅构造，不连接
            "FORGE_MODEL_PREF": "gateway",
        })
        try:
            pid = h.manager.get_or_create_container("proj-local")["id"]
            h.manager.update_project(pid, model_pref="local")
            self.assertEqual(h.manager.get_container(pid)["model_pref"], "local")
            res = h.run("1+1", container_id=pid)
            self.assertTrue(res.ok)
            self.assertEqual(h.captured["agent_model"], os.getenv("AGENT_MODEL"))
            self._assert_gateway_provider(h)
        finally:
            h.close()

    def test_project_local_without_config_falls_back_gateway(self):
        h = ModelSelectHarness(self, local_env={"FORGE_MODEL_PREF": "local"})  # 无本地配置
        try:
            pid = h.manager.get_or_create_container("proj-nolocal")["id"]
            h.manager.update_project(pid, model_pref="local")
            res = h.run("1+1", container_id=pid)
            self.assertTrue(res.ok)
            self._assert_gateway_provider(h)  # 未配置本地 → 回退网关 Provider
            self.assertEqual(h.captured["agent_model"], os.getenv("AGENT_MODEL"))
        finally:
            h.close()

    def test_env_default_local_applies_when_project_empty(self):
        h = ModelSelectHarness(self, local_env={
            "FORGE_LOCAL_MODEL_NAME": "Ornith-Local",
            "FORGE_LOCAL_MODEL_BASE_URL": "http://127.0.0.1:9/v1",
            "FORGE_MODEL_PREF": "local",
        })
        try:
            pid = h.manager.get_or_create_container("proj-envlocal")["id"]
            res = h.run("1+1", container_id=pid)
            self.assertTrue(res.ok)
            self.assertEqual(h.captured["agent_model"], "Ornith-Local")
            self.assertIsNotNone(h.captured["provider"])
        finally:
            h.close()

    def test_stale_project_gateway_cannot_override_backend_local(self):
        h = ModelSelectHarness(self, local_env={
            "FORGE_LOCAL_MODEL_NAME": "Ornith-Local",
            "FORGE_LOCAL_MODEL_BASE_URL": "http://127.0.0.1:9/v1",
            "FORGE_MODEL_PREF": "local",
        })
        try:
            pid = h.manager.get_or_create_container("proj-gw")["id"]
            h.manager.update_project(pid, model_pref="gateway")
            res = h.run("1+1", container_id=pid)
            self.assertTrue(res.ok)
            self.assertIsNotNone(h.captured["provider"])
            self.assertEqual(h.captured["agent_model"], "Ornith-Local")
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
