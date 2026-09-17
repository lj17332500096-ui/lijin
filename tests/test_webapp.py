import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import webapp


class WebAppTests(unittest.TestCase):
    def test_routes_registered(self) -> None:
        paths = {route.path for route in webapp.app.routes}
        self.assertIn("/", paths)
        self.assertIn("/api/sessions", paths)
        self.assertIn("/api/history", paths)
        self.assertIn("/api/stream", paths)
        # 新模型：Project / Task 容器 / Message / Run
        self.assertIn("/api/projects", paths)
        self.assertIn("/api/projects/create", paths)
        self.assertIn("/api/tasks", paths)
        self.assertIn("/api/tasks/create", paths)
        self.assertIn("/api/tasks/{task_id}", paths)
        self.assertIn("/api/tasks/{task_id}/messages", paths)
        self.assertIn("/api/tasks/{task_id}/messages/create", paths)
        self.assertIn("/api/tasks/{task_id}/stream", paths)
        self.assertIn("/api/runs", paths)
        self.assertIn("/api/runs/{run_id}", paths)
        self.assertIn("/api/runs/{run_id}/events", paths)
        for action in ("pause", "resume", "cancel"):
            self.assertIn(f"/api/runs/{{run_id}}/{action}", paths)
        # legacy run-level 兼容仍在
        self.assertIn("/api/tasks/{task_id}/events", paths)
        self.assertIn("/api/tasks/{task_id}/cancel", paths)

    def test_sse_format(self) -> None:
        chunk = webapp._sse("tool", {"name": "calculate", "args": "1+1"})
        self.assertTrue(chunk.startswith("event: tool\n"))
        self.assertIn('"name": "calculate"', chunk)
        self.assertTrue(chunk.endswith("\n\n"))

    def test_friendly_error_classification(self) -> None:
        from agents.exceptions import MaxTurnsExceeded

        text = webapp._friendly_error(MaxTurnsExceeded("Max turns (10) exceeded"))
        self.assertIn("最大循环次数", text)
        self.assertNotIn("API Key", text)


if __name__ == "__main__":
    unittest.main()
