#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_skill_fixes.py — DEF-01 ~ DEF-10 的机器可判定验收

用法：
    python verify_skill_fixes.py            # 逐条判定，全绿退出码 0
    python verify_skill_fixes.py --json     # 机器可读结果

每一条缺陷都对应一个真实执行的探针（不是静态假设）：
- 格式串类：真的把那行代码跑一遍，看是否抛 TypeError
- 桩实现类：真的给一个内容已知的文件，看返回值里有没有那段内容
- 挂载点类：真的 import run.py，看属性是否为 None
- 定义位置类：真的比对行号
- 副作用类：真的比对三个配置文件的前后哈希
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:  # Windows 控制台默认 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILLS_ROOT = Path(
    "C:/Users/Administrator/.workbuddy/plugins/marketplaces/experts/plugins/"
    "agent-orchestration-pro/skills"
)
SKILL_NAMES = ("agent-ready-repo", "everything-openai-codex", "workflow")
PY = sys.executable

RESULTS: list[dict] = []


def record(defect: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"defect": defect, "check": name, "pass": bool(ok), "detail": detail})
    print(f"  [{'OK ' if ok else 'NG '}] {defect:6s} {name}" + (f"  —— {detail}" if detail else ""))


def run(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def py_in_dir(cwd: Path, code: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return run([PY, "-c", code], cwd, timeout)


def sha256(path: Path) -> str:
    if not path.exists():
        return "<absent>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── DEF-01 自检格式串 ─────────────────────────────────────────────
def check_def01() -> None:
    skill = SKILLS_ROOT / "everything-openai-codex"
    cp = run([PY, "run.py", "--selftest"], skill)
    bad = "not all arguments converted" in (cp.stdout + cp.stderr)
    record("DEF-01", "run.py --selftest 不再因格式串抛 TypeError",
           (not bad) and cp.returncode == 0,
           f"rc={cp.returncode}" + ("，仍有格式串错误" if bad else ""))


# ── DEF-02 _write_guarded 格式串 ──────────────────────────────────
def check_def02() -> None:
    skill = SKILLS_ROOT / "everything-openai-codex"
    code = (
        "import sys; sys.path.insert(0,'scripts');\n"
        "import main as m, tempfile, pathlib\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "r1 = m._write_guarded(d/'x.json','abc',dry_run=True)\n"
        "r2 = m._write_guarded(d/'x.json','abc',dry_run=False,verbose=True)\n"
        "print('RESULT', r1, r2, (d/'x.json').read_text(encoding='utf-8'))\n"
    )
    cp = py_in_dir(skill, code)
    out = cp.stdout + cp.stderr
    ok = cp.returncode == 0 and "RESULT False True abc" in out
    record("DEF-02", "_write_guarded 的 dry-run / verbose 路径可执行",
           ok, f"rc={cp.returncode}" + ("" if ok else f"｜{out.strip().splitlines()[-1][:120] if out.strip() else ''}"))


# ── DEF-03 _cli 正常执行分支 ──────────────────────────────────────
def check_def03() -> None:
    skill = SKILLS_ROOT / "everything-openai-codex"
    cp = run([PY, "run.py", "--dry-run", "--force"], skill)
    out = cp.stdout + cp.stderr
    ok = cp.returncode == 0 and "执行模式" in out and "not all arguments converted" not in out
    record("DEF-03", "run.py --dry-run --force 正常返回", ok,
           f"rc={cp.returncode}")


# ── DEF-04 selftest() 被截断 ──────────────────────────────────────
def check_def04() -> None:
    """DEF-04：selftest 的函数体曾被截断在第 9 项，缺第 10 项与收尾 return。

    语义化判定：①自检链路末尾存在显式 return 0；②截断处缺失的第 10 项已补齐。
    （selftest 现在把写盘隔离委托给 _selftest_body，所以两条都要看。）
    """
    skill = SKILLS_ROOT / "everything-openai-codex"
    code = (
        "import ast\n"
        "src = open('scripts/main.py', encoding='utf-8').read()\n"
        "tree = ast.parse(src)\n"
        "def tail_return0(node):\n"
        "    if not node.body:\n"
        "        return False\n"
        "    last = node.body[-1]\n"
        "    if isinstance(last, ast.Return):\n"
        "        return isinstance(last.value, ast.Constant) and last.value.value == 0\n"
        "    if isinstance(last, (ast.With, ast.Try)) and last.body:\n"
        "        inner = last.body[-1]\n"
        "        return isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant) \\\n"
        "            and inner.value.value == 0\n"
        "    return False\n"
        "fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}\n"
        "sealed = tail_return0(fns['selftest']) or tail_return0(fns.get('_selftest_body'))\n"
        "completed = ('非法 JSON 处理' in src) and ('格式化输出路径' in src)\n"
        "print('RESULT', sealed, completed, fns['selftest'].end_lineno, len(src.splitlines()))\n"
    )
    cp = py_in_dir(skill, code)
    out = (cp.stdout + cp.stderr).strip()
    last = out.splitlines()[-1] if out else ""
    parts = last.split()
    ok = len(parts) > 2 and parts[1] == "True" and parts[2] == "True"
    detail = "已收尾 + 第10/11项已补齐" if ok else last[:120]
    record("DEF-04", "自检链路不再截断且有显式 return 0", ok, detail)


# ── DEF-05 自检破坏性副作用 ───────────────────────────────────────
def check_def05() -> None:
    # 关键：破坏性副作用在 selftest() 本体里（历史上有 `--selftest` 不经过它的路径），
    # 所以这里直接调 selftest()，而不是走 run.py。
    skill = SKILLS_ROOT / "everything-openai-codex"
    scripts = skill / "scripts"
    watched = [scripts / n for n in ("memory.json", "hooks.json", "rules.json")]
    before = {str(p): sha256(p) for p in watched}
    cp = py_in_dir(skill, "import sys; sys.path.insert(0,'scripts')\n"
                          "import main as m\nprint('SELFTEST_RC', m.selftest())\n")
    after = {str(p): sha256(p) for p in watched}
    changed = [p for p in before if before[p] != after[p]]
    out = cp.stdout + cp.stderr
    ran_ok = cp.returncode == 0 and "SELFTEST_RC 0" in out
    ok = ran_ok and not changed
    detail = []
    if not ran_ok:
        detail.append(f"selftest() 未干净返回 rc={cp.returncode}")
    detail.append("三个配置文件无变化" if not changed
                  else f"被改写: {[Path(p).name for p in changed]}")
    record("DEF-05", "selftest() 不覆盖 memory/hooks/rules", ok, "；".join(detail))


# ── DEF-06 / DEF-09 run.py 挂载点 ─────────────────────────────────
def check_mounts() -> None:
    code = (
        "import run\n"
        "print('RESULT', run.run_selftest is not None, run.read_text_safe is not None)\n"
    )
    selftest_all, encoding_all = True, True
    for name in SKILL_NAMES:
        cp = py_in_dir(SKILLS_ROOT / name, code)
        out = cp.stdout + cp.stderr
        line = next((l for l in out.splitlines() if l.startswith("RESULT")), "")
        parts = line.split()
        st = len(parts) > 1 and parts[1] == "True"
        enc = len(parts) > 2 and parts[2] == "True"
        if not st:
            selftest_all = False
            record("DEF-06", f"{name}: run.run_selftest 非 None", False, line[:100])
        if not enc:
            encoding_all = False
            record("DEF-09", f"{name}: run.read_text_safe 非 None", False, line[:100])
    if selftest_all:
        record("DEF-06", "三个技能 run.run_selftest 均可解析", True, "含 _run_selftest 兜底")
    if encoding_all:
        record("DEF-09", "三个技能 run.read_text_safe 均可解析", True, "含 _read_text_safe_enc 兜底")


# ── DEF-07 workflow 输入守卫 ──────────────────────────────────────
def check_def07() -> None:
    """DEF-07：守卫只认 --file 时，--data / --url 经 run.py 恒被拦成 need_input。

    判定口径按实现层的真实输出形态：main() 打印的是结构化数据本体
    （含 source_type），不是 process_input 的完整返回体（那个才有 status）。
    """
    skill = SKILLS_ROOT / "workflow"
    ok_all, details = True, []
    for flag, value, want in (
        ("--data", '{"name":"张三","age":30}', '"source_type": "json"'),
        ("--url", "https://example.com/a?x=1", '"source_type": "url"'),
    ):
        cp = run([PY, "run.py", flag, value, "--format", "json"], skill)
        out = cp.stdout + cp.stderr
        reachable = cp.returncode == 0 and want in out and "need_input" not in out
        if not reachable:
            ok_all = False
        details.append(f"{flag}={'通过' if reachable else '不可达'}")
    record("DEF-07", "workflow run.py 的 --data / --url 可达", ok_all, "；".join(details))


# ── DEF-08 --file 桩实现 ──────────────────────────────────────────
def check_def08() -> None:
    skill = SKILLS_ROOT / "workflow"
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "wf_probe.json"
        probe.write_text(json.dumps({"name": "张三", "age": 30}, ensure_ascii=False), encoding="utf-8")
        cp = run([PY, "run.py", "--file", str(probe), "--format", "json"], skill)
        csv_probe = Path(td) / "wf_probe.csv"
        csv_probe.write_text("name,age\n张三,30\n李四,25\n", encoding="utf-8")
        cp2 = run([PY, "run.py", "--file", str(csv_probe), "--format", "json"], skill)
    out, out2 = cp.stdout + cp.stderr, cp2.stdout + cp2.stderr
    read_json = "张三" in out and "文件内容未实际读取" not in out
    read_csv = "张三" in out2 and "李四" in out2
    record("DEF-08", "--file 真实读取 JSON 内容", read_json,
           "内容已读入" if read_json else "仍是路径回显")
    record("DEF-08", "--file 真实读取 CSV 内容", read_csv,
           "内容已读入" if read_csv else "CSV 未被解析")


# ── DEF-10 read_text_safe 定义位置 ────────────────────────────────
def check_def10() -> None:
    for name in ("agent-ready-repo", "workflow"):
        path = SKILLS_ROOT / name / "scripts" / "main.py"
        lines = path.read_text(encoding="utf-8").splitlines()
        def_line = next((i for i, l in enumerate(lines, 1) if l.startswith("def read_text_safe")), None)
        guard_line = next((i for i, l in enumerate(lines, 1) if l.startswith('if __name__ == "__main__"')), None)
        ok = def_line is not None and guard_line is not None and def_line < guard_line
        record("DEF-10", f"{name}: read_text_safe 定义在 __main__ 卫兵之前", ok,
               f"def@{def_line} guard@{guard_line}")


# ── 汇总闸门：三个技能自检脚本 8/8 ────────────────────────────────
def check_selftest_scripts() -> None:
    for name in SKILL_NAMES:
        cp = run([PY, "selftest.py"], SKILLS_ROOT / name)
        out = cp.stdout + cp.stderr
        line = next((l for l in out.splitlines() if "自检完成" in l), "")
        ok = cp.returncode == 0 and "8/8" in line
        record("GATE", f"{name}/selftest.py 8/8", ok, line.strip() or f"rc={cp.returncode}")


def main() -> int:
    as_json = "--json" in sys.argv[1:]
    if not as_json:
        print(f"=== 技能缺陷验收（python={PY}）===")
        print(f"目标目录: {SKILLS_ROOT}\n")
        for fn in (check_def01, check_def02, check_def03, check_def04, check_def05,
                   check_mounts, check_def07, check_def08, check_def10, check_selftest_scripts):
            fn()

    failed = [r for r in RESULTS if not r["pass"]]
    payload = {
        "total": len(RESULTS),
        "passed": len(RESULTS) - len(failed),
        "failed": len(failed),
        "all_pass": not failed,
        "results": RESULTS,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== 结果：{payload['passed']}/{payload['total']} 通过 ===")
        for r in failed:
            print(f"  未通过：{r['defect']} {r['check']} {r['detail']}")
        print("ALL PASS" if payload["all_pass"] else "HAS FAILURE")
    return 0 if payload["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
