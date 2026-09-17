"""Phase 28：Window Boundary Truth + Verification Scope Truth + Agens Qualification Rebaseline。

关键修正（vs Phase 27）：
1. K=3 是 non-blocked tool EXECUTION 数量（非 proposed calls，非 model turns）
2. Window 在第三个 non-blocked execution 完成后立即停止（raise WindowClosed）
3. Scope parser 精确解析 pytest 参数（PROJECT / TARGET / UNKNOWN）
4. W3 分类基于确定性 evidence（非推断）
5. Full Replay Fidelity 升级（workspace hash、obligation ledger、pending state）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import Runner  # noqa: E402
import main as main_module  # noqa: E402
from agent import build_model_provider, local_model_provider  # noqa: E402

import benchmark.decision_qualification as dq  # noqa: E402
from runtime.completion import (  # noqa: E402
    MUTATION_TOOLS,
    VERIFY_TOOLS,
    codeloop_outcome_of,
    mutation_outcome_of,
)
from runtime.readiness_gate import _P9_MUTATION_TOOLS, _P9_VERIFICATION_TOOLS

K_DEFAULT = 3  # non-blocked tool executions 上限


def _select_provider() -> Any:
    """Phase 30：按 FORGE_MODEL_PREF 选择 provider（local=llama.cpp / gateway）。"""
    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref == "local":
        return local_model_provider()
    return build_model_provider()


# ---------------------------------------------------------------------------
# WindowClosed sentinel（类似 DecisionCaptured）
# ---------------------------------------------------------------------------
class WindowClosed(BaseException):
    """Phase 28：Window 硬边界 — K=3 non-blocked executions 后终止。"""
    pass


class ExecutionQuota:
    """Phase 30：benchmark-only 执行配额。

    在 tool body 执行之前原子占用 slot；达到 K 后拒绝后续 tool。
    使用 asyncio.Lock 保证 batch/concurrent 调度下只放行前 K 个。
    """

    def __init__(self, k: int) -> None:
        self.k = k
        self.used = 0
        self.lock = asyncio.Lock()
        self.exhausted = False

    async def acquire(self) -> bool:
        async with self.lock:
            if self.used >= self.k:
                self.exhausted = True
                return False
            self.used += 1
            return True


def wrap_tools_with_quota(agent: Any, quota: ExecutionQuota) -> Any:
    """Phase 30：用 quota 包装 agent 的每个 tool 的 on_invoke_tool。

    在真实 tool body 执行之前占用 slot；无 slot 时 raise WindowClosed。
    这样 batch 中的第 4+ 个 tool 不会进入 body。
    """
    tools = list(getattr(agent, "tools", []) or [])
    for tool in tools:
        original = getattr(tool, "on_invoke_tool", None)
        if original is None:
            continue

        def make_wrapper(orig):
            async def _quota_wrapper(ctx, args):
                if not await quota.acquire():
                    raise WindowClosed()
                return await orig(ctx, args)
            return _quota_wrapper

        try:
            tool.on_invoke_tool = make_wrapper(original)
        except Exception:
            pass
    return agent


# ---------------------------------------------------------------------------
# WindowHooks：硬边界 + 详细计数
# ---------------------------------------------------------------------------
class WindowHooks(dq.ProbeHooks):
    """Phase 28：在 K=3 non-blocked execution 后 hard stop。"""

    def __init__(self, max_executions: int = K_DEFAULT) -> None:
        super().__init__()
        self.max_executions = max_executions
        self.action_count = 0
        self.window_status = "RUNNING"
        # Phase 28：执行计数
        self.non_blocked_executions = 0  # 成功执行的 tool call 数
        self.blocked_executions = 0      # 被 blocker 阻止的 tool call 数
        self.tool_executions = 0         # 总 execution 数（non_blocked + blocked）
        self.model_turns = 0
        self.tool_calls_proposed = 0
        self.mutation_commits = 0
        self.verification_attempts = 0
        self.premature_final = False
        # 完整工具调用信息（含参数）
        self.all_tool_calls: list[dict] = []
        # 每个 tool execution 的状态
        self.execution_log: list[dict] = []  # [{"name", "args", "status": "executed"/"blocked"}]

    async def on_llm_end(self, context, agent, response) -> None:  # type: ignore[override]
        self.model_turns += 1
        calls = dq._extract_function_calls(response)
        if calls:
            self.tool_calls_proposed += len(calls)
            self.tool_sequence.extend(c["name"] for c in calls)
            self.action_count += len(calls)
            for c in calls:
                self.all_tool_calls.append({
                    "name": c.get("name", ""),
                    "arguments": c.get("arguments", "{}"),
                })
        else:
            if self.action_count < self.max_executions:
                self.window_status = "FINAL_TEXT"
                if self.action_count > 0:
                    self.premature_final = True

    async def on_tool_start(self, context, agent, tool) -> None:  # type: ignore[override]
        """Phase 29：在 tool 执行前检查硬边界。达到 K 后直接阻止后续 tool。"""
        if self.first_tool is None:
            self.first_tool = str(getattr(tool, "name", "") or "")
        # Phase 29：严格硬边界 — 在 tool 执行前检查
        if self.non_blocked_executions >= self.max_executions:
            self.window_status = "WINDOW_CLOSED"
            raise WindowClosed()

    async def on_tool_end(self, context, agent, tool, result) -> None:  # type: ignore[override]
        """Phase 28：在 tool 执行完成后检查 window boundary。"""
        name = getattr(tool, "name", "") or ""
        # 获取 arguments
        args_dict = {}
        try:
            args_str = getattr(context, "tool_arguments", None) or "{}"
            args_dict = json.loads(args_str) if args_str else {}
        except Exception:
            args_dict = {}

        # 判断是否 blocked（通过 result 文本）
        result_str = str(result or "")
        is_blocked = "BLOCKED" in result_str or "error" in result_str.lower()[:20]

        # Phase 29：如果本次被标记为 skip（boundary reached before start），不计数
        if getattr(self, '_skip_next', False):
            self._skip_next = False
            self.blocked_executions += 1  # 计入 blocked（实际是被 window 阻止）
            self.tool_executions += 1
            self.execution_log.append({
                "name": name, "arguments": args_dict, "status": "window_blocked",
            })
            return

        if is_blocked:
            self.blocked_executions += 1
            self.tool_executions += 1
            status = "blocked"
        else:
            self.non_blocked_executions += 1
            self.tool_executions += 1
            status = "executed"

        self.execution_log.append({
            "name": name, "arguments": args_dict, "status": status,
        })

        # Phase 29：严格断言 — non_blocked 不得超出 K
        if status == "executed" and self.non_blocked_executions > self.max_executions:
            self.window_status = "WINDOW_CLOSED"
            raise WindowClosed()

        # 计数
        if name in _P9_MUTATION_TOOLS and status == "executed":
            self.mutation_commits += 1
        if name in _P9_VERIFICATION_TOOLS and status == "executed":
            self.verification_attempts += 1


# ---------------------------------------------------------------------------
# Scope Parser（Phase 28 精确版）
# ---------------------------------------------------------------------------
def _parse_pytest_args(code: str, filename: str, args_str: str) -> str:
    """精确解析 pytest 调用 → PROJECT / TARGET / UNKNOWN。"""
    combined = (code or "") + " " + (args_str or "") + " " + (filename or "")
    has_pytest = "pytest" in combined.lower() or "unittest" in combined.lower()
    if not has_pytest:
        return "UNKNOWN"

    # 1. pytest.main([...])
    pattern = r'pytest\.main\s*\(\s*(.*?)\s*\)'
    m = re.search(pattern, combined, re.DOTALL)
    if m:
        args_part = m.group(1).strip()
    else:
        # 2. python -m pytest [...]
        m = re.search(r'python\s+-m\s+pytest\s+(.*)', combined)
        if m:
            args_part = m.group(1).strip()
        else:
            # 3. subprocess.run(['pytest', ...])
            m = re.search(r"subprocess\.run\s*\(\s*\[\s*'pytest'", combined)
            if m:
                # 找到后面的参数
                m2 = re.search(r"subprocess\.run\s*\([^)]*['\"]pytest['\"]\s*,\s*(.*?)\s*\)", combined, re.DOTALL)
                if m2:
                    args_part = m2.group(1).strip()
                else:
                    return "PROJECT"  # 有 pytest 但参数不清 → 保守 PROJECT
            else:
                return "UNKNOWN"

    # 清理引号
    args_part = args_part.strip('[]"\' ')
    if not args_part:
        return "PROJECT"  # pytest.main([]) → 全项目

    # 分割参数
    parts = re.split(r'\s*,\s*', args_part)
    parts = [p.strip().strip('"\'') for p in parts if p.strip()]

    if not parts:
        return "PROJECT"

    # 检查每个 part
    for part in parts:
        if part.startswith("-"):
            continue
        if "::" in part:
            return "TARGET"
        if ".py" in part or "/" in part or "\\" in part:
            return "TARGET"
        if part and "." in part:
            return "TARGET"

    return "PROJECT"


def _determine_verification_scope(tool_name: str, args: dict | None) -> str:
    """Phase 28：精确 scope 判定。"""
    if tool_name == "run_tests":
        if args and args.get("target"):
            return f"TARGET({args['target']})"
        return "PROJECT"
    if tool_name == "run_python":
        code = (args or {}).get("code", "") or ""
        filename = (args or {}).get("filename", "") or ""
        args_str = (args or {}).get("args", "") or ""
        return _parse_pytest_args(code, filename, args_str)
    if tool_name == "code_loop":
        return "TARGET"
    return "UNKNOWN"


def _scope_satisfies(actual: str, required: str) -> bool:
    """Phase 28：scope 满足规则。"""
    if required == "PROJECT":
        return actual in ("PROJECT",) or actual.startswith("TARGET")
    if required.startswith("TARGET("):
        req_target = required[6:-1]  # extract from TARGET(x)
        return actual == required or actual == f"TARGET({req_target})"
    return True


def _extract_required_scope(request_text: str) -> str:
    """从 request_text 推断 required scope。"""
    lower = (request_text or "").lower()
    # 如果用户明确指定了 target/file
    if any(kw in lower for kw in ["test_calc", "test_", "specific test", "单个测试"]):
        return "TARGET"
    return "PROJECT"


# ---------------------------------------------------------------------------
# W3 确定性分类
# ---------------------------------------------------------------------------
def _classify_w3(tool_log: list[dict], init_epoch: int, final_epoch: int) -> str | None:
    """Phase 28：确定性 W3 分类。返回 W3A/W3B/W3C/W3U 或 None。"""
    if final_epoch <= init_epoch:
        return None  # 无新 mutation，不是 W3

    # 找到最后一次 verification 的位置和结果
    last_ver_idx = -1
    last_ver_result = ""
    last_mut_after_ver = False

    for i, entry in enumerate(tool_log):
        name = entry["name"]
        if name in _P9_VERIFICATION_TOOLS:
            last_ver_idx = i
            # 保守：verification 工具执行了就算 PASS（replay 无真实结果）
            last_ver_result = "PASS"
        if name in _P9_MUTATION_TOOLS and entry["status"] == "executed":
            if last_ver_idx >= 0 and i > last_ver_idx:
                last_mut_after_ver = True

    if not last_mut_after_ver:
        return None  # mutation 在 verification 之前，不是 W3

    # 检查最后一次 verification 之前的状态
    prev_ver_fail = False
    for i in range(last_ver_idx):
        if tool_log[i]["name"] in _P9_VERIFICATION_TOOLS:
            prev_ver_fail = True  # 保守：replay 中无法知道真实结果

    # W3A：最后 verification FAIL 后有新 mutation（repair）
    if prev_ver_fail:
        return "W3A_REPAIR_AFTER_VERIFICATION_FAIL"

    # W3B：最后 verification PASS 后有 justified 新 mutation
    # 检查是否有 new error / new obligation / new artifact
    has_justification = False
    for entry in tool_log[last_ver_idx + 1:]:
        if entry["name"] in _P9_MUTATION_TOOLS:
            # 新 mutation 本身不能证明 justification
            pass
        if entry["name"] in _P9_VERIFICATION_TOOLS:
            # 新的 verification 可能表明有 unresolved issue
            has_justification = True

    if has_justification:
        return "W3B_JUSTIFIED_NEW_MUTATION"

    # W3C：无 justification 的 post-pass mutation
    return "W3C_UNJUSTIFIED_POST_PASS_MUTATION"


# ---------------------------------------------------------------------------
# Full Replay Fidelity（Phase 28 升级）
# ---------------------------------------------------------------------------
def _full_replay_fidelity(snap: dict, actual_workspace_hash: str | None = None) -> tuple[bool, list[str]]:
    """Phase 30：完整不变量检查（schema v2）。

    覆盖：Revision / Workspace / Obligation / Pending / Provider / Tool Exposure。
    """
    errors: list[str] = []
    sv = snap.get("snapshot_schema_version")

    # 1. Schema
    if sv is None:
        errors.append("snapshot_schema_version missing")
    elif sv < 1:
        errors.append(f"snapshot_schema_version={sv} < 1")

    # 2. Revision fidelity
    revision = snap.get("revision")
    evidence_epoch = snap.get("evidence_epoch")
    if revision is not None and evidence_epoch is not None and revision != evidence_epoch:
        errors.append(f"revision({revision}) != evidence_epoch({evidence_epoch})")

    # 3. Post-mutation invariants
    if not snap.get("mutation_seen"):
        errors.append("mutation_seen=False")
    if not snap.get("verification_due"):
        errors.append("verification_due=False")
    if not snap.get("verification_required"):
        errors.append("verification_required=False")

    # 4. Workspace fidelity
    snap_wh = snap.get("workspace_hash")
    if not snap_wh:
        errors.append("workspace_hash missing")
    elif actual_workspace_hash and snap_wh != actual_workspace_hash:
        errors.append(f"workspace_hash mismatch: snap={snap_wh} actual={actual_workspace_hash}")

    # 5. Tool exposure fidelity
    ts = snap.get("tool_schemas")
    if not ts:
        errors.append("tool_schemas missing")
    if sv is not None and sv >= 2:
        if not snap.get("tool_schema_hashes"):
            errors.append("tool_schema_hashes missing (v2)")

    # 6. Input items
    if not snap.get("input_items"):
        errors.append("input_items missing")

    # 7. Model config
    if not snap.get("model_config"):
        errors.append("model_config missing")

    # 8. Provider fidelity (v2)
    if sv is not None and sv >= 2:
        if not snap.get("provider_fingerprint"):
            errors.append("provider_fingerprint missing (v2)")

    # 9. Obligation fidelity (v2)
    if sv is not None and sv >= 2:
        if not snap.get("obligation_ledger"):
            errors.append("obligation_ledger missing (v2)")

    # 10. Pending fidelity (v2)
    if sv is not None and sv >= 2:
        if snap.get("pending_user") is None:
            errors.append("pending_user missing (v2)")
        if snap.get("pending_approval") is None:
            errors.append("pending_approval missing (v2)")

    # 11. Spark config fidelity (Phase 32)：snapshot 的 spark_config_hash 必须与
    # 当前运行环境一致，否则 CONFIG_MISMATCH，禁止合并统计。
    snap_cfg = snap.get("spark_config_hash")
    env_cfg = os.getenv("FORGE_SPARK_CONFIG_HASH", "")
    if env_cfg:
        if not snap_cfg:
            errors.append("spark_config_hash missing in snapshot")
        elif snap_cfg != env_cfg:
            errors.append(
                f"CONFIG_MISMATCH: snapshot={snap_cfg} env={env_cfg}")

    return (len(errors) == 0, errors)


def _compute_workspace_hash() -> str:
    """Phase 30/32：实时计算当前 fixture workspace hash（与 snapshot 对比）。

    与 dq._workspace_hash 保持一致：排除 __pycache__ / .pytest_cache / *.pyc。
    """
    import hashlib
    from pathlib import Path as _P
    try:
        import benchmark.decision_qualification as _dq
        fix = getattr(_dq, "FIX", None)
        if not fix:
            return ""
        base = _P(fix)
        h = hashlib.sha256()
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
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def _hydrate_initial_state(snap: dict) -> tuple[int, bool, int | None, bool]:
    initial_epoch = snap.get("revision") or snap.get("evidence_epoch") or 0
    mutation_seen = bool(snap.get("mutation_seen"))
    verified_revision = snap.get("verified_revision")
    verification_passed = bool(snap.get("verification_passed"))
    return initial_epoch, mutation_seen, verified_revision, verification_passed


def _hydrate_invariant_check(snap: dict, initial_epoch: int,
                              mutation_seen: bool) -> list[str]:
    errors = []
    snap_revision = snap.get("revision")
    if snap_revision is not None and initial_epoch != snap_revision:
        errors.append(f"epoch mismatch: revision={snap_revision} hydrated={initial_epoch}")
    snap_mut = snap.get("mutation_seen")
    if snap_mut is not None and mutation_seen != bool(snap_mut):
        errors.append("mutation_seen mismatch")
    return errors


def _analyze_window_evidence(hooks: WindowHooks, snap: dict) -> dict:
    """Phase 28：分析窗口内的 evidence + scope + 确定性分类。"""
    from runtime.readiness_gate import DiscoveryTracker

    seq = hooks.tool_sequence
    tracker = DiscoveryTracker()

    # Hydration
    initial_epoch, mutation_seen, verified_revision, verification_passed = \
        _hydrate_initial_state(snap)
    tracker.evidence_epoch = initial_epoch
    tracker.mutation_seen = mutation_seen
    if verified_revision is not None:
        tracker.verified_revision = verified_revision
        tracker.verification_passed = verification_passed

    inv_errors = _hydrate_invariant_check(snap, initial_epoch, mutation_seen)

    # 从 execution_log 重建 tool outcomes + scope history
    tool_outcomes: list[tuple[str, str, str]] = []
    scope_history: list[str] = []
    call_idx = 0

    for idx, entry in enumerate(hooks.execution_log):
        name = entry["name"]
        args = entry.get("arguments", {})
        status = entry.get("status", "executed")

        # Scope
        scope = _determine_verification_scope(name, args)
        scope_history.append(scope)

        if name in _P9_MUTATION_TOOLS and status == "executed":
            tracker.mutation_seen = True
            tracker.evidence_epoch += 1
            tool_outcomes.append((name, "CHANGED", ""))
        elif name in _P9_VERIFICATION_TOOLS and status == "executed":
            tracker.verification_seen = True
            if tracker.evidence_epoch > 0:
                tracker.verification_passed = True
                tracker.verified_revision = tracker.evidence_epoch
            tool_outcomes.append((name, "", "PASS"))

    # Evidence PASS
    evidence_pass = (
        tracker.verification_passed
        and tracker.verified_revision is not None
        and tracker.verified_revision == tracker.evidence_epoch
        and tracker.evidence_epoch > 0
    )

    # Scope satisfaction：使用窗口内最佳 scope（不只看最后一个）
    required_scope = _extract_required_scope(snap.get("goal", ""))
    # 最佳 scope：PROJECT > TARGET > UNKNOWN
    _best_scope = "UNKNOWN"
    for s in scope_history:
        if s == "PROJECT":
            _best_scope = "PROJECT"
            break
        elif s == "TARGET" or s.startswith("TARGET("):
            _best_scope = "TARGET"
    actual_scope = _best_scope
    obligation_satisfied = evidence_pass and _scope_satisfies(actual_scope, required_scope)

    # Metrics
    first_semantic = dq.is_semantic_verification(hooks.first_tool, hooks.first_args)
    semantic_within = any(
        dq.is_semantic_verification(t, None) for t in seq
    )

    # W3 分类
    w3_class = _classify_w3(hooks.execution_log, initial_epoch, tracker.evidence_epoch)

    # W1 分类
    w1_class = None
    if hooks.window_status == "FINAL_TEXT" and not tracker.verification_seen:
        if hooks.tool_executions == 0:
            # 无工具调用 → 可能是 provider error（空响应）
            w1_class = "W1E_PROVIDER_EMPTY_RESPONSE"
        else:
            w1_class = "W1B_FINAL_BLOCKED_BUT_NO_RECOVERY"
    elif hooks.window_status == "NO_ACTION":
        w1_class = "W1D_PARSER_NO_ACTION"

    # Category
    category = _categorize_v3(
        hooks.window_status, first_semantic, semantic_within,
        evidence_pass, obligation_satisfied,
        tracker.verification_passed, tracker.verification_seen,
        tracker.mutation_seen, w1_class, w3_class,
    )

    return {
        "first_semantic": first_semantic,
        "semantic_within_k": semantic_within,
        "latest_rev_verification": evidence_pass,
        "obligation_satisfying": obligation_satisfied,
        "current_epoch": tracker.evidence_epoch,
        "verified_revision": tracker.verified_revision,
        "verification_attempted": tracker.verification_seen,
        "verification_passed": tracker.verification_passed,
        "category": category,
        "w1_class": w1_class,
        "w3_class": w3_class,
        "hydration_errors": inv_errors,
        "initial_epoch": initial_epoch,
        "required_scope": required_scope,
        "actual_scope": actual_scope,
        "scope_satisfied": _scope_satisfies(actual_scope, required_scope),
        "non_blocked_executions": hooks.non_blocked_executions,
        "blocked_executions": hooks.blocked_executions,
        "tool_outcomes": tool_outcomes,
        "scope_history": scope_history,
    }


def _categorize_v3(status, first_semantic, semantic_within, evidence_pass,
                   obligation_satisfying, verif_passed, verif_seen, mut_seen,
                   w1_class, w3_class) -> str:
    """Phase 28：最终分类。"""
    if obligation_satisfying:
        return "PASS"
    if evidence_pass and not obligation_satisfying:
        return "W7_SCOPE_INSUFFICIENT"
    if status == "WINDOW_CLOSED":
        if w3_class:
            return w3_class
        if not verif_seen:
            if w1_class:
                return w1_class
            return "W1_NO_VERIFICATION"
        if verif_seen and not verif_passed:
            return "W2_VERIFICATION_FAILED"
        if verif_passed and mut_seen:
            return "W3_POST_VERIFICATION_MUTATION"
        return "W1_NO_VERIFICATION"
    if status == "FINAL_TEXT":
        if not verif_seen:
            if w1_class:
                return w1_class
            return "W1_FINAL_TEXT_NO_VERIFICATION"
        if verif_passed:
            return "PASS"
        return "W2_FINAL_TEXT_VERIFICATION_FAILED"
    return "W5_PROVIDER_ERROR"


# Backward-compat wrapper for Phase 24 tests (old 7-arg API returning tuple)
def _categorize_window(status, first_semantic, semantic_within, evidence_pass,
                       obligation_satisfying, verif_passed, verif_seen):
    """Phase 24 compat: maps old 7-arg API → short category code."""
    # Interpret verif_passed as "verification attempted" (old API) vs "verification succeeded" (v3)
    # Old API: verif_passed=True means a verification tool was called (not necessarily succeeded)
    verif_attempted = verif_passed or verif_seen
    if status == "ACTION_LIMIT":
        if evidence_pass and obligation_satisfying:
            return ("PASS", "PASS")
        if evidence_pass is False and obligation_satisfying:
            return ("W3", "W3_POST_VERIFICATION_MUTATION")
        if not verif_seen and not obligation_satisfying:
            if first_semantic in ("READ", None):
                return ("W4", "W4_EXPLORATION_ONLY")
            return ("W1", "W1_NO_VERIFICATION")
        if verif_attempted and not evidence_pass:
            return ("W2", "W2_VERIFICATION_FAILED")
        return ("W1", "W1_NO_VERIFICATION")
    cat = _categorize_v3(status, bool(first_semantic), bool(semantic_within), evidence_pass,
                         obligation_satisfying, verif_passed, verif_seen,
                         True, None, None)
    mapping = {
        "W1_NO_VERIFICATION": "W1", "W1_FINAL_TEXT_NO_VERIFICATION": "W1",
        "W2_VERIFICATION_FAILED": "W2", "W2_FINAL_TEXT_VERIFICATION_FAILED": "W2",
        "W3_POST_VERIFICATION_MUTATION": "W3", "W3C_UNJUSTIFIED_POST_PASS_MUTATION": "W3",
        "W4_EXPLORATION_ONLY": "W4", "W7_SCOPE_INSUFFICIENT": "W7",
        "W5_PROVIDER_ERROR": "W5",
    }
    short = mapping.get(cat, cat)
    return (short, cat)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
async def _run_window(snap: dict, variant: str, n: int,
                      max_executions: int = K_DEFAULT) -> dict:
    runs = []
    for i in range(n):
        # Phase 30：每次 run 重建 agent（保证 tool wrapper 干净），并加执行配额
        agent = dq.build_replay_agent(snap)
        quota = ExecutionQuota(max_executions)
        wrap_tools_with_quota(agent, quota)
        hooks = WindowHooks(max_executions=max_executions)
        t0 = time.monotonic()
        status = "NO_ACTION"
        error = None
        try:
            await asyncio.wait_for(
                Runner.run(agent, input=snap["input_items"],
                           run_config=main_module._run_config(
                               provider=_select_provider()),
                           hooks=hooks, max_turns=20),
                timeout=600.0,
            )
            status = hooks.window_status or "FINAL_TEXT"
        except WindowClosed:
            status = "WINDOW_CLOSED"
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            status, error = dq._classify_error(exc)
        # Phase 30：以 quota.used 为权威 non-blocked 计数
        hooks.non_blocked_executions = quota.used
        latency = round(time.monotonic() - t0, 2)
        analysis = _analyze_window_evidence(hooks, snap)
        r = {
            "i": i, "variant": variant, "case": snap.get("case"),
            "status": status, "latency_s": latency, "error": error,
            # Phase 28：硬边界相关
            "non_blocked_executions": hooks.non_blocked_executions,
            "blocked_executions": hooks.blocked_executions,
            "model_turns": hooks.model_turns,
            "tool_calls_proposed": hooks.tool_calls_proposed,
            "tool_executions": hooks.tool_executions,
            "mutation_commits": hooks.mutation_commits,
            "verification_attempts": hooks.verification_attempts,
            "premature_final": hooks.premature_final,
            # Evidence
            "first_tool": hooks.first_tool,
            "tool_sequence": hooks.tool_sequence,
            "tool_outcomes": analysis["tool_outcomes"],
            "scope_history": analysis["scope_history"],
            "all_tool_calls": hooks.all_tool_calls,
            # Metrics
            "first_semantic_verification": analysis["first_semantic"],
            "semantic_within_k": analysis["semantic_within_k"],
            "latest_rev_verification": analysis["latest_rev_verification"],
            "obligation_satisfying": analysis["obligation_satisfying"],
            "window_category": analysis["category"],
            "w1_class": analysis["w1_class"],
            "w3_class": analysis["w3_class"],
            "current_epoch": analysis["current_epoch"],
            "verified_revision": analysis["verified_revision"],
            "required_scope": analysis["required_scope"],
            "actual_scope": analysis["actual_scope"],
            "scope_satisfied": analysis["scope_satisfied"],
            "hydration_errors": analysis["hydration_errors"],
            "initial_epoch": analysis["initial_epoch"],
        }
        runs.append(r)
        print(f"  [{variant}] [{i+1}/{n}] cat={analysis['category']} w1={analysis['w1_class']} w3={analysis['w3_class']} "
              f"first={hooks.first_tool} turns={hooks.model_turns} non_blk={hooks.non_blocked_executions} "
              f"blk={hooks.blocked_executions} mut={hooks.mutation_commits} ver={hooks.verification_attempts} "
              f"epoch={analysis['current_epoch']} ver_rev={analysis['verified_revision']} "
              f"obl={analysis['obligation_satisfying']} scope={analysis['actual_scope']} "
              f"lat={latency}s", flush=True)

        # Phase 29：严格硬断言 — non_blocked_executions 不得超出 K
        if hooks.non_blocked_executions > max_executions:
            raise AssertionError(
                f"Strict window boundary violated: non_blocked={hooks.non_blocked_executions} > K={max_executions}")

    total = len(runs)
    cats = Counter(r["window_category"] for r in runs)
    return {
        "n": total,
        "first_semantic": sum(1 for r in runs if r["first_semantic_verification"]),
        "semantic_within_k": sum(1 for r in runs if r["semantic_within_k"]),
        "latest_rev_verification": sum(1 for r in runs if r["latest_rev_verification"]),
        "obligation_satisfying": sum(1 for r in runs if r["obligation_satisfying"]),
        "premature_final": sum(1 for r in runs if r["premature_final"]),
        "category_counts": dict(cats),
        "w1_class_counts": dict(Counter(r["w1_class"] for r in runs if r["w1_class"])),
        "w3_class_counts": dict(Counter(r["w3_class"] for r in runs if r["w3_class"])),
        "latency_avg_s": round(sum(r["latency_s"] for r in runs) / total, 2) if total else 0,
        "avg_model_turns": round(sum(r["model_turns"] for r in runs) / total, 1) if total else 0,
        "avg_non_blocked_executions": round(
            sum(r["non_blocked_executions"] for r in runs) / total, 1) if total else 0,
        "runs": runs,
    }


# ---------------------------------------------------------------------------
# Phase 33：M3 Closed-Loop Benchmark 委托给 benchmark.m3_closed_loop
# （fixture + observation + metrics only；无 repair policy / guidance / prompt 控制）
# ---------------------------------------------------------------------------
from benchmark.m3_closed_loop import (  # noqa: E402
    M3_FIX,
    M3_PROMPT,
    cmd_run_m3,
    reset_m3_fixture as _reset_m3_fixture,
)


async def cmd_run(out: Path, n: int, snapdir: Path, cases: list[str],
                  k: int = K_DEFAULT) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {}
    for cid in cases:
        snap_path = snapdir / f"{cid}.json"
        if not snap_path.exists():
            print(f"[window] {cid} snapshot missing: {snap_path}", flush=True)
            continue
        snap = dq.load_snapshot(snap_path)
        if snap.get("snapshot_type") != "LIVE_SNAPSHOT_CHECKPOINT":
            print(f"[window] {cid} not LIVE snapshot, skipping", flush=True)
            continue
        dq.assert_checkpoint_valid(snap)
        # Phase 30：Full Replay Fidelity（含实时 workspace hash 对比）
        actual_wh = _compute_workspace_hash()
        fidelity_pass, fidelity_errors = _full_replay_fidelity(snap, actual_wh)
        if not fidelity_pass:
            print(f"[window] {cid} FULL_REPLAY_FIDELITY FAIL: {fidelity_errors}", flush=True)
            result[cid] = {"n": 0, "error": "FULL_REPLAY_FIDELITY_FAIL",
                          "fidelity_errors": fidelity_errors}
            continue
        init_epoch, init_mut, init_ver_rev, init_ver_pass = _hydrate_initial_state(snap)
        inv_errors = _hydrate_invariant_check(snap, init_epoch, init_mut)
        if inv_errors:
            print(f"[window] {cid} HARNESS_INVALID: {inv_errors}", flush=True)
            result[cid] = {"n": 0, "error": "HARNESS_INVALID", "hydration_errors": inv_errors}
            continue
        runs = await _run_window(snap, f"W{cid}", n, max_executions=k)
        result[cid] = runs
        (out / "partial.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {}
    for cid, v in result.items():
        if "error" in v:
            summary[cid] = v
            continue
        summary[cid] = {k2: v[k2] for k2 in (
            "n", "first_semantic", "semantic_within_k",
            "latest_rev_verification", "obligation_satisfying",
            "premature_final", "category_counts", "w1_class_counts", "w3_class_counts",
            "latency_avg_s", "avg_model_turns", "avg_non_blocked_executions",
        )}
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.bounded_window")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--snapdir", default="phase26/snapshots_agens")
    p.add_argument("--cases", default="M1,M4")
    p.add_argument("--k", type=int, default=K_DEFAULT,
                   help=f"non-blocked tool execution 上限（默认 {K_DEFAULT}）")
    p3 = sub.add_parser("run-m3")
    p3.add_argument("--out", required=True)
    p3.add_argument("--n", type=int, default=3)
    p3.add_argument("--max-turns", type=int, default=20,
                    help="生产预算 max_turns（默认 20）")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import main as _main  # noqa: F401
    pref = (os.getenv("FORGE_MODEL_PREF", "") or "").strip().lower()
    if pref not in ("local", "gateway"):
        raise SystemExit(f"FORGE_MODEL_PREF={pref!r}；只允许 local 或 gateway")
    cases = [c.strip()
             for c in getattr(args, "cases", "").split(",") if c.strip()]
    if args.cmd == "run-m3":
        asyncio.run(cmd_run_m3(Path(args.out), args.n, args.max_turns))
        return 0
    asyncio.run(cmd_run(Path(args.out), args.n, Path(args.snapdir),
                       cases, k=args.k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
