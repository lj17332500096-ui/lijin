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
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import code_exec


def call_tool(tool, **kwargs) -> str:
    """@function_tool 包装过的工具不是裸函数，按 SDK 约定用 on_invoke_tool 调。"""
    import asyncio
    import json

    from agents.tool_context import ToolContext

    input_json = json.dumps(kwargs, ensure_ascii=False)

    async def _invoke() -> str:
        ctx = ToolContext(context=None, tool_name=tool.name,
                          tool_call_id="t", tool_arguments=input_json)
        r = tool.on_invoke_tool(ctx, input_json)
        if asyncio.iscoroutine(r):
            r = await r
        return str(r)

    return asyncio.run(_invoke())


#: P1-14（2026-09-22）：受信代码根现在是**评测/测试专用**免审批通道
#: （code_exec._trusted_code_roots 只在 FORGE_EVAL_MODE/FORGE_TEST_MODE=1 时读取）。
#: 本文件测的正是该机制，因此显式声明测试模式。
os.environ.setdefault("FORGE_TEST_MODE", "1")


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
        # P1-14：受信代码根是评测/测试专用免审批通道，只有显式声明测试模式才生效。
        # 每个用例重设一次（其他测试文件可能 del 掉该变量）。
        os.environ["FORGE_TEST_MODE"] = "1"
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


class TestSourceRootNeverWorkdir(unittest.TestCase):
    """P0-blat 回归护栏：工作目录**绝不能**是项目源码根 BASE_DIR。

    缺陷背景（2026-10-06 实测）：FORGE_TRUSTED_CODE_ROOTS 被设成仓库根时，
    _project_dir 曾直接 `return root`，run_python 便以仓库根为 CWD 执行 LLM 生成代码，
    生成代码的 `tempfile.mkstemp(prefix='', dir=os.getcwd())` 在仓库根落下 100 个
    4 字节 `blat` 垃圾文件，且 .gitignore 无规则匹配，`git add -A` 会全部收走。

    **判据说明**：判「root 是否等于 BASE_DIR」而不是「resolved 是否等于 root」——
    实测污染场景 project 是纯项目名 "my_creative_agent"，`Path(proj).resolve()`
    会拼上 CWD，根本不等于 root，用后者判会漏修；而正常评测场景
    （受信根 = benchmark_fixture，project = 该根自身）反而会被前者误伤免审批通道。
    """

    def setUp(self):
        self._old_env = os.environ.get("FORGE_TRUSTED_CODE_ROOTS")
        os.environ["FORGE_TEST_MODE"] = "1"
        os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(code_exec.BASE_DIR)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("FORGE_TRUSTED_CODE_ROOTS", None)
        else:
            os.environ["FORGE_TRUSTED_CODE_ROOTS"] = self._old_env

    def test_project_dir_never_returns_source_root(self):
        """主断言：受信根 = 仓库根时，_project_dir 不得返回 BASE_DIR 本身。"""
        d = code_exec._project_dir("my_creative_agent")
        self.assertIsNotNone(d)
        self.assertNotEqual(
            Path(d).resolve(), code_exec.BASE_DIR.resolve(),
            "工作目录回落到了源码根 BASE_DIR —— 会让 LLM 生成代码的临时文件污染仓库",
        )

    def test_absolute_source_root_path_never_returns_source_root(self):
        """绝对路径形态的仓库根（含正反斜杠）同样不得返回 BASE_DIR。"""
        for raw in (str(code_exec.BASE_DIR), code_exec.BASE_DIR.as_posix()):
            with self.subTest(raw=raw):
                d = code_exec._project_dir(raw)
                self.assertIsNotNone(d)
                self.assertNotEqual(Path(d).resolve(), code_exec.BASE_DIR.resolve())

    def test_safe_workdir_guard_redirects_source_root(self):
        """防御纵深：即使调用方硬塞 BASE_DIR 进来，_safe_workdir 也必须改道。"""
        guarded = code_exec._safe_workdir(code_exec.BASE_DIR, "my_creative_agent")
        self.assertIsNotNone(guarded)
        self.assertNotEqual(Path(guarded).resolve(), code_exec.BASE_DIR.resolve())

    def test_trusted_subdir_still_works(self):
        """反向护栏（防过度修复）：受信根的**子目录**仍须正常命中，不得被误伤。

        这是 benchmark 本地验证（benchmark_fixture/app/auth.py 那类）的正常语义；
        若本断言变红，说明修法用错了判据（把「合法子树」一起拒了）。
        """
        tmp = tempfile.TemporaryDirectory()
        try:
            fixture = Path(tmp.name) / "fixture"
            (fixture / "app").mkdir(parents=True)
            os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(fixture)
            d = code_exec._project_dir(str(fixture / "app"))
            self.assertIsNotNone(d)
            self.assertEqual(Path(d).resolve(), fixture.resolve())
        finally:
            tmp.cleanup()


class TestTrustedRootLoopDoesNotShortCircuit(unittest.TestCase):
    """P0-2 回归护栏：多受信根时，源码根**不得**让循环提前终止。

    缺陷背景（2026-10-06 实测）：`_project_dir` 的受信分支里写的是 `break`，
    而 `break` 跳出的是**整个 for 循环** ⇒ 当列表里第一个匹配的 root 就是仓库根时，
    后续的合法受信根**再无机会被检查**。

    实测配置（评测真实场景）：
        FORGE_TRUSTED_CODE_ROOTS=<仓库根>,<仓库根>/benchmark_fixture
        project=<仓库根>/benchmark_fixture/app

    | 版本          |落点                    | 命中 fixture |
    |---------------|------------------------|--------------|
    | HEAD          | BASE_DIR（**危险**）   | ✅           |
    | break（上一轮）| code_sandbox\\app       | ❌ 合法通道丢 |
    | continue（本轮）| benchmark_fixture      | ✅           |
    """

    def setUp(self):
        self._old_env = os.environ.get("FORGE_TRUSTED_CODE_ROOTS")
        os.environ["FORGE_TEST_MODE"] = "1"
        self._fixture = code_exec.BASE_DIR / "benchmark_fixture"
        if not self._fixture.is_dir():
            self.skipTest("仓库内无 benchmark_fixture 目录，无法构造多受信根场景")
        # 仓库根**排在前**，fixture 排在后—— 正是触发提前终止的顺序
        os.environ["FORGE_TRUSTED_CODE_ROOTS"] = (
            f"{code_exec.BASE_DIR},{self._fixture}"
        )

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("FORGE_TRUSTED_CODE_ROOTS", None)
        else:
            os.environ["FORGE_TRUSTED_CODE_ROOTS"] = self._old_env

    def test_later_trusted_root_still_checked(self):
        """仓库根排第一时，后面的合法受信根仍须被检查并命中。"""
        d = code_exec._project_dir(str(self._fixture / "app"))
        self.assertIsNotNone(
            d, "多受信根场景下合法受信根被跳过 —— break 应为 continue"
        )
        self.assertEqual(
            Path(d).resolve(),
            self._fixture.resolve(),
            "落点不是 benchmark_fixture：源码根让循环提前终止了",
        )

    def test_source_root_still_never_returned(self):
        """反向锁定：修break 时不能把「不返回 BASE_DIR」这个保证弄丢。"""
        d = code_exec._project_dir("my_creative_agent")
        self.assertIsNotNone(d)
        self.assertNotEqual(Path(d).resolve(), code_exec.BASE_DIR.resolve())


class TestDirectoryJunctionEscapeBlocked(unittest.TestCase):
    """P0-1 回归护栏：**目录级** junction/symlink 不得把工作目录带出SANDBOX_ROOT。

    缺陷背景（2026-10-06 实测，验证者 A-3）：`_sandbox_dir` 末行 `.resolve()`
    **会跟随目录级 junction**，于是两步即可越界，且只需工具自身能力：

        ① 一次 run_python，生成代码执行 `mklink /J code_sandbox\\stage2 <仓外目录>`
          （junction **不需要管理员权限**）
        ② 第二次用 `project="stage2"`（**纯项目名**，完全通过字符白名单）

    实测 HEAD：`_project_dir("stage2")` 返回**仓外**路径，
    且`write_code_file`/`read_code_file`/`list_code_files` 同样越界，
    **不需要 `ALLOW_CODE_EXEC`**（门槛比 run_python 更低）。

    本类不真的写受害目录，只断言「落点」（_project_dir / _safe_workdir）。
    """

    def setUp(self):
        # 实验室一律建在**系统 tempdir**，绝不在仓库内留活symlink
        # （上一轮 `_symlink_lab` 留在仓库根，导致 test_tool_roster_consistency
        #   6 条假红，浪费一整轮排查）
        self._lab = Path(tempfile.mkdtemp(prefix="junction_escape_"))
        self._sandbox = self._lab / "code_sandbox"
        self._sandbox.mkdir()
        self._outside = self._lab / "outside_victim"
        self._outside.mkdir()
        self._orig_root = code_exec.SANDBOX_ROOT
        code_exec.SANDBOX_ROOT = self._sandbox

    def tearDown(self):
        code_exec.SANDBOX_ROOT = self._orig_root
        # junction 必须用 os.rmdir 删（只删链接本身，不动目标）
        link = self._sandbox / "stage2"
        if link.exists():
            try:
                os.rmdir(link)
            except OSError:
                pass
        shutil.rmtree(self._lab, ignore_errors=True)

    def _make_junction(self) -> bool:
        """在沙箱根下建一个指向仓外目录的 junction（junction 不需要管理员权限）。"""
        link = self._sandbox / "stage2"
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(self._outside)],
            capture_output=True, text=True, timeout=60,
            # mklink 的输出是本地代码页（GBK），不是 UTF-8 ⇒ 必须 errors="replace"
            # 否则 Windows 上会抛 UnicodeDecodeError（实测 position 180 invalid start byte）
            encoding="utf-8", errors="replace",
        )
        return link.exists()

    def test_project_dir_rejects_outside_junction(self):
        """主断言：目录级 junction 指向根外时，_project_dir 不得返回该落点。

        ⚠️ 断言方向（2026-10-06 中和实验 A 实测踩过）：**不能**写成
        `assertFalse(_path_is_within(d, root))` —— 攻击**成功**时 d 就是根外路径，
        `_path_is_within` 返 False，`assertFalse(False)` **照样绿**
        ⇒ 护栏鉴别力为零（EXP A 撤掉校验后 42 passed 全绿）。
        正确写法：要么返回 None（拒绝），要么返回的路径**必在根内**。
        """
        if os.name != "nt":
            self.skipTest("junction 是 Windows 专有")
        if not self._make_junction():
            self.skipTest("本机无法创建 junction（权限/文件系统不支持）")
        d = code_exec._project_dir("stage2")
        if d is None:
            return  # 拒绝落点 = 期望行为（调用方走 base_dir is None 早退分支）
        self.assertTrue(
            code_exec._path_is_within(d, self._sandbox),
            f"目录级 junction 逃逸：落点 {d} 的真实位置在 {self._sandbox} 之外",
        )

    def test_safe_workdir_rejects_outside_junction(self):
        """纵深：_safe_workdir 也必须挡住这个落点（同上，断言方向是「在根内」）。"""
        if os.name != "nt":
            self.skipTest("junction 是 Windows 专有")
        if not self._make_junction():
            self.skipTest("本机无法创建 junction（权限/文件系统不支持）")
        w = code_exec._safe_workdir(self._sandbox / "stage2", "stage2")
        if w is None:
            return
        self.assertTrue(
            code_exec._path_is_within(w, self._sandbox),
            f"_safe_workdir 放过了根外落点：{w}",
        )

    def test_write_tool_cannot_escape_via_junction(self):
        """端到端扩散面：`write_code_file` 不得经junction 把文件写进根外。

        这是 P0-1 危害最大的那条通路——**不需要 ALLOW_CODE_EXEC**，门槛最低。
        """
        if os.name != "nt":
            self.skipTest("junction 是 Windows 专有")
        if not self._make_junction():
            self.skipTest("本机无法创建 junction（权限/文件系统不支持）")
        canary = self._outside / "PWNED.txt"
        out = call_tool(code_exec.write_code_file, project="stage2",
                        filename="PWNED.txt", content="x")
        self.assertFalse(
            canary.exists(),
            f"write_code_file 经 junction 把文件写到了根外：{canary}（返回：{out[:80]}）",
        )

    def test_real_path_helper_rejects_sibling_prefix(self):
        """防「前缀比较」回退：`code_sandbox_evil` 以 `code_sandbox` 开头。

        这是本项目的铁律——一旦有人把 `_path_is_within` 改成 `startswith`，
        这条会立刻变红。
        """
        sibling = self._lab / "code_sandbox_evil"
        sibling.mkdir()
        self.assertFalse(
            code_exec._path_is_within(sibling, self._sandbox),
            "sibling 前缀目录被判成在根内 —— 用了 startswith/字符串前缀？",
        )
        self.assertTrue(code_exec._path_is_within(self._sandbox / "ok", self._sandbox))


class TestWindowsReservedNames(unittest.TestCase):
    """P2-3：`CON`/`NUL`/`COM1` 等 Windows 保留设备名不得原样落成沙箱目录名。

    这些名字在 Win32 命名空间里是**设备**而非普通目录，`mkdir` 会失败或
    （更糟）静默指向设备。带扩展名的形态（`CON.txt`）同样按设备处理。
    """

    def test_reserved_names_detected(self):
        for name in ("CON", "con", "NUL", "PRN", "AUX", "COM1", "LPT9", "CON.txt"):
            with self.subTest(name=name):
                self.assertTrue(code_exec._is_windows_reserved(name))

    def test_normal_names_not_flagged(self):
        for name in ("demo", "console", "m3_fixture", "com10", "auxiliary"):
            with self.subTest(name=name):
                self.assertFalse(code_exec._is_windows_reserved(name))

    def test_leaf_never_returns_reserved_name(self):
        for name in ("CON", "NUL", "COM1"):
            with self.subTest(name=name):
                leaf = code_exec._sandbox_leaf(name)
                self.assertFalse(
                    code_exec._is_windows_reserved(leaf),
                    f"保留设备名 {name} 竟原样落成目录名 {leaf}",
                )


class TestProjectNameCollision(unittest.TestCase):
    """P2-1：`'a/b'` 与 `'b'` 不得共用同一个沙箱目录。

    缺陷背景（2026-10-06 实测）：`_sandbox_dir` 只取末段名字 ⇒
    `write_code_file(project="a/b", filename="secret.py")` 写到 `b\\secret.py`，
    随后 `read_code_file(project="b", ...)` 就能读到 —— 隔离边界失效。
    """

    def _leaf(self, proj: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        try:
            orig = code_exec.SANDBOX_ROOT
            code_exec.SANDBOX_ROOT = Path(tmp.name)
            d = code_exec._project_dir(proj)
            return "" if d is None else Path(d).name
        finally:
            code_exec.SANDBOX_ROOT = orig
            tmp.cleanup()

    def test_multi_segment_does_not_collide_with_leaf(self):
        self.assertNotEqual(
            self._leaf("a/b"), self._leaf("b"),
            "'a/b' 与 'b' 落进同一目录 —— 跨 project 隔离失效",
        )
        self.assertNotEqual(
            self._leaf("nested/deep/proj"), self._leaf("proj"),
            "'nested/deep/proj' 与 'proj' 落进同一目录",
        )

    def test_same_input_is_stable(self):
        """哈希后缀必须**稳定**（跨调用一致），否则同一 project 每次落点都变。"""
        self.assertEqual(self._leaf("a/b"), self._leaf("a/b"))

    def test_plain_name_unchanged(self):
        """防过度修复：单个安全段名保持原样（benchmark/常用名零行为变更）。"""
        for name in ("demo", "m3_fixture", "micro_fixture"):
            with self.subTest(name=name):
                self.assertEqual(self._leaf(name), name)

    def test_leaf_is_readable(self):
        """可读性不被哈希吃掉：目录名仍以原末段名开头。"""
        leaf = self._leaf("a/b")
        self.assertTrue(leaf.startswith("b"), f"{leaf} 应以末段名 b 开头，便于排查")


class TestRunTestsNeverUsesSourceRoot(unittest.TestCase):
    """P1-1 回归护栏：`run_tests_impl` 不得以 BASE_DIR 为 CWD。

    缺陷背景（2026-10-06 实测，验证者 E-3）：上一轮只把 `_safe_workdir`
    加在 `run_python_impl`，`run_tests_impl` 是**同一缺陷的漏网分支** ——
    它走 `trusted_root_for` 的「绝对 filename」分支直接 `return root`，
    而 `_is_source_root` 的判据只加在 `_project_dir` 的两个分支上，管不到这条路。

    实测：`run_tests_impl(target=<仓内绝对路径>)` 时 `PROBE_CWD_IS_BASE_DIR = True`
    且 pytest 退出码 0（看起来"成功"）。危害不止 CWD：pytest **收集阶段**
    就会执行 conftest.py 与已注册插件 ⇒ 真实的代码执行通道。
    """

    def setUp(self):
        self._old_env = os.environ.get("FORGE_TRUSTED_CODE_ROOTS")
        self._old_exec = os.environ.get("ALLOW_CODE_EXEC")
        os.environ["FORGE_TEST_MODE"] = "1"
        os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(code_exec.BASE_DIR)
        os.environ["ALLOW_CODE_EXEC"] = "true"
        # 仓内一个真实存在的测试文件当 target（绝对路径分支）
        self._target = code_exec.BASE_DIR / "tests" / "test_trust.py"
        if not self._target.is_file():
            self.skipTest("仓内无 tests/test_trust.py，无法构造绝对 target 场景")

    def tearDown(self):
        for k, v in (("FORGE_TRUSTED_CODE_ROOTS", self._old_env),
                     ("ALLOW_CODE_EXEC", self._old_exec)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_trusted_root_for_absolute_target_still_resolves(self):
        """受信判定本身仍须能识别「绝对 target 落在受信根内」。

        ⚠️ 这里**刻意不断言** `trusted_root_for` 不得返回 BASE_DIR：
        `trusted_root_for` 的职责是「这个声明位置是否在某个受信根内」，
        当运维把 `FORGE_TRUSTED_CODE_ROOTS` 设成仓库根时，它**如实**返回仓库根
        ——那是配置声明，不是漏洞。真正的防线是「**不得把它当CWD**」，
        由 `_safe_workdir` 在 `run_python_impl`/`run_tests_impl` 两个执行点承担
        （见下面两条）。把 `trusted_root_for` 改成对源码根返 None会连带
        改变 `runtime/approval.py:246` 的免审批语义，属越界改动。
        """
        root = code_exec.trusted_root_for("", str(self._target), "")
        self.assertIsNotNone(root, "受信根判定失效：仓内绝对路径本应命中受信根")

    def test_run_tests_impl_cwd_is_not_source_root(self):
        """端到端：拦截 _spawn_with_job 捕获真实 CWD，断言不是 BASE_DIR。

        这是 P1-1 的**主断言**。用**相对** target：绝对 target 在受信根=
        源码根时会被`_canonical_under` 早退（"测试目标必须在项目根内"），
        那样就**测不到 CWD 了** —— 早退本身安全，但不是本条要证的命题。
        """
        import code_exec as ce

        seen: dict[str, str] = {}
        real_spawn = ce._spawn_with_job

        def spy(job, argv, **kwargs):
            seen["cwd"] = str(kwargs.get("cwd") or "")
            return None  # 立刻返回 None，不真的起 pytest（零副作用）

        ce._spawn_with_job = spy
        try:
            ce.run_tests_impl("anything", target="tests/test_trust.py")
        finally:
            ce._spawn_with_job = real_spawn

        self.assertIn("cwd", seen, "未捕获到 CWD（调用链变了？）")
        self.assertFalse(
            code_exec._is_source_root(Path(seen["cwd"])),
            f"run_tests_impl 以源码根为 CWD：{seen['cwd']}",
        )

    def test_run_tests_impl_absolute_target_not_executed_at_source_root(self):
        """绝对 target 场景：要么被早退拒绝，要么 CWD 不是源码根——**两者都安全**。"""
        import code_exec as ce

        seen: dict[str, str] = {}
        real_spawn = ce._spawn_with_job
        ce._spawn_with_job = lambda j, a, **k: (seen.__setitem__("cwd", str(k.get("cwd"))), None)[1]
        try:
            out = ce.run_tests_impl("anything", target=str(self._target))
        finally:
            ce._spawn_with_job = real_spawn

        if "cwd" in seen:
            self.assertFalse(
                code_exec._is_source_root(Path(seen["cwd"])),
                f"绝对 target 场景以源码根为 CWD 执行了：{seen['cwd']}",
            )
        else:
            self.assertIn("错误", out, f"既没执行也没给出错误说明：{out[:120]}")

    def test_legitimate_trusted_root_not_broken(self):
        """反向护栏（防过度修复）：受信根**不是**源码根时，run_tests 语义不变。"""
        import code_exec as ce

        tmp = tempfile.TemporaryDirectory()
        try:
            fixture = Path(tmp.name) / "fixture"
            (fixture / "app").mkdir(parents=True)
            os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(fixture)
            seen: dict[str, str] = {}
            real_spawn = ce._spawn_with_job
            ce._spawn_with_job = lambda j, a, **k: (seen.__setitem__("cwd", str(k.get("cwd"))), None)[1]
            try:
                ce.run_tests_impl("app", target=str(fixture / "app"))
            finally:
                ce._spawn_with_job = real_spawn
            self.assertEqual(
                Path(seen.get("cwd", "")).resolve(), fixture.resolve(),
                "合法受信根（benchmark fixture 那类）被误改道了",
            )
        finally:
            tmp.cleanup()


class TestSafeWorkdirIsActuallyCalled(unittest.TestCase):
    """P1-2 回归护栏：`run_python_impl` / `run_tests_impl` **真的调用**了 `_safe_workdir`。

    缺陷背景（2026-10-06 实测，验证者 D-1）：原护栏
    `test_safe_workdir_guard_redirects_source_root` 只**直接调用** `_safe_workdir()`
    验证函数自身行为，**没有任何用例验证调用点接了它** ⇒ 实测撤掉
    `run_python_impl` 里的那行调用后，**24 个用例全绿**，第二道防线静默失效。

    ⚠️ 这条正是中和实验 3 判定为「无护栏」的缺口。
    """

    def setUp(self):
        self._old_test_mode = os.environ.get("FORGE_TEST_MODE")
        self._old_roots = os.environ.get("FORGE_TRUSTED_CODE_ROOTS")
        self._old_exec = os.environ.get("ALLOW_CODE_EXEC")
        os.environ["FORGE_TEST_MODE"] = "1"
        os.environ["FORGE_TRUSTED_CODE_ROOTS"] = str(code_exec.BASE_DIR)
        os.environ["ALLOW_CODE_EXEC"] = "true"

    def tearDown(self):
        for k, v in (("FORGE_TEST_MODE", self._old_test_mode),
                     ("FORGE_TRUSTED_CODE_ROOTS", self._old_roots),
                     ("ALLOW_CODE_EXEC", self._old_exec)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_run_python_impl_calls_safe_workdir(self):
        """撤掉 run_python_impl 里的 _safe_workdir 调用 ⇒ 本条必须变红。"""
        calls: list = []
        real = code_exec._safe_workdir

        def spy(base_dir, project):
            out = real(base_dir, project)
            calls.append((base_dir, project, out))
            return out

        code_exec._safe_workdir = spy
        try:
            code_exec.run_python_impl("my_creative_agent", code="print(1)", timeout=5)
        finally:
            code_exec._safe_workdir = real
        self.assertTrue(
            calls, "run_python_impl 没有调用 _safe_workdir —— 纵深防线已失效"
        )

    def test_run_tests_impl_calls_safe_workdir(self):
        """撤掉 run_tests_impl 里的 _safe_workdir 调用 ⇒ 本条必须变红。"""
        calls: list = []
        real = code_exec._safe_workdir

        def spy(base_dir, project):
            out = real(base_dir, project)
            calls.append((base_dir, project, out))
            return out

        target = code_exec.BASE_DIR / "tests" / "test_trust.py"
        if not target.is_file():
            self.skipTest("仓内无 tests/test_trust.py")
        code_exec._safe_workdir = spy
        real_spawn = code_exec._spawn_with_job
        code_exec._spawn_with_job = lambda *a, **k: None  # 不起 pytest
        try:
            code_exec.run_tests_impl("anything", target=str(target))
        finally:
            code_exec._safe_workdir = real
            code_exec._spawn_with_job = real_spawn
        self.assertTrue(
            calls, "run_tests_impl 没有调用 _safe_workdir —— 漏网分支未修"
        )


if __name__ == "__main__":
    unittest.main()
