"""Phase 19：Decision Qualification Harness（checkpoint-based decision probe）。

不跑完整 coding run：重建“mutation 已完成、verification_due=true”的真实 checkpoint
（真实 RunContext / Tool Router / tool definitions / system prompt / 真实 mutation 工具结果 /
真实 obligation feedback），然后让**真实本地模型**做一次真实 decision turn，只观察第一项动作。

不是 Mock：使用真实 agent（route_agent）、真实工具、真实模型、真实 obligation 文本。
Decision qualification ≠ E2E qualification。

用法：
    python -m benchmark.decision_checkpoint --n 10 --out phase19
强制本地模型。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIX = "F:/Byong-hermes/Byong-hermes/my_creative_agent/micro_fixture"

CASES = {
    "M1": f"修复 {FIX}/calc.py 中 add 函数结果错误的问题（应返回两数之和），"
          f"并运行 {FIX} 下的测试确认通过。",
    "M4": f"修复 {FIX}/calc.py 的 add 函数，然后运行测试验证修改是否正确。",
}

MUTATION_TOOLS = {"write_project_file", "edit_project_file", "write_code_file",
                  "save_note", "save_word_doc", "save_excel_workbook", "save_ppt_deck"}
VERIFICATION_TOOLS = {"run_tests", "run_python", "code_loop", "code_loop_tool"}
PREP_TOOLS = {"read_workspace_file", "list_workspace_files", "search_documents",
              "read_code_file", "list_code_files"}


def _capability(name: str) -> str:
    from runtime.spec import capability_of
    return capability_of(name)


def _obligation_feedback(revision: int) -> str:
    return (
        "\n\n【执行义务 / Execution obligation】\n"
        "verification = REQUIRED\n"
        "status = UNSATISFIED\n"
        f"revision = {revision}\n"
        "在最终完成（final answer）之前，必须为本 revision 获得真实 verification 证据。"
        "请自行选择合适的验证工具（Runtime 不指定具体命令）。"
    )


async def _real_mutation() -> tuple[str, str, str]:
    """真实执行一次 mutation（write_code_file 修复 calc.py），返回 (tool, args, result)。"""
    from agents.tool_context import ToolContext

    import code_exec

    content = ("def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n")
    args = {"project": "micro_fixture", "filename": "calc.py", "content": content}
    args_json = json.dumps(args, ensure_ascii=False)
    ctx = ToolContext(context=None, tool_name="write_code_file",
                      tool_call_id="ck-mut", tool_arguments=args_json)
    res = code_exec.write_code_file.on_invoke_tool(ctx, args_json)
    if asyncio.iscoroutine(res):
        res = await res
    return "write_code_file", args_json, str(res)


async def _probe_case(case_id: str) -> dict:
    from agents import Runner, RunHooks
    import main as main_module
    from agent import local_model_provider
    from runtime.runner import AgentRuntime
    from runtime.runctx import RunContext, bind

    rt = AgentRuntime.get_default()
    rt._ensure()
    prompt = CASES[case_id]
    agent = rt.route_agent(prompt, channel="chat")

    # 真实 mutation（同时把 revision 记为 1）
    tool_name, tool_args, mutation_result = await _real_mutation()
    rc = RunContext(run_id=f"ck-{case_id}", request_text=prompt)
    rc.note_progress(tool_name, json.loads(tool_args), mutation_result)
    rc.note_execution_identity(tool_name, json.loads(tool_args))
    rev = rc._t().evidence_epoch
    due = rc.verification_due()
    checkpoint = [
        {"role": "user", "content": prompt},
        {"type": "function_call", "name": tool_name, "arguments": tool_args, "call_id": "ck-mut"},
        {"type": "function_call_output", "call_id": "ck-mut",
         "output": mutation_result + _obligation_feedback(rev)},
    ]
    bind(rc)  # 真实 RunContext 绑定（工具 wrapper 若触发也能读到）
    class _FirstToolHooks(RunHooks):
        def __init__(self) -> None:
            self.first: str | None = None

        async def on_tool_start(self, context, ag, tool) -> None:  # type: ignore[override]
            if self.first is None:
                self.first = str(getattr(tool, "name", "") or "")

    hooks = _FirstToolHooks()
    error = None
    t = time.monotonic()
    try:
        await Runner.run(agent, input=checkpoint,
                         run_config=main_module._run_config(
                             provider=local_model_provider()),
                         hooks=hooks, max_turns=1)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
    first = hooks.first
    latency = round(time.monotonic() - t, 1)
    return {"case": case_id, "verification_due": due, "revision": rev,
            "first_tool": first, "capability": _capability(first) if first else None,
            "latency_s": latency, "error": error,
            "mutation_result_head": mutation_result[:80]}


async def run(n: int, outdir: Path) -> dict:
    import main as main_module  # noqa: F401
    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref != "local":
        raise SystemExit(f"FORGE_MODEL_PREF={pref!r}；本轮禁止 gateway，必须为 local")
    rows: dict[str, list[dict]] = {}
    for case_id in CASES:
        runs = []
        for i in range(n):
            r = await _probe_case(case_id)
            runs.append(r)
            print(f"  {case_id} [{i+1}/{n}] first={r['first_tool']} cap={r['capability']} "
                  f"due={r['verification_due']} {r['latency_s']}s err={r['error']}", flush=True)
        rows[case_id] = runs
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "partial.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def _summary(rows: dict[str, list[dict]]) -> dict:
    out = {}
    for case_id, runs in rows.items():
        n = len(runs)
        ver = sum(1 for r in runs if r.get("capability") == "VERIFICATION")
        disc = sum(1 for r in runs if r.get("capability") in ("DISCOVERY", "READ"))
        final = sum(1 for r in runs if r.get("first_tool") is None and not r.get("error"))
        out[case_id] = {
            "n": n, "verification": ver, "discovery_or_read": disc,
            "final_attempt": final,
            "verification_rate": round(ver / n, 3) if n else 0.0,
            "latency_avg": round(sum(r.get("latency_s", 0) for r in runs) / n, 1) if n else 0,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.decision_checkpoint")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    outdir = Path(args.out)
    rows = asyncio.run(run(args.n, outdir))
    summary = _summary(rows)
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
