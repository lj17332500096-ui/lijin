"""Phase 8：受控决策探针（Router Ablation + 固定工具结果 Replay）。

在**完全固定**的 Prompt / System Prompt / Model / 工具结果 下，只改变“暴露的工具集”，
从而隔离 Tool Router 暴露对模型决策路径的因果影响；再用固定结果 replay 量化纯模型采样方差。

- 不进入生产代码；只用于实验。
- 使用真实本地模型（AGENT_MODEL / FORGE_LOCAL_MODEL_*）。
- 工具为 fixture（固定返回），因此不判真实 Behavior Pass，只测：
  first_decision / tool_sequence / tool_attempts / drift / stability。

用法：
    python -m benchmark.decision_probe --variant router --n 3 --out phase8/probe_router
    python -m benchmark.decision_probe --variant oracle --n 3 --out phase8/probe_oracle
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import get_case  # noqa: E402

# Router Causal Set（与 Router 暴露最相关）
CAUSAL_SET = ["T007", "T009", "T014", "T017", "T019", "T021", "T024", "T026",
              "T028", "T036", "T038", "T042", "T044"]

# Oracle 最小合法工具集（按 Expected Behavior）
ORACLE = {
    "coding": ["read_workspace_file", "list_workspace_files", "search_documents",
               "edit_project_file", "write_code_file", "run_python"],
    "read_only": ["read_workspace_file", "list_workspace_files", "search_documents"],
    "code_search": ["read_workspace_file", "list_workspace_files", "search_documents"],
    "ask_user": ["read_workspace_file", "list_workspace_files"],
    "search_answer": ["web_search", "get_current_datetime"],
    "search_answer2": ["web_search", "get_current_datetime"],
    "truthful_unknown": ["web_search", "get_current_datetime"],
    "capability_plus_weather": ["web_search", "get_current_datetime"],
    "direct_answer": [],
    "direct_text": [],
    "capability_intro": [],
    "capability_boundary": [],
    "calculate": ["calculate"],
    "recall": ["recall_memory", "read_workspace_file", "list_workspace_files"],
    "memory_write": ["remember", "save_note"],
    "session_only_no_persist": [],
    "read_run_ask": ["read_workspace_file", "list_workspace_files", "run_python"],
    "destructive_needs_confirm": ["read_workspace_file", "list_workspace_files"],
    "refuse_false_claim": ["read_workspace_file", "list_workspace_files"],
    "comprehensive": ["read_workspace_file", "list_workspace_files", "search_documents",
                      "edit_project_file", "run_python", "web_search"],
    "research": ["web_search", "search_documents"],
    "run_tests": ["read_workspace_file", "list_workspace_files", "run_python"],
    "read_plus_search": ["read_workspace_file", "list_workspace_files", "web_search"],
    "direct_answer2": [],
}

FIXED_RESULTS = {
    "list_workspace_files": "app/auth.py\ntests/test_auth.py\nREADME.md\nrequirements.txt",
    "read_workspace_file": "def login(u,p):\n    return SESSIONS.get(u)\n\ndef calculate(x):\n    return float(x)*2\n",
    "read_code_file": "print('sandbox')",
    "search_documents": "命中：app/auth.py 第 12 行 login()",
    "index_workspace": "已索引 3 个文件",
    "web_search": "结果：北京今天晴，25℃。来源：weather.example",
    "get_current_datetime": "2026-09-11 12:00",
    "calculate": "39965",
    "run_python": "15 passed in 0.02s\n退出码: 0",
    "edit_project_file": "已修改 app/auth.py",
    "write_code_file": "已写入 sandbox/test_x.py",
    "write_project_file": "已写入 app/auth.py",
    "remember": "已记住",
    "recall_memory": "你之前要求：正式环境数据库不可直接执行 destructive migration",
    "save_note": "已保存到 notes/x.md",
}


def _oracle_tools(behavior: str) -> list[str]:
    return ORACLE.get(behavior, [])


def _make_fixture_tools(names: list[str]):
    from agents.tool import FunctionTool
    from agent import assistant_agent

    real = {getattr(t, "name", ""): t for t in (assistant_agent.tools or [])}
    tools = []
    for n in names:
        result = FIXED_RESULTS.get(n, "ok")

        async def _invoke(ctx, args_json, _r=result):
            return _r

        src = real.get(n)
        schema = (getattr(src, "params_json_schema", None)
                  if src is not None else None) or {
            "type": "object",
            "properties": {"query": {"type": "string"}, "path": {"type": "string"},
                           "content": {"type": "string"}},
            "additionalProperties": True,
        }
        tools.append(FunctionTool(
            name=n,
            description=(getattr(src, "description", "") if src is not None else f"fixture {n}"),
            params_json_schema=schema,
            on_invoke_tool=_invoke, strict_json_schema=False,
        ))
    return tools


async def _one(behavior: str, names: list[str], prompt: str, max_turns: int,
               provider: str = "local") -> dict:
    from agents import Agent, Runner
    import main as main_module

    tools = _make_fixture_tools(names)
    if provider == "local":
        from agent import local_model_name, local_model_provider
        model = local_model_name()
        run_config = main_module._run_config(provider=local_model_provider())
    else:
        import os as _os
        model = _os.getenv("AGENT_MODEL", "").strip() or None
        run_config = main_module._run_config()
    # 使用真实 Agent 的 instructions（保持与生产一致的 System Prompt），只替换工具集。
    try:
        from agent import assistant_agent
        agent = assistant_agent.clone(tools=tools, model=model)
    except Exception:
        agent = Agent(name="probe", instructions="你是私人助手。需要时调用工具；完成后直接给出最终回答。",
                      model=model, tools=tools)
    seq: list[str] = []
    try:
        result = await Runner.run(agent, prompt, run_config=run_config,
                                  max_turns=max_turns)
        for item in getattr(result, "new_items", []) or []:
            raw = getattr(item, "raw_item", None)
            t = ""
            if isinstance(raw, dict):
                t = str(raw.get("type", ""))
            else:
                t = str(getattr(raw, "type", "") or "")
            if "function_call" in t:
                nm = raw.get("name") if isinstance(raw, dict) else getattr(raw, "name", "")
                if nm:
                    seq.append(nm)
        final = str(getattr(result, "final_output", "") or "")
        return {"ok": True, "sequence": seq, "first": seq[0] if seq else None,
                "final": final[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "sequence": seq, "first": seq[0] if seq else None,
                "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def run(variant: str, n: int, max_turns: int, cases: list[str],
              outdir: Path | None = None, provider: str = "local") -> dict:
    from runtime.tool_router import select_tool_names
    from agent import assistant_agent

    all_tools = [t.name for t in assistant_agent.tools]
    rows = []
    for cid in cases:
        case = get_case(cid)
        if variant == "router":
            names = select_tool_names(case.prompt, all_tools)
        else:
            names = _oracle_tools(case.expected.behavior)
        runs = []
        for _ in range(n):
            runs.append(await _one(case.expected.behavior, names, case.prompt, max_turns,
                                   provider=provider))
        seqs = [tuple(r.get("sequence") or []) for r in runs]
        firsts = [r.get("first") for r in runs]
        rows.append({
            "case": cid,
            "behavior": case.expected.behavior,
            "exposed": names,
            "runs": runs,
            "first_stable": len(set(firsts)) == 1,
            "sequence_stable": len(set(seqs)) == 1,
            "avg_attempts": round(sum(len(s) for s in seqs) / max(1, len(seqs)), 2),
        })
        if outdir is not None:
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "partial.json").write_text(
                json.dumps({"variant": variant, "n": n, "cases": rows},
                           ensure_ascii=False, indent=2), encoding="utf-8")
    return {"variant": variant, "n": n, "cases": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.decision_probe")
    parser.add_argument("--variant", choices=["router", "oracle"], required=True)
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--cases", default=",".join(CAUSAL_SET))
    parser.add_argument("--provider", choices=["local"], default="local",
                        help="仅允许 local（禁止 gateway/agnes）")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    cases = [c for c in args.cases.split(",") if c]
    outdir = Path(args.out)
    result = asyncio.run(run(args.variant, args.n, args.max_turns, cases, outdir,
                             provider=args.provider))
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    first_stable = sum(1 for r in result["cases"] if r["first_stable"])
    seq_stable = sum(1 for r in result["cases"] if r["sequence_stable"])
    avg = round(sum(r["avg_attempts"] for r in result["cases"]) / max(1, len(result["cases"])), 2)
    print(f"{args.variant}: cases={len(result['cases'])} first_stable={first_stable} "
          f"seq_stable={seq_stable} avg_attempts={avg} -> {outdir/'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
