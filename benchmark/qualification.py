"""Provider Qualification（Phase 6）—— 每轮 Benchmark 前的固定 Provider 健康检查。

- 10 个最小 prompt，**不调用任何 Agent 工具**；
- 只测：request success / first-token latency / completion latency / provider error / timeout；
- 不改变 Benchmark 结果，只说明本轮测试时 Provider 状态；
- 若严重异常，本轮仍可运行，但标注 environment degraded。

用法：
    python -m benchmark.qualification [--out qualification.json] [--timeout 90]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPTS = [
    "只回答一个数字：1+1 等于几？",
    "用一句话介绍你自己。",
    "把 'hello' 翻译成中文。",
    "列出三种颜色。",
    "1 到 5 求和等于多少？",
    "用一句话解释什么是 HTTP。",
    "输出 'ok'。",
    "今天是星期几？（不确定就说不确定）",
    "用中文写一个 10 字以内的句子。",
    "回答：地球是圆的吗？",
]


async def _one(agent: object, prompt: str, timeout: float, run_config: object = None) -> dict:
    from agents import Runner
    import main as main_module

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, prompt,
                       run_config=run_config or main_module._run_config(),
                       max_turns=1),
            timeout=timeout,
        )
        latency = (time.monotonic() - started) * 1000
        text = str(getattr(result, "final_output", "") or "")
        return {"ok": True, "latency_ms": round(latency, 1),
                "tokens": bool(text), "error": None}
    except asyncio.TimeoutError:
        return {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": "timeout", "timeout": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


async def run_qualification(timeout: float = 90.0) -> dict:
    from agents import Agent
    import main as _main  # noqa: F401  确保加载 .env
    from agent import local_model_name, local_model_provider

    model = local_model_name()
    agent = Agent(name="provider-qualification", instructions="简洁回答。",
                  model=model, tools=[])
    run_config = _main._run_config(provider=local_model_provider())
    results = []
    for p in PROMPTS:
        results.append(await _one(agent, p, timeout, run_config))
    latencies = sorted(r["latency_ms"] for r in results if r.get("ok"))
    ok = sum(1 for r in results if r.get("ok"))
    timeouts = sum(1 for r in results if r.get("timeout"))
    errors = sum(1 for r in results if (not r.get("ok")) and not r.get("timeout"))

    def _pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        idx = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
        return round(vals[idx], 1)

    qid = "pq-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    degraded = (ok / len(PROMPTS) < 0.8) or (_pct(latencies, 0.95) > 60000)
    return {
        "qualification_id": qid,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": os.getenv("OPENAI_BASE_URL", "") or "openai",
        "model": model or "sdk-default",
        "success_rate": round(ok / len(PROMPTS), 3),
        "P50_latency_ms": _pct(latencies, 0.50),
        "P95_latency_ms": _pct(latencies, 0.95),
        "max_latency_ms": round(max(latencies), 1) if latencies else 0.0,
        "provider_error_count": errors,
        "timeout_count": timeouts,
        "environment_degraded": degraded,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.qualification")
    parser.add_argument("--out", default="")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    report = asyncio.run(run_qualification(args.timeout))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"qualification_id={report['qualification_id']} "
          f"success={report['success_rate']*100:.0f}% "
          f"P50={report['P50_latency_ms']}ms P95={report['P95_latency_ms']}ms "
          f"errors={report['provider_error_count']} timeouts={report['timeout_count']} "
          f"degraded={report['environment_degraded']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
