"""Benchmark Spec 冻结（Phase 6）：版本号 + cases/expected/evaluator 哈希。

Run A/B/C 必须使用同一 spec；任何 cases.py / expected behavior / evaluator 语义
变更都会改变哈希，从而使本轮 Benchmark 作废并需要升版本重跑。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCHMARK_SPEC_VERSION = "FORGE-AB-50-v1.3"
EVALUATOR_VERSION = "phase6-evaluator-v2"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cases_payload() -> list[dict[str, Any]]:
    from benchmark.cases import BENCHMARK_CASES

    return [{"id": c.id, "prompt": c.prompt} for c in BENCHMARK_CASES]


def _expected_payload() -> list[dict[str, Any]]:
    from benchmark.cases import BENCHMARK_CASES

    out: list[dict[str, Any]] = []
    for c in BENCHMARK_CASES:
        d = asdict(c.expected)
        # frozenset -> sorted list（稳定序列化）
        for k, v in list(d.items()):
            if isinstance(v, (set, frozenset)):
                d[k] = sorted(v)
        out.append({"id": c.id, "expected": d})
    return out


def cases_hash() -> str:
    return _sha(json.dumps(_cases_payload(), ensure_ascii=False, sort_keys=True))


def expected_behavior_hash() -> str:
    return _sha(json.dumps(_expected_payload(), ensure_ascii=False, sort_keys=True))


def evaluator_hash() -> str:
    """对 evaluator 源码做哈希（语义变更即变哈希）。"""
    p = Path(__file__).resolve().parent / "evaluator.py"
    try:
        return _sha(p.read_text(encoding="utf-8"))
    except Exception:
        return "unavailable"


def runtime_version() -> str:
    try:
        from runtime import __version__ as v  # type: ignore
        return str(v)
    except Exception:
        return "unknown"


def git_commit() -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "no-git"
    except Exception:
        return "no-git"


def config_hash() -> str:
    """对影响 Runtime 行为的关键环境变量做哈希（不含 secret 值）。"""
    keys = (
        "TOOL_ROUTER", "TOOL_BUDGET_TOTAL", "TOOL_BUDGET_WEB_SEARCH",
        "FORGE_RUN_WALL_TIMEOUT_SECONDS", "FORGE_RUN_MAX_LIFETIME_SECONDS",
        "FORGE_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "FORGE_STREAM_IDLE_TIMEOUT_SECONDS",
        "ALLOW_CODE_EXEC", "ALLOW_PROJECT_EDIT", "FORGE_TRUSTED_CODE_ROOTS",
        "FORGE_MODEL_PREF", "AGENT_MODEL",
    )
    payload = {k: os.getenv(k, "") for k in keys}
    return _sha(json.dumps(payload, sort_keys=True))


def spec_manifest() -> dict[str, Any]:
    return {
        "benchmark_spec_version": BENCHMARK_SPEC_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "cases_hash": cases_hash(),
        "expected_behavior_hash": expected_behavior_hash(),
        "evaluator_hash": evaluator_hash(),
        "runtime_version": runtime_version(),
        "git_commit": git_commit(),
        "runtime_config_hash": config_hash(),
    }


def write_run_manifest(outdir: str | Path, *, run_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    d = Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **spec_manifest(),
    }
    if extra:
        manifest.update(extra)
    (d / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
