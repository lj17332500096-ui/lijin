import os
import unittest
from unittest.mock import patch

from runtime.laya_tui_startup import _server_bind_host


class LayaTuiBindTests(unittest.TestCase):
    def test_loopback_hosts_are_allowed_without_remote_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_LAYA_TUI_ALLOW_REMOTE_BIND", None)
            self.assertEqual(_server_bind_host("127.0.0.1"), "127.0.0.1")
            self.assertEqual(_server_bind_host("::1"), "::1")
            self.assertEqual(_server_bind_host("localhost"), "127.0.0.1")

    def test_non_loopback_hosts_are_rejected_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_LAYA_TUI_ALLOW_REMOTE_BIND", None)
            for hostname in ("0.0.0.0", "192.168.1.25", "agent-host.local"):
                with self.subTest(hostname=hostname):
                    with self.assertRaisesRegex(RuntimeError, "FORGE_LAYA_TUI_ALLOW_REMOTE_BIND=on"):
                        _server_bind_host(hostname)

    def test_remote_bind_requires_and_honors_explicit_opt_in(self) -> None:
        with patch.dict(os.environ, {"FORGE_LAYA_TUI_ALLOW_REMOTE_BIND": "on"}):
            self.assertEqual(_server_bind_host("192.168.1.25"), "192.168.1.25")
            self.assertEqual(_server_bind_host("0.0.0.0"), "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
