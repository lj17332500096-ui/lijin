"""sandbox 快照三件套的冒烟验证（首次真实执行）。

动机：`sandbox_snapshot` / `sandbox_rollback` / `list_sandbox_snapshots` 在生产库
累计 0 次调用，`code_sandbox/.snapshots` 目录**从未被创建过**。
一个从未被执行过的代码路径，等于没有验证过的约束 —— 它可能一跑就报错。

本探针在一个临时项目上跑完整闭环：
    写文件 → 拍快照 → 破坏文件 → 回滚 → 校验内容 → 列快照
结束时删除自己创建的临时项目与快照，不留痕迹。

用法：
    ./.venv/Scripts/python.exe delivery/probe_sandbox_smoke.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROJECT = "zz_smoke_probe_tmp"  # 前缀 zz_ 便于识别为探针产物


def main() -> int:
    from runtime import sandbox_snapshot as ss

    sandbox_root = ss.SANDBOX_ROOT
    project_dir = sandbox_root / PROJECT
    snap_base = ss._snap_base(PROJECT)
    print(f"沙箱根目录   : {sandbox_root}")
    print(f"快照根目录   : {snap_base}")
    print(f"探针前是否存在: {snap_base.exists()}\n")

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    try:
        if project_dir.exists():
            shutil.rmtree(project_dir)
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        (project_dir / "helper.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        print("步骤 1 · 拍快照")
        out = ss.snapshot_impl(PROJECT, note="smoke probe")
        check("snapshot_impl 返回成功", "失败" not in out and "错误" not in out, out.strip().splitlines()[0][:70])
        check("快照目录已创建", snap_base.exists())
        snaps = [p for p in snap_base.rglob("*") if p.is_dir()]
        print(f"         快照目录下子目录数: {len(snaps)}")

        print("\n步骤 2 · 破坏文件（改内容 + 删文件 + 加新文件）")
        (project_dir / "main.py").write_text("VALUE = 999\n", encoding="utf-8")
        (project_dir / "helper.py").unlink()
        (project_dir / "junk.py").write_text("junk = True\n", encoding="utf-8")

        print("\n步骤 3 · dry_run 回滚（只列变更）")
        dry = ss.rollback_impl(PROJECT, dry_run=True)
        check("dry_run 报告包含被改动的 main.py", "main.py" in dry, dry.strip().splitlines()[0][:70])

        print("\n步骤 4 · 真实回滚")
        rb = ss.rollback_impl(PROJECT)
        check("rollback_impl 返回成功", "失败" not in rb and "错误" not in rb, rb.strip().splitlines()[0][:70])

        print("\n步骤 5 · 校验回滚结果")
        main_text = (project_dir / "main.py").read_text(encoding="utf-8")
        check("main.py 内容已还原", main_text == "VALUE = 1\n", repr(main_text))
        check("被删的 helper.py 已恢复", (project_dir / "helper.py").is_file())
        check("新增的 junk.py 已被清除", not (project_dir / "junk.py").exists())

        print("\n步骤 6 · 列出快照")
        listing = ss.list_snapshots_impl(PROJECT)
        check("list 能列出快照", bool(listing.strip()) and "错误" not in listing,
              listing.strip().splitlines()[0][:70])

    finally:
        print("\n清理探针产物")
        removed = []

        def _show(p: Path) -> str:
            try:
                return str(p.relative_to(ROOT))
            except ValueError:
                return str(p)

        if project_dir.exists():
            shutil.rmtree(project_dir)
            removed.append(_show(project_dir))
        # 只删本探针自己那一个项目快照目录，绝不动 SNAPSHOT_ROOT 的父层
        if snap_base.exists():
            shutil.rmtree(snap_base, ignore_errors=True)
            removed.append(_show(snap_base))
        print("   已删除: " + (", ".join(removed) if removed else "无需清理"))

    print("\n" + "=" * 56)
    if failures:
        print(f"冒烟验证失败 {len(failures)} 项: {', '.join(failures)}")
        return 1
    print("冒烟验证全部通过 —— 三件套在真实执行下工作正常（属「备而未用」，不是坏的）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
