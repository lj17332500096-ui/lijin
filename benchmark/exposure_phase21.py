"""Phase 21：Final Tool Exposure Boundary + Capability-Focused Routing Causal Proof。

只在 benchmark 层工作，不修改生产 runtime。

回答：
- Tool Router 输出后，谁又把额外工具 materialize 进最终模型请求？
- E0（full 120）/ E1（router-authoritative）/ E2（verification-focused）/ E3（verification-only）
  对 verification action selection 的因果贡献。

用法：
    python -m benchmark.exposure_phase21 audit --out phase21/audit
    python -m benchmark.exposure_phase21 exposure --out phase21/exposure --n 10 --n3 5
    python -m benchmark.exposure_phase21 controls --out phase21/controls
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import benchmark.decision_qualification as dq  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# verification-focused / verification-only 名称集合（基于真实工具名，来自 snapshot 120）
E2_VERIFY = ["run_tests", "run_python", "code_loop", "code_loop_tool"]
E3_VERIFY = ["run_tests", "run_python"]
E2_PREP_READ = ["read_code_file", "list_code_files", "read_workspace_file",
                "list_workspace_files", "search_documents"]
E2_MUTATION = ["write_code_file", "edit_project_file", "write_project_file"]
E3_PREP = ["read_code_file", "list_code_files"]

CONTROL_TASKS = {
    "C_WEB": "搜索一下北京今天的天气。",
    "C_MEMORY": "记住我的邮箱是 bench@example.com，以后需要时告诉我。",
    "C_MCP": "列出我的 gitee 仓库。",
}


# ---------------------------------------------------------------------------
# 运行时 / 工具 provenance
# ---------------------------------------------------------------------------

async def runtime_with_mcp():
    import tempfile

    import mcp_bridge
    from runtime.runner import AgentRuntime

    tmp = tempfile.mkdtemp(prefix="p21_rt_")
    rt = AgentRuntime(db_path=os.path.join(tmp, "agent.db"))
    rt._ensure()
    try:
        await mcp_bridge.ensure_connected()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] ensure_connected failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    return rt


def _mcp_names(tools: list) -> set[str]:
    return {getattr(t, "name", "") for t in tools if getattr(t, "_mcp_source", None) == "mcp"}


def provenance_of(tool: Any) -> dict:
    name = getattr(tool, "name", "")
    origin = getattr(tool, "_tool_origin", None)
    if getattr(tool, "_mcp_source", None) == "mcp":
        source = "MCP"
        detail = getattr(tool, "_mcp_server", "") or "mcp"
    elif origin == "plugin":
        source = "SKILL"
        detail = "skills_loader"
    else:
        source = "CORE_RUNTIME"
        detail = "native"
    from runtime.spec import capability_of, spec_for

    spec = spec_for(name)
    return {
        "tool_name": name,
        "source": source,
        "source_detail": detail,
        "capability": capability_of(name),
        "category": spec.category,
        "side_effect": spec.side_effect,
        "risk": spec.risk,
        "schema_hash": dq._sha({"n": name,
                                "d": getattr(tool, "description", "") or "",
                                "s": getattr(tool, "params_json_schema", {}) or {}}),
    }


def _tool_schemas(tools: list) -> list[dict]:
    return [{"name": getattr(t, "name", ""),
             "description": getattr(t, "description", "") or "",
             "params_json_schema": getattr(t, "params_json_schema", None) or {}}
            for t in tools]


def schema_cost(schemas: list[dict]) -> dict:
    raw = json.dumps(schemas, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    chars = len(raw.decode("utf-8"))
    return {
        "tool_count": len(schemas),
        "schema_bytes": len(raw),
        "schema_chars": chars,
        "approx_tokens_bytes_div4": int(len(raw) / 4),
        "approx_tokens_chars_div3": int(chars / 3),
    }


def composition(entries: list[dict]) -> dict:
    by_source = Counter(e["source"] for e in entries)
    by_cap = Counter(e["capability"] for e in entries)
    coding_caps = {"VERIFICATION", "CODE_EXECUTION", "MUTATION_VERIFICATION_LOOP"}
    coding_domains = {"code", "filesystem"}
    coding = [e for e in entries
              if e["capability"] in coding_caps or e["category"] in coding_domains]
    return {
        "total": len(entries),
        "by_source": dict(by_source),
        "by_capability": dict(by_cap),
        "coding_related": len(coding),
        "unrelated_to_coding_verification": len(entries) - len(coding),
    }


# ---------------------------------------------------------------------------
# Final Tool Exposure Trace
# ---------------------------------------------------------------------------

async def build_trace(goals: dict[str, str]) -> dict:
    import tempfile

    import mcp_bridge
    from runtime.runner import AgentRuntime

    import agent as agent_module

    tmp = tempfile.mkdtemp(prefix="p21_trace_")
    rt = AgentRuntime(db_path=os.path.join(tmp, "agent.db"))
    rt._ensure()
    # BASE：未挂 MCP 的 agent 工具（native + skills）
    base_tools = list(agent_module.assistant_agent.tools or [])
    base_entries = [provenance_of(t) for t in base_tools]
    # MCP materialization
    try:
        await mcp_bridge.ensure_connected()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] ensure_connected: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    final_global_tools = list(agent_module.assistant_agent.tools or [])
    final_entries = [provenance_of(t) for t in final_global_tools]

    traces: dict[str, Any] = {
        "stage_base_agent_tools": {
            "count": len(base_entries),
            "composition": composition(base_entries),
        },
        "stage_after_mcp_materialization": {
            "count": len(final_entries),
            "composition": composition(final_entries),
            "added_by_mcp": sorted(set(e["tool_name"] for e in final_entries)
                                   - set(e["tool_name"] for e in base_entries)),
        },
        "tool_provenance": final_entries,
        "final_sdk_global_tools": {
            "count": len(final_global_tools),
            "note": "main._run_attempt async 分支实际使用的 agent",
        },
        "per_goal": {},
    }
    for cid, goal in goals.items():
        routed = rt.route_agent(goal, channel="chat")
        routed_names = [getattr(t, "name", "") for t in (routed.tools or [])]
        traces["per_goal"][cid] = {
            "router_selected_count": len(routed_names),
            "router_selected_names": routed_names,
            "final_async_exposure_count": len(final_entries),
            "final_stream_exposure_count": len(routed_names),
            "router_is_final_authority_async": len(final_entries) == len(routed_names),
        }
    return traces


# ---------------------------------------------------------------------------
# 变体
# ---------------------------------------------------------------------------

def variant_e0(snapshot: dict) -> list[dict]:
    return list(snapshot.get("tool_schemas") or [])


def variant_e1(snapshot: dict, router_names: list[str]) -> list[dict]:
    by_name = {s["name"]: s for s in (snapshot.get("tool_schemas") or [])}
    out = [by_name[n] for n in router_names if n in by_name]
    return out


def variant_e2(snapshot: dict) -> list[dict]:
    wanted = set(E2_VERIFY) | set(E2_PREP_READ) | set(E2_MUTATION)
    return [s for s in (snapshot.get("tool_schemas") or []) if s["name"] in wanted]


def variant_e3(snapshot: dict) -> list[dict]:
    wanted = set(E3_VERIFY) | set(E3_PREP)
    return [s for s in (snapshot.get("tool_schemas") or []) if s["name"] in wanted]


def variant_e4(snapshot: dict) -> list[dict]:
    """E4：纯 verification capability（run_tests + run_python），无 prep/discovery。"""
    wanted = set(E3_VERIFY)
    return [s for s in (snapshot.get("tool_schemas") or []) if s["name"] in wanted]


def build_agent(goal: str, system_prompt: str, schemas: list[dict]):
    from runtime.runner import AgentRuntime

    rt = AgentRuntime.get_default()
    rt._ensure()
    base = rt.route_agent(goal, channel="chat")
    return base.clone(tools=dq._dummy_tools(schemas),
                      instructions=system_prompt or None)


# ---------------------------------------------------------------------------
# 命令：audit
# ---------------------------------------------------------------------------

async def cmd_audit(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    goals = {cid: dq.CASES[cid]["prompt"] for cid in ("M1", "M4")}
    trace = await build_trace(goals)
    # 组合表（M1 120 tools）
    entries = trace["tool_provenance"]
    comp = composition(entries)
    trace["m1_120_composition"] = comp
    trace["m1_120_composition"] = comp
    # 每工具 provenance 输出
    (out / "tool_provenance.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    # schema cost per variant
    snaps = {}
    for cid in ("M1", "M4"):
        p = ROOT / "phase20" / "snapshots" / f"{cid}.json"
        snaps[cid] = dq.load_snapshot(p)
    router_names = {cid: trace["per_goal"][cid]["router_selected_names"] for cid in goals}
    costs = {}
    for cid in ("M1", "M4"):
        snap = snaps[cid]
        costs[cid] = {
            "E0": schema_cost(variant_e0(snap)),
            "E1": schema_cost(variant_e1(snap, router_names[cid])),
            "E2": schema_cost(variant_e2(snap)),
            "E3": schema_cost(variant_e3(snap)),
        }
    trace["schema_cost"] = costs
    trace["variant_tool_names"] = {
        cid: {
            "E1": [s["name"] for s in variant_e1(snaps[cid], router_names[cid])],
            "E2": [s["name"] for s in variant_e2(snaps[cid])],
            "E3": [s["name"] for s in variant_e3(snaps[cid])],
        } for cid in ("M1", "M4")
    }
    (out / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print(json.dumps({k: v for k, v in trace.items()
                      if k not in ("tool_provenance",)}, ensure_ascii=False, indent=2))
    return trace


# ---------------------------------------------------------------------------
# 命令：exposure（E1/E2/E3，E0 复用 Phase 20）
# ---------------------------------------------------------------------------

async def _run_variant(cid: str, snap: dict, label: str, schemas: list[dict],
                       n: int) -> dict:
    agent = build_agent(snap["goal"], snap["system_prompt"], schemas)
    runs = []
    for i in range(n):
        p = await dq.run_decision_probe(agent, snap["input_items"])
        p.update({"i": i, "variant": label, "tool_count": len(schemas)})
        runs.append(p)
        print(f"  [{label}] {cid} [{i+1}/{n}] {p['status']} first={p['first_tool']} "
              f"class={p['semantic_action_class']} {p['decision_latency_s']}s", flush=True)
    s = dq.summarize_probes(runs)
    s["tool_count"] = len(schemas)
    s["schema_cost"] = schema_cost(schemas)
    s["prep_rate"] = dq._rate(
        sum(1 for r in runs if r.get("semantic_action_class") == "VERIFICATION_PREPARATION"),
        len(runs))
    s["invalid_tool_call"] = sum(1 for r in runs
                                 if r.get("status") in ("PARSER_ERROR", "PROVIDER_ERROR"))
    return s


async def cmd_exposure(out: Path, n: int, n3: int, run_e0: bool = False,
                       snapdir: Path | None = None,
                       variants: tuple[str, ...] = ("E0", "E1", "E2", "E3")) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    snapdir = snapdir or (ROOT / "phase20" / "snapshots")
    result: dict[str, Any] = {}
    for cid in ("M1", "M4"):
        snap = dq.load_snapshot(snapdir / f"{cid}.json")
        if snap.get("snapshot_type") == "LIVE_SNAPSHOT_CHECKPOINT":
            dq.assert_checkpoint_valid(snap)
        cid_res: dict[str, Any] = {}
        if "E0" in variants:
            if run_e0:
                cid_res["E0"] = await _run_variant(cid, snap, "E0", variant_e0(snap), n)
            else:
                p20 = json.loads((ROOT / "phase20" / "baseline" / "summary.json").read_text(
                    encoding="utf-8"))
                e0 = dict(p20[cid])
                e0["tool_count"] = len(snap["tool_schemas"])
                e0["schema_cost"] = schema_cost(snap["tool_schemas"])
                e0["prep_rate"] = dq._rate(
                    sum(1 for r in json.loads((ROOT / "phase20" / "baseline" / "partial.json")
                                              .read_text(encoding="utf-8"))[cid]
                        if r.get("semantic_action_class") == "VERIFICATION_PREPARATION"), 10)
                cid_res["E0"] = e0
        # router names：用真实 route_agent 计算
        rt = await runtime_with_mcp()
        routed = rt.route_agent(snap["goal"], channel="chat")
        router_names = [getattr(t, "name", "") for t in (routed.tools or [])]
        if "E1" in variants:
            cid_res["E1"] = await _run_variant(cid, snap, "E1", variant_e1(snap, router_names), n)
        if "E2" in variants:
            cid_res["E2"] = await _run_variant(cid, snap, "E2", variant_e2(snap), n)
        if "E3" in variants:
            cid_res["E3"] = await _run_variant(cid, snap, "E3", variant_e3(snap), n3)
        result[cid] = cid_res
        (out / "partial.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    # lift（仅在成对存在时）
    lifts = {}
    for cid in ("M1", "M4"):
        r = result.get(cid, {})
        pair = {}
        if "E0" in r and "E1" in r:
            pair["E1_minus_E0"] = round(r["E1"]["semantic_rate"] - r["E0"]["semantic_rate"], 4)
        if "E1" in r and "E2" in r:
            pair["E2_minus_E1"] = round(r["E2"]["semantic_rate"] - r["E1"]["semantic_rate"], 4)
        if "E2" in r and "E3" in r:
            pair["E3_minus_E2"] = round(r["E3"]["semantic_rate"] - r["E2"]["semantic_rate"], 4)
        lifts[cid] = pair
    result["semantic_lift"] = lifts
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "semantic_lift"},
                     ensure_ascii=False, indent=2))
    print("LIFT", json.dumps(lifts, ensure_ascii=False))
    return result


async def cmd_ceiling(out: Path, n: int, snapdir: Path | None = None) -> dict:
    """E4：纯 verification capability 上限（因果 ceiling）。"""
    out.mkdir(parents=True, exist_ok=True)
    snapdir = snapdir or (ROOT / "phase20" / "snapshots")
    result: dict[str, Any] = {}
    for cid in ("M1", "M4"):
        snap = dq.load_snapshot(snapdir / f"{cid}.json")
        result[cid] = await _run_variant(cid, snap, "E4", variant_e4(snap), n)
        (out / "partial.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


# ---------------------------------------------------------------------------
# 命令：controls（router authority + M6/M7/M8 + plugin/MCP）
# ---------------------------------------------------------------------------

async def cmd_controls(out: Path, n: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rt = await runtime_with_mcp()
    import agent as agent_module

    all_tools = list(agent_module.assistant_agent.tools or [])
    all_names = [getattr(t, "name", "") for t in all_tools]
    mcp = _mcp_names(all_tools)

    routing = {}
    for cid, prompt in CONTROL_TASKS.items():
        routed = rt.route_agent(prompt, channel="chat")
        names = [getattr(t, "name", "") for t in (routed.tools or [])]
        routing[cid] = {"prompt": prompt, "router_selected": names,
                        "count": len(names)}
    # plugin/MCP control: router 必须选择 MCP 工具
    c_mcp = routing["C_MCP"]["router_selected"]
    routing["C_MCP"]["mcp_tools_exposed"] = [nm for nm in c_mcp if nm in mcp]
    routing["C_MCP"]["plugin_control_pass"] = bool(routing["C_MCP"]["mcp_tools_exposed"])
    # M6/M7/M8 decision probes under E1-style (router-authoritative) exposure
    probes = {}
    for cid in ("M6", "M7", "M8"):
        reset = dq.reset_fixture
        reset()
        syn = await dq.build_synthetic_checkpoint(cid)
        routed = rt.route_agent(syn["goal"], channel="chat")
        names = [getattr(t, "name", "") for t in (routed.tools or [])]
        by = {s["name"]: s for s in syn["tool_schemas"]}
        schemas = [by[nm] for nm in names if nm in by] or syn["tool_schemas"]
        agent = build_agent(syn["goal"], syn["system_prompt"], schemas)
        runs = []
        for i in range(n):
            p = await dq.run_decision_probe(agent, syn["input_items"])
            p.update({"i": i, "verification_due": syn["verification_due"]})
            runs.append(p)
            print(f"  [control/{cid}] [{i+1}/{n}] due={syn['verification_due']} "
                  f"first={p['first_tool']} sem={p['semantic_verification']}", flush=True)
        probes[cid] = dq.summarize_probes(runs)
        probes[cid]["unnecessary_verification"] = sum(
            1 for r in runs if r.get("semantic_verification"))
    # plugin/MCP task decision probe（router-authoritative exposure）
    cid = "C_MCP"
    routed = rt.route_agent(CONTROL_TASKS[cid], channel="chat")
    names = [getattr(t, "name", "") for t in (routed.tools or [])]
    by = {s["name"]: s for s in _tool_schemas(all_tools)}
    schemas = [by[nm] for nm in names if nm in by]
    agent = build_agent(CONTROL_TASKS[cid], getattr(agent_module.assistant_agent,
                                                    "instructions", "") or "", schemas)
    runs = []
    for i in range(n):
        p = await dq.run_decision_probe(agent, [{"role": "user", "content": CONTROL_TASKS[cid]}])
        p["i"] = i
        runs.append(p)
        print(f"  [control/C_MCP] [{i+1}/{n}] first={p['first_tool']} "
              f"in_mcp={p['first_tool'] in mcp}", flush=True)
    probes["C_MCP"] = dq.summarize_probes(runs)
    probes["C_MCP"]["mcp_first_rate"] = dq._rate(
        sum(1 for r in runs if r.get("first_tool") in mcp), len(runs))

    result = {"routing_control": routing, "probes": probes,
              "total_tools": len(all_names), "mcp_tool_count": len(mcp)}
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.exposure_phase21")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("audit"); p.add_argument("--out", required=True)
    p = sub.add_parser("exposure"); p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=10); p.add_argument("--n3", type=int, default=5)
    p.add_argument("--run-e0", action="store_true", dest="run_e0")
    p.add_argument("--snapdir", default="")
    p.add_argument("--variants", default="E0,E1,E2,E3")
    p = sub.add_parser("controls"); p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=2)
    p = sub.add_parser("ceiling"); p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=5); p.add_argument("--snapdir", default="")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import main as _main  # noqa: F401
    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref != "local":
        raise SystemExit(f"FORGE_MODEL_PREF={pref!r}；本轮禁止 gateway，必须为 local")
    if args.cmd == "audit":
        asyncio.run(cmd_audit(Path(args.out)))
    elif args.cmd == "exposure":
        sd = Path(args.snapdir) if getattr(args, "snapdir", "") else None
        vs = tuple(v.strip() for v in getattr(args, "variants", "").split(",") if v.strip())
        asyncio.run(cmd_exposure(Path(args.out), args.n, args.n3,
                                 getattr(args, "run_e0", False), sd, vs))
    elif args.cmd == "controls":
        asyncio.run(cmd_controls(Path(args.out), args.n))
    elif args.cmd == "ceiling":
        sd = Path(args.snapdir) if getattr(args, "snapdir", "") else None
        asyncio.run(cmd_ceiling(Path(args.out), args.n, sd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
