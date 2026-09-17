"""Phase 20：Decision Qualification Harness（checkpoint + live snapshot + L1）。

本模块只在 benchmark 层工作，不修改生产 runtime。

能力：
- 语义分类：区分 run_tests（首选）与 run_python 的真实 verification intent；
- DecisionCaptured sentinel：捕获第一项模型动作后主动停止 probe，不再用
  MaxTurnsExceeded 当作正常完成；
- 记录 tool_name / tool_capability / normalized_args / semantic_action_class；
- LIVE_SNAPSHOT：用真实 Full Agent run + RunHooks.on_llm_start 抓取
  “mutation 成功、verification_due=true、下一次模型调用尚未开始”的真实模型输入；
- Synthetic checkpoint 对照（Phase 19 口径）；
- L1 Structured Action Commitment（Self / Oracle VERIFY）两步实验；
- Checkpoint Fidelity 校验。

强制本地模型（FORGE_MODEL_PREF=local）；禁止 gateway/agnes。

用法示例：
    python -m benchmark.decision_qualification snapshot --out phase20 --cases M1,M4
    python -m benchmark.decision_qualification reclass --out phase20/reclass --n 10
    python -m benchmark.decision_qualification ab       --out phase20/ab --n 5
    python -m benchmark.decision_qualification baseline --out phase20/baseline --n 10
    python -m benchmark.decision_qualification l1       --out phase20/l1 --n 10
    python -m benchmark.decision_qualification controls --out phase20/controls --n 2
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from agents import RunHooks

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
FIX = str(ROOT / "micro_fixture")

CASES: dict[str, dict] = {
    "M1": {
        "prompt": f"修复 {FIX}/calc.py 中 add 函数结果错误的问题（应返回两数之和），"
                  f"并运行 {FIX} 下的测试确认通过。",
        "verification_due": True,
        "mutation": True,
    },
    "M4": {
        "prompt": f"修复 {FIX}/calc.py 的 add 函数，然后运行测试验证修改是否正确。",
        "verification_due": True,
        "mutation": True,
    },
    "M6": {
        "prompt": f"只阅读 {FIX}/calc.py，解释 add 函数的作用，不要修改任何文件。",
        "verification_due": False,
        "mutation": False,
    },
    "M7": {
        "prompt": f"修改 {FIX}/calc.py 的 add 函数，让它返回两数之和。",
        "verification_due": False,
        "mutation": True,
    },
    "M8": {
        "prompt": f"修改 {FIX}/README.md 中关于测试的说明文字，让它更清晰。",
        "verification_due": False,
        "mutation": True,
    },
}

FIXED_CONTENT = ("def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n")

# ---------------------------------------------------------------------------
# 语义分类
# ---------------------------------------------------------------------------

_SEMANTIC_TEST_TOKENS = (
    "pytest", "unittest", "unittest.main", "pytest.main", "nose", "tox",
    "def test", "test_", "_test", "assert ", "assert(", "assert ",
    "run_tests", "runtests", "test discovery", "doctest", "check_output",
    "subprocess.run", "subprocess.check", "sys.exit(", "all(",
)
_EXPLORE_TOKENS = ("print(", "listdir", "glob", "open(", "inspect.", "dir(")


def _args_dict(normalized_args: Any) -> dict:
    if isinstance(normalized_args, dict):
        return normalized_args
    if isinstance(normalized_args, str):
        try:
            v = json.loads(normalized_args)
            return v if isinstance(v, dict) else {"_raw": normalized_args}
        except Exception:
            return {"_raw": normalized_args}
    return {}


def _run_python_text(args: dict) -> str:
    return " ".join(str(args.get(k, "")) for k in ("code", "args", "filename", "target")).lower()


def classify_run_python(normalized_args: Any) -> str:
    """run_python 语义分类：SEMANTIC_VERIFICATION / VERIFICATION_PREPARATION /
    DISCOVERY / OTHER（只看真实 arguments，绝不只看 tool_name）。"""
    args = _args_dict(normalized_args)
    text = _run_python_text(args)
    if not text.strip():
        return "OTHER"
    if any(tok in text for tok in _SEMANTIC_TEST_TOKENS):
        return "SEMANTIC_VERIFICATION"
    if any(tok in text for tok in _EXPLORE_TOKENS):
        return "DISCOVERY"
    if any(tok in text for tok in ("import ", "from ", "load", "parse", "read")):
        return "VERIFICATION_PREPARATION"
    return "OTHER"


def semantic_action_class(tool_name: str | None, normalized_args: Any = None) -> str:
    """统一 semantic action class（用于 §十 记录与 §四 分类）。"""
    if not tool_name:
        return "NO_ACTION"
    if tool_name == "run_tests":
        return "SEMANTIC_VERIFICATION"
    if tool_name == "run_python":
        return classify_run_python(normalized_args)
    if tool_name in ("code_loop", "code_loop_tool"):
        return classify_run_python(normalized_args)
    from runtime.spec import capability_of

    cap = capability_of(tool_name)
    if cap == "VERIFICATION":
        return "SEMANTIC_VERIFICATION"
    if cap in ("DISCOVERY", "READ"):
        return "DISCOVERY"
    if cap == "MUTATION":
        return "REVISE"
    return "OTHER"


def preferred_verification(tool_name: str | None) -> bool:
    return tool_name == "run_tests"


def is_semantic_verification(tool_name: str | None, normalized_args: Any = None) -> bool:
    return semantic_action_class(tool_name, normalized_args) == "SEMANTIC_VERIFICATION"


# ---------------------------------------------------------------------------
# sentinel（benchmark-only，生产 runtime 永不看到）
# ---------------------------------------------------------------------------

class DecisionCaptured(BaseException):
    """probe 捕获第一项模型动作后主动停止。"""


class SnapshotCaptured(BaseException):
    """live snapshot 已抓取，主动停止真实 run。"""


class ProbeTimeout(BaseException):
    pass


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _jsonable(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "to_dict"):
        try:
            return _jsonable(obj.to_dict())
        except Exception:
            pass
    return str(obj)


def _sha(obj: Any) -> str:
    raw = json.dumps(_jsonable(obj), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _workspace_hash() -> str:
    """Phase 30/32：fixture workspace 的稳定 hash。

    排除易变缓存（__pycache__ / .pytest_cache / *.pyc），否则每次跑测试后
    hash 都会变化，导致 Full Replay Fidelity 误报。
    """
    h = hashlib.sha256()
    base = Path(FIX)
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(base)).replace("\\", "/")
        if ("__pycache__" in rel or ".pytest_cache" in rel
                or rel.endswith(".pyc")):
            continue
        h.update(rel.encode())
        try:
            h.update(p.read_bytes())
        except Exception:
            pass
    return h.hexdigest()[:16]


def _tool_schema_hashes(schemas: list[dict]) -> dict:
    """Phase 30：每个 tool 的 schema hash（用于 Tool Exposure Fidelity）。"""
    out = {}
    for s in schemas:
        payload = json.dumps({
            "name": s.get("name", ""),
            "params": s.get("params_json_schema") or {},
        }, ensure_ascii=False, sort_keys=True)
        out[s.get("name", "?")] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return out


def _provider_fingerprint() -> dict:
    """Phase 30/32：provider/model fingerprint（用于 Provider Fidelity）。"""
    return {
        "provider": (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower() or "unknown",
        "model": os.getenv("FORGE_LOCAL_MODEL_NAME", "") if
                 (os.getenv("FORGE_MODEL_PREF", "").strip().lower() == "local")
                 else os.getenv("AGENT_MODEL", ""),
        "base_url": os.getenv("FORGE_LOCAL_MODEL_BASE_URL", "")
                    if (os.getenv("FORGE_MODEL_PREF", "").strip().lower() == "local")
                    else os.getenv("OPENAI_BASE_URL", ""),
        "spark_config_hash": os.getenv("FORGE_SPARK_CONFIG_HASH", ""),
    }


def _required_scope(request_text: str) -> str:
    """Phase 30：从 request 推断 required verification scope。"""
    lower = (request_text or "").lower()
    if any(kw in lower for kw in ["只运行", "specific test", "单个测试", "test_calc"]):
        return "TARGET"
    return "PROJECT"


def _obligation_ledger(rc: Any) -> dict:
    """Phase 30：从 RunContext 提取 obligation ledger（用于 Obligation Fidelity）。"""
    if rc is None:
        return {}
    try:
        return rc.obligation_ledger()
    except Exception:
        try:
            t = rc._t()
            return {
                "mutation_seen": bool(t.mutation_seen),
                "evidence_epoch": int(t.evidence_epoch),
                "verified_revision": t.verified_revision,
                "verification_passed": bool(t.verification_passed),
                "verification_due": bool(rc.verification_due()),
            }
        except Exception:
            return {}


def _pending_approval_count() -> int:
    try:
        from runtime.approval import pending_count
        return int(pending_count())
    except Exception:
        return 0


def reset_fixture() -> None:
    base = Path(FIX)
    base.mkdir(parents=True, exist_ok=True)
    # Phase 33：M1/M4 fixture 只含 calc.py + test_calc.py + README.md；
    # 清理 M3 专用文件，保证 workspace_hash 与 M1/M4 快照一致、且 run_tests
    # 解析到本项目 fixture（FORGE_TRUSTED_CODE_ROOTS=micro_fixture）。
    for extra in ("helpers.py", "series.py"):
        try:
            (base / extra).unlink(missing_ok=True)
        except Exception:
            pass
    (base / "calc.py").write_text(
        "def add(a, b):\n    # BUG\n    return a - b\n\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8")
    (base / "test_calc.py").write_text(
        "from calc import add, multiply\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_multiply():\n    assert multiply(2, 3) == 6\n",
        encoding="utf-8")
    readme = base / "README.md"
    if not readme.exists():
        readme.write_text("# micro fixture\n\n测试说明：旧文案。\n", encoding="utf-8")


def _classify_error(exc: BaseException) -> tuple[str, str]:
    name = type(exc).__name__
    text = str(exc)
    low = (name + " " + text).lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in low:
        return "TIMEOUT", f"{name}: {text[:160]}"
    if any(m in low for m in ("connection", "connect", "refused", "provider",
                              "api", "401", "403", "429", "500", "502", "503",
                              "no available channel", "error code")):
        return "PROVIDER_ERROR", f"{name}: {text[:160]}"
    if any(m in low for m in ("json", "parse", "function", "tool call", "arguments",
                              "validationerror", "pydantic")):
        return "PARSER_ERROR", f"{name}: {text[:160]}"
    return "PROVIDER_ERROR", f"{name}: {text[:160]}"


def _tool_schemas(agent: Any) -> list[dict]:
    out = []
    for t in (getattr(agent, "tools", None) or []):
        out.append({
            "name": getattr(t, "name", ""),
            "description": getattr(t, "description", "") or "",
            "params_json_schema": getattr(t, "params_json_schema", None) or {},
        })
    return out


def _dummy_tools(schemas: list[dict]):
    from agents.tool import FunctionTool

    tools = []
    for s in schemas:
        async def _invoke(ctx, args_json, _n=s.get("name", "?")):
            return f"[probe-noop {_n}]"
        tools.append(FunctionTool(
            name=s.get("name", "?"),
            description=s.get("description", "") or f"probe tool {s.get('name')}",
            params_json_schema=s.get("params_json_schema") or {"type": "object", "properties": {}},
            on_invoke_tool=_invoke,
            strict_json_schema=False,
        ))
    return tools


def _mutation_result_ok(text: str) -> bool:
    """判断 mutation 工具结果是否代表真实成功（用于 checkpoint 有效性门槛）。"""
    t = str(text or "")
    bad = ("错误", "没有找到", "未找到", "失败", "not found", "error:", "exception")
    if any(b in t for b in bad):
        return False
    good = ("已写入", "已在", "替换", "已保存", "已创建", "已修改", "已更新", "已生成")
    return any(g in t for g in good)


def _verification_required(request_text: str) -> bool:
    from runtime.completion import OBLIGATION_REQUIRED, extract_obligations

    try:
        return extract_obligations(request_text or "")["verification"] == OBLIGATION_REQUIRED
    except Exception:
        return False


def _verification_satisfied(rc: Any) -> bool:
    if rc is None:
        return False
    try:
        t = rc._t()
        return bool(t.verification_passed and t.verified_revision is not None
                    and t.verified_revision == t.evidence_epoch)
    except Exception:
        return False


def _extract_function_calls(response: Any) -> list[dict]:
    calls: list[dict] = []
    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    for item in output or []:
        if isinstance(item, dict):
            if item.get("type") == "function_call":
                calls.append({"name": item.get("name") or "",
                              "arguments": item.get("arguments") or "{}",
                              "call_id": item.get("call_id") or ""})
        else:
            t = str(getattr(item, "type", "") or "")
            if "function_call" in t:
                calls.append({"name": getattr(item, "name", "") or "",
                              "arguments": getattr(item, "arguments", "") or "{}",
                              "call_id": getattr(item, "call_id", "") or ""})
    return calls


# ---------------------------------------------------------------------------
# Probe hooks：捕获第一项模型动作后主动停止
# ---------------------------------------------------------------------------

class ProbeHooks(RunHooks):
    """RunHooks 子类；捕获 on_llm_end 的 function_call 后抛 DecisionCaptured。"""

    def __init__(self) -> None:
        self.first_tool: str | None = None
        self.first_args: Any = None
        self.llm_calls = 0
        self.llm_started: float | None = None
        self.decision_at: float | None = None
        self.final_text: str | None = None
        self.tool_sequence: list[str] = []

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:  # type: ignore[override]
        self.llm_calls += 1
        self.llm_started = time.monotonic()

    async def on_llm_end(self, context, agent, response) -> None:  # type: ignore[override]
        self.decision_at = time.monotonic()
        calls = _extract_function_calls(response)
        if calls:
            self.first_tool = calls[0]["name"]
            self.first_args = calls[0]["arguments"]
            self.tool_sequence = [c["name"] for c in calls]
            raise DecisionCaptured()
        # no tool call → final text
        try:
            out = getattr(response, "output", None) or []
            texts = []
            for it in out:
                if isinstance(it, dict):
                    for c in (it.get("content") or []):
                        if isinstance(c, dict) and c.get("text"):
                            texts.append(c["text"])
                else:
                    for c in (getattr(it, "content", None) or []):
                        txt = getattr(c, "text", None)
                        if txt:
                            texts.append(txt)
            self.final_text = ("".join(texts))[:500]
        except Exception:
            self.final_text = None

    async def on_tool_start(self, context, agent, tool) -> None:  # type: ignore[override]
        # 正常情况下 on_llm_end 已抛 sentinel，不会走到这里。
        if self.first_tool is None:
            self.first_tool = str(getattr(tool, "name", "") or "")
            self.first_args = getattr(context, "tool_arguments", None)
            raise DecisionCaptured()

    async def on_tool_end(self, context, agent, tool, result) -> None:  # type: ignore[override]
        return None


def _probe_hooks() -> Any:
    return ProbeHooks()


# ---------------------------------------------------------------------------
# 单次 decision probe
# ---------------------------------------------------------------------------

async def run_decision_probe(agent: Any, input_items: Any, *, timeout: float = 240.0) -> dict:
    from agents import Runner
    import main as main_module
    from agent import local_model_provider

    hooks = _probe_hooks()
    t0 = time.monotonic()
    status = "NO_ACTION"
    error = None
    try:
        await asyncio.wait_for(
            Runner.run(agent, input=input_items,
                       run_config=main_module._run_config(provider=local_model_provider()),
                       hooks=hooks, max_turns=12),
            timeout=timeout,
        )
        status = "NO_ACTION"
    except DecisionCaptured:
        status = "DECISION_CAPTURED"
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        status, error = _classify_error(exc)
    latency = round(time.monotonic() - t0, 2)
    tool = hooks.first_tool
    args = _args_dict(hooks.first_args)
    cap = None
    if tool:
        from runtime.spec import capability_of
        cap = capability_of(tool)
    return {
        "status": status,
        "first_tool": tool,
        "normalized_args": args,
        "capability": cap,
        "semantic_action_class": semantic_action_class(tool, args),
        "semantic_verification": is_semantic_verification(tool, args),
        "preferred_verification": preferred_verification(tool),
        "tool_sequence": hooks.tool_sequence,
        "llm_calls": hooks.llm_calls,
        "decision_latency_s": latency,
        "final_text": hooks.final_text,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Synthetic checkpoint（Phase 19 口径）
# ---------------------------------------------------------------------------

async def _real_mutation(tool_name: str = "write_code_file",
                         project: str = "micro_fixture") -> tuple[str, str, str]:
    from agents.tool_context import ToolContext

    import code_exec

    args = {"project": project, "filename": "calc.py", "content": FIXED_CONTENT}
    args_json = json.dumps(args, ensure_ascii=False)
    ctx = ToolContext(context=None, tool_name=tool_name,
                      tool_call_id="ck-mut", tool_arguments=args_json)
    fn = getattr(code_exec, tool_name, None) or code_exec.write_code_file
    res = fn.on_invoke_tool(ctx, args_json)
    if asyncio.iscoroutine(res):
        res = await res
    return tool_name, args_json, str(res)


def _obligation_feedback(revision: int) -> str:
    return (
        "\n\n【执行义务 / Execution obligation】\n"
        "verification = REQUIRED\n"
        "status = UNSATISFIED\n"
        f"revision = {revision}\n"
        "在最终完成（final answer）之前，必须为本 revision 获得真实 verification 证据。"
        "请自行选择合适的验证工具（Runtime 不指定具体命令）。"
    )


async def build_synthetic_checkpoint(case_id: str) -> dict:
    from runtime.runner import AgentRuntime
    from runtime.runctx import RunContext, bind

    rt = AgentRuntime.get_default()
    rt._ensure()
    case = CASES[case_id]
    prompt = case["prompt"]
    agent = rt.route_agent(prompt, channel="chat")

    rc = RunContext(run_id=f"ck-{case_id}", request_text=prompt)
    items: list[dict] = [{"role": "user", "content": prompt}]
    if case["mutation"]:
        tool_name, tool_args, result = await _real_mutation()
        rc.note_progress(tool_name, json.loads(tool_args), result)
        rc.note_execution_identity(tool_name, json.loads(tool_args))
        rev = rc._t().evidence_epoch
        out = result + (_obligation_feedback(rev) if case["verification_due"] else "")
        items.append({"type": "function_call", "name": tool_name,
                      "arguments": tool_args, "call_id": "ck-mut"})
        items.append({"type": "function_call_output", "call_id": "ck-mut", "output": out})
    else:
        # 只读控制：模拟一次 read 结果
        items.append({"type": "function_call", "name": "read_code_file",
                      "arguments": json.dumps({"project": "micro_fixture",
                                               "filename": "calc.py"}, ensure_ascii=False),
                      "call_id": "ck-read"})
        items.append({"type": "function_call_output", "call_id": "ck-read",
                      "output": "def add(a, b):\n    # BUG\n    return a - b\n"})
        rc.note_progress("read_code_file", {"project": "micro_fixture", "filename": "calc.py"},
                         "read ok")
    bind(rc)
    due = rc.verification_due()
    return {
        "case": case_id,
        "snapshot_type": "SYNTHETIC_CHECKPOINT",
        "goal": prompt,
        "system_prompt": getattr(agent, "instructions", "") or "",
        "input_items": _jsonable(items),
        "tool_schemas": _tool_schemas(agent),
        "tool_names": [s["name"] for s in _tool_schemas(agent)],
        "runctx": rc.as_dict(),
        "revision": int(rc._t().evidence_epoch),
        "verification_due": bool(due),
        "workspace_hash": _workspace_hash(),
        "hashes": {
            "goal": _sha(prompt),
            "system_prompt": _sha(getattr(agent, "instructions", "") or ""),
            "message_history": _sha(items),
            "tool_definitions": _sha(_tool_schemas(agent)),
        },
    }


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------

class SnapshotHooks(ProbeHooks):
    def __init__(self, case_id: str) -> None:
        super().__init__()
        self.case_id = case_id
        self.snapshot: dict | None = None
        self.executed: list[dict] = []
        self.last_mutation_ok: bool | None = None
        self.last_mutation_tool: str | None = None
        self.last_mutation_result: str | None = None
        self.debug = os.getenv("P20_DEBUG", "").strip().lower() in ("on", "1", "true")

    async def on_tool_end(self, context, agent, tool, result) -> None:  # type: ignore[override]
        name = str(getattr(tool, "name", "") or "")
        self.executed.append({"tool": name, "result_head": str(result)[:120]})
        from runtime.readiness_gate import _P9_MUTATION_TOOLS

        if name in _P9_MUTATION_TOOLS:
            self.last_mutation_ok = _mutation_result_ok(str(result))
            self.last_mutation_tool = name
            self.last_mutation_result = str(result)
        if self.debug:
            print(f"    [debug] tool_end {name} -> {str(result)[:80]} "
                  f"(mut_ok={self.last_mutation_ok})", flush=True)

    async def on_tool_start(self, context, agent, tool) -> None:  # type: ignore[override]
        return None

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:  # type: ignore[override]
        self.llm_calls += 1
        self.llm_started = time.monotonic()
        from runtime.runctx import current as _cur

        rc = _cur()
        try:
            due = bool(rc.verification_due()) if rc is not None else False
            mutated = bool(rc._t().mutation_seen) if rc is not None else False
            rev = int(rc._t().evidence_epoch) if rc is not None else -1
        except Exception:
            due, mutated, rev = False, False, -1
        ok = bool(self.last_mutation_ok)
        if self.debug:
            print(f"    [debug] llm_start #{self.llm_calls} rc={rc is not None} "
                  f"mutated={mutated} mut_ok={self.last_mutation_ok} due={due} rev={rev} "
                  f"items={len(input_items or [])}", flush=True)
        if self.snapshot is not None or not (due and mutated and ok):
            return
        schemas = _tool_schemas(agent)
        try:
            t = rc._t()
            _ver_rev = t.verified_revision
            _ver_pass = t.verification_passed
            _evidence_epoch = t.evidence_epoch
        except Exception:
            _ver_rev = None
            _ver_pass = False
            _evidence_epoch = 0
        self.snapshot = {
            "case": self.case_id,
            "snapshot_type": "LIVE_SNAPSHOT_CHECKPOINT",
            "snapshot_schema_version": 2,
            "goal": (rc.request_text if rc is not None else "") or "",
            "system_prompt": system_prompt or getattr(agent, "instructions", "") or "",
            "input_items": _jsonable(input_items),
            "tool_schemas": schemas,
            "tool_names": [s["name"] for s in schemas],
            "tool_schema_hashes": _tool_schema_hashes(schemas),
            "runctx": (rc.as_dict() if rc is not None else {}),
            "revision": _evidence_epoch,
            "evidence_epoch": _evidence_epoch,
            "mutation_seen": mutated,
            "verification_required": _verification_required(
                rc.request_text if rc is not None else ""),
            "verification_satisfied": _verification_satisfied(rc),
            "verification_due": due,
            "verified_revision": _ver_rev,
            "verification_passed": bool(_ver_pass),
            "required_verification_scope": _required_scope(
                rc.request_text if rc is not None else ""),
            "obligation_ledger": _obligation_ledger(rc),
            "pending_user": bool(getattr(rc, "needs_user_input", False)) if rc is not None else False,
            "pending_approval": _pending_approval_count() > 0,
            "blocking_reason": "",
            "workspace_hash": _workspace_hash(),
            "provider_fingerprint": _provider_fingerprint(),
            "spark_config_hash": os.getenv("FORGE_SPARK_CONFIG_HASH", ""),
            "model_config": {
                "FORGE_MODEL_PREF": os.getenv("FORGE_MODEL_PREF", ""),
                "FORGE_LOCAL_MODEL_NAME": os.getenv("FORGE_LOCAL_MODEL_NAME", ""),
                "FORGE_LOCAL_MODEL_BASE_URL": os.getenv("FORGE_LOCAL_MODEL_BASE_URL", ""),
            },
            "executed_tools": list(self.executed),
            "mutation_success_proof": {
                "tool": self.last_mutation_tool,
                "result_ok": bool(self.last_mutation_ok),
                "result_head": (self.last_mutation_result or "")[:240],
                "marker": "SUCCESS_MARKER_MATCHED" if self.last_mutation_ok
                          else "NO_SUCCESS_MARKER",
            },
            "captured_llm_call_index": self.llm_calls,
            "captured_at": time.time(),
        }
        raise SnapshotCaptured()

    async def on_llm_end(self, context, agent, response) -> None:  # type: ignore[override]
        return None


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


async def capture_live_snapshot(case_id: str, *, attempts: int = 6,
                                wall_timeout: float = 600.0) -> dict | None:
    import tempfile

    import main as main_module
    from runtime.runner import AgentRuntime

    os.environ["FORGE_OBLIGATION_FEEDBACK"] = "on"
    os.environ["FORGE_OBLIGATION_GATE"] = "on"
    os.environ["FORGE_DECISION_HINT"] = "off"
    os.environ["FORGE_REDUNDANT_GUARD"] = "off"
    os.environ["FORGE_COMPLETION_READY"] = "off"

    tmp = tempfile.mkdtemp(prefix="p20_snap_")
    rt = AgentRuntime(db_path=os.path.join(tmp, "agent.db"))
    rt._ensure()
    prompt = CASES[case_id]["prompt"]
    for attempt in range(1, attempts + 1):
        reset_fixture()
        hooks = SnapshotHooks(case_id)
        real_runner = main_module.Runner
        main_module.Runner = _RunnerProxy(real_runner, hooks)  # type: ignore[assignment]
        result = None

        async def _drive() -> Any:
            res = await rt.run_turn(prompt, session_id=f"p20-snap-{case_id}-{attempt}",
                                    mode="async", max_turns=16)
            for _ in range(4):
                try:
                    st = res.task.state.value
                except Exception:
                    st = ""
                if st != "waiting_approval":
                    break
                approved = 0
                try:
                    for ap in rt.tasks.list_pending_approvals(res.task.id):
                        try:
                            rt.tasks.decide_approval(ap["id"], "approved")
                            approved += 1
                        except Exception:
                            pass
                except Exception:
                    pass
                if approved == 0:
                    break
                res = await rt.run_turn("", task_id=res.task.id, mode="async", max_turns=16)
            return res

        try:
            result = await asyncio.wait_for(_drive(), timeout=wall_timeout)
        except SnapshotCaptured:
            pass
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            print(f"  snapshot {case_id} attempt {attempt}: {type(exc).__name__}: {str(exc)[:160]}",
                  flush=True)
        finally:
            main_module.Runner = real_runner  # type: ignore[assignment]
        if hooks.snapshot is not None:
            snap = hooks.snapshot
            snap["hashes"] = {
                "goal": _sha(snap["goal"]),
                "system_prompt": _sha(snap["system_prompt"]),
                "message_history": _sha(snap["input_items"]),
                "tool_definitions": _sha(snap["tool_schemas"]),
            }
            snap["attempt"] = attempt
            return snap
        state = getattr(getattr(result, "task", None), "state", None)
        state = getattr(state, "value", state)
        print(f"  snapshot {case_id} attempt {attempt}: no post-mutation llm_start "
              f"(llm_calls={hooks.llm_calls} tools={[e['tool'] for e in hooks.executed]} "
              f"state={state})", flush=True)
    return None


def load_snapshot(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_replay_agent(snapshot: dict):
    from runtime.runner import AgentRuntime

    rt = AgentRuntime.get_default()
    rt._ensure()
    base = rt.route_agent(snapshot["goal"], channel="chat")
    tools = _dummy_tools(snapshot.get("tool_schemas") or [])
    return base.clone(tools=tools, instructions=snapshot.get("system_prompt") or None)


def validate_fidelity(snapshot: dict, agent: Any) -> dict:
    checks: dict[str, bool] = {}
    checks["goal_hash"] = _sha(snapshot.get("goal")) == (snapshot.get("hashes") or {}).get("goal")
    checks["system_prompt_hash"] = (
        _sha(snapshot.get("system_prompt")) == (snapshot.get("hashes") or {}).get("system_prompt"))
    checks["message_history_hash"] = (
        _sha(snapshot.get("input_items")) == (snapshot.get("hashes") or {}).get("message_history"))
    checks["tool_definitions_hash"] = (
        _sha(snapshot.get("tool_schemas")) == (snapshot.get("hashes") or {}).get("tool_definitions"))
    checks["revision_present"] = snapshot.get("revision") is not None
    checks["verification_obligation"] = bool(snapshot.get("verification_due"))
    checks["tool_exposure"] = bool(snapshot.get("tool_names"))
    checks["model_config_local"] = (
        (snapshot.get("model_config") or {}).get("FORGE_MODEL_PREF", "local") == "local")
    checks["workspace_hash"] = bool(snapshot.get("workspace_hash"))
    return checks


def checkpoint_validity(snapshot: dict) -> dict:
    """Phase 22：Checkpoint Validity Hard Invariant（§二）。

    只有全部成立才允许标 VALID_POST_MUTATION_CHECKPOINT；否则 INVALID_CHECKPOINT，
    禁止进入 behavior statistics。
    """
    proof = snapshot.get("mutation_success_proof") or {}
    rev = snapshot.get("revision")
    checks = {
        "mutation_tool_execution_success": bool(proof.get("result_ok")),
        "mutation_evidence_exists": bool(proof.get("tool")),
        "mutation_result_explicit_success": (
            proof.get("marker") == "SUCCESS_MARKER_MATCHED"),
        "evidence_epoch_incremented": bool(isinstance(rev, int) and rev > 0),
        "mutation_seen": bool(snapshot.get("mutation_seen")),
        "current_revision_gt_previous": bool(isinstance(rev, int) and rev >= 1),
        "verification_required": bool(snapshot.get("verification_required")),
        "verification_satisfied_false": not bool(snapshot.get("verification_satisfied")),
        "verification_due": bool(snapshot.get("verification_due")),
        "snapshot_type_live": snapshot.get("snapshot_type") == "LIVE_SNAPSHOT_CHECKPOINT",
    }
    status = "VALID_POST_MUTATION_CHECKPOINT" if all(checks.values()) else "INVALID_CHECKPOINT"
    return {"status": status, "checks": checks,
            "failed": [k for k, v in checks.items() if not v]}


def assert_checkpoint_valid(snapshot: dict) -> None:
    v = checkpoint_validity(snapshot)
    if v["status"] != "VALID_POST_MUTATION_CHECKPOINT":
        raise AssertionError(
            f"INVALID_CHECKPOINT {snapshot.get('case')}: failed={v['failed']}")


# ---------------------------------------------------------------------------
# L1 Structured Action Commitment
# ---------------------------------------------------------------------------

ACTION_CLASSES = ("VERIFY", "VERIFICATION_PREPARATION", "REVISE", "BLOCKED")

_L1_INSTRUCTION = (
    "\n\n【L1 Structured Action Commitment / 实验性】\n"
    "在调用任何工具之前，先只输出下一步的 action class，"
    "且只能从以下四者中选择一个：\n"
    "VERIFY / VERIFICATION_PREPARATION / REVISE / BLOCKED。\n"
    "verification_due=true 时禁止输出 FINAL。只输出这一个词，不要输出任何解释或其它内容。"
)


def parse_commitment(text: str | None) -> str | None:
    if not text:
        return None
    up = text.upper()
    found = []
    for cls in ACTION_CLASSES:
        idx = up.rfind(cls)
        if idx >= 0:
            found.append((idx, cls))
    if not found:
        return None
    return max(found)[1]


async def run_commitment_step(snapshot: dict, *, timeout: float = 180.0) -> dict:
    from agents import Agent, Runner
    import main as main_module
    from agent import local_model_name, local_model_provider

    agent = Agent(name="l1-commitment",
                  instructions=(snapshot.get("system_prompt") or "") + _L1_INSTRUCTION,
                  model=local_model_name(), tools=[])
    items = list(snapshot.get("input_items") or [])
    items.append({"role": "user",
                  "content": "[L1] 只输出下一步 action class（VERIFY / VERIFICATION_PREPARATION "
                             "/ REVISE / BLOCKED 之一）。"})
    t0 = time.monotonic()
    status = "NO_ACTION"
    text = None
    error = None
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, input=items,
                       run_config=main_module._run_config(provider=local_model_provider()),
                       max_turns=1),
            timeout=timeout,
        )
        text = str(getattr(result, "final_output", "") or "")
        status = "DECISION_CAPTURED"
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        status, error = _classify_error(exc)
    return {
        "status": status,
        "commitment_raw": (text or "")[:200],
        "commitment": parse_commitment(text),
        "commit_latency_s": round(time.monotonic() - t0, 2),
        "error": error,
    }


def _with_commitment(snapshot: dict, commitment: str) -> list[dict]:
    items = list(snapshot.get("input_items") or [])
    items.append({"role": "user",
                  "content": f"[Runtime Fact] Committed next action: {commitment}"})
    return items


def _follow_through(commitment: str | None, probe: dict) -> str:
    if commitment != "VERIFY":
        return "n/a"
    tool = probe.get("first_tool")
    if probe.get("preferred_verification"):
        return "honored"
    if probe.get("semantic_verification"):
        return "semantic_honored"
    return "violated"


async def run_l1_probe(snapshot: dict, mode: str) -> dict:
    """mode: self | oracle。self = 先自选 commitment；oracle = 固定 VERIFY。"""
    result: dict = {"condition": mode, "case": snapshot.get("case"),
                    "snapshot_type": snapshot.get("snapshot_type")}
    if mode == "self":
        step1 = await run_commitment_step(snapshot)
        result.update(step1)
        commitment = step1.get("commitment")
    else:
        result.update({"status": "ORACLE", "commitment": "VERIFY", "commitment_raw": "VERIFY",
                       "commit_latency_s": 0.0, "error": None})
        commitment = "VERIFY"
    agent = build_replay_agent(snapshot)
    items = _with_commitment(snapshot, commitment or "BLOCKED")
    probe = await run_decision_probe(agent, items)
    result.update(probe)
    result["follow_through"] = _follow_through(commitment, probe)
    result["total_latency_s"] = round(
        (result.get("commit_latency_s") or 0) + (probe.get("decision_latency_s") or 0), 2)
    return result


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def summarize_probes(rows: list[dict]) -> dict:
    n = len(rows)
    pref = sum(1 for r in rows if r.get("preferred_verification"))
    sem = sum(1 for r in rows if r.get("semantic_verification"))
    disc = sum(1 for r in rows if r.get("capability") in ("DISCOVERY", "READ"))
    mut = sum(1 for r in rows if r.get("capability") == "MUTATION")
    final = sum(1 for r in rows if r.get("status") == "NO_ACTION")
    statuses = Counter(r.get("status") for r in rows)
    lat = [r.get("decision_latency_s", 0) for r in rows]
    lat_sorted = sorted(lat)
    def pct(q):
        if not lat_sorted:
            return 0.0
        return lat_sorted[min(len(lat_sorted) - 1, int(round(q * (len(lat_sorted) - 1))))]
    return {
        "n": n,
        "preferred_run_tests": pref,
        "semantic_verification": sem,
        "discovery_or_read": disc,
        "mutation": mut,
        "no_action": final,
        "preferred_rate": _rate(pref, n),
        "semantic_rate": _rate(sem, n),
        "discovery_drift_rate": _rate(disc, n),
        "status_counts": dict(statuses),
        "latency_avg_s": round(sum(lat) / n, 2) if n else 0.0,
        "latency_p50_s": round(pct(0.5), 2),
        "latency_p95_s": round(pct(0.95), 2),
        "first_tools": dict(Counter(r.get("first_tool") for r in rows)),
        "semantic_classes": dict(Counter(r.get("semantic_action_class") for r in rows)),
    }


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

async def cmd_snapshot(out: Path, cases: list[str]) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    snaps: dict[str, dict] = {}
    for cid in cases:
        print(f"[snapshot] {cid} ...", flush=True)
        snap = await capture_live_snapshot(cid)
        if snap is None:
            print(f"[snapshot] {cid} FAILED (no snapshot)", flush=True)
            continue
        snaps[cid] = snap
        v = checkpoint_validity(snap)
        snap["checkpoint_validity"] = v
        (out / f"{cid}.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[snapshot] {cid} OK validity={v['status']} due={snap['verification_due']} "
              f"rev={snap['revision']} tools={len(snap['tool_names'])} "
              f"items={len(snap['input_items'])} "
              f"llm_call={snap['captured_llm_call_index']}", flush=True)
        if v["status"] != "VALID_POST_MUTATION_CHECKPOINT":
            print(f"[snapshot] {cid} INVALID: failed={v['failed']}", flush=True)
    return {"snapshots": list(snaps.keys())}


async def cmd_reclass(out: Path, n: int, cases: list[str]) -> dict:
    """Phase 19 口径重跑（synthetic），但记录真实 args + 语义分类。"""
    out.mkdir(parents=True, exist_ok=True)
    rows: dict[str, list[dict]] = {}
    for cid in cases:
        runs = []
        for i in range(n):
            reset_fixture()
            snap = await build_synthetic_checkpoint(cid)
            agent = build_replay_agent(snap)
            probe = await run_decision_probe(agent, snap["input_items"])
            probe.update({"case": cid, "i": i, "verification_due": snap["verification_due"],
                          "revision": snap["revision"]})
            runs.append(probe)
            print(f"  {cid} [{i+1}/{n}] {probe['status']} first={probe['first_tool']} "
                  f"class={probe['semantic_action_class']} args={json.dumps(probe['normalized_args'], ensure_ascii=False)[:120]} "
                  f"{probe['decision_latency_s']}s", flush=True)
        rows[cid] = runs
        (out / "partial.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    summary = {cid: summarize_probes(runs) for cid, runs in rows.items()}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


async def cmd_ab(out: Path, n: int, cases: list[str]) -> dict:
    """Synthetic vs Live A/B（每 checkpoint N>=5）。"""
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"synthetic": {}, "live": {}, "fidelity": {},
                              "exposure": {}}
    for cid in cases:
        live_path = out.parent / "snapshots" / f"{cid}.json"
        live = load_snapshot(live_path) if live_path.exists() else None
        reset_fixture()
        syn = await build_synthetic_checkpoint(cid)
        # 受控 A/B：合成 checkpoint 复用 live 的 system prompt + 工具暴露，
        # 从而把“message history 重建”与“工具暴露差异”分离开。
        if live is not None:
            result["exposure"][cid] = {
                "synthetic_tools": len(syn["tool_names"]),
                "live_tools": len(live["tool_names"]),
            }
            syn["system_prompt"] = live["system_prompt"]
            syn["tool_schemas"] = live["tool_schemas"]
            syn["tool_names"] = live["tool_names"]
            syn["hashes"] = {
                "goal": _sha(syn["goal"]),
                "system_prompt": _sha(syn["system_prompt"]),
                "message_history": _sha(syn["input_items"]),
                "tool_definitions": _sha(syn["tool_schemas"]),
            }
        syn_agent = build_replay_agent(syn)
        result["fidelity"].setdefault(cid, {})["synthetic"] = validate_fidelity(syn, syn_agent)
        syn_runs = []
        for i in range(n):
            p = await run_decision_probe(syn_agent, syn["input_items"])
            p["i"] = i
            syn_runs.append(p)
            print(f"  [ab/synthetic] {cid} [{i+1}/{n}] {p['status']} {p['first_tool']} "
                  f"{p['decision_latency_s']}s", flush=True)
        result["synthetic"][cid] = summarize_probes(syn_runs)
        (out / "partial.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    for cid in cases:
        path = out.parent / "snapshots" / f"{cid}.json"
        if not path.exists():
            path = out / f"{cid}.json"
        if not path.exists():
            print(f"  [ab/live] {cid} no snapshot, skip", flush=True)
            continue
        snap = load_snapshot(path)
        agent = build_replay_agent(snap)
        result["fidelity"].setdefault(cid, {})["live"] = validate_fidelity(snap, agent)
        live_runs = []
        for i in range(n):
            p = await run_decision_probe(agent, snap["input_items"])
            p["i"] = i
            live_runs.append(p)
            print(f"  [ab/live] {cid} [{i+1}/{n}] {p['status']} {p['first_tool']} "
                  f"{p['decision_latency_s']}s", flush=True)
        result["live"][cid] = summarize_probes(live_runs)
        (out / "partial.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    # checkpoint validity
    validity = {}
    for cid in cases:
        s = result["synthetic"].get(cid)
        l = result["live"].get(cid)
        if not s or not l:
            validity[cid] = "PARTIAL"
            continue
        d_pref = abs(s["preferred_rate"] - l["preferred_rate"])
        d_sem = abs(s["semantic_rate"] - l["semantic_rate"])
        d_disc = abs(s["discovery_drift_rate"] - l["discovery_drift_rate"])
        if max(d_pref, d_sem, d_disc) <= 0.30:
            validity[cid] = "PASS"
        elif max(d_pref, d_sem, d_disc) <= 0.50:
            validity[cid] = "PARTIAL"
        else:
            validity[cid] = "FAIL"
    result["checkpoint_validity"] = validity
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


async def cmd_baseline(out: Path, n: int, cases: list[str], snapdir: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rows: dict[str, list[dict]] = {}
    for cid in cases:
        path = snapdir / f"{cid}.json"
        if not path.exists():
            print(f"[baseline] {cid} missing snapshot at {path}", flush=True)
            continue
        snap = load_snapshot(path)
        agent = build_replay_agent(snap)
        runs = []
        for i in range(n):
            p = await run_decision_probe(agent, snap["input_items"])
            p.update({"i": i, "snapshot_type": snap["snapshot_type"]})
            runs.append(p)
            print(f"  [baseline] {cid} [{i+1}/{n}] {p['status']} first={p['first_tool']} "
                  f"class={p['semantic_action_class']} {p['decision_latency_s']}s", flush=True)
        rows[cid] = runs
        (out / "partial.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    summary = {cid: summarize_probes(runs) for cid, runs in rows.items()}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


async def cmd_l1(out: Path, n: int, cases: list[str], snapdir: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"self": {}, "oracle": {}}
    for cid in cases:
        path = snapdir / f"{cid}.json"
        if not path.exists():
            print(f"[l1] {cid} missing snapshot", flush=True)
            continue
        snap = load_snapshot(path)
        for mode in ("self", "oracle"):
            runs = []
            for i in range(n):
                r = await run_l1_probe(snap, mode)
                r["i"] = i
                runs.append(r)
                print(f"  [l1/{mode}] {cid} [{i+1}/{n}] commit={r.get('commitment')} "
                      f"first={r.get('first_tool')} follow={r.get('follow_through')} "
                      f"lat={r.get('total_latency_s')}s", flush=True)
            commits = Counter(r.get("commitment") for r in runs)
            verified = sum(1 for r in runs if r.get("semantic_verification"))
            pref = sum(1 for r in runs if r.get("preferred_verification"))
            ft_rows = [r for r in runs if r.get("commitment") == "VERIFY"]
            honored = sum(1 for r in ft_rows
                          if r.get("follow_through") in ("honored", "semantic_honored"))
            result[mode][cid] = {
                "n": len(runs),
                "commitments": dict(commits),
                "verify_commit_rate": _rate(commits.get("VERIFY", 0), len(runs)),
                "preferred_rate": _rate(pref, len(runs)),
                "semantic_rate": _rate(verified, len(runs)),
                "follow_through_rate": _rate(honored, len(ft_rows)),
                "follow_through_denom": len(ft_rows),
                "commit_latency_avg_s": round(
                    sum(r.get("commit_latency_s") or 0 for r in runs) / len(runs), 2) if runs else 0,
                "decision_latency_avg_s": round(
                    sum(r.get("decision_latency_s") or 0 for r in runs) / len(runs), 2) if runs else 0,
                "total_latency_avg_s": round(
                    sum(r.get("total_latency_s") or 0 for r in runs) / len(runs), 2) if runs else 0,
                "status_counts": dict(Counter(r.get("status") for r in runs)),
            }
            (out / "partial.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


async def cmd_controls(out: Path, n: int) -> dict:
    """M6/M7/M8：verification_due=false，确认无多余 verification。"""
    out.mkdir(parents=True, exist_ok=True)
    rows: dict[str, list[dict]] = {}
    for cid in ("M6", "M7", "M8"):
        reset_fixture()
        snap = await build_synthetic_checkpoint(cid)
        agent = build_replay_agent(snap)
        runs = []
        for i in range(n):
            p = await run_decision_probe(agent, snap["input_items"])
            p.update({"i": i, "verification_due": snap["verification_due"]})
            runs.append(p)
            print(f"  [control] {cid} [{i+1}/{n}] due={snap['verification_due']} "
                  f"first={p['first_tool']} sem={p['semantic_verification']}", flush=True)
        rows[cid] = runs
        (out / "partial.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    summary = {}
    for cid, runs in rows.items():
        summary[cid] = summarize_probes(runs)
        summary[cid]["unnecessary_verification"] = sum(
            1 for r in runs if r.get("semantic_verification"))
        summary[cid]["l1_activation"] = sum(1 for r in runs if r.get("verification_due"))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.decision_qualification")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--out", required=True)
    p_snap.add_argument("--cases", default="M1,M4")

    p_rc = sub.add_parser("reclass")
    p_rc.add_argument("--out", required=True)
    p_rc.add_argument("--n", type=int, default=10)
    p_rc.add_argument("--cases", default="M1,M4")

    p_ab = sub.add_parser("ab")
    p_ab.add_argument("--out", required=True)
    p_ab.add_argument("--n", type=int, default=5)
    p_ab.add_argument("--cases", default="M1,M4")

    p_bl = sub.add_parser("baseline")
    p_bl.add_argument("--out", required=True)
    p_bl.add_argument("--n", type=int, default=10)
    p_bl.add_argument("--cases", default="M1,M4")
    p_bl.add_argument("--snapdir", default="phase20/snapshots")

    p_l1 = sub.add_parser("l1")
    p_l1.add_argument("--out", required=True)
    p_l1.add_argument("--n", type=int, default=10)
    p_l1.add_argument("--cases", default="M1,M4")
    p_l1.add_argument("--snapdir", default="phase20/snapshots")

    p_ctl = sub.add_parser("controls")
    p_ctl.add_argument("--out", required=True)
    p_ctl.add_argument("--n", type=int, default=2)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import main as _main  # noqa: F401  确保加载 .env

    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref != "local":
        raise SystemExit(f"FORGE_MODEL_PREF={pref!r}；本轮禁止 gateway，必须为 local")

    cases = [c.strip() for c in getattr(args, "cases", "").split(",") if c.strip()]
    if args.cmd == "snapshot":
        asyncio.run(cmd_snapshot(Path(args.out), cases))
    elif args.cmd == "reclass":
        asyncio.run(cmd_reclass(Path(args.out), args.n, cases))
    elif args.cmd == "ab":
        asyncio.run(cmd_ab(Path(args.out), args.n, cases))
    elif args.cmd == "baseline":
        asyncio.run(cmd_baseline(Path(args.out), args.n, cases, Path(args.snapdir)))
    elif args.cmd == "l1":
        asyncio.run(cmd_l1(Path(args.out), args.n, cases, Path(args.snapdir)))
    elif args.cmd == "controls":
        asyncio.run(cmd_controls(Path(args.out), args.n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
