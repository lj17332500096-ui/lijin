"""Benchmark 50 case 真值化运行器（Phase 5）。

把 benchmark.cases 的 50 个结构化 prompt 喂给**真实 AgentRuntime**（同 .env、
同路由、同收口门、同 ApprovalGate），把每次 Run 的事实（状态 / 工具逐次明细 /
provider 事件 / 最终回答）聚合成 benchmark.evaluator.Observation 所需的 raw 记录，
再由 benchmark.__main__.evaluate 评成权威报告。

设计原则
--------
- **不建第二套评测系统**：直接 import benchmark.cases / evaluator / report，
  复用既有 ExpectedBehavior / Observation / CaseResult / build_report。
- **事实源 = Runtime 事件表**：工具执行数 / 阻塞数 / provider 错误数全部从
  task_events 读，不从内存推测；终态以 run.terminal 事件为准。
- **观测 vs 终态分离**（Phase 6 口径）：等 Run 到真正终态或墙钟上限截止，
  截止时若仍在 running 则记 observed_state_at_deadline=running，
  不冒充"已完成"。

用法
----
    python -m benchmark.eval_runner --cases T001,T002,T003 --out runs_eval --label RUN-A
    python -m benchmark.eval_runner --all --out runs_eval --label RUN-A
    # 评报告（同 benchmark 包的 evaluate 入口）：
    python -m benchmark evaluate --runs runs_eval --label RUN-A --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.cases import BENCHMARK_CASES, case_ids  # noqa: E402

# 截止等待：真终态（completed/failed/cancelled/timeout/...）即停；
# 否则到 deadline 记 observed_state_at_deadline（Phase 6 口径）。
TERMINAL_STATES = frozenset({
    "completed", "failed", "cancelled", "timeout", "provider_error",
    "stalled", "waiting_user", "waiting_approval",
})


def _terminal_kind(events: list[dict]) -> str:
    for e in reversed(events):
        if e.get("type") == "run.terminal":
            return str((e.get("payload") or {}).get("kind") or "")
    return ""


def _convergence(events: list[dict]) -> dict:
    out = {"convergence": 0, "blocked_reasons": []}
    for e in events:
        p = e.get("payload") or {}
        if e.get("type") == "convergence.forced":
            out["convergence"] += 1
        if e.get("type") in ("tool.budget_exhausted", "tool.convergence",
                             "tool.readiness_gate", "tool.intent_gate",
                             "tool.blocked_needs_user_input",
                             "tool.missing_required", "tool.user_constraint",
                             "tool.discovery_exhausted",
                             "tool.discovery_hard_stopped",
                             "tool.mutation_exhausted", "tool.permission_error",
                             "tool.persistence_idempotent", "tool.network_policy"):
            out["blocked_reasons"].append(e.get("type"))
    return out


async def _auto_approve(rt, task_id: str) -> int:
    """跑评测时统一自动批准（评测是行为验收，不是审批交互验收）。"""
    n = 0
    try:
        for ap in rt.tasks.list_pending_approvals(task_id):
            try:
                rt.tasks.decide_approval(ap["id"], "approved")
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


def _run_state(rt, task_id: str) -> str:
    t = rt.tasks.get_task(task_id)
    return t.state.value if t else "unknown"


async def _drive_to_terminal(rt, task_id: str, *, deadline_s: float,
                             session_id: str) -> dict:
    """驱动 Run 到终态（含 waiting_approval 自动批准）或截止。"""
    t0 = time.monotonic()
    for _ in range(200):
        st = _run_state(rt, task_id)
        if st in TERMINAL_STATES:
            break
        if time.monotonic() - t0 > deadline_s:
            break
        if st == "waiting_approval":
            if await _auto_approve(rt, task_id) == 0:
                break
            try:
                await rt.run_turn("", task_id=task_id, mode="async", max_turns=16)
            except Exception:
                break
        elif st == "waiting_user":
            break
        elif st in ("running", "submitted", "paused"):
            await asyncio.sleep(0.05)
        else:
            break
    return {"final_state": _run_state(rt, task_id),
            "deadline_s": deadline_s,
            "elapsed_s": round(time.monotonic() - t0, 2)}


def _episode_injection(prompt: str) -> str:
    """评测用：给 prompt 前缀注入情节记忆。

    两个必须守住的约束：
    1. **留一法**（exclude_self=True）—— 历史库里 33/50 的 case 有完全同指纹的既往
       执行记录，不排除就是让模型背自己上次的标准答案，测出来的提升是假的。
    2. 注入块归档到 raw 记录，事后能逐 case 审计到底喂了什么进去。
    """
    try:
        from runtime.episode_recall import build_context

        return build_context(prompt, exclude_self=True)
    except Exception:
        return ""


async def _run_case(rt, case, *, deadline_s: float, db_dir: Path,
                    inject_episode: bool = False) -> dict:
    import main as _m  # noqa: F401  （provider / agent 配置加载）

    sid = f"eval50-{case.id}-{int(time.time() * 1000) % 1_000_000_000}"
    out: dict = {"id": case.id}
    started = time.monotonic()
    try:
        message = case.prompt
        if inject_episode:
            block = _episode_injection(case.prompt)
            if block:
                out["episode_injected"] = True
                out["episode_block"] = block
                message = f"{block}\n\n{case.prompt}"
        res = await rt.run_turn(message, session_id=sid, mode="async",
                                max_turns=16)
        rid = res.task.id
        drive = await _drive_to_terminal(rt, rid, deadline_s=deadline_s, session_id=sid)
        out.update(drive)
        events = [{"type": e.event_type, "payload": e.payload}
                  for e in rt.tasks.list_events(rid, limit=2000)]
        # 工具逐次明细（DB 唯一事实源；status 归一）
        calls = []
        for e in events:
            if e["type"] != "tool.invocation":
                continue
            p = e["payload"] or {}
            status = str(p.get("status") or p.get("execution_status") or "executed").lower()
            if status in ("succeeded", "success", "ok"):
                status = "executed"
            if status == "denied":
                status = "blocked"
            # 补上 trace 此前丢失的字段：
            # - normalized_args：工具调用参数（脱敏前；敏感参数走 _record_tool 的 redaction）
            # - result_fingerprint：结果指纹（用于"同参数换关键词"vs"真不同子查询"判别）
            # - progress_event：进展判定签名（NO_PROGRESS/NEW_EVIDENCE 等）
            # - canonical_target：逻辑目标身份（read/search/verify 的 target）
            # - workspace_epoch：本次调用时的 mutation 代数（判定"中间是否有 write/edit"）
            calls.append({
                "name": p.get("tool_name") or p.get("name"),
                "status": status,
                "reason": str(p.get("blocked_reason") or p.get("reason") or "")[:300],
                "result_excerpt": str(p.get("result_summary") or p.get("result") or "")[:1500],
                "invocation_id": p.get("invocation_id"),
                "normalized_args": str(p.get("normalized_args") or "")[:600],
                "result_fingerprint": str(p.get("result_fingerprint") or "")[:16],
                "progress_event": str(p.get("progress_event") or "")[:120],
                "canonical_target": str(p.get("canonical_target") or "")[:200],
                "workspace_epoch": int(p.get("workspace_epoch") or 0),
            })
        out["tool_calls"] = calls
        out["model_turns"] = sum(
            1 for e in events if e["type"] in ("run.model_call", "model.turn"))
        out["provider_errors"] = sum(
            1 for e in events if e["type"] == "provider.failure")
        out["terminal_kind"] = _terminal_kind(events)
        conv = _convergence(events)
        out["convergence"] = conv["convergence"]
        out["blocked_reasons"] = conv["blocked_reasons"]
        out["duration_s"] = round(time.monotonic() - started, 2)
        # 最终回答：从容器消息读（run_id 匹配 + 倒序 + assistant）
        try:
            container = rt.tasks.get_run_container_id(rid)
            msgs = rt.tasks.list_messages(container, limit=200) if container else []
            for m in reversed(msgs or []):
                if m.get("run_id") == rid and m.get("role") == "assistant":
                    out["finalText"] = m.get("content") or ""
                    out["reply_kind"] = ((m.get("meta") or {}).get("kind") or "")
                    break
        except Exception:
            pass
        out["state"] = out["final_state"]
        # 真值三件套（唯一事实源：事件表）
        started_events = sum(1 for c in calls if c["status"] == "executed")
        blocked_events = sum(1 for c in calls if c["status"] == "blocked")
        out["tool_truth"] = {
            "attempts": started_events + blocked_events,
            "blocked": blocked_events,
            "executions": started_events,
            "successes": started_events,
            "failures": 0,
            "budget_consumed": started_events,
        }
        out["_exec"] = {}
        out["_blocked"] = {}
        for c in calls:
            n = c["name"] or "?"
            if c["status"] == "executed":
                out["_exec"][n] = out["_exec"].get(n, 0) + 1
            elif c["status"] == "blocked":
                out["_blocked"][n] = out["_blocked"].get(n, 0) + 1
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        out["state"] = "harness_error"
        out["final_state"] = "harness_error"
    # 落盘（供 __main__.evaluate 直接读）
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / f"{case.id}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _set_recall_exclusion(prompts: list[str] | None) -> None:
    """按 prompt 设置召回期排除（留一法）。任何异常都静默。"""
    try:
        from runtime.episode import fingerprint
        from runtime.episode_store import set_recall_exclusion

        set_recall_exclusion({fingerprint(p) for p in prompts} if prompts else None)
    except Exception:
        pass


async def _run_all(rt, cases, *, deadline_s: float, db_dir: Path,
                   parallelism: int = 1, inject_episode: bool = False) -> list[dict]:
    rows = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case.id} …", flush=True)
        # 逐 case 留一：跑这道题时，记忆里不许出现**它自己**的既往执行。
        # 只排除当前这一道，其他 49 题的经验仍可见（那才是真正的迁移学习）。
        _set_recall_exclusion([case.prompt])
        try:
            out = await _run_case(rt, case, deadline_s=deadline_s, db_dir=db_dir,
                                  inject_episode=inject_episode)
        finally:
            _set_recall_exclusion(None)
        rows.append(out)
        print(f"  -> {case.id} state={out.get('state')} "
              f"exec={len(out.get('_exec') or {})} blocked={len(out.get('_blocked') or {})} "
              f"t={out.get('duration_s')}s", flush=True)
    return rows


def _apply_fixed_tools(fixed: bool) -> bool:
    """把 tool_router 固定成常量，消除注入块对工具可用集的污染（修法 1）。

    为什么必须固定
    --------------
    tool_router 第 1 条规则是「查询里出现工具名 → 必选」，而 episode 注入块会以
    「路径：a → b → c」的形式列出历史用过的工具名。于是注入组的模型看到的工具列表
    与基线组系统性不同 —— 实测 46/46 题都不同、平均多 9 个工具（最多 0→16），
    两组跑的其实不是同一套工具，A/B 差值无法归因于记忆层。
    见 delivery/probe_injection_confound.py。

    为什么改用中文别名脱敏不行
    --------------------------
    实测脱敏后 0/46 题恢复：中文别名又命中关键词分组（「运行测试」含"测试"→coding 族、
    「联网检索」含"检索"→web 族）。注入语义必然影响路由，只能做结构性隔离。
    见 delivery/probe_sanitize_fix.py。

    ``TOOL_ROUTER=off`` 时 select_tool_names 直接返回全量，返回值与 query 无关 ——
    这是结构性保证，不是碰巧。返回当前 router 是否已关闭。
    """
    if fixed:
        os.environ["TOOL_ROUTER"] = "off"
    try:
        from runtime.tool_router import router_enabled
        return not router_enabled()
    except Exception:
        return bool(fixed)


def _pin_workspace_root() -> str:
    """P1-1：把 WORKSPACE_ROOT 钉到项目根，让 benchmark_fixture 落入工作区子树。

    根因：生产 WORKSPACE_ROOT 默认 = 项目根的父目录（`F:\\Byong-hermes\\Byong-hermes`），
    而 fixture 在 `my_creative_agent\\benchmark_fixture` 下。case 文本里的
    绝对路径（`F:/Byong-hermes/Byong-hermes/my_creative_agent/benchmark_fixture/app`）
    相对**父目录**的 WORKSPACE_ROOT 是合法的，但模型被派发到的工具上下文根
    是**项目根**，于是首调越界 → "只能查看工作区…内的目录" → 模型盲翻触发死循环。

    评测时把 WORKSPACE_ROOT 显式钉到项目根，fixture 就落在子树内，
    首调直接命中真实目录，消除 ① 死循环的触发链。
    """
    project_root = Path(__file__).resolve().parent.parent
    os.environ["WORKSPACE_ROOT"] = str(project_root)
    return str(project_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.eval_runner")
    parser.add_argument("--cases", default="",
                        help="逗号分隔的 case id（默认跑 BENCHMARK_CASES 全部）")
    parser.add_argument("--all", action="store_true", help="显式跑全部 50")
    parser.add_argument("--deadline", type=float, default=300.0,
                        help="单 case 观察截止（秒），超过记 observed_state_at_deadline")
    parser.add_argument("--out", default="runs_eval", help="raw 记录输出目录")
    parser.add_argument("--db", default="",
                        help="显式指定 agent.db 路径；缺省用临时 DB（隔离生产 agent.db）")
    parser.add_argument("--inject-episode", action="store_true",
                        help="给每个 case 注入情节记忆（留一法，排除自身既往执行）")
    parser.add_argument("--no-seed-episodes", action="store_true",
                        help="不做 episode 种子注入（记忆层在临时库里为空）")
    parser.add_argument("--fixed-tools", action="store_true",
                        help="固定工具集（等效 TOOL_ROUTER=off，两组都用全量工具）。"
                             "消除『注入块里列出的工具名会改变工具可用性』这一混淆变量；"
                             "凡是跑注入类 A/B 都应开启，否则两组对比无效。")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # P1-1：先把工作区根钉到项目根，fixture 落入子树
    _ws_root = _pin_workspace_root()
    print(f"[workspace-root] WORKSPACE_ROOT={_ws_root}（P1-1 路径漂移修复）", flush=True)
    # 修法 1：把 tool_router 固定成常量，消除注入块对工具可用集的污染。
    # 注意：全量工具与生产配置（router 裁剪到 16）不同，结论不可直接外推。
    _fixed = _apply_fixed_tools(args.fixed_tools)

    from runtime.runner import AgentRuntime
    import main as _m  # noqa: F401

    if args.fixed_tools:
        print(f"[fixed-tools] TOOL_ROUTER=off，router 已关闭={_fixed}"
              "（两组工具集因此必然一致）", flush=True)
    elif args.inject_episode:
        print("[warn] --inject-episode 未配 --fixed-tools：注入块会列出历史工具名，"
              "撞中 router『提到工具名即必选』规则，两组工具集 46/46 都不同 —— "
              "本轮 A/B 差值无法归因于记忆层。", flush=True)

    if args.cases and not args.all:
        want = [c.strip() for c in args.cases.split(",") if c.strip()]
        cases = [get_case_by_id(c) for c in want]
    else:
        cases = list(BENCHMARK_CASES)

    db_dir = Path(args.out)
    db_path = args.db or os.path.join(tempfile.gettempdir(),
                                      f"forge_eval_{int(time.time())}.db")
    rt = AgentRuntime(db_path=db_path)
    rt._ensure()

    # 记忆层跟随本次运行的库（否则会去读生产 agent.db = 隔离泄漏 + 答案泄漏），
    # 再把生产库积累的经验**按留一法**搬进临时库，让 pull 方案的
    # recall_memory 真的有东西可查。
    seeded = 0
    try:
        from runtime.episode_store import EpisodeStore, set_default_db_path
        from runtime.task_manager import DEFAULT_DB_PATH as PROD_DB

        set_default_db_path(db_path)
        prod = Path(PROD_DB)
        if not args.no_seed_episodes and Path(db_path).resolve() != prod.resolve():
            # 全量注入：留一法在**召回期**逐 case 生效（见 _run_all），
            # 而不是在种子期排掉整个评测集 —— 后者会把 447 条砍到 112 条，
            # 典型查询召回为 0，实验直接失去灵敏度。
            seeded = EpisodeStore(db_path).copy_from(EpisodeStore(prod))
            print(f"[seed] episode {seeded} 条（留一法在召回期逐 case 生效）",
                  flush=True)
    except Exception as exc:  # 记忆是增益项，绝不能因为它拖垮评测
        print(f"[seed] 跳过（{type(exc).__name__}: {exc}）", flush=True)

    rows = asyncio.run(_run_all(rt, cases, deadline_s=args.deadline, db_dir=db_dir,
                                inject_episode=args.inject_episode))
    injected = sum(1 for r in rows if r.get("episode_injected"))
    (db_dir / "manifest.json").write_text(json.dumps({
        "label": "eval_runner",
        "n": len(rows),
        "db": db_path,
        "inject_episode": bool(args.inject_episode),
        "cases_with_injection": injected,
        "episodes_seeded": seeded,
        "episode_leave_one_out": "per_case",
        "fixed_tools": bool(args.fixed_tools),
        "tool_router": "off" if args.fixed_tools else "on",
        "model_pref": (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower() or "gateway",
        "rows": [r["id"] for r in rows],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(rows)} case -> {db_dir}/ ；下一步：python -m benchmark evaluate --runs {db_dir}")
    return 0


def get_case_by_id(cid: str):
    for c in BENCHMARK_CASES:
        if c.id == cid:
            return c
    raise SystemExit(f"unknown case id: {cid}")


if __name__ == "__main__":
    raise SystemExit(main())
