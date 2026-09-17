"""Phase 14：Tool Invocation Usability Probe（A2b）。

只暴露单个 verification 工具（run_python 或 code_loop），给一个最小验证任务，
看本地模型是否选择、参数是否合法、是否执行成功。严格 vs truthful schema 对照。

用法：
    python -m benchmark.tool_probe --tool run_python --strict both --n 3 --out phase14/probe_run_python
强制本地模型。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TASK = ("运行项目 micro_fixture 的测试并报告结果。"
        "项目目录：F:/Byong-hermes/Byong-hermes/my_creative_agent/micro_fixture")

_OLD_STRICT_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"title": "Project", "type": "string"},
        "filename": {"title": "Filename", "type": "string"},
        "code": {"title": "Code", "type": "string"},
        "args": {"title": "Args", "type": "string"},
        "timeout": {"title": "Timeout", "type": "integer"},
    },
    "required": ["project", "filename", "code", "args", "timeout"],
    "additionalProperties": False,
}


def _clone_tool(name: str, strict: bool):
    from agents.tool import FunctionTool

    import code_exec

    if name == "run_python":
        src = code_exec.run_python
    elif name == "run_tests":
        src = code_exec.run_tests
    else:
        from runtime.codex_loop import code_loop as src  # type: ignore
    schema = _OLD_STRICT_SCHEMA if strict else (getattr(src, "params_json_schema", {}) or {})
    return FunctionTool(
        name=getattr(src, "name", name),
        description=getattr(src, "description", "") or "",
        params_json_schema=schema,
        on_invoke_tool=src.on_invoke_tool,
        strict_json_schema=strict,
    )


async def _one(tool_name: str, strict: bool) -> dict:
    from agents import Agent, Runner
    import main as main_module
    from agent import local_model_name, local_model_provider

    tool = _clone_tool(tool_name, strict)
    agent = Agent(name="probe", instructions="你是代码助手。需要运行测试时调用提供的工具。",
                  model=local_model_name(), tools=[tool])
    seq: list[dict] = []
    error = None
    try:
        result = await Runner.run(agent, TASK,
                                  run_config=main_module._run_config(provider=local_model_provider()),
                                  max_turns=4)
        for item in getattr(result, "new_items", []) or []:
            raw = getattr(item, "raw_item", None)
            t = raw.get("type", "") if isinstance(raw, dict) else str(getattr(raw, "type", "") or "")
            if "function_call" in t:
                nm = raw.get("name") if isinstance(raw, dict) else getattr(raw, "name", "")
                args = raw.get("arguments") if isinstance(raw, dict) else getattr(raw, "arguments", "")
                seq.append({"name": nm, "args": str(args)[:200]})
        final = str(getattr(result, "final_output", "") or "")[:200]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
        final = ""
    selected = any(s["name"] == tool_name for s in seq)
    return {
        "tool": tool_name, "strict": strict, "selected": selected,
        "calls": seq, "error": error, "final": final,
    }


async def run(tool_name: str, strict_modes: list[bool], n: int, outdir: Path) -> dict:
    import main as main_module  # noqa: F401
    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref != "local":
        raise SystemExit(f"FORGE_MODEL_PREF={pref!r}；本轮禁止 gateway，必须为 local")
    rows = []
    for strict in strict_modes:
        runs = [await _one(tool_name, strict) for _ in range(n)]
        sel = sum(1 for r in runs if r["selected"])
        valid = sum(1 for r in runs if r["selected"] and r["calls"] and r["calls"][0]["args"])
        rows.append({
            "strict": strict, "n": n,
            "selection_rate": round(sel / n, 3),
            "valid_arg_rate": round(valid / n, 3),
            "runs": runs,
        })
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "partial.json").write_text(
            json.dumps({"tool": tool_name, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  {tool_name} strict={strict} selection={sel}/{n} valid={valid}/{n}", flush=True)
    return {"tool": tool_name, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.tool_probe")
    parser.add_argument("--tool", choices=["run_python", "run_tests", "code_loop"], required=True)
    parser.add_argument("--strict", choices=["true", "false", "both"], default="both")
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    modes = {"true": [True], "false": [False], "both": [True, False]}[args.strict]
    outdir = Path(args.out)
    result = asyncio.run(run(args.tool, modes, args.n, outdir))
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print(f"{args.tool} -> {outdir/'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
