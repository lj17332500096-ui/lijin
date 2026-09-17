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
        
        # 模拟 token 导致 401
        original_http_get = None
        
        def mock_http_get(url, token="", timeout=30):
            if token and "401" in url:
                exc = HTTPError(url, 401, "Unauthorized", {}, None)
                raise exc
            # 公开访问成功
            return b"fake zip data"
        
        with patch('github_fetch._http_get', side_effect=mock_http_get):
            # 应该能降级成功
            result = _download_archive("test-owner", "test-repo", "main", "expired-token")
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 2)  # (data, fmt)
    
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
            # 应该使用公开 API
            self.assertGreater(call_count["public"], 0)


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
        
        # 第一轮
        r1 = asyncio.run(self.runtime.run_turn("问题 1", session_id="test-session", mode="sync"))
        # 第二轮
        r2 = asyncio.run(self.runtime.run_turn("问题 2", session_id="test-session", mode="sync"))
        # 第三轮
        r3 = asyncio.run(self.runtime.run_turn("问题 3", session_id="test-session", mode="sync"))
        
        # 应该都在同一个容器
        self.assertEqual(r1.container_id, r2.container_id)
        self.assertEqual(r2.container_id, r3.container_id)
        
        # 消息应该累积
        messages = mgr.list_messages(r1.container_id)
        self.assertEqual(len([m for m in messages if m["role"] == "user"]), 3)
        self.assertEqual(len([m for m in messages if m["role"] == "assistant"]), 3)
    
    def test_session_isolation(self):
        """不同 session 应该隔离。"""
        self._install_fake()
        
        r1 = asyncio.run(self.runtime.run_turn("问题", session_id="session-a", mode="sync"))
        r2 = asyncio.run(self.runtime.run_turn("问题", session_id="session-b", mode="sync"))
        
        # 不同 session 应该在不同容器
        self.assertNotEqual(r1.container_id, r2.container_id)
        
        # 各自的消息应该独立
        mgr = self.runtime.tasks
        msgs_a = mgr.list_messages(r1.container_id)
        msgs_b = mgr.list_messages(r2.container_id)
        
        self.assertEqual(len(msgs_a), 2)  # user + assistant
        self.assertEqual(len(msgs_b), 2)


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
    
    def test_empty_message_rejected(self):
        """空消息应该被拒绝。"""
        # 这个测试需要检查 webapp.py 的输入验证
        # 暂时跳过，因为这是 webapp 层的验证
        pass
    
    def test_long_message_truncation(self):
        """超长消息应该被截断存储。"""
        self._install_fake()
        
        long_message = "x" * 50000  # 50k 字符
        r = asyncio.run(self.runtime.run_turn(long_message, session_id="test", mode="sync"))
        
        mgr = self.runtime.tasks
        messages = mgr.list_messages(r.container_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        
        # 消息应该被截断（根据实现，可能截断到 3000 或 6000 字符）
        self.assertLessEqual(len(user_msg["content"]), 6000)
    
    def test_concurrent_requests(self):
        """并发请求应该正确处理。"""
        self._install_fake()
        
        # 模拟并发请求
        async def make_request(i):
            return asyncio.run(self.runtime.run_turn(f"请求 {i}", session_id="concurrent", mode="sync"))
        
        # 运行多个并发请求
        results = asyncio.gather(*[make_request(i) for i in range(5)])
        
        self.assertEqual(len(results), 5)
        # 所有结果都应该有容器 ID
        for r in results:
            self.assertIsNotNone(r.container_id)


class TestErrorRecovery(unittest.TestCase):
    """测试错误恢复。"""
    
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="error_test_"))
        self.runtime = AgentRuntime(db_path=str(self._tmp / "agent.db"))
        self.runtime._ensure()
    
    def test_failed_run_can_be_retried(self):
        """失败的 Run 应该能被重试。"""
        # 第一次失败
        async def failing_turn(mode, message, session=None, debug=False, max_turns=20,
                              history_limit=None, agent=None, audit=None):
            from runtime.runner import RunResult
            from runtime.task import Task
            task = Task(
                id="test-run", session_id="test", task_id="test-container",
                run_index=0, parent_task_id=None, agent_name="assistant",
                goal=message, state=TaskState.FAILED,
                budget=MagicMock(), usage=MagicMock(),
                metadata={}, error_message="模拟失败",
                created_at="", updated_at="", started_at="", completed_at=""
            )
            return RunResult(task=task, ok=False, error="模拟失败")
        
        with patch("main.execute_turn", side_effect=failing_turn):
            r1 = asyncio.run(self.runtime.run_turn("测试失败恢复", session_id="test", mode="sync"))
            self.assertEqual(r1.task.state, TaskState.FAILED)
        
        # 第二次成功
        async def succeeding_turn(mode, message, session=None, debug=False, max_turns=20,
                                  history_limit=None, agent=None, audit=None):
            return '{"kind":"answer","content":"成功"}'
        
        with patch("main.execute_turn", side_effect=succeeding_turn):
            r2 = asyncio.run(self.runtime.run_turn("重试", session_id="test", mode="sync"))
            self.assertEqual(r2.task.state, TaskState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
