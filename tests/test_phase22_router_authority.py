"""Phase 22：Router Authority + Final Exposure Parity 回归。

- MCP domain approval（gitee read/list vs mutation；provider identity 不得 github 替换 gitee）
- capability metadata 不得 unknown external → MUTATION
- Final Exposure Parity：sync/stream/async 必须把同一个 routed agent 交给 Runner
"""

import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime import tool_router as tr  # noqa: E402
from runtime.spec import capability_of  # noqa: E402


def _catalog(entries):
    cat = {}
    for name, source, server, cap in entries:
        cat[name] = {"name": name, "source": source, "server": server,
                     "domain": tr._MCP_SERVER_DOMAIN.get(server, ""),
                     "capability": cap, "side_effect": cap == "MUTATION",
                     "risk": "medium", "description": ""}
    return cat


GITEE = [
    ("gitee_list_user_repos", "MCP", "gitee", "DISCOVERY"),
    ("gitee_get_user_info", "MCP", "gitee", "READ"),
    ("gitee_list_repo_issues", "MCP", "gitee", "DISCOVERY"),
    ("gitee_create_repo", "MCP", "gitee", "MUTATION"),
    ("gitee_update_issue", "MCP", "gitee", "MUTATION"),
]


class McpCapabilityMetadataTests(unittest.TestCase):
    def test_mcp_list_is_discovery_not_mutation(self):
        self.assertEqual(capability_of("gitee_list_user_repos"), "DISCOVERY")
        self.assertEqual(capability_of("gitee_get_user_info"), "READ")
        self.assertEqual(capability_of("playwright_browser_snapshot"), "DISCOVERY")
        self.assertEqual(capability_of("youtube_get-transcript"), "READ")

    def test_mcp_mutation_semantics(self):
        self.assertEqual(capability_of("gitee_create_repo"), "MUTATION")
        self.assertEqual(capability_of("gitee_update_issue"), "MUTATION")
        self.assertEqual(capability_of("playwright_browser_click"), "MUTATION")


class McpDomainApprovalTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("TOOL_ROUTER")
        os.environ["TOOL_ROUTER"] = "on"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TOOL_ROUTER", None)
        else:
            os.environ["TOOL_ROUTER"] = self._saved

    def test_gitee_read_exposes_read_tools_not_mutation(self):
        cat = _catalog(GITEE)
        names = tr.select_tool_names("列出我的 gitee 仓库", [n for n, *_ in GITEE],
                                     external=[n for n, *_ in GITEE], catalog=cat)
        self.assertIn("gitee_list_user_repos", names)
        self.assertNotIn("gitee_create_repo", names)
        self.assertNotIn("gitee_update_issue", names)

    def test_gitee_mutation_intent_exposes_mutation_tool(self):
        cat = _catalog(GITEE)
        names = tr.select_tool_names("帮我创建一个 gitee 仓库", [n for n, *_ in GITEE],
                                     external=[n for n, *_ in GITEE], catalog=cat)
        self.assertIn("gitee_create_repo", names)

    def test_gitee_does_not_substitute_github(self):
        cat = _catalog(GITEE)
        available = [n for n, *_ in GITEE] + ["fetch_github_repo"]
        names = tr.select_tool_names("列出我的 gitee 仓库", available,
                                     external=[n for n, *_ in GITEE], catalog=cat)
        self.assertNotIn("fetch_github_repo", names)

    def test_ordinary_coding_exposes_no_mcp(self):
        cat = _catalog(GITEE)
        available = [n for n, *_ in GITEE] + [
            "read_workspace_file", "list_workspace_files", "edit_project_file",
            "write_code_file", "run_python", "run_tests", "read_code_file",
        ]
        names = tr.select_tool_names("修复 calc.py 的 add 函数并运行测试验证",
                                     available, external=[n for n, *_ in GITEE],
                                     catalog=cat)
        self.assertFalse([n for n in names if n.startswith("gitee_")], names)
        self.assertLessEqual(len(names), tr.DEFAULT_MAX_TOOLS)

    def test_web_and_memory_controls(self):
        names = tr.select_tool_names("北京今天天气怎么样", ["web_search", "get_current_datetime"])
        self.assertIn("web_search", names)
        names2 = tr.select_tool_names("记住我的邮箱是 a@b.com", ["remember", "save_note"])
        self.assertIn("remember", names2)


class _DummyResult:
    final_output = "ok"
    new_items = []


class _DummyStreamed:
    def stream_events(self):
        async def _gen():
            if False:
                yield None
        return _gen()


class ExposureParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_stream_async_use_same_active_agent(self):
        import main as main_module

        class _FakeRunner:
            def __init__(self):
                self.seen = []

            async def run(self, agent, message, **kw):
                self.seen.append(("async", agent))
                return _DummyResult()

            def run_sync(self, agent, message, **kw):
                self.seen.append(("sync", agent))
                return _DummyResult()

            def run_streamed(self, agent, message, **kw):
                self.seen.append(("stream", agent))
                return _DummyStreamed()

        fake = _FakeRunner()
        sentinel_agent = object()
        real = main_module.Runner
        main_module.Runner = fake  # type: ignore[assignment]
        try:
            for mode in ("sync", "async", "stream"):
                await main_module._run_attempt(mode, "x", None, None, 1,
                                               agent=sentinel_agent)
        finally:
            main_module.Runner = real  # type: ignore[assignment]
        modes = [m for m, _ in fake.seen]
        self.assertEqual(sorted(modes), ["async", "stream", "sync"])
        self.assertTrue(all(a is sentinel_agent for _, a in fake.seen),
                        "某 mode 未把 router 选择的 active_agent 交给 Runner")


if __name__ == "__main__":
    unittest.main()
