#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_skill_fixes.py — 把 DEF-01 ~ DEF-10 的修复重放到技能目录

为什么需要这个脚本：
  修复对象位于 WorkBuddy 的**市场插件缓存目录**
  （`~/.workbuddy/plugins/marketplaces/experts/plugins/agent-orchestration-pro/skills/`）。
  该目录由插件安装/升级流程整体覆盖，任何就地改动都会在下次升级时静默丢失。
  因此修复以「补丁包 + 幂等重放脚本」的形式交付：升级后重跑一次即可复原。

用法：
    python apply_skill_fixes.py            # 应用修复 + 自动跑验收（幂等，可反复执行）
    python apply_skill_fixes.py --check    # 只比对，不写盘
    python apply_skill_fixes.py --restore  # 回滚到 orig/ 快照（整目录还原）
    python apply_skill_fixes.py --no-verify

回滚的例外（有意为之，脚本会打印说明）：
  · `everything-openai-codex/scripts/memory.json` 不还原 orig 版本 —— 该快照里存的是
    自检写入的测试数据（DEF-05 的真实污染现场），还原它等于把污染带回来；
    脚本改为写入规范的空记忆结构。
  · `everything-openai-codex/scripts/hooks.json` / `rules.json` 若存在则删除 ——
    这两个文件在取证前并不存在，是自检的测试产物（rules 里还带着 `forbidden: 禁止词`
    这条会真实拦截任务生效规则）。
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ORIG = HERE / "orig"
FIXED = HERE / "fixed"
VERIFY = HERE / "verify_skill_fixes.py"

SKILLS_ROOT = Path(
    "C:/Users/Administrator/.workbuddy/plugins/marketplaces/experts/plugins/"
    "agent-orchestration-pro/skills"
)
SKILLS = ("agent-ready-repo", "everything-openai-codex", "workflow")

# 取证前不存在、由自检写出来的测试产物
TEST_ARTIFACTS = ("scripts/hooks.json", "scripts/rules.json")
CLEAN_MEMORY = '{\n  "entries": [],\n  "last_updated": null\n}\n'
CLEAN_MEMORY_REL = "scripts/memory.json"


def iter_files(root: Path, skill: str):
    base = root / skill
    if not base.exists():
        return
    for p in sorted(base.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            yield p


def rel_posix(path: Path, base: Path) -> str:
    """统一用 / 展示相对路径，避免 Windows 上打出反斜杠。"""
    return path.relative_to(base).as_posix()


def apply_fixes(dry: bool) -> dict:
    """把 fixed/ 覆盖到技能目录。返回变更清单。"""
    report = {"created": [], "updated": [], "unchanged": [], "missing_source": []}
    if not FIXED.exists():
        raise SystemExit(f"[ERROR] 未找到修复快照目录: {FIXED}")
    for skill in SKILLS:
        target_base = SKILLS_ROOT / skill
        if not target_base.exists():
            report["missing_source"].append(skill)
            continue
        for src in iter_files(FIXED, skill):
            rel = src.relative_to(FIXED / skill)
            dst = target_base / rel
            label = f"{skill}/{rel_posix(src, FIXED / skill)}"
            if dst.exists() and filecmp.cmp(src, dst, shallow=False):
                report["unchanged"].append(label)
                continue
            kind = "updated" if dst.exists() else "created"
            if not dry:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            report[kind].append(label)
    return report


def restore() -> None:
    """整目录回滚到取证前快照（含上面 docstring 说明的两处例外）。"""
    if not ORIG.exists():
        raise SystemExit(f"[ERROR] 未找到原始快照目录: {ORIG}")
    restored = 0
    for skill in SKILLS:
        target_base = SKILLS_ROOT / skill
        for src in iter_files(ORIG, skill):
            rel = src.relative_to(ORIG / skill)
            if rel.as_posix() == CLEAN_MEMORY_REL:
                continue  # 例外 1：不还原被测试数据污染的记忆
            dst = target_base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            restored += 1
    # 例外 1：写入规范空记忆
    mem = SKILLS_ROOT / "everything-openai-codex" / CLEAN_MEMORY_REL
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(CLEAN_MEMORY, encoding="utf-8")
    print(f"  已还原 {restored} 个文件；{CLEAN_MEMORY_REL} 改写为规范空记忆")
    # 例外 2：清掉自检写出来的测试钩子/规则
    for rel in TEST_ARTIFACTS:
        p = SKILLS_ROOT / "everything-openai-codex" / rel
        if p.exists():
            p.unlink()
            print(f"  已删除自检测试产物: everything-openai-codex/{rel}")


def run_verify() -> int:
    if not VERIFY.exists():
        print(f"  [跳过] 未找到验收脚本 {VERIFY}")
        return 0
    print("\n=== 重放后自动验收 ===")
    cp = subprocess.run([sys.executable, str(VERIFY)], cwd=str(HERE.parents[1]))
    return cp.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="重放技能缺陷修复（DEF-01~DEF-10）")
    ap.add_argument("--check", action="store_true", help="只比对，不写盘")
    ap.add_argument("--restore", action="store_true", help="回滚到 orig/ 快照")
    ap.add_argument("--no-verify", action="store_true", help="不跑验收")
    args = ap.parse_args()

    if args.restore:
        print("=== 回滚技能目录到取证前状态 ===")
        restore()
        return 0

    print("=== 重放修复（fixed/ → 技能目录）===")
    print(f"  目标: {SKILLS_ROOT}")
    report = apply_fixes(dry=args.check)
    for key, label in (("created", "新增"), ("updated", "更新"), ("unchanged", "已是最新")):
        for item in report[key]:
            print(f"  [{label}] {item}")
    for skill in report["missing_source"]:
        print(f"  [缺失] 技能目录不存在: {skill}")

    changed = len(report["created"]) + len(report["updated"])
    if args.check:
        print(f"\n检查完成：{changed} 个文件待修复，{len(report['unchanged'])} 个已是最新")
        return 1 if changed else 0

    print(f"\n已应用：新增 {len(report['created'])} / 更新 {len(report['updated'])} / "
          f"无需改动 {len(report['unchanged'])}")
    if not args.no_verify:
        return run_verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
