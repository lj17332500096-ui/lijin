"""L2 前端冒烟（Playwright，?backend=mock）。

运行 node tests/frontend_smoke.js：静态服务 + mock 后端，覆盖
加载→欢迎→设置/资料→composer/模型选择器→主题切换→无 JS 错误。
零 LLM、零真实后端、确定性。若 playwright 或浏览器未安装则跳过（便于 CI 无前端依赖时仍绿）。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_frontend_smoke_mock():
    if shutil.which("node") is None:
        pytest.skip("node 不可用")
    proc = subprocess.run(
        ["node", str(Path(__file__).parent / "frontend_smoke.js")],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if "SKIP_" in proc.stdout:
        pytest.skip("playwright / chromium 未安装：" + proc.stdout.strip())
    assert proc.returncode == 0, (
        f"前端冒烟失败 rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "SMOKE_RESULT: PASS" in proc.stdout, f"缺少 PASS 标记\n{proc.stdout}"
