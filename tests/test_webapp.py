"""webapp.py 归档态契约测试（UI 冻结期默认整模块跳过）。

背景
----
2026-09-22：webapp.py 的 63 条 /api/* 路由已归档删除，见
FORGE-API-LAYER-AUDIT-2026-09-22.md §17（前置决策：「归档」而非「复活」）。
依据是生产 bundle 中 "/api/" 命中 0 次、assistant_delta/run_id/EventSource 命中均为 0、
全仓无 HTTP 消费者、唯一测试消费者（即本文件）处于冻结跳过——它是一套前端从未
调用过的并行协议面。

所以本文件从「断言 /api/* 存在」反转为「断言 /api/* 不再注册」，并守住
llama-ui 这个唯一对外协议面的完整性。

恢复
----
恢复点：git tag archive/api-routes-pre-delete-20260922
        （`git show <tag>:webapp.py` 可取回含 63 条路由的原文件）
若将来要复活 /api/*，路由与本文件应**连同一起**从该 tag 取回，不要只取其一。

冻结期默认跳过：set FORGE_ENABLE_UI=1 后恢复运行；冻结范围见 ui_frozen.py。
"""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tests._ui_freeze import skip_if_frozen  # noqa: E402

skip_if_frozen()

import llama_bridge  # noqa: E402
import webapp  # noqa: E402


class WebAppArchiveTests(unittest.TestCase):
    """归档态：/api/* 归零，llama-ui 协议面完整。"""

    def test_no_api_routes_registered(self) -> None:
        """核心不变量：任何 /api* 路由复活都会让这条失败。"""
        paths = {route.path for route in webapp.app.routes}
        residual = sorted(p for p in paths if p.startswith("/api"))
        self.assertEqual(
            residual,
            [],
            "63 条 /api/* 已于 2026-09-22 归档删除（报告 §17）；"
            "以下路由意外复活：\n  " + "\n  ".join(residual),
        )

    def test_llama_bridge_routes_still_registered(self) -> None:
        """llama-ui 是归档后唯一对外协议，根路径版与 /llama-ui 前缀版都必须在位。

        前缀版是必需的：llama-ui 构建产物内大量 fetch 是相对路径（./props、
        ./v1/models），页面挂在 /llama-ui/ 下会被解析成 /llama-ui/props。
        """
        paths = {route.path for route in webapp.app.routes}
        for p in (
            "/v1/models", "/props", "/tools", "/v1/chat/completions",
            "/v1/chat/completions/control", "/slots", "/models/load",
            "/models/unload", "/models/sse", "/v1/streams/lookup", "/v1/stream",
        ):
            self.assertIn(p, paths)
            self.assertIn("/llama-ui" + p, paths)

    def test_page_entries_present(self) -> None:
        paths = {route.path for route in webapp.app.routes}
        for p in ("/", "/chat", "/runtime"):
            self.assertIn(p, paths)

    def test_route_count_is_pages_plus_bridge_plus_mount(self) -> None:
        """自我更新的形状闸：3 页面 + 桥路由 + 1 静态挂载。

        用 len(build_llama_ui_routes()) 推导而不是写死数字，这样桥合法增删路由
        不会产生假失败；但任何额外注册（如悄悄加回 /api/*）仍会被挡下。
        """
        expected = 3 + len(llama_bridge.build_llama_ui_routes()) + 1
        self.assertEqual(
            len(webapp.app.routes), expected,
            "webapp 路由数量偏离「页面 + llama_bridge + 静态挂载」形状",
        )


if __name__ == "__main__":
    unittest.main()
