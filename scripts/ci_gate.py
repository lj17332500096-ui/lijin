"""通用 pre-commit 门禁：按改动文件精确选测试，并支持「已知失败基线」。

设计要点（2026-09-26 重构）：
1. **精确选子集**：改动文件 → 测试文件的映射集中在本文件 CHANGE_TEST_MAP，
   不再散落在 bash 钩子里。没有映射的改动不跑测试（避免"改一行注释触发全量"）。
2. **已知失败基线**：`.ci/known_failures.txt` 里登记的失败只警告、不阻断；
   未登记的失败才阻断。每条基线**必须写原因**（否则脚本报错），防止把红灯
   无声无息地永久化 —— 基线是"已确认并排期"的凭证，不是免检牌。
3. 退出码：0 = 放行；1 = 有未登记的失败；2 = 用法/解析错误。

用法：
    # 按改动文件选子集
    # 从 stdin 读改动文件列表（pre-commit 钩子走这条）
    git diff --cached --name-only | python scripts/ci_gate.py --stdin
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_FILE = PROJECT_ROOT / ".ci" / "known_failures.txt"

#: 改动文件（精确路径）→ 需要跑的测试文件。顺序即去重后的执行顺序。
#:
#: P2-2：本表原先只有 4 组，**改 runtime/runner.py（4819 行，承载 E/T/S/L 四层）
#: 不会跑任何测试**。这解释了为什么"评估层记 0"这类问题能长期存活：
#: 没有任何本地门禁会在改主干后提醒你补测试。
#:
#: 扩表原则：宁可多跑，不可漏跑。漏跑的代价是"改主干无测试"这种静默失效；
#: 多跑的代价只是几秒到几十秒。
CHANGE_TEST_MAP: list[tuple[tuple[str, ...], list[str]]] = [
    # —— E/T/S/L 主干：改动面最大，必须触发实质回归 ——
    (("runtime/runner.py",),
     ["tests/test_concurrency_stress.py", "tests/test_conversation_workflow.py",
      "tests/test_task_runtime.py"]),
    # S 层：连接管理 / 事件流 / 容器
    (("runtime/task_manager.py",),
     ["tests/test_task_manager_resilience.py", "tests/test_task_runtime.py",
      "tests/test_audit.py", "tests/test_concurrency_stress.py"]),
    (("runtime/startup_guard.py",),
     ["tests/test_approval_startup_guard.py", "tests/test_approval.py"]),
    # L 层：护栏本体（P0-1 已移除其 import 期 load_dotenv，仍需守住语义）
    (("runtime/approval.py", "runtime/errors.py"),
     ["tests/test_approval.py", "tests/test_approval_lifecycle.py",
      "tests/test_approval_execution_parity.py", "tests/test_approval_startup_guard.py"]),
    # 配置加载：改任何一处都可能影响"谁在 import 期喂环境变量"
    (("runtime/compact.py", "runtime/codex_loop.py", "runtime/reply_parser.py",
      "runtime_paths.py", "agent.py", "main.py"),
     ["tests/test_approval.py", "tests/test_approval_startup_guard.py"]),
    # C 层：能力自省 + 意图门（tools=[] 误伤风险）
    (("runtime/capability_introspection.py",),
     ["tests/test_capability_introspection.py",
      "tests/test_capability_gate_no_false_strip.py"]),
    # T 层：名册 / 文件边界
    (("runtime/filescope.py", "runtime/registry.py", "runtime/spec.py",
      "integrations/mcp_bridge.py"),
     ["tests/test_tool_roster_consistency.py", "tests/test_capability_introspection.py",
      "tests/test_mcp_bridge_policy.py"]),
    # V 层：评估口径（搜索计数恒 0 这类问题只能靠这里挡住）
    (("benchmark/evaluator.py", "benchmark/matrix.py"),
     ["tests/test_behavior_layer_rework.py", "tests/test_benchmark_evaluator.py",
      "tests/test_tool_roster_consistency.py"]),
    # C 层残留架构债：task_plan 的规则选择器
    (("runtime/task_plan.py",),
     ["tests/test_task_plan.py", "tests/test_task_plan_no_regex_authority.py"]),
    # 工作流图
    (("runtime/conversation_workflow.py", "runtime/api_workflow.py"),
     ["tests/test_conversation_workflow.py"]),
    # 其余既有映射
    (("cli/tui/app.py", "cli/tui/panels.py", "cli/tui/models.py"),
     ["tests/test_tui.py"]),
    (("runtime/public_activity.py",),
     ["tests/test_public_activity.py"]),
    (("runtime/provider_gateway.py",),
     ["tests/test_provider_errors.py"]),
    # 记忆文件：改它要跑过期探针。
    # 理由：MEMORY.md 里的「已知遗留」会因**对应修复已完成而失效**，而条目
    # 读起来仍言之凿凿，下个会话会去"修"一个已修好的东西。改记忆时跑这条
    # 探针，可在提交前就抓出「已修却没划掉」的条目。
    #（`tests/test_stale_memory_probes.py` 会git 追溯 + 跑回归测试双重取证）
    ((".workbuddy/memory/MEMORY.md",),
     ["tests/test_stale_memory_probes.py"]),
    # workflow 文件：改它要跑表达式语法 + shell 语法两条校验。
    # 理由（10-05 首跑实测事故）：`ci.yml` 里有一处**注释**写了双花括号表达式
    # 的字面量示例，GitHub 连注释一起求值 ⇒ 解析失败 ⇒ **整个文件被拒绝执行**，
    # 表现为 `completed/failure` 但 **jobs=0**、check-runs=0、耗时同一秒。
    # 本地 `yaml.safe_load` 完全查不出（YAML 层面合法），`check_workflow_shells.py`
    # 也查不出（它查的是 shell 语法）。两者互补，都得跑。
    ((".github/workflows/ci.yml",),
     ["tests/test_ci_workflow_expr_syntax.py",
      "tests/test_ci_workflow_shell_syntax.py"]),
    # 路径落根的四个模块：改它们要跑「8.3 短名 root」下的对称性检查。
    # 理由（10-05 首次 push 到 GitHub 的实据）：这四处都做过
    # `X.relative_to(Y)` 判定，而**只对一侧做了 `.resolve()`**。
    # 本地 tempdir 目录没有 8.3 短名 ⇒ 本地 1576 全绿；
    # GitHub runner 的 tempdir 是 `C:\Users\RUNNER~1\...`（短名）⇒
    # target.resolve() 展开成长名后 relative_to 失败 ⇒ **14 条测试变红**
    # （test_office_docs 2 / test_dep_doctor 4 / test_trust 1 /
    #  test_rag 5 / test_code_exec 1 + project_edit 的 diff 变绝对路径）。
    #「本地全绿」在这里不是证据。
    (("tools.py", "rag.py", "code_exec.py", "project_edit.py"),
     ["tests/test_path_normalization_symmetry.py"]),
    # 检索链路（10-05 CI run 37326450894 实据）：FTS5 的 unicode61 分词器把
    # **中文整段当一个 token**（"按钮" 对 "按钮必须放在右下角…" 命中 0），
    # 而 sources 侧曾只走 FTS 那条路 ⇒ 中文检索恒失效。本地全绿是因为
    # rag.py 的 ONNX 向量兜住了；而 requirements.txt 无 onnxruntime、
    # 模型目录 models/bge-small-zh-v1.5 也不入库 ⇒ **CI 上向量必然不可用**
    # ⇒ 掩盖消失，暴露成 mode='no_results'。
    # 这条护栏专门钉住「**无向量时中文仍能召回**」+「无关中文不误召回」。
    (("sources/retriever.py", "sources/store.py", "sources/indexer.py"),
     ["tests/test_cjk_retrieval_without_vectors.py", "tests/test_sources_rag.py"]),
    # 工具错误可见性（10-05 CI run 37350998914 实据）：requirements.txt 写
    # `openai-agents>=0.22.0`（**开放上界**）⇒ CI 装到当时最新版，而新版
    # `default_tool_error_function` 出于安全加固**删掉了 `Error: {error}` 后缀**
    # ⇒ 依赖该 detail 的断言在 CI 红、本地（0.22.0）绿。
    # 修法是给工具装 `failure_error_function`（官方机制），只放行**业务边界错误**。
    # 这条护栏会**自己模拟新版 SDK**（本地 0.22.0 对新版零鉴别力），
    # 且双向：业务错误必须可见 / 内部错误（含本机路径）必须隐藏。
    (("rag.py", "runtime/tool_errors.py"),
     ["tests/test_tool_error_visibility.py", "tests/test_rag.py"]),
]

VENV_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)


def tests_for_changed(changed: list[str]) -> list[str]:
    """按映射表选出测试文件（去重、保持顺序）。"""
    out: list[str] = []
    for patterns, tests in CHANGE_TEST_MAP:
        if any(c in patterns for c in changed):
            for t in tests:
                if t not in out:
                    out.append(t)
    return out


def load_baseline() -> dict[tuple[str, str], str]:
    """读已知失败基线 → {(文件路径, 用例名): 原因}。

    行格式：`tests/foo.py::test_bar   原因（必填）`
    缺原因的行视为格式错误，直接抛错 —— 宁可让提交失败，也不允许无理由的红灯常驻。
    """
    if not BASELINE_FILE.exists():
        return {}
    entries: dict[tuple[str, str], str] = {}
    for lineno, raw in enumerate(BASELINE_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = line.split("#", 1)[0].strip() if "#" in line else line
        comment = line.split("#", 1)[1].strip() if "#" in line else ""
        if "::" not in body:
            raise SystemExit(
                f"{BASELINE_FILE}:{lineno} 基线格式错误（需 path::test_name）：{raw!r}\n"
                f"   示例：tests/test_runtime.py::test_x  原因：xxx"
            )
        if not comment:
            raise SystemExit(
                f"{BASELINE_FILE}:{lineno} 基线缺原因说明（# 原因：…），拒绝加载"
            )
        path, name = body.split("::", 1)
        entries[(path.strip(), name.strip())] = comment
    return entries


def missing_test_files(tests: list[str]) -> list[str]:
    """返回映射表里引用、但仓库中不存在的测试文件。

    为什么需要它：映射表指向一个不存在的文件时，pytest 会报
    "file or directory not found" 并**整体失败**——看起来像"测试红了"，
    实际是门禁配置写错了。两种故障混在一起会消耗排查时间，故提前显式区分。
    """
    return [t for t in tests if not (PROJECT_ROOT / t).is_file()]


def run_tests(tests: list[str], junit_path: Path) -> int:
    absent = missing_test_files(tests)
    if absent:
        print(
            "[CI Gate] 映射表引用了不存在的测试文件（门禁配置错误，非测试失败）：\n  "
            + "\n  ".join(absent),
            file=sys.stderr,
        )
        return 2
    cmd = [str(VENV_PY), "-m", "pytest", *tests, "-q",
           f"--junitxml={junit_path}"]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    return result.returncode


def collect_failures(junit_path: Path) -> list[tuple[str, str]]:
    """从 junit xml 里取 (文件路径, 用例名) 列表。"""
    if not junit_path.exists():
        return []
    root = ET.parse(junit_path).getroot()
    out: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        failed = any(child.tag in ("failure", "error") for child in case)
        if not failed:
            continue
        cls = case.get("classname") or ""
        name = case.get("name") or ""
        parts = cls.split(".")
        path = "/".join(parts[:2]) + ".py" if len(parts) >= 2 else cls
        out.append((path, name))
    return out


def main(argv: list[str]) -> int:
    tests: list[str] = []
    changed: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tests":
            tests.append(argv[i + 1]); i += 2
        elif a == "--changed":
            changed.append(argv[i + 1]); i += 2
        elif a == "--stdin":
            changed.extend(
                l.strip() for l in sys.stdin.read().splitlines() if l.strip()
            )
            i += 1
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            return 2
        if i > len(argv):
            break

    explicit = bool(tests)
    if not tests:
        tests = tests_for_changed(changed)
    if not tests:
        print("[CI Gate] 改动文件无对应测试映射，跳过。")
        return 0

    why = "显式指定" if explicit else "改动命中映射"
    print(f"[CI Gate] {why}，跑：{', '.join(tests)}")
    baseline = load_baseline()
    with tempfile.TemporaryDirectory() as td:
        junit = Path(td) / "junit.xml"
        rc = run_tests(tests, junit)
        if rc == 0:
            print("\n[CI Gate] PASSED — 允许提交。")
            return 0
        failures = collect_failures(junit)
        blocking: list[tuple[str, str]] = []
        for f in failures:
            reason = baseline.get(f)
            if reason:
                print(f"[CI Gate] 已知失败（不阻断）：{f[0]}::{f[1]} —— {reason}")
            else:
                blocking.append(f)
        if not blocking:
            print("\n[CI Gate] 失败全部在基线内 —— 允许提交（请尽快排期修复）。")
            return 0
        print("\n[CI Gate] FAILED — 阻断提交。以下失败未登记基线：", file=sys.stderr)
        for f in blocking:
            print(f"    {f[0]}::{f[1]}", file=sys.stderr)
        print(f"\n  确认是既有问题且不阻塞本提交时，把它加进 {BASELINE_FILE}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
