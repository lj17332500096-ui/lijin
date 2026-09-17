"""L1 契约测试：mock 与 client 对同一接口返回相同的契约字段集。

用 node 运行 tests/contract_probe.js（加载 types/mock/client，stub 掉 RT.http），
断言两 API 适配器对 task/run/approval/artifact/tool 暴露的前端依赖字段集一致，
且含 title、stat.latest_run.state 等关键字段。零 LLM 成本、零网络。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_mock_client_contract_fields():
    if shutil.which("node") is None:
        pytest.skip("node 不可用")
    proc = subprocess.run(
        ["node", str(Path(__file__).parent / "contract_probe.js")],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"契约探针失败 rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "CONTRACT_RESULT: PASS" in proc.stdout, f"输出中缺少 PASS 标记\n{proc.stdout}"
