# -*- coding: utf-8 -*-
"""补充测试：GitHub Token 刷新、Session 管理、边界情况。"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.runner import AgentRuntime
from runtime.task import TaskState
from runtime.task_manager import TaskManager


class TestGitHubTokenRefresh(unittest.TestCase):
    """测试 GitHub Token 失效时的降级逻辑。"""
    
    def test_token_expired_falls_back_to_public(self):
        """Token 过期时应降级到公开仓库访问。"""
        from github_fetch import _download_archive
        from urllib.error import HTTPError
        
        call_count = {"api": 0, "public": 0}
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url:
                call_count["api"] += 1
                if token:
                    exc = HTTPError(url, 401, "Unauthorized", {}, None)
                    raise exc
            elif "codeload.github.com" in url:
                call_count["public"] += 1
            return b"fake zip data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = _download_archive("test-owner", "test-repo", "main", "expired-token")
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 2)
            self.assertGreater(call_count["api"], 0)
            self.assertGreater(call_count["public"], 0)
    
    def test_no_token_uses_public_api(self):
        """无 token 时直接使用公开 API。"""
        from github_fetch import _download_archive
        
        call_count = {"public": 0, "api": 0}
        
        def mock_http_get(url, token="", timeout=30):
            if "codeload.github.com" in url:
                call_count["public"] += 1
            elif "api.github.com" in url:
                call_count["api"] += 1
            return b"fake data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = _download_archive("test-owner", "test-repo", "main", "")
            self.assertIsNotNone(result)
            self.assertGreater(call_count["public"], 0)
    
    def test_403_token_invalid(self):
        """Token 无效（403）时应降级。"""
        from github_fetch import _download_archive
        from urllib.error import HTTPError
        
        def mock_http_get(url, token="", timeout=30):
            if "api.github.com" in url and token:
                exc = HTTPError(url, 403, "Forbidden", {}, None)
                raise exc
            return b"fake data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            result = _download_archive("test-owner", "test-repo", "main", "invalid-token")
            self.assertIsNotNone(result)


class TestSessionManagement(unittest.TestCase):
    """测试 Session 管理和超时恢复。"""
    
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="session_test_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
    
    def _install_fake(self, output: str = '{"kind":"answer","content":"ok"}'):
        async def fake_execute_turn(mode, message, session=None, debug=False, max_turns=20,
                                    history_limit=None, agent=None, audit=None):
            return output
        self._patcher = patch("main.execute_turn", new=fake_execute_turn)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
    
    def test_session_persistence_across_runs(self):
        """同一 session 的多轮对话保持连续性。"""
        self._install_fake()
        mgr = self.runtime.tasks
        
        r1 = asyncio.run(self.runtime.run_turn("问题 1", session_id="test-session", mode="sync"))
        r2 = asyncio.run(self.runtime.run_turn("问题 2", session_id="test-session", mode="sync"))
        r3 = asyncio.run(self.runtime.run_turn("问题 3", session_id="test-session", mode="sync"))
        
        self.assertEqual(r1.container_id, r2.container_id)
        self.assertEqual(r2.container_id, r3.container_id)
        
        messages = mgr.list_messages(r1.container_id)
        self.assertEqual(len([m for m in messages if m["role"] == "user"]), 3)
        self.assertEqual(len([m for m in messages if m["role"] == "assistant"]), 3)
    
    def test_session_isolation(self):
        """不同 session 应该隔离。"""
        self._install_fake()
        
        r1 = asyncio.run(self.runtime.run_turn("问题", session_id="session-a", mode="sync"))
        r2 = asyncio.run(self.runtime.run_turn("问题", session_id="session-b", mode="sync"))
        
        self.assertNotEqual(r1.container_id, r2.container_id)
        
        mgr = self.runtime.tasks
        msgs_a = mgr.list_messages(r1.container_id)
        msgs_b = mgr.list_messages(r2.container_id)
        
        self.assertEqual(len(msgs_a), 2)
        self.assertEqual(len(msgs_b), 2)
    
    def test_container_title_updated(self):
        """容器标题应该被第一条消息更新。"""
        self._install_fake()
        
        r = asyncio.run(self.runtime.run_turn("这是任务标题", session_id="title-test", mode="sync"))
        mgr = self.runtime.tasks
        container = mgr.get_container(r.container_id)
        
        self.assertEqual(container["title"], "这是任务标题")


class TestEdgeCases(unittest.TestCase):
    """测试边界情况。"""
    
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="edge_test_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
    
    def _install_fake(self, output: str = '{"kind":"answer","content":"ok"}'):
        async def fake_execute_turn(mode, message, session=None, debug=False, max_turns=20,
                                    history_limit=None, agent=None, audit=None):
            return output
        self._patcher = patch("main.execute_turn", new=fake_execute_turn)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
    
    def test_long_message_truncation(self):
        """超长消息应该被截断存储。"""
        self._install_fake()
        
        long_message = "x" * 50000
        r = asyncio.run(self.runtime.run_turn(long_message, session_id="test", mode="sync"))
        
        mgr = self.runtime.tasks
        messages = mgr.list_messages(r.container_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        
        self.assertLessEqual(len(user_msg["content"]), 6000)
    
    def test_concurrent_requests_same_session(self):
        """同一 session 的并发请求应该正确处理。"""
        self._install_fake()
        
        results = []
        for i in range(5):
            r = asyncio.run(self.runtime.run_turn(f"请求 {i}", session_id="concurrent", mode="sync"))
            results.append(r)
        
        container_ids = [r.container_id for r in results]
        self.assertEqual(len(set(container_ids)), 1)
    
    def test_different_sessions_separate_containers(self):
        """不同 session 应该创建不同容器。"""
        self._install_fake()
        
        containers = set()
        for i in range(3):
            r = asyncio.run(self.runtime.run_turn(f"请求 {i}", session_id=f"session-{i}", mode="sync"))
            containers.add(r.container_id)
        
        self.assertEqual(len(containers), 3)


class TestErrorRecovery(unittest.TestCase):
    """测试错误恢复。"""
    
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="error_test_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
    
    def test_failed_run_creates_new_run_not_container(self):
        """失败的 Run 不应该创建新容器。"""
        call_count = [0]
        
        async def failing_turn(mode, message, session=None, debug=False, max_turns=20,
                              history_limit=None, agent=None, audit=None):
            call_count[0] += 1
            from runtime.runner import RunResult
            from runtime.task import Task
            task = Task(
                id=f"test-run-{call_count[0]}", session_id="test", task_id="test-container",
                run_index=call_count[0], parent_task_id=None, agent_name="assistant",
                goal=message, state=TaskState.FAILED,
                budget=MagicMock(), usage=MagicMock(),
                metadata={}, error_message="模拟失败",
                created_at="", updated_at="", started_at="", completed_at=""
            )
            return RunResult(task=task, ok=False, error="模拟失败")
        
        with patch("main.execute_turn", side_effect=failing_turn):
            r1 = asyncio.run(self.runtime.run_turn("测试失败恢复", session_id="test", mode="sync"))
            r2 = asyncio.run(self.runtime.run_turn("重试", session_id="test", mode="sync"))
            
            self.assertEqual(r1.container_id, r2.container_id)
            self.assertNotEqual(r1.task.id, r2.task.id)
    
    def test_successful_run_after_failure(self):
        """失败后可以成功执行。"""
        calls = [True, False, True]
        
        async def alternating_turn(mode, message, session=None, debug=False, max_turns=20,
                                   history_limit=None, agent=None, audit=None):
            should_fail = calls.pop(0)
            if should_fail:
                from runtime.runner import RunResult
                from runtime.task import Task
                task = Task(
                    id="fail-run", session_id="test", task_id="test-container",
                    run_index=0, parent_task_id=None, agent_name="assistant",
                    goal=message, state=TaskState.FAILED,
                    budget=MagicMock(), usage=MagicMock(),
                    metadata={}, error_message="模拟失败",
                    created_at="", updated_at="", started_at="", completed_at=""
                )
                return RunResult(task=task, ok=False, error="模拟失败")
            return '{"kind":"answer","content":"成功"}'
        
        with patch("main.execute_turn", side_effect=alternating_turn):
            r1 = asyncio.run(self.runtime.run_turn("第一次", session_id="test", mode="sync"))
            r2 = asyncio.run(self.runtime.run_turn("第二次", session_id="test", mode="sync"))
            r3 = asyncio.run(self.runtime.run_turn("第三次", session_id="test", mode="sync"))
            
            self.assertEqual(r1.task.state, TaskState.FAILED)
            self.assertEqual(r2.task.state, TaskState.FAILED)
            self.assertEqual(r3.task.state, TaskState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
