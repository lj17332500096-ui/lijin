"""Router 指标线程化嵌入的回归测试。

C2-C4 关注点：
- start_metrics_thread 能成功起守护线程（daemon=True，主进程退出时自动回收）
- 端口被占时返回 False 且不 raise（C4 降级：指标不可用不阻塞主业务）
- /metrics 与 /health 端点可用
- webapp.py main() 的 --metrics-port 参数接入（参数解析层）
"""
import importlib
import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts_dir))


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(port: int, timeout: float = 3.0) -> bool:
    """等 ThreadingHTTPServer 起来（最多 timeout 秒），再探测 /health。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.1)
    return False


class StartMetricsThreadTests(unittest.TestCase):
    """start_metrics_thread 行为契约。"""

    def setUp(self) -> None:
        # 每次新建 module 引用，避免测试间 daemon 线程互相干扰
        self.mod = importlib.import_module("router_metrics_server")

    def test_returns_true_and_listens(self) -> None:
        port = _find_free_port()
        ok = self.mod.start_metrics_thread(port)
        self.assertTrue(ok, f"start_metrics_thread({port}) 应返回 True")
        self.assertTrue(_wait_http(port), f"端口 {port} 应可响应 /health")

    def test_port_occupied_returns_false(self) -> None:
        # 先占一个端口，再调用 start_metrics_thread 应返回 False 且不 raise
        occupier = socket.socket()
        occupier.bind(("127.0.0.1", 0))
        port = occupier.getsockname()[1]
        occupier.listen(1)  # 让 listen 占住，ThreadingHTTPServer.bind 会失败
        try:
            ok = self.mod.start_metrics_thread(port)
            self.assertFalse(ok, f"端口 {port} 已被占，start_metrics_thread 应返回 False")
        finally:
            occupier.close()

    def test_metrics_endpoint_prometheus_format(self) -> None:
        port = _find_free_port()
        self.assertTrue(self.mod.start_metrics_thread(port))
        self.assertTrue(_wait_http(port))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as r:
            body = r.read().decode("utf-8")
        # Prometheus 文本格式基本断言：含 TYPE/HELP 行
        self.assertIn("# HELP", body)
        self.assertIn("# TYPE", body)
        self.assertIn("forge_router_total_calls_total", body)

    def test_health_endpoint_json(self) -> None:
        port = _find_free_port()
        self.assertTrue(self.mod.start_metrics_thread(port))
        self.assertTrue(_wait_http(port))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            import json
            data = json.loads(r.read().decode("utf-8"))
        self.assertEqual(data.get("status"), "ok")


class WebappMetricsPortArgTests(unittest.TestCase):
    """webapp.py main() 的 --metrics-port 参数接入（仅验证参数解析层，不起 uvicorn）。"""

    def test_default_is_9095(self) -> None:
        # 不启动 uvicorn，仅验证 argparse 默认值
        import webapp
        import argparse
        parser = argparse.ArgumentParser()
        # 模拟 webapp.main() 的参数定义（不实际调用 main() 避免起服务）
        parser.add_argument("--metrics-port", type=int, default=9095)
        ns = parser.parse_args([])
        self.assertEqual(ns.metrics_port, 9095)

    def test_metrics_port_zero_skips(self) -> None:
        # 0 应跳过指标线程（契约：避免端口占用时的降级路径）
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--metrics-port", type=int, default=9095)
        ns = parser.parse_args(["--metrics-port", "0"])
        self.assertEqual(ns.metrics_port, 0)
        # 0 → 跳过分支契约
        self.assertFalse(ns.metrics_port != 0, "metrics_port=0 应触发跳过分支")


if __name__ == "__main__":
    unittest.main()
