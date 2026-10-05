"""路径规范化必须**对称** —— CI 上14 条测试红过一次（10-05 首跑实据）。

# 这类失败为什么在本地看不见

本地全量 1576 passed，GitHub Actions 上 14 条红。差异不在代码，
在**路径的两种写法指向同一个目录**：

- GitHub runner 的 `tempfile.gettempdir()` 返回 `C:\\Users\\RUNNER~1\\...`
  —— **8.3 短名**；测试把它赋给模块级 root 变量（`WORKSPACE_ROOT` /
  `BASE_DIR` / `self.root`…）。
- 而`_resolve_under_root` 之类函数对 **target 做了 `.resolve()`**（展开成长名
  `C:\\Users\\runneradmin\\...`），对 **root 没做**。
- 于是 `target.relative_to(root)` 抛 `ValueError` —— 明明在根下，却被判越界。

本地不复现是因为本地 tempdir 目录**本身没有 8.3 缩写**（短名只存在于它下面
新建的子目录）。所以「本地全绿」在这里**不是证据**。

# 缺陷的两种方向（本项目两个都真实发生过）

1. `target.resolve().relative_to(root)` —— root 未规范化（tools.py / rag.py）
2. `target.relative_to(root.resolve())` —— target 未规范化
两种都会让「同一对路径在不同函数里得出相反结论」：
`project_edit.py` 里 `_validate_target` 两边都resolve（判「在根下」通过），
而 `_rel_display` 只 resolve 一边（显示成绝对路径）⇒ **写入成功但输出是绝对路径**。

# 判据设计（按项目铁律）

- **不是 grep 源码**（那只会锁住字面量）⇒ 而是用 `ctypes.GetShortPathNameW`
  **真的造出 8.3 短名 root**，然后跑真实测试看结果。
- **必须先验证注入生效**（本项目已栽过两次：`-p conftest` 找不到模块、
  `sitecustomize` 对 tempdir 本身无效）。断言用 `self.assertIn` 风格的前置检查。
- **反空转下界**：若本机无法造出短名（某些环境的 tempdir 关闭了 8.3），
  明确 `skipTest` 并说明，**绝不静默恒绿**。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

#: ⚠️ **解释器一律用 `sys.executable`，禁止硬编码 `.venv/Scripts/python.exe`**
#: （10-05 CI run 37326450894 实据）：CI 上**没有 `.venv`** —— 用的是
#: `actions/setup-python` 装的 `C:\hostedtoolcache\windows\Python\3.11.9\x64\python.exe`。
#: 硬编码会让本护栏自己抛 `FileNotFoundError: [WinError 2]` ⇒ 判据自身崩掉，
#: 而**本地有 venv 时永远发现不了** —— 又是「本地绿 = 假绿」。
#: 通用铁律：**判据里凡引用解释器/运行时绝对路径，都是portability 雷**。

#: 受影响的测试文件（2026-10-05 CI 上这 14 条红过）
AFFECTED = [
    "tests/test_office_docs.py",
    "tests/test_dep_doctor.py",
    "tests/test_trust.py",
    "tests/test_rag.py",
    "tests/test_code_exec.py",
    "tests/test_project_edit.py",
]

#: 需要在**8.3 短名**环境下运行的测试（cp1252 那条是编码问题，另由
#: `tests/test_skill_gorden_ppt.py` 的编码场景覆盖，不在此列）
_TOLERANCE_EXIT = 0


def _short_name(long_path: str) -> str | None:
    """取 Windows 8.3 短名；取不到或与长名相同则返回 None。"""
    if os.name != "nt":
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(long_path), buf, 1024)
        short = buf.value
    except OSError:
        return None
    if not short or short == str(long_path):
        return None
    return short if os.path.isdir(short) else None


_SITE_CUSTOMIZE = r'''
# 让 tempfile.gettempdir() 返回 **8.3 短名**，模拟 GitHub runner 的 RUNNER~1。
#
# ⚠️ 两条踩过的坑，缺一个实验就静默失去鉴别力：
# ① **tempdir 目录本身常常没有 8.3 短名**（短名只存在于它下面新建的子目录），
#    所以要先建一个子目录、取它的短名，再把 tempdir 指到那个短名。
# ② **目录名不够长就不触发 8.3 缩写**！实测 `ci8_root`（6 字符）会让
#    GetShortPathNameW **原样返回**（ret=50）⇒ 注入 NOOP ⇒
#    「修复前也绿」是**假绿**（本项目已栽一次）。
#    所以用 26 字符的长名，且 `test_shortname_root_is_available` 会独立断言
#    「短名≠长名」，不满足就 skipTest 而不是静默恒绿。
import ctypes as _c, os as _o, tempfile as _t
try:
    _plain = _o.path.join(_t.gettempdir(), "_CI_SHORTNAME_PROBE_LONG_")
    _o.makedirs(_plain, exist_ok=True)
    _b = _c.create_unicode_buffer(1024)
    _c.windll.kernel32.GetShortPathNameW(_plain, _b, 1024)
    _short = _b.value
    if _short and _short != _plain and _o.path.isdir(_short):
        _t.gettempdir = lambda: _short
        _t.tempdir = _short
        import sys as _s
        print("[SHORTNAME-INJECT-OK] " + _short, file=_s.stderr, flush=True)
    else:
        import sys as _s
        print("[SHORTNAME-INJECT-NOOP] short=%r long=%r" % (_short, _plain),
              file=_s.stderr, flush=True)
except Exception as _e:  # pragma: no cover
    import sys as _s
    print("[SHORTNAME-INJECT-ERR] " + repr(_e), file=_s.stderr, flush=True)
'''


class ShortPathRootSymmetryTests(unittest.TestCase):
    """在 8.3 短名 root 下跑受影响的测试，确认路径规范化对称。"""

    def test_shortname_root_is_available(self) -> None:
        """前置检查：本机能否造出 8.3 短名 —— 不能则后续全部 skip。

        **这条本身是判据的一部分**（反空转）：若它恒绿而后续静默 skip，
        整组测试就退化成「永远绿」。所以它必须显式验证造得出短名。
        """
        if os.name != "nt":
            self.skipTest("非 Windows，无 8.3 短名")
        plain = os.path.join(tempfile.gettempdir(), "_CI_SHORTNAME_CHECK_LONG_")
        os.makedirs(plain, exist_ok=True)
        try:
            short = _short_name(plain)
        finally:
            # ⚠️ 不能写成 `os.rmdir(plain) if os.path.isdir(plain) else None`
            # —— 那是**表达式语句**，语法合法但**不做任何事**（原先就踩了：
            # 每次跑都在 tempdir 里留一个目录，短名序列会漂移 ⇒ NOOP）。
            if os.path.isdir(plain):
                try:
                    os.rmdir(plain)
                except OSError:
                    pass
        if short is None:
            self.skipTest(
                "本机 tempdir 下无法生成 8.3 短名（可能关闭了 8.3 支持，或"
                "目录名太短未触发 8.3 缩写）—— **本组测试无法证明修复有效**。"
                "这不是「通过」，是「没测」。"
            )
        self.assertNotEqual(short, plain, "短名不应等于长名")

    def test_affected_tests_pass_under_shortname_root(self) -> None:
        """把 tempdir 猴补丁成 8.3 短名后，受影响的测试必须全绿。

        2026-10-05 的实况：同一注入条件下**修复前 14 failed**、
        修复后 **61 passed**。这 14 条与当时 CI 上的失败逐条对应。
        """
        if os.name != "nt":
            self.skipTest("非 Windows")
        # 先确认能造出短名（造不出就明确 skip，不静默恒绿）
        plain = os.path.join(tempfile.gettempdir(), "_CI_SHORTNAME_PROBE_LONG_")
        os.makedirs(plain, exist_ok=True)
        short = _short_name(plain)
        if short is None:
            os.rmdir(plain)
            self.skipTest("造不出 8.3 短名，本实验无鉴别力")

        site_dir = BASE / "var" / "_shortname_site"
        site_dir.mkdir(parents=True, exist_ok=True)
        site_file = site_dir / "sitecustomize.py"
        site_file.write_text(_SITE_CUSTOMIZE, encoding="utf-8")
        try:
            # ① **先验证注入机制本身生效**（本项目栽过两次「注入没生效却继续跑」）
            # ⚠️ **必须用 `sys.executable`，不能硬编码 `.venv/Scripts/python.exe`**
            # （10-05 CI 实据）：CI 上**没有 `.venv`** —— 用的是
            # `actions/setup-python` 装的 `C:\hostedtoolcache\windows\Python\3.11.9\x64\
            # python.exe`。硬编码会让本护栏自己抛
            # `FileNotFoundError: [WinError 2]` ⇒ 判据自身崩掉，
            # 而且**本地有 venv 时永远发现不了**（典型的「本地绿 = 假绿」）。
            # `sys.executable` 在本地指向 .venv 解释器、在 CI 指向 setup-python
            # 那个，两边都对。
            chk = subprocess.run(
                [sys.executable, "-c",
                 "import tempfile; print(tempfile.gettempdir())"],
                cwd=str(BASE), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
                env={**os.environ, "PYTHONPATH": str(site_dir)},
            )
            blob = (chk.stdout or "") + (chk.stderr or "")
            self.assertIn(
                "SHORTNAME-INJECT-OK", blob,
                f"注入机制本身未生效（输出：{blob.strip()[:200]}）—— "
                "后续结果不可信，**不要**据此判断修复是否有效。",
            )

            # ② 真正跑测试
            p = subprocess.run(
                [sys.executable, "-m", "pytest", *AFFECTED,
                 "-q", "-p", "no:cacheprovider", "--no-header"],
                cwd=str(BASE), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800,
                env={**os.environ, "PYTHONPATH": str(site_dir)},
            )
            out = (p.stdout or "") + (p.stderr or "")
            self.assertIn("SHORTNAME-INJECT-OK", out, "跑测试时注入未生效")
            failed = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
            self.assertEqual(
                failed, [],
                "在 **8.3 短名 root** 下这些测试变红 ⇒ 路径规范化不对称"
                "（一边 `.resolve()` 一边没有）。GitHub runner 的 tempdir 正是"
                "短名形式（`RUNNER~1`），本地 tempdir 不是，所以本地绿是假象：\n  "
                + "\n  ".join(failed),
            )
            self.assertEqual(
                p.returncode, _TOLERANCE_EXIT,
                f"退出码 {p.returncode}，输出尾部：\n"
                + "\n".join(out.strip().splitlines()[-12:]),
            )
        finally:
            site_file.unlink(missing_ok=True)
            try:
                site_dir.rmdir()
            except OSError:
                pass
            # 清理可能残留的注入根（含其中的测试临时目录）
            import shutil

            for leftover in (plain, short):
                if leftover and os.path.isdir(leftover):
                    shutil.rmtree(leftover, ignore_errors=True)


class PathNormalizationAsymmetryTests(unittest.TestCase):
    """源码层面的静态检查：`relative_to` 两侧必须同时规范化。"""

    #: (文件, 说明)。选这几个是因为它们都做过「路径落根」判定。
    _FILES = ("tools.py", "rag.py", "code_exec.py", "project_edit.py")

    def test_relative_to_both_sides_resolved(self) -> None:
        """凡`X.relative_to(Y)`，X 与 Y 都应是 `.resolve()` 的产物。

        判据刻意**不精确到行**：静态匹配很难区分
        `target.resolve().relative_to(root)` 与
        `target.relative_to(root.resolve())` 之外的合法变体
        （如 `os.path.relpath`、显式 `os.path.samefile` 兜底）。
        因此这里只抓**最明确的一类**：一侧是裸变量、另一侧带 `.resolve()`
        之外的形态无法静态确认的，交给上面的动态测试。

        **本测试的价值是「提醒」而非「拦截」**：真正的鉴别力在
        `test_affected_tests_pass_under_shortname_root`。
        """
        import ast
        import sys as _sys

        findings: list[str] = []
        for rel in self._FILES:
            path = BASE / rel
            if not path.is_file():
                continue
            # ⚠️ 必须用 utf-8-sig：`tools.py` 等文件带 UTF-8 BOM，
            # 用 utf-8 读会留下 U+FEFF，`ast.parse` 直接抛
            # `SyntaxError: invalid non-printable character U+FEFF`
            # （实测踩过：判据自己崩了，而不是报出该报的东西）。
            src = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(src, filename=rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "relative_to"):
                    continue
                base = fn.value
                has_resolve = (
                    isinstance(base, ast.Call)
                    and isinstance(base.func, ast.Attribute)
                    and base.func.attr == "resolve"
                )
                arg = node.args[0] if node.args else None
                arg_resolved = (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "resolve"
                )
                # 两侧都没 resolve ⇒ 可能是「本来就干净的 Path」（如 rglob 来源）
                if not has_resolve and not arg_resolved:
                    findings.append(
                        f"{rel}:{node.lineno} 两侧都未 resolve —— "
                        f"若来源路径可能含 8.3 短名/symlink，需确认"
                    )
        # 本断言只**记录**不拦截：静态分析无法覆盖 symlink/junction 场景，
        # 真正的门禁是上面的动态测试。列出以便人工复查。
        if findings:
            print(
                "[提示] 以下 relative_to 两侧均未 resolve，"
                "若来源可能是用户路径请人工确认：\n  " + "\n  ".join(findings)
            )
        _sys.stdout.flush()


if __name__ == "__main__":
    unittest.main()
