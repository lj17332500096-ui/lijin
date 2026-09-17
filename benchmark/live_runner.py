"""Phase 5 live benchmark harness —— 对真实运行中的 webapp 跑 50 case。

与旧 bench_runner.js 的区别（Phase 5 要求）：
- 通过 HTTP API 驱动（真实 Provider / Runtime / Router / Permission / Approval / Completion）；
- 观测窗口内把 **所有终态/暂停态** 都视为结束（completed/failed/cancelled/
  waiting_user/waiting_approval），不再把 waiting_approval 当成“没结束”而轮询到窗口关闭；
- 采集 `?debug=1` 的逐次 tool_calls / events / model_calls，产出含
  action_signature / result_fingerprint / convergence / terminal 的 rich artifact；
- 不做 auto-approve（审批 case 保持 waiting_approval，才是真实行为）；
- 默认观测超时 240s；超时仍 running → 记录 running 并 cancel，标记 observation_timeout。

用法：
    python -m benchmark.live_runner --out phase5/runA [--from 0 --to 50]
                                    [--timeout 240] [--base http://127.0.0.1:8765]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import BENCHMARK_CASES  # noqa: E402
from benchmark.spec import spec_manifest  # noqa: E402

END_STATES = {"completed", "failed", "cancelled", "waiting_user", "waiting_approval"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http(method: str, url: str, body: dict | None = None, timeout: float = 30.0):
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, raw


def _get(url: str, timeout: float = 30.0):
    try:
        return _http("GET", url, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, {"error": f"HTTP {e.code}"}


def _terminal_kind(events: list[dict]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "run.terminal":
            return str((ev.get("payload") or {}).get("kind") or "")
    return ""


def _convergence_summary(events: list[dict]) -> dict:
    transitions = []
    forced = 0
    for ev in events:
        t = ev.get("type")
        if t == "tool.convergence":
            p = ev.get("payload") or {}
            transitions.append({"tool": p.get("tool"), "level": p.get("level")})
        elif t == "convergence.forced":
            forced += 1
    return {"transitions": transitions, "forced": forced}


def run_case(case, base: str, timeout_s: int) -> dict:
    started = time.monotonic()
    out: dict = {"id": case.id, "prompt": case.prompt}
    try:
        _, created = _http("POST", f"{base}/api/tasks/create", {"title": f"P5 {case.id}"})
        container = (created or {}).get("task") or {}
        cid = container.get("id")
        if not cid:
            out.update({"final_status": "error", "error": f"create failed: {created}"})
            return out
        _, runresp = _http("POST", f"{base}/api/tasks/{cid}/runs",
                           {"message": case.prompt,
                            "client_message_id": f"p5-{case.id}-{int(time.time()*1000)}"})
        run_id = (runresp or {}).get("run_id")
        if not run_id:
            out.update({"final_status": "error", "error": f"run create failed: {runresp}"})
            return out
        out["container_id"] = cid
        out["run_id"] = run_id

        # ---- 观测窗口：所有终态/暂停态都视为结束 ----
        last_state = ""
        while time.monotonic() - started < timeout_s:
            time.sleep(2.0)
            _, detail = _get(f"{base}/api/runs/{run_id}")
            last_state = str((detail or {}).get("state") or "")
            if last_state in END_STATES:
                break
        observed_at = _now_iso()
        terminal = last_state in END_STATES
        out["observed_state_at_deadline"] = last_state or "running"
        out["observation_deadline"] = timeout_s
        out["observed_at"] = observed_at
        # 若截止仍未终态：请求取消，并短暂等待其进入真正终态（区分 observation timeout 与 terminal）
        if not terminal:
            try:
                _http("POST", f"{base}/api/runs/{run_id}/cancel", {})
            except Exception:
                pass
            t2 = time.monotonic() + 25
            while time.monotonic() < t2:
                time.sleep(2.0)
                _, detail = _get(f"{base}/api/runs/{run_id}")
                st = str((detail or {}).get("state") or "")
                if st in END_STATES:
                    last_state = st
                    terminal = True
                    break
        out["terminal_state"] = last_state if terminal else None
        out["terminal_at"] = _now_iso() if terminal else ""
        out["final_status"] = last_state or "running"
        out["observation_timeout"] = (not terminal)

        # ---- rich debug 明细 ----
        _, dbg = _get(f"{base}/api/runs/{run_id}?debug=1")
        dbg = dbg or {}
        out["error"] = dbg.get("error") or None
        out["duration_s"] = round(time.monotonic() - started, 2)
        tool_calls = dbg.get("tool_calls") or []
        model_calls = dbg.get("model_calls") or []
        events = dbg.get("events") or []
        out["model_turns"] = len(model_calls)
        out["provider_errors"] = sum(
            1 for e in events if e.get("type") == "provider.failure"
        )
        out["terminal_kind"] = _terminal_kind(events)
        out["convergence"] = _convergence_summary(events)
        out["tools"] = [
            {
                "name": tc.get("tool_name"),
                "normalized_args": tc.get("arguments_json") or tc.get("arguments"),
                "status": tc.get("status"),
                "invocation_id": tc.get("invocation_id"),
                "result_excerpt": (tc.get("result_excerpt") or "")[:300],
            }
            for tc in tool_calls
        ]
        # final assistant text
        try:
            _, msgs = _get(f"{base}/api/tasks/{cid}/messages")
            items = msgs.get("messages") if isinstance(msgs, dict) else msgs
            for m in reversed(items or []):
                if m.get("run_id") == run_id and m.get("role") == "assistant":
                    out["answer"] = m.get("content") or ""
                    out["reply_kind"] = (m.get("meta") or {}).get("kind") or ""
                    break
        except Exception:
            pass
        # ---- Tool 真值（唯一事实源：public.tool.started = 通过全部 gate 的真实执行）----
        started_events = sum(1 for e in events if e.get("type") == "public.tool.started")
        completed_events = sum(1 for e in events if e.get("type") == "public.tool.completed")
        failed_events = sum(1 for e in events if e.get("type") == "public.tool.failed")
        blocked_event_types = {
            "tool.budget_exhausted", "tool.convergence", "tool.readiness_gate",
            "tool.intent_gate", "tool.blocked_needs_user_input", "tool.missing_required",
            "tool.user_constraint", "tool.discovery_exhausted",
            "tool.discovery_hard_stopped", "tool.mutation_exhausted",
            "tool.permission_error", "tool.persistence_idempotent",
            "tool.network_policy", "convergence.forced",
        }
        blocked_events = sum(1 for e in events if e.get("type") in blocked_event_types)
        # DB 唯一 invocation 执行数（去重后的执行真值，交叉校验）
        uniq_inv = {tc.get("invocation_id") for tc in tool_calls
                    if tc.get("invocation_id")
                    and str(tc.get("status")).lower() in ("succeeded", "executed", "ok")}
        out["tool_truth"] = {
            "attempts": started_events + blocked_events,
            "blocked": blocked_events,
            "executions": started_events,
            "successes": completed_events,
            "failures": failed_events,
            "budget_consumed": started_events,
            "db_unique_executions": len(uniq_inv),
        }
        out["_exec"] = {}
        out["_blocked"] = {}
        for tc in tool_calls:
            name = tc.get("tool_name") or "?"
            st = str(tc.get("status") or "").lower()
            if st in ("succeeded", "executed", "ok"):
                out["_exec"][name] = out["_exec"].get(name, 0) + 1
            elif st in ("blocked", "denied"):
                out["_blocked"][name] = out["_blocked"].get(name, 0) + 1
        # ---- Provider 调用时间线（真实可得字段）----
        provider_calls: list[dict] = []
        for e in events:
            if e.get("type") != "provider.model_attempts":
                continue
            for a in (e.get("payload") or {}).get("attempts") or []:
                provider_calls.append({
                    "kind": a.get("kind"),
                    "tag": a.get("tag"),
                    "attempt": a.get("attempt"),
                    "latency_ms": a.get("latency_ms"),
                    "tokens_output": a.get("tokens_output"),
                    "interrupted_reason": a.get("interrupted_reason"),
                    "fallback_used": a.get("fallback_used"),
                })
        out["provider_calls"] = provider_calls
        return out
    except Exception as exc:  # noqa: BLE001
        out.update({"final_status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "duration_s": round(time.monotonic() - started, 2)})
        return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.live_runner")
    parser.add_argument("--out", required=True)
    parser.add_argument("--from", dest="start", type=int, default=0)
    parser.add_argument("--to", dest="end", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--only", default="", help="只跑指定 case id，逗号分隔")
    parser.add_argument("--run-id", default="", help="Run Manifest 的 run_id")
    parser.add_argument("--qualification-id", default="", help="本轮 Provider Qualification id")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        cases = [c for c in BENCHMARK_CASES if c.id in wanted]
    else:
        cases = BENCHMARK_CASES[args.start:args.end]
    run_id = args.run_id or f"run-{int(time.time())}"
    from benchmark.spec import write_run_manifest
    write_run_manifest(outdir, run_id=run_id, extra={
        "provider": os.getenv("OPENAI_BASE_URL", "") or "openai",
        "model": os.getenv("AGENT_MODEL", ""),
        "observation_deadline": args.timeout,
        "runtime_wall_timeout": os.getenv("FORGE_RUN_WALL_TIMEOUT_SECONDS", "1800"),
        "provider_qualification_id": args.qualification_id or "",
        "case_count": len(cases),
    })
    print(f"Phase6 live run: {len(cases)} cases -> {outdir} (timeout={args.timeout}s) run_id={run_id}")
    for i, case in enumerate(cases, 1):
        r = run_case(case, args.base, args.timeout)
        (outdir / f"{case.id}.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        tt = r.get("tool_truth") or {}
        print(f"[{i}/{len(cases)}] {case.id} | observed={r.get('observed_state_at_deadline')} "
              f"terminal={r.get('terminal_state')} | exec={tt.get('executions')} "
              f"blocked={tt.get('blocked')} | {r.get('duration_s')}s | kind={r.get('terminal_kind')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
