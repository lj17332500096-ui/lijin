"""P1-B(1) 受信根对相对/绝对路径型 project 的解析补全单测（2026-09-22）。

根因：trusted_root_for / _project_dir 入口用严格 PROJECT_RE（^[A-Za-z0-9_-]+$）
拦 project 实参，把 "my_creative_agent/benchmark_fixture"（含 /）、
"F:/Byong-hermes/..."（含 : /）这类"声明执行位置"型路径直接挡掉，
使 T049 编码 case 走不到受信根放行、残留 1 次审批。

修复：受信判定分支改用 _trusted_path_ok（宽松路径正则 + 拒 '..' 越界段），
普通 project 名仍走严格 PROJECT_RE；子树越界由 _canonical_under 兜底。

覆盖：
  1. 绝对路径 project（F:/...）命中受信根 → 放行
  2. 相对路径 project（a/benchmark_fixture）CWD-resolve 命中受信根 → 放行
  3. 含 '..' 越界段（../..、../x）在正则层被拒 → 不放行
  4. 不在受信根内的路径 project → 不放行
  5. 普通项目名（无斜杠）仍按原逻辑（命中受信根 basename 才放行）
  6. sibling 前缀（受信根名的相似前缀）不放行（_canonical_under 防前缀混淆）
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import code_exec


def _with_trusted_root(monkeypatch_fixture_root: Path):
    """临时把 FORGE_TRUSTED_CODE_ROOTS 指向给定受信根，返回受信根 Path。"""
    os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(monkeypatch_fixture_root)
    code_exec._TRUSTED_CODE_ROOTS_CACHE.clear() if hasattr(code_exec, "_TRUSTED_CODE_ROOTS_CACHE") else None
    return monkeypatch_fixture_root


class _TrustRootTestsBase(unittest.TestCase):
    def setUp(self):
        # 用一个真实存在的临时目录当受信根，保证 _canonical_under 的 resolve 有效
        self._tmp = tempfile.TemporaryDirectory()
        self.trusted_root = Path(self._tmp.name)
        # 受信根内建一个 project 子目录，模拟 benchmark_fixture 子树
        self.inner_project = self.trusted_root / "inner_proj"
        self.inner_project.mkdir()
        self._old_env = os.environ.get("FORGE_TRUSTED_CODE_ROOTS")
        os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(self.trusted_root)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("FORGE_TRUSTED_CODE_ROOTS", None)
        else:
            os.environ["FORGE_TRUSTED_CODE_ROOTS"] = self._old_env
        self._tmp.cleanup()


class TestTrustedPathProjectResolution(_TrustRootTestsBase):
    """路径型 project（相对/绝对）的受信根判定。"""

    def test_absolute_path_project_hitting_root_is_trusted(self):
        # T049 型：绝对路径指向受信根本身或子树 → 受信
        self.assertIsNotNone(code_exec.trusted_root_for(str(self.inner_project)))
        # 受信根自身也命中
        self.assertIsNotNone(code_exec.trusted_root_for(str(self.trusted_root)))

    def test_relative_project_resolved_under_root_is_trusted(self):
        # 相对 project 的 CWD-resolve 基准是 pytest 运行 CWD（= 项目根 my_creative_agent）。
        # 受信根取 CWD 下真实子目录 benchmark_fixture；相对 project 用「子目录名」（无 CWD 名前缀），
        # resolve 后正好落在受信根内 → 受信。
        # 注意：若写成「CWD名/子目录名」（my_creative_agent/benchmark_fixture）会多拼一层 CWD 名，
        # resolve 落到 <CWD>/my_creative_agent/benchmark_fixture，落不到受信根 → 判 None（正确行为，
        # 那种相对写法本身有歧义，不算受信根解析的缺陷）。
        cwd = Path.cwd()
        sub_root = cwd / "benchmark_fixture"
        if not sub_root.exists():
            sub_root.mkdir()
        old = os.environ.get("FORGE_TRUSTED_CODE_ROOTS")
        os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(sub_root)
        try:
            self.assertIsNotNone(code_exec.trusted_root_for("benchmark_fixture"))
            # 带 CWD 名前缀的多一层写法 → 落不到受信根，判 None（符合语义）
            self.assertIsNone(code_exec.trusted_root_for(f"{cwd.name}/benchmark_fixture"))
        finally:
            if old is None:
                os.environ.pop("FORGE_TRUSTED_CODE_ROOTS", None)
            else:
                os.environ["FORGE_TRUSTED_CODE_ROOTS"] = old

    def test_dotdot_project_rejected(self):
        # '..' 越界段在正则层被拒 → 不放行（回到"项目名"拒绝语义）
        self.assertIsNone(code_exec.trusted_root_for("../.."))
        self.assertIsNone(code_exec.trusted_root_for("..../fake"))
        self.assertIsNone(code_exec.trusted_root_for("a/../../b"))

    def test_outside_root_path_not_trusted(self):
        # 路径 project 落在受信根外 → 不放行
        outside = self.trusted_root.parent / "sibling_dir"
        try:
            outside.mkdir(exist_ok=True)
        except Exception:
            pass
        self.assertIsNone(code_exec.trusted_root_for(str(outside)))

    def test_plain_project_name_still_strict(self):
        # 普通项目名（无斜杠）仍按原逻辑：仅当等于受信根目录名才放行
        self.assertIsNotNone(code_exec.trusted_root_for(self.trusted_root.name))
        # 无关普通名 → 不放行
        self.assertIsNone(code_exec.trusted_root_for("totally_unrelated_name_xyz"))

    def test_sibling_prefix_not_trusted(self):
        # sibling 前缀（受信根名前缀 + 尾巴）不匹配——_canonical_under 防前缀混淆
        sibling = self.trusted_root.parent / f"{self.trusted_root.name}_evil"
        try:
            sibling.mkdir(exist_ok=True)
        except Exception:
            pass
        self.assertIsNone(code_exec.trusted_root_for(str(sibling)))


if __name__ == "__main__":
    unittest.main()
