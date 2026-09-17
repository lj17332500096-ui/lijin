"""Phase 33: M3 Closed-Loop Benchmark (fixture + observation + metrics only).

No repair policy / no guidance / no prompt control. The real Runtime
(Runner, Approval, Mutation Truth, Verification Scope, Obligation Gate,
Completion Gate, Repair Semantics, No-Progress, Terminalization) drives
everything; this module only selects the fixture, observes, and measures.

M3 fixture (qualified by tests/m3_fixture_qualification.py):
  - stage-1 defect: calc.py::add returns a-b        (fixable by inspection)
  - stage-2 defect: series.py::series_sum off-by-one (only revealed at
    TEST RUNTIME via a hidden oracle; not readable by the model)

Repair Boundary Established iff:
  >=1 COMMITTED mutation
  AND a PROJECT verification executed
  AND verification_result = FAIL
  AND the failure evidence appears in a LATER model input (injection proof)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402
from agents import RunHooks  # noqa: E402

M3_FIX = r"F:\Byong-hermes\Byong-hermes\code_sandbox\m3_fixture"
M3_ORACLE = r"F:\Byong-hermes\Byong-hermes\my_creative_agent\m3_oracle\expected.json"
M3_PROMPT = (
    "修复 {fix}/calc.py 中的 bug，并运行 {fix} 项目下的全部测试；"
    "如果测试失败，继续修复直到全部通过。"
)

_M3_CALC = "def add(a, b):\n    return a - b\n"
_M3_SERIES = ("def series_sum(n):\n    total = 0\n"
              "    for i in range(n):\n        total += i\n"
              "    return total\n")
_M3_TEST = (
    "import json\n\n"
    "from calc import add\n"
    "from series import series_sum\n\n"
    f'_ORACLE = r"{M3_ORACLE}"\n\n\n'
    "def test_add():\n"
    "    assert add(1, 2) == 3\n\n\n"
    "def test_series_sum():\n"
    '    cases = json.load(open(_ORACLE, encoding="utf-8"))\n'
    '    for c in cases["series_sum"]:\n'
    '        assert series_sum(c["n"]) == c["total"]\n'
)


def reset_m3_fixture() -> None:
    base = Path(M3_FIX)
    base.mkdir(parents=True, exist_ok=True)
    (base / "calc.py").write_text(_M3_CALC, encoding="utf-8")
    (base / "series.py").write_text(_M3_SERIES, encoding="utf-8")
    (base / "test_calc.py").write_text(_M3_TEST, encoding="utf-8")


# ---------------------------------------------------------------------------
# Observation hooks
# ---------------------------------------------------------------------------

def _flatten(items: Any) -> str:
    try:
        return json.dumps(items, ensure_ascii=False, default=str)
    except Exception:
        return str(items)


class M3Hooks(RunHooks):
    def __init__(self) -> None:
        self.llm_inputs: list[dict] = []
        self.tool_calls: list[dict] = []
        self.llm_calls = 0

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:  # type: ignore[override]
        self.llm_calls += 1
        self.llm_inputs.append({"idx": self.llm_calls, "text": _flatten(input_items)})

    async def on_llm_end(self, context, agent, response) -> None:  # type: ignore[override]
        return None

    async def on_tool_start(self, context, agent, tool) -> None:  # type: ignore[override]
        return None

    async def on_tool_end(self, context, agent, tool, result) -> None:  # type: ignore[override]
        # 并发安全：不依赖跨回调的临时状态；直接用 tool/context 参数
        args = ""
        try:
            args = str(getattr(context, "tool_arguments", "") or "")
        except Exception:
            args = ""
        self.tool_calls.append({"name": str(getattr(tool, "name", "")),
                                "args": args, "result": str(result)})


class _RunnerProxy:
    def __init__(self, real: Any, hooks: Any) -> None:
        self._real = real
        self._hooks = hooks

    async def run(self, *a, **k):
        k.setdefault("hooks", self._hooks)
        return await self._real.run(*a, **k)

    def run_sync(self, *a, **k):
        k.setdefault("hooks", self._hooks)
        return self._real.run_sync(*a, **k)

    def run_streamed(self, *a, **k):
        k.setdefault("hooks", self._hooks)
        return self._real.run_streamed(*a, **k)


# ---------------------------------------------------------------------------
# Parsing helpers (deterministic)
# ---------------------------------------------------------------------------

def _v_passed(text: str) -> bool | None:
    """Parse verification result. Returns True/False/None (unknown).

    Authority order (consistent with runtime.completion.verification_outcome_of):
    1. Explicit fail marker "未通过" → False
    2. Explicit pass marker "✅ 通过" → True
    3. Exit code pattern "退出码: N" / "退出码：N" → 0 = True, non-zero = False
    4. Pytest summary "\d+ failed" → False
    5. Pytest summary "\d+ passed" (with no failed) → True
    6. Otherwise → None (unknown, do not guess)
    """
    t = str(text or "")
    # 1) explicit fail
    if "未通过" in t:
        return False
    # 2) explicit pass
    if "✅ 通过" in t:
        return True
    # 3) exit code – last occurrence wins
    import re as _re
    codes = _re.findall(r"退出码\s*[:：]\s*(\d+)", t)
    if codes:
        last = int(codes[-1])
        return last == 0
    # 4) pytest "N failed" or "FAILED test_name"
    m = _re.search(r"(\d+)\s+failed", t)
    if m and int(m.group(1)) > 0:
        return False
    if _re.search(r"\bFAILED\b", t):
        return False
    # 5) pytest "N passed" (only count if no failed above)
    m = _re.search(r"(\d+)\s+passed", t)
    if m and int(m.group(1)) > 0:
        return True
    return None


_M3_TEST_FILES = {"test_calc.py"}


def _scope_of(tool_name: str, args_str: Any) -> str:
    try:
        args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if tool_name == "run_tests":
        tgt = str(args.get("target") or "").strip()
        if not tgt:
            return "PROJECT"
        base = tgt.replace("\\", "/").split("/")[-1]
        # 单模块 fixture：命中全部测试文件的 target == 全项目覆盖
        if base in _M3_TEST_FILES:
            return "PROJECT"
        return f"TARGET({tgt})"
    if tool_name in ("run_python", "code_loop"):
        return "TARGET"
    return "UNKNOWN"


def _mut_committed(name: str, args_str: Any, result: str) -> bool:
    from runtime.runner import _mutation_result_ok
    from runtime.readiness_gate import _P9_MUTATION_TOOLS
    if name not in _P9_MUTATION_TOOLS:
        return False
    return _mutation_result_ok(name, result) == "COMMITTED"


def _mut_target(name: str, args_str: Any) -> str:
    try:
        args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    for k in ("path", "file", "filename", "project"):
        v = args.get(k)
        if v:
            return str(v).replace("\\", "/").split("/")[-1]
    return ""


def _failure_fingerprint(text: str) -> str:
    """失败证据指纹：优先选取不含引号的 assert 行（JSON 序列化后仍可原样匹配）。"""
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    for s in lines:
        if "assert" in s and "==" in s and '"' not in s and "'" not in s:
            return s[:120]
    for s in lines:
        if "assert" in s and "==" in s:
            return s.replace('"', "").replace("'", "")[:120]
    m = re.search(r"(test_\w+)\s+FAILED", str(text or ""))
    if m:
        return m.group(0)
    m = re.search(r"(\d+)\s+failed", str(text or ""))
    if m:
        return m.group(0)
    # 事件摘要被截断时的兜底：真实失败执行的退出码标记
    if "退出码: 1" in str(text or "") or "退出码：1" in str(text or ""):
        return "退出码: 1"
    return ""


#: deterministic failure→module mapping for the M3 fixture
_FUNC_TO_MODULE = {"add": "calc.py", "series_sum": "series.py"}


def _failed_test_module(fail_text: str) -> str:
    """从 pytest 输出确定失败测试对应的源码模块（确定性映射）。"""
    m = re.search(r"test_calc\.py::(\w+)", str(fail_text or ""))
    if not m:
        return ""
    test = m.group(1)
    func = test[len("test_"):] if test.startswith("test_") else test
    return _FUNC_TO_MODULE.get(func, "")


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _v_is_true(text: str) -> bool:
    """Strict pass detector: only True when _v_passed explicitly returns True.
    None (unknown) and False are both treated as 'not pass'."""
    return _v_passed(text) is True


def _v_is_fail(text: str) -> bool:
    """Strict fail detector: only True when _v_passed explicitly returns False."""
    return _v_passed(text) is False


def analyze(hooks: M3Hooks, events: list, state: str, error: str | None) -> dict:
    """Build revision timeline + repair-boundary + injection + attribution."""
    # Revision timeline from events (has workspace_epoch)
    steps: list[dict] = []
    muts: list[dict] = []
    vers: list[dict] = []
    for e in events:
        if getattr(e, "event_type", None) != "tool.invocation":
            continue
        p = e.payload or {}
        if p.get("blocked"):
            continue
        name = p.get("tool_name") or ""
        cap = p.get("tool_capability")
        epoch = int(p.get("workspace_epoch") or 0)
        args_s = str(p.get("normalized_args") or "")
        step = {"tool": name, "capability": cap, "revision_before": epoch,
                "result": str(p.get("result_summary") or "")[:200]}
        if cap == "MUTATION":
            committed = _mut_committed(name, args_s, step["result"])
            step["committed"] = committed
            step["revision_after"] = epoch + 1 if committed else epoch
            if committed:
                muts.append({"tool": name, "revision_after": epoch + 1,
                             "target": _mut_target(name, args_s)})
        elif cap == "VERIFICATION":
            sc = _scope_of(name, args_s)
            passed = _v_passed(step["result"])
            step["scope"] = sc
            step["passed"] = passed
            vers.append({"tool": name, "revision": epoch, "scope": sc,
                         "passed": passed, "result": step["result"]})
        steps.append(step)

    # Hooks give full tool results (aligned ordering)
    hook_vers = [tc for tc in hooks.tool_calls
                 if str(tc.get("name")) in ("run_tests", "run_python", "code_loop")]

    # Repair boundary: COMMITTED mutation -> PROJECT verification FAIL ->
    # failure evidence observed in a LATER model input.
    project_vers = [v for v in vers if v["scope"] == "PROJECT"]
    # Only treat explicitly-failed verifications as boundary trigger
    fail_v = next((v for v in project_vers if _v_is_fail(v.get("result") or "")), None)
    boundary = bool(muts) and fail_v is not None

    # Failure evidence injection proof（使用完整 hook 结果；排除审批/拒绝文案）
    fp = ""
    injected = False
    failed_module = ""
    fail_text = ""
    _BLOCK_MARKS = ("需要审批", "仍在等待", "已被拒绝", "已拒绝", "拒绝执行", "DENIED")
    if fail_v is not None:
        for tc in hook_vers:
            res = str(tc.get("result") or "")
            if any(m in res for m in _BLOCK_MARKS):
                continue
            sc = _scope_of(str(tc.get("name")), tc.get("args"))
            if sc == "PROJECT" and _v_is_fail(res):
                fail_text = res
                break
        if not fail_text:
            fail_text = str(fail_v.get("result") or "")
        fp = _failure_fingerprint(fail_text)
        failed_module = _failed_test_module(fail_text)
        if fp:
            injected = any(fp in li["text"] for li in hooks.llm_inputs)

    # Repair: a COMMITTED mutation AFTER the failed revision
    repair = None
    if fail_v is not None:
        repair = next((m for m in muts if m["revision_after"] > fail_v["revision"]),
                      None)

    # Reverification: PROJECT PASS at >= repair revision, latest revision
    reverify = None
    latest_rev = max([m["revision_after"] for m in muts], default=0)
    if repair is not None:
        reverify = next((v for v in project_vers
                         if _v_is_true(v.get("result") or "")
                         and v["revision"] >= repair["revision_after"]),
                        None)

    boundary_established = bool(boundary and injected)

    # Attribution: repair target maps to the failing module (deterministic)
    attribution = None
    if repair is not None and fail_v is not None:
        attribution = {
            "repair_target": repair["target"],
            "expected_target": failed_module,
            "matches": bool(failed_module) and repair["target"] == failed_module,
        }

    # Completion eligibility / task success
    task_success = (state == "completed")
    false_completion = bool(state == "completed" and reverify is None)

    repair_loop_success = bool(
        boundary_established and repair is not None and reverify is not None
        and reverify["revision"] == latest_rev
        and task_success)

    # Failure class
    if error:
        fclass = "PROVIDER_FAILURE"
    elif not muts:
        fclass = "NO_INITIAL_MUTATION"
    elif fail_v is None and vers and all(_v_is_true(v.get("result") or "") for v in vers):
        fclass = "INITIAL_VERIFICATION_UNEXPECTED_PASS"
    elif fail_v is None:
        fclass = "REPAIR_BOUNDARY_NOT_REACHED"
    elif not injected:
        fclass = "FAILURE_NOT_INJECTED"
    elif repair is None:
        fclass = "FAILURE_NOT_USED_FOR_REPAIR"
    elif reverify is None:
        fclass = "REVERIFY_NOT_ATTEMPTED_OR_FAILED"
    elif reverify["revision"] != latest_rev:
        fclass = "LATEST_REVISION_MISMATCH"
    elif not task_success:
        fclass = "PREMATURE_COMPLETION"
    else:
        fclass = ""

    return {
        "steps": steps,
        "mutations": [m["revision_after"] for m in muts],
        "mutation_targets": [m["target"] for m in muts],
        "verifications": [(v["revision"], v["scope"], v.get("passed")) for v in vers],
        "repair_boundary_established": boundary,
        "failure_evidence_injected": injected,
        "failure_fingerprint": fp,
        "failure_text_len": len(fail_text),
        "repair": repair,
        "reverify": reverify,
        "latest_revision": latest_rev,
        "repair_attribution": attribution,
        "task_success": task_success,
        "repair_loop_success": repair_loop_success,
        "false_completion": false_completion,
        "failure_class": fclass,
    }


async def run_m3_case(prompt: str, i: int, max_turns: int) -> dict:
    import tempfile

    from runtime.runner import AgentRuntime

    reset_m3_fixture()
    tmp = tempfile.mkdtemp(prefix="m3_")
    rt = AgentRuntime(db_path=str(Path(tmp) / "agent.db"))
    rt._ensure()  # 必须先初始化，否则 rt.tasks 为 None，WorkLocation 绑定静默失败

    os.environ["FORGE_OBLIGATION_GATE"] = "on"
    os.environ["FORGE_OBLIGATION_FEEDBACK"] = "on"
    os.environ["FORGE_REDUNDANT_GUARD"] = "off"
    os.environ["FORGE_COMPLETION_READY"] = "on"   # Phase 35：production parity，启用 Completion Ready Guard
    os.environ["FORGE_VERIFICATION_FOCUS"] = "off"
    os.environ["FORGE_DECISION_HINT"] = "off"

    sid = f"proj-m3-{i}"
    try:
        wl = rt.tasks.create_work_location(f"m3-wl-{i}", local_path=M3_FIX)
        container = rt.tasks.get_or_create_container(sid)
        rt.tasks.update_project(container["id"], work_location_id=wl["id"])
    except Exception:
        pass

    hooks = M3Hooks()
    real_runner = main_module.Runner
    main_module.Runner = _RunnerProxy(real_runner, hooks)  # type: ignore[assignment]
    t0 = time.monotonic()
    state = "error"
    err = None
    try:
        res = await rt.run_turn(prompt, session_id=sid, mode="async",
                                max_turns=max_turns)
        for _ in range(15):
            if res.task.state.value != "waiting_approval":
                break
            approved = 0
            for ap in rt.tasks.list_pending_approvals(res.task.id):
                tool = str(ap.get("tool_name") or "")
                # 用户策略：批准 run_tests（验证）；拒绝 run_python/code_loop
                # （防止绕过 mutation 追踪 + 防止读取 fixture 外的 oracle）。
                decision = "denied" if tool in ("run_python", "code_loop") else "approved"
                try:
                    rt.tasks.decide_approval(ap["id"], decision)
                    approved += 1
                except Exception:
                    pass
            if approved == 0:
                break
            # 与生产一致：resume 使用原始 goal（main.py/webapp.py 语义）
            res = await rt.run_turn(prompt, task_id=res.task.id, mode="async",
                                    max_turns=max_turns)
        state = res.task.state.value
        events = list(rt.tasks.list_events(res.task.id, limit=4000))
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        err = f"{type(exc).__name__}: {str(exc)[:160]}"
        events = []
    finally:
        main_module.Runner = real_runner  # type: ignore[assignment]

    latency = round(time.monotonic() - t0, 2)
    a = analyze(hooks, events, state, err)
    out = {"i": i, "state": state, "error": err, "latency_s": latency,
           "llm_calls": hooks.llm_calls, "tool_calls": len(hooks.tool_calls),
            "tool_log": [{"tool": t.get("name"),
                          "scope": _scope_of(str(t.get("name")), t.get("args")),
                          "ok": _v_passed(str(t.get("result")))
                          if str(t.get("name")) in ("run_tests", "run_python", "code_loop")
                          else None,
                          "result_head": str(t.get("result"))[:120]}
                         for t in hooks.tool_calls]}
    out.update(a)
    return out


async def cmd_run_m3(out: Path, n: int, max_turns: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    prompt = M3_PROMPT.format(fix=M3_FIX.replace("\\", "/"))
    runs = []
    for i in range(n):
        r = await run_m3_case(prompt, i, max_turns)
        runs.append(r)
        print(f"  [M3] [{i+1}/{n}] state={r['state']} task={r['task_success']} "
              f"loop={r['repair_loop_success']} boundary={r['repair_boundary_established']} "
              f"injected={r['failure_evidence_injected']} "
              f"muts={r['mutations']} vers={r['verifications']} "
              f"class={r['failure_class']} lat={r['latency_s']}s", flush=True)
        (out / "partial.json").write_text(
            json.dumps({"M3": {"runs": runs}}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    total = len(runs)
    exec_valid = sum(1 for r in runs if not r["error"])
    boundary_valid = sum(1 for r in runs if r["repair_boundary_established"])
    loop_success = sum(1 for r in runs if r["repair_loop_success"])
    task_success = sum(1 for r in runs if r["task_success"])
    fclasses: dict = {}
    for r in runs:
        if r["failure_class"]:
            fclasses[r["failure_class"]] = fclasses.get(r["failure_class"], 0) + 1
    summary = {
        "M3": {
            "requested": total,
            "execution_valid": exec_valid,
            "provider_invalid": total - exec_valid,
            "repair_boundary_valid": boundary_valid,
            "task_success": task_success,
            "repair_loop_success": loop_success,
            "false_completion": sum(1 for r in runs if r["false_completion"]),
            "failure_classes": fclasses,
            "latency_avg_s": round(sum(r["latency_s"] for r in runs) / total, 2)
            if total else 0,
            "runs": runs,
        }
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary["M3"].items() if k != "runs"},
                     ensure_ascii=False, indent=2))
    return summary
