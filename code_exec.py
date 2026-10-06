"""代码执行沙箱：让 Agent 能“写代码并真正运行”的小型本地环境。

借鉴 awesome-llm-apps 的 coding agent 模式（生成方案 → 沙箱执行 → 看结果迭代），
该仓库用 E2B 云沙箱 + 30 秒超时；本项目没有云服务，改用在**本机受限子进程**上落地：

- 代码只能写在 工作区/code_sandbox/<项目名>/ 下（路径强校验，杜绝越界）；
- 运行用的是当前 Python 环境（与项目同一解释器），带超时强杀；
- 传给子进程的环境变量会剔除 API Key 类密钥，代码看不到 .env 里的凭据；
- 执行需显式开启：.env 里 ALLOW_CODE_EXEC=true 才算授权（默认关闭）。

安全边界（真实沙箱需要容器，这里只是“受限执行”，请只让 Agent 运行可信任务）：
超时、目录围栏、密钥剔除、输出截断；不做 OS 级隔离。
"""

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agents import function_tool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or BASE_DIR.parent).resolve()
SANDBOX_ROOT = WORKSPACE_ROOT / "code_sandbox"

PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# P1-B(1)（2026-09-22）：受信根判定专用宽松路径正则——允许字母数字、_-. ~ 和
# 路径分隔符 / \（覆盖 "my_creative_agent/benchmark_fixture" 相对路径与
# "F:/Byong-hermes/..." 绝对路径这类"声明执行位置"型 project），但禁止 ".." 越界段。
# 仅在 trusted_root_for / _project_dir 的受信判定分支使用；普通 project 名仍走严格 PROJECT_RE。
# 越界（../ 、sibling 前缀、symlink）由 _canonical_under 在第二道继续拦截，本正则只放行合法路径进入判定。
#
# ⚠️ **`~` 必须放行**（10-05 CI 实据）：GitHub Actions 的 runner 用户目录是
# `C:\Users\runneradmin`，**8.3 短名形式为 `RUNNER~1`** —— `tempfile.gettempdir()`
# 在 CI 上返回的就是这个短名。原字符类 `[A-Za-z0-9_.\-/\\]` **不含 `~`** ⇒
# 整串 match 失败（`$` 锚定）⇒ `_trusted_path_ok` 返 False ⇒ `_project_dir`
# 返 None ⇒ `trusted_root_for` 返 None ⇒
# `tests/test_trusted_path_project.py::test_absolute_path_project_hitting_root_is_trusted`
# 在 CI 上必红，而本地全绿（本地 tempdir 不含 `~`）。
# 受控实验（双向对照）：造 `tilde~probe` ⇒ None；造 `plainprobe_nospaces` ⇒ 正常返回。
#
# **为什么放行 `~` 不削弱安全边界**：真正的越界防线是
# ① `_TRUSTED_PATH_HAS_DOTDOT_RE`（拒 `..` 段）② `_canonical_under`（按路径分量
# 比较，防sibling 前缀与 symlink 逃逸）。`tests/test_trusted_path_project.py` 里
# `test_dotdot_project_rejected` / `test_outside_root_path_not_trusted` /
# `test_sibling_prefix_not_trusted` 三条安全断言都**不依赖这个字符白名单** ——
# 放行更多合法路径字符不会让越界路径通过。
# 只加 `~`（**不扩到空格/中文**：那无实据，且中文路径另有编码维度）。
TRUSTED_PROJECT_PATH_RE = re.compile(r"^(?:[A-Za-z]:)?(?:[A-Za-z0-9_.~\-/\\]+)$")
# ".." 段在正则层就拒（保持原 PROJECT_RE 对 "../.." 的"项目名"拒绝语义），不进下游子树判定。
_TRUSTED_PATH_HAS_DOTDOT_RE = re.compile(r"(^|/|\\)\.\.(/|\\|$)")


def _trusted_path_ok(proj: str) -> bool:
    """受信判定是否接受该 project 声明：宽松路径正则匹配 且 不含 '..' 越界段。"""
    if not TRUSTED_PROJECT_PATH_RE.match(proj or ""):
        return False
    return not _TRUSTED_PATH_HAS_DOTDOT_RE.search(proj)

MAX_FILE_BYTES = 300 * 1024
MAX_OUTPUT_CHARS = 12000
DEFAULT_TIMEOUT = 40

_SECRET_ENV_KEYS = ("OPENAI", "GITHUB_TOKEN", "TAVILY", "ANTHROPIC", "GOOGLE", "API_KEY", "APITOKEN", "AGENT_TOKEN")
_KEEP_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "COMSPEC", "PATHEXT", "WINDIR", "PYTHONPATH", "HF_ENDPOINT")


def _exec_enabled() -> bool:
    return os.getenv("ALLOW_CODE_EXEC", "").strip().lower() == "true"


def _eval_or_test_mode() -> bool:
    """是否处于**显式声明**的评测/测试进程。

    口径与 guardrails.py:62-63 一致（FORGE_EVAL_MODE / FORGE_TEST_MODE == "1"），
    生产进程两者都不设。
    """
    return (os.environ.get("FORGE_EVAL_MODE") == "1"
            or os.environ.get("FORGE_TEST_MODE") == "1")


def _trusted_code_roots() -> list[Path]:
    """受信评测/本地代码根（环境变量 FORGE_TRUSTED_CODE_ROOTS，逗号分隔绝对路径）。
    只用于让受控的 benchmark fixture 能作为 run_python 的沙箱根执行本地测试；
    不影响生产 Project（默认未配置时行为不变）。

    P1-14（2026-09-22）：这是**评测专用免审批通道**，绝不允许在生产生效。
    实测生产 ``.env`` 曾把该变量指向 ``benchmark_fixture`` —— 等于把评测配置
    泄漏进生产：该目录下 ``run_python``/``code_loop``/``run_tests`` 完全无审批直接执行。
    现在只有显式声明评测/测试模式的进程才读取该配置（评测配置应写在评测器里，
    而不是生产 ``.env``；见 benchmark/eval_runner.py::_pin_workspace_root）。
    """
    if not _eval_or_test_mode():
        return []
    raw = os.getenv("FORGE_TRUSTED_CODE_ROOTS", "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            p = Path(part).resolve()
        except Exception:
            continue
        if p.is_dir():
            out.append(p)
    return out


def _is_source_root(path: Path) -> bool:
    """该路径是否就是项目源码根 BASE_DIR（仓库根本身）。

    P0-blat（2026-10-06）：**免审批通道不等于允许把源码树当工作目录**。
    受信根（FORGE_TRUSTED_CODE_ROOTS）被设成仓库根时，_project_dir 曾直接 `return root`，
    使 run_python 以仓库根为 CWD 执行 LLM 生成代码（实测污染出 100 个 `blat` 垃圾文件）。
    实测判据必须是「root 是否等于 BASE_DIR」，**不是**「resolved 是否等于 root」——
    后者在污染场景（project="my_creative_agent" 纯项目名）根本不成立：
    `Path("my_creative_agent").resolve()` 是 CWD/my_creative_agent，用 `break` 判据会漏修；
    而在正常评测场景（受信根=benchmark_fixture、project=该根自身）反而会误伤免审批通道。
    """
    try:
        return Path(path).resolve() == BASE_DIR.resolve()
    except Exception:
        return False


def _project_dir(project: str) -> Path | None:
    # P1-B(1)：受信 resolve 分支用 _trusted_path_ok 放行路径型 project（相对 a/b、绝对 F:/...），
    # 拒 '..' 越界段；严格 PROJECT_RE 仍用于非受信场景的沙箱命名。越界安全由 _canonical_under 兜底。
    if not (_trusted_path_ok(project or "") or PROJECT_RE.match(project or "")):
        return None
    proj = (project or "").strip()
    # P1-B(1)（2026-09-22）：相对路径（如 "my_creative_agent/benchmark_fixture"）
    # 先 CWD-resolve 再按 canonical 路径比对受信根，不再整串字符串比 basename。
    # 受信命中：resolve 后路径（或其任一祖先）落在某受信根下 → 取该根为沙箱 project。
    roots = _trusted_code_roots()
    try:
        resolved = Path(proj).resolve()
        for root in roots:
            if _canonical_under(resolved, root):
                if not _is_source_root(root):
                    return root
                # P0-2（2026-10-06）：这里**必须 continue 而不是 break**。
                # break 跳出的是**整个 for 循环** ⇒ 当列表里第一个匹配的 root 就是
                # 仓库根时，后续的合法受信根**再无机会被检查** ⇒
                # FORGE_TRUSTED_CODE_ROOTS=<仓库根>,<仓库根>/benchmark_fixture
                # + project=<仓库根>/benchmark_fixture/app 时，本该命中的
                # benchmark_fixture 被跳过，合法 benchmark 评测通道失效。
                # 实测：break 变体落 code_sandbox\app（丢了受信语义），
                #       continue 变体落 benchmark_fixture（既不返回 BASE_DIR 又命中受信根）。
                continue
    except Exception:
        pass
    # 受信基准根：project 名命中受信根目录名 → 以该根为沙箱 project（benchmark 本地验证）。
    for root in roots:
        if root.name.lower() == proj.lower() and not _is_source_root(root):
            return root
    return _sandbox_dir(proj)


#: Windows 保留设备名：这些名字在 Win32 命名空间里是**设备**而非普通目录，
#: `mkdir` 会失败或（更糟）静默指向设备。判定大小写不敏感，且**带扩展名也算**
#: （`CON.txt` 同样是设备）—— 所以比对的是第一个 `.` 之前的部分。
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _is_windows_reserved(name: str) -> bool:
    """该目录名是否是 Windows 保留设备名（CON/NUL/COM1… 含带扩展名的形态）。"""
    stem = (name or "").split(".", 1)[0].strip().upper()
    return stem in _WINDOWS_RESERVED_NAMES


def _path_is_within(child: Path, root: Path) -> bool:
    """child 的**真实位置**（解析所有 symlink/junction 之后）是否仍位于 root 之内。

    P0-1（2026-10-06）：目录级 junction/symlink 逃逸的**唯一正确判据**。

    - 两边都 `os.path.realpath` ⇒ 目录级 junction 会被跟随展开（`.resolve()` 也一样）
    - 两边都 `os.path.normcase` ⇒ Windows 大小写不敏感下 `C:\\Users\\ADMINI~1`
      与长名、盘符大小写差异不会造成假阴性
    - 比选用 `relative_to`（**按路径分量**），**绝不用 `startswith`/字符串前缀** ——
      `code_sandbox_evil` 以 `code_sandbox` 开头，前缀比较会把它判成"在根内"
      （本项目铁律，`_canonical_under` 同样遵守）
    """
    try:
        c = Path(os.path.normcase(os.path.realpath(str(child))))
        r = Path(os.path.normcase(os.path.realpath(str(root))))
    except (OSError, ValueError):
        return False
    if c == r:
        return True
    try:
        c.relative_to(r)
        return True
    except ValueError:
        return False


def _sandbox_leaf(proj: str) -> str:
    """把任意 project 声明收敛成 SANDBOX_ROOT 下的一个安全、**无碰撞**目录名。

    P2-1（2026-10-06）：上一版只取末段名字，导致 `'a/b'` 与 `'b'`、`'x/y/proj'`
    与 `'proj'` 落进**同一目录**（实测可跨 project 读文件，隔离边界失效）。

    方案：保留末段名字保证**可读性**（出错的路径一眼能看出是哪个 project），
    再追加一小段**稳定哈希后缀**保证**唯一性**。哈希用 `sha1`（非 `hash()`——
    后者带进程随机盐，跨进程不稳定，会让同一 project 每次落点不同）。
    只在**确实有收敛/截断**时才加后缀：原本就是单个安全段名的输入
    （`demo`、`m3_fixture` 等 benchmark/常用名）保持原样，零行为变更。
    """
    raw = (proj or "").strip()
    leaf = Path(raw).name or "workspace"
    safe = "".join(ch for ch in leaf if ch.isalnum() or ch in "_-") or "workspace"
    if _is_windows_reserved(safe):
        # 保留设备名不可用目录名；加前缀消歧（CON -> _CON）
        safe = "_" + safe
    # 「有损收敛」判定：只有当末段名被改写过、或原始声明不是单个安全段名时才加哈希
    if safe == leaf and raw == safe and not _is_windows_reserved(safe):
        return safe
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]
    return f"{safe}__{digest}"


def _sandbox_dir(proj: str) -> Path | None:
    """把 project 声明收敛成 SANDBOX_ROOT 下的一个**真实位置必在根内**的子目录。

    P0-blat（2026-10-06）：原实现是 `(SANDBOX_ROOT / proj).resolve()`，但 pathlib 的
    `/` 运算符在右操作数为**绝对路径**时会直接丢弃左操作数 ——
    实测 `Path("F:/ws/code_sandbox") / "F:/repo/my_creative_agent"` == `F:/repo/my_creative_agent`。
    ⇒ 任何绝对路径型 project 都能让工作目录落到磁盘任意位置（实测可写到 F:/tmp/... ），
    这是独立于受信根设置的**第二道逃逸口**，且在生产默认配置（roots=[]）下就成立。

    P0-1（2026-10-06）：仅取末段名字**还不够** —— 末段 `.resolve()` **会跟随
    目录级 junction/symlink**。实测两步即可越界：① 一次 `run_python` 生成代码执行
    `mklink /J code_sandbox\\stage2 <仓外目录>`（junction **不需要管理员权限**）
    ② 第二次用 `project="stage2"`（纯项目名，完全通过字符白名单）⇒ 落点仓外。
    而 `write_code_file`/`read_code_file`/`list_code_files` 走同一条路，
    且**不需要 `ALLOW_CODE_EXEC`** ⇒ 门槛更低。

    ⚠️ 本函数**只算路径、不创建目录**（`mkdir` 在调用方：`:454`/`:551`）——
    所以这里的校验**不会引入「先建后校验」的 TOCTOU 窗口**。
    命中 junction 逃逸时**返回 None**（调用方已有 `base_dir is None` 早退分支）
    而不是回退到某个固定目录：回退目录自己可能正是一个 junction，等于把洞换个位置。
    """
    safe = _sandbox_leaf(proj)
    candidate = SANDBOX_ROOT / safe
    if _path_is_within(candidate, SANDBOX_ROOT):
        return candidate
    # 真实位置逃出 SANDBOX_ROOT（目录级 junction/symlink 指到根外）⇒ 拒绝该落点。
    # 刻意不 mkdir：调用方拿 None 会走早退分支，不会把文件写进根外。
    return None  # type: ignore[return-value]



def _canonical_under(child: Path, root: Path) -> bool:
    """child 的规范化真实路径是否位于 root 之内（防 ../ 、sibling-prefix、symlink 逃逸）。
    用 resolve() + relative_to（按路径分量比较），绝不用 startswith/子串。"""
    try:
        child_r = Path(child).resolve()
        root_r = Path(root).resolve()
    except Exception:
        return False
    if child_r == root_r:
        return True
    try:
        child_r.relative_to(root_r)
        return True
    except ValueError:
        return False


def trusted_root_for(project: str, filename: str = "", args: str = "") -> Path | None:
    """返回该 run_python/code_loop 真正会执行所在的受信根（canonical），否则 None。

    判定依据是**声明的执行位置**（project 解析出的目录 / 绝对 filename），
    绝不扫描自由文本 code/args 中的路径字符串（防止注释伪造 trusted root 绕过 Approval）。
    越界（../、sibling 前缀、symlink、大小写/盘符/斜杠差异）一律不匹配。
    """
    roots = _trusted_code_roots()
    if not roots:
        return None
    proj = (project or "").strip()
    # P1-B(1)：受信判定分支用宽松路径校验（_trusted_path_ok）放行路径型 project
    # （相对 "a/b"、绝对 "F:/..."），拒 '..' 越界段；子树越界由 _canonical_under 兜底。
    if proj and _trusted_path_ok(proj):
        # 声明的 project 解析出的真实目录必须落在某受信根内
        pdir = _project_dir(proj)
        if pdir is not None:
            for root in roots:
                if _canonical_under(pdir, root):
                    return root
    # 绝对 filename：解析后必须落在受信根内
    fp = (filename or "").strip()
    if fp:
        try:
            fpath = Path(fp)
        except Exception:
            fpath = None
        if fpath is not None and fpath.is_absolute():
            for root in roots:
                if _canonical_under(fpath, root):
                    return root
    return None


def _file_target(project_dir: Path, filename: str) -> Path | None:
    if not filename or filename.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", filename):
        return None
    target = (project_dir / filename).resolve()
    try:
        target.relative_to(project_dir.resolve())
    except ValueError:
        return None
    return target


def _sanitized_env() -> dict[str, str]:
    """运行用的环境：保留系统必需项，剔除一切疑似密钥变量。"""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _KEEP_ENV_KEYS:
            env[key] = value
        elif any(secret in upper for secret in _SECRET_ENV_KEYS):
            continue  # 密钥不传
        elif upper in ("ALLOW_CODE_EXEC",):
            continue
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n……（输出过长，截断显示前 {limit} 字符）", True


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# 64-bit JOBOBJECT_EXTENDED_LIMIT_INFORMATION：sizeof=136；LimitFlags 位于偏移 16。
# 用 raw bytes 布局而非 ctypes.Structure（后者在部分解释器/大小位下 sizeof 对不齐）。
_JOB_EXT_LIMIT_INFO_SIZE = 136
_JOB_LIMIT_FLAGS_OFFSET = 16


def _job_handle() -> Any:
    """Windows Job Object（KILL_ON_JOB_CLOSE）句柄：子进程挂入后，_release_job 关闭
    句柄即原子杀掉整棵进程树（含孙进程），无 taskkill 竞态。
    纯 raw ctypes（无 pywin32 依赖）；任何失败（非 Windows / API 调用失败）返回 None，
    调用方退化为 taskkill 兜底。句柄为 int（HANDLE）。"""
    if os.name != "nt":
        return None
    try:
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = ctypes.c_void_p
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            return None
        buf = ctypes.create_string_buffer(_JOB_EXT_LIMIT_INFO_SIZE)
        # 写 LimitFlags（DWORD）到偏移 16
        ctypes.memmove(
            ctypes.addressof(buf) + _JOB_LIMIT_FLAGS_OFFSET,
            ctypes.c_uint32(_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
            4,
        )
        k.SetInformationJobObject.restype = ctypes.c_int
        k.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        # 9 = JobObjectExtendedLimitInformation
        if not k.SetInformationJobObject(
            handle, 9, buf, _JOB_EXT_LIMIT_INFO_SIZE
        ):
            k.CloseHandle(handle)
            return None
        return int(handle)
    except Exception:
        return None


def _spawn_with_job(job: Any, argv: list[str], **kwargs: Any) -> subprocess.Popen | None:
    """带 job 句柄创建子进程；挂 job 失败（句柄无效/Assign 异常）退化为普通 Popen。
    job 句柄是 int（HANDLE）；用 CREATE_BREAKAWAY_FROM_JOB 保证子进程能挂到本 job。"""
    creationflags = kwargs.get("creationflags", 0)
    env = kwargs.get("env")
    cwd = kwargs.get("cwd")
    if job is not None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.AssignProcessToJobObject.restype = ctypes.c_long
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=cwd,
                creationflags=getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0) | creationflags,
            )
            ok = kernel32.AssignProcessToJobObject(job, proc.pid)
            if ok == 0:
                # Assign 失败 → 杀掉刚启的孤儿子进程（它不在 job 内，taskkill 兜底）
                _kill_tree(proc.pid)
                return None
            return proc
        except Exception:
            pass
    try:
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            creationflags=creationflags,
        )
    except Exception:
        return None


def _release_job(job: Any, proc: subprocess.Popen) -> None:
    """释放 job 句柄 → KILL_ON_JOB_CLOSE 生效，整棵树被原子杀掉。
    正常完成路径 proc 已退出，本函数幂等；超时/失败路径先调本函数再等 proc。"""
    if job is None:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
    except Exception:
        pass


def _kill_tree(pid: int) -> None:
    """P2-2 降级兜底：无 job 句柄时（非 Windows/pywin32 缺失）按 taskkill /T 杀进程树。
    有 job 句柄时 KILL_ON_JOB_CLOSE 在句柄关闭时自动杀整棵树，不再调用本函数。"""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _communicate_or_cancel(job: Any, proc: subprocess.Popen, timeout: int) -> tuple[str, str, bool]:
    """等待子进程结束，期间把「杀掉这棵进程树」登记进取消表（审计 P1-2）。

    返回 ``(stdout, stderr, timed_out)``。

    为什么需要这一步：同步工具由 SDK 用 ``asyncio.to_thread`` 执行，``task.cancel()``
    只能取消 awaiting 的 Future —— worker 线程仍会阻塞在 ``proc.communicate()`` 上，
    子进程继续写文件/跑测试。用户看到「已取消」，副作用却已经落盘。
    登记 killer 后，Runner 在 CancelledError 到达 await 点时调用
    ``cancel_scope.kill_run(run_id)`` 即可让整棵树立刻死掉，``communicate()`` 随即返回。

    幂等性说明：句柄释放只能发生一次。``CloseHandle`` 重复调用是真实风险
    （句柄号可能已被系统复用，第二次会关掉别的对象），所以用状态位而不是
    「关两次无所谓」的写法。
    """
    # 延迟导入：模块顶层导入 runtime.* 会先执行 runtime/__init__（它导入 runner），
    # 而 runner 会反过来导入本模块 → 循环导入。工具被调用时 runtime 早已加载完毕。
    from runtime.cancel_scope import register as _register, unregister as _unregister

    state = {"job_released": False, "tree_killed": False}

    def _stop_process_tree() -> None:
        if state["tree_killed"] or state["job_released"]:
            return
        state["tree_killed"] = True
        if job is not None:
            _release_job(job, proc)
            state["job_released"] = True
        else:
            _kill_tree(proc.pid)

    token = _register(_stop_process_tree)
    timed_out = False
    # 必须先绑定：超时分支里 communicate 抛异常，两个名字都不会被赋值，
    # 直接 `return stdout or ""` 会变成 UnboundLocalError（旧代码靠"超时即提前 return"
    # 侥幸避开了这一点，收进公共函数后这个侥幸就没了）。
    stdout: str | None = None
    stderr: str | None = None
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        _unregister(token)
        if timed_out:
            _stop_process_tree()
        elif not state["job_released"]:
            # 正常完成：进程已退出，只需释放 job 句柄。
            # 这里**不能**走 _kill_tree —— 对一个已经退出的 PID 做 taskkill /T /F，
            # 在 PID 被复用时会杀掉无关进程。
            _release_job(job, proc)
            state["job_released"] = True
    return stdout or "", stderr or "", timed_out


@function_tool
def write_code_file(project: str, filename: str, content: str) -> str:
    """在代码沙箱里写/覆盖一个代码文件（写代码任务的第一步）。
    project 是沙箱项目名（字母数字_-）；filename 是文件相对路径（可含子目录，如 utils/math.py）；
    content 是完整文件内容。写完后用 run_python 运行验证。只允许写在 工作区/code_sandbox/ 下。"""
    project = (project or "").strip()
    project_dir = _project_dir(project)
    if project_dir is None:
        return "错误：项目名只能包含字母、数字、下划线和连字符（如 demo、my_tool）。"
    target = _file_target(project_dir, (filename or "").strip())
    if target is None:
        return "错误：文件名必须是沙箱内的相对路径，不能越界或使用绝对路径。"
    if len(content) > MAX_FILE_BYTES:
        return f"错误：文件超过 {MAX_FILE_BYTES // 1024}KB 上限。"
    project_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    # ⚠️ SANDBOX_ROOT 与 target 必须**对称规范化**（同 rag.py::_resolve_root）。
    # `target` 来自 _file_target()，已被 .resolve() 成绝对长名；而 SANDBOX_ROOT
    # 是模块级 WORKSPACE_ROOT / "code_sandbox"，测试里被赋成短名 tmp 目录且未
    # resolve。Windows 8.3 短名（GitHub runner 上 tempfile.gettempdir() 返回
    # `C:\Users\RUNNER~1\...`）会让两侧字符串不等 ⇒ relative_to 抛 ValueError
    # ⇒ 被 function_tool 吞成 'An error occurred while running the tool.'。
    # 两边都 resolve 后仍产出**相对路径**（形如 a/a.py），测试断言依赖这一点。
    try:
        rel = target.relative_to(Path(SANDBOX_ROOT).resolve()).as_posix()
    except (ValueError, OSError):
        rel = target.name  # 防御性兜底：非正常路径，不让整条写入崩掉
    return f"已写入 {rel}（{len(content)} 字符）。现在可以 run_python 运行它。"


@function_tool
def read_code_file(project: str, filename: str, max_chars: int = 20000) -> str:
    """读取【代码沙箱】项目里的某个文件（用于回看/定位问题）。

作用域：只能读 工作区/code_sandbox/<project>/ 下的文件 —— 那是写代码、跑代码的临时目录，
不是你的项目源码。读真实项目/工作区的文件请改用 read_workspace_file（只读，任意路径）；
改真实项目请用 edit_project_file。
project 是沙箱项目名；filename 是项目内相对路径（如 utils/math.py）。"""
    project = (project or "").strip()
    project_dir = _project_dir(project)
    target = _file_target(project_dir, (filename or "").strip()) if project_dir else None
    if target is None:
        return "错误：文件名必须是沙箱内的相对路径。"
    if not target.exists() or not target.is_file():
        return f"错误：沙箱里找不到 {filename}（可用 list_code_files 查看项目内文件）。"
    data = target.read_bytes()
    if b"\x00" in data[:4096]:
        return "该文件看起来是二进制，无法直接阅读。"
    text = data.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n……（截断，仅显示前 {max_chars} 字符）"
    from runtime.trust import tag

    return tag("代码文件", None, text)


@function_tool
def list_code_files(project: str) -> str:
    """列出【代码沙箱】某个项目里的全部文件与大小（含子目录）。

作用域：只看 工作区/code_sandbox/<project>/，用于确认 write_code_file 写了哪些文件、
run_python 该跑哪一个。列真实项目/工作区的目录请改用 list_workspace_files。
project 是沙箱项目名。"""
    project = (project or "").strip()
    project_dir = _project_dir(project)
    if project_dir is None or not project_dir.exists():
        return "错误：项目还不存在，先用 write_code_file 写第一个文件。"
    lines = []
    for path in sorted(project_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(project_dir).as_posix()
            lines.append(f"- {rel}  ({path.stat().st_size} B)")
    if not lines:
        return "项目目录是空的。"
    return "\n".join(lines)


def _safe_workdir(base_dir: Path | None, project: str) -> Path | None:
    """兜底守卫：工作目录**绝不能**是项目源码根，也**绝不能**逃出沙箱/受信边界。

    P0-blat（2026-10-06）防御纵深，与 _project_dir 的修法互不依赖 ——
    未来任何新增调用方、或 FORGE_TRUSTED_CODE_ROOTS 被误设成仓库根，
    都不应再能把源码树当 CWD。命中时改道到隔离沙箱目录。

    P0-1（2026-10-06）：再叠加一道**落点校验**——若 base_dir 的**真实位置**
    （解析所有 symlink/junction 之后）跑出了它自己声明的**父目录**，说明它是
    「指向别处的目录级 junction/symlink」⇒ 拒绝（返 None）。
    这条**独立于** `_sandbox_dir`：即便有人绕过 `_sandbox_dir` 直接把一个
    根外落点塞进来（未来新增调用方、或误设环境变量），这里仍会拦。
    """
    if base_dir is None:
        return None
    if _is_source_root(base_dir):
        proj = (project or "").strip()
        safe = "".join(ch for ch in Path(proj).name if ch.isalnum() or ch in "_-") or "workspace"
        return _sandbox_dir(f"{safe}__redirected")
    # 目录级 junction/symlink 逃逸：真实位置跑出了**自己声明的父目录** ⇒ 落点不可信。
    #
    # ⚠️ **判据必须两侧都 realpath + 分量比较**（2026-10-06 实测踩过）：
    # 早先写成 `normcase(abspath(x)) != normcase(realpath(x))` 是**错的**——
    # `abspath` **不展开 8.3 短名**而 `realpath` 展开，于是 GitHub runner
    #（tempdir = RUNNER~1）上短名 root 下**每个正常落点**都被误判成 junction
    # ⇒ 9 条测试全红。这正是 tests/test_path_normalization_symmetry.py 守的
    # 那类不对称，也说明「realpath 两侧对称」是本项目反复踩的坑。
    #
    # 现在的判据：realpath(落点) 必须仍在 realpath(声明的父目录) 之内——
    # `_path_is_within` 内部两侧都 realpath + normcase + 按分量比较，天然对称。
    parent = Path(os.path.dirname(str(base_dir).rstrip("\\/")) or ".")
    if not _path_is_within(base_dir, parent):
        return None
    return base_dir


def run_python_impl(
    project: str, filename: str = "", code: str = "", args: str = "", timeout: int = 40
) -> str:
    """run_python 的内部实现（供 codex_loop 等复用；不经过防重复提醒）。"""
    if not _exec_enabled():
        return (
            "错误：代码执行未开启（安全设计）。想启用请在 .env 里加一行 ALLOW_CODE_EXEC=true，"
            "并确认你信任本 Agent 在本机执行它自己写的 Python。"
        )
    project = (project or "").strip()
    # 受信根优先（canonical）：声明的 project 或绝对 filename 落在受信根内 → 以受信根为运行目录；
    # 否则走沙箱 project。绝不扫描 code 文本。
    trusted_run = trusted_root_for(project, filename, args)
    base_dir = trusted_run if trusted_run is not None else _project_dir(project)
    base_dir = _safe_workdir(base_dir, project)
    if base_dir is None:
        return "错误：项目名只能包含字母、数字、下划线和连字符。"
    base_dir.mkdir(parents=True, exist_ok=True)

    # P0-blat（2026-10-06）：inline_tmp 只标记「本次调用自己生成的 _inline_*.py」。
    # 用户经 filename 传入的既有文件绝不能删—— 两者用这个独立的标记区分，不靠文件名猜测。
    inline_tmp: Path | None = None
    run_file: Path | None = None
    if (filename or "").strip():
        # 若 filename 是受信根内绝对路径，直接使用；否则按相对路径解析到 base_dir。
        fp = Path(filename.strip())
        if fp.is_absolute():
            run_file = fp.resolve()
            if trusted_run is None or not _canonical_under(run_file, trusted_run):
                return "错误：绝对路径必须在受信代码根内。"
        else:
            run_file = _file_target(base_dir, filename.strip())
        if run_file is None:
            return "错误：文件名必须是沙箱内的相对路径。"
        if not run_file.exists():
            return f"错误：沙箱里找不到文件 {filename}（先用 write_code_file 写文件）。"
    elif (code or "").strip():
        # P2-2（2026-10-06）：`datetime.now()` 的**实际**时间分辨率约 1.6ms
        # （实测 300ms 采样 194 个不同值，中位间隔 1.6ms；远粗于 `%f` 暗示的 1µs）
        # ⇒ 并发调用极易撞名，第二个调用在 write_text 阶段就 PermissionError 崩溃
        # （Windows 文件锁）。实测 12 并发只产生 5 个文件名、7 个调用崩溃。
        # ⚠️ 责任归属：**HEAD 已有**，非上一轮清理改动引入（HEAD 对照 8 并发挂 4 个）。
        # 改用 `tempfile.mkstemp`：内核级 O_EXCL 原子创建，**并发下不可能撞名**，
        # 也不需要「撞名后重试」这种补救分支。前缀保留 `_inline_` 以维持
        # `.gitignore` 的 `/_inline_*.py` 规则与既有清理测试的 `rglob("_inline_*.py")`。
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="_inline_", suffix=".py", dir=str(base_dir))
        except OSError:
            # 沙箱根不可写等：交回原有语义（明确报错，不静默走危险分支）
            return "错误：无法在沙箱目录创建临时脚本（目录不可写？）。"
        run_file = Path(tmp_name)
        inline_tmp = run_file
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
    else:
        return "错误：需要提供 filename 或 code 二者之一。"

    try:
        return _run_python_file(base_dir, run_file, args, timeout)
    finally:
        # 每次调用必产生的确定性垃圾：无论成功/失败/超时/子进程创建失败都必须清掉。
        # 早于本修复时全文件无任何 unlink ⇒ 磁盘单调增长（实测 benchmark_fixture 已积55 个）。
        if inline_tmp is not None:
            try:
                inline_tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _run_python_file(base_dir: Path, run_file: Path, args: str, timeout: int) -> str:
    """在 base_dir 下执行 run_file 并整理输出（run_python_impl 的执行段）。

    单独抽出成函数，是为了让 run_python_impl 的 try/finally 能覆盖**所有**返回路径
    （成功 / 超时 / 子进程创建失败 / 异常），而不是逐个 return 前手写清理。
    """
    if not isinstance(timeout, int) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT
    timeout = min(int(timeout), 120)

    start = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    job = _job_handle()
    argv = [sys.executable, str(run_file)] + (args.split() if args else [])
    proc = _spawn_with_job(
        job,
        argv,
        cwd=str(base_dir),
        env=_sanitized_env(),
        creationflags=creationflags,
    )
    if proc is None:
        return "运行失败：无法创建子进程（job object 或 Popen 不可用）。"
    # P1-2：等待期间登记「杀进程树」回调，取消 Run 时整棵树立刻停下
    stdout, stderr, timed_out = _communicate_or_cancel(job, proc, timeout)
    if timed_out:
        # job 句柄关闭时 KILL_ON_JOB_CLOSE 已杀掉整棵树（含孙进程）；
        # 无 job 句柄（非 Windows / 缺 pywin32）时降级用 taskkill /T 兜底。
        return f"运行超时（>{timeout}s）：已强制终止进程树（含子孙进程），请检查是否死循环或运行太慢。"

    elapsed = time.monotonic() - start
    stdout, stdout_trunc = _truncate(stdout or "")
    stderr, stderr_trunc = _truncate(stderr or "")
    lines = [f"退出码: {proc.returncode} ｜ 用时: {elapsed:.1f}s"]
    if stdout_trunc or stderr_trunc:
        lines.append("注意：输出过长已截断。")
    if stdout.strip():
        lines.append("\n[stdout]\n" + stdout.rstrip("\n"))
    if stderr.strip():
        lines.append("\n[stderr]\n" + stderr.rstrip("\n"))
    if not stdout.strip() and not stderr.strip():
        lines.append("（程序没有产生任何输出）")
    return "\n".join(lines)


def _trust_wrap_run(text: str) -> str:
    """给运行输出加外部数据边界（保留真实 stdout，仅标记+清控制字符）。"""
    from runtime.trust import tag

    return tag("程序输出(沙箱运行)", None, text)


_TEST_ARG_RE = re.compile(r"^[A-Za-z0-9_\-\.\/=:,]+$")
_TEST_ALLOWED_FLAGS = ("-q", "-v", "-x", "-s", "-k", "--maxfail", "-p", "-m", "--tb", "-r")


def _safe_pytest_extra_args(raw: str) -> list[str] | None:
    """把 extra_args 拆成结构化 pytest 参数；含 shell 元字符/越界 token 时返回 None。"""
    if not (raw or "").strip():
        return []
    out: list[str] = []
    for tok in str(raw).split():
        if not _TEST_ARG_RE.match(tok):
            return None  # 拒绝 ; | & > < $ ` 等
        out.append(tok)
    return out


def run_tests_impl(project: str, target: str = "", extra_args: str = "", timeout: int = 120) -> str:
    """纯验证：对项目运行 Python 测试（pytest），只读执行，不修改源码。"""
    if not _exec_enabled():
        return (
            "错误：代码执行未开启（安全设计）。想启用请在 .env 里加一行 ALLOW_CODE_EXEC=true。"
        )
    project = (project or "").strip()
    trusted_run = trusted_root_for(project, target, "")
    base_dir = trusted_run if trusted_run is not None else _project_dir(project)
    if base_dir is None:
        return "错误：项目名只能包含字母、数字、下划线和连字符。"
    # P1-1（2026-10-06）：与 run_python_impl 对称接入 _safe_workdir。
    # 上一轮只加在 run_python_impl，run_tests_impl 是**同一缺陷的漏网分支**：
    # 它走 trusted_root_for 的「绝对 filename」分支（:307-317）直接 `return root`，
    # 而 _is_source_root 的判据只加在 _project_dir 的两个分支上，管不到这条路。
    # 实测（FORGE_TRUSTED_CODE_ROOTS=仓库根 + target=仓内绝对路径）：
    # PROBE_CWD_IS_BASE_DIR = True，且 pytest 退出码 0（看起来"成功"）。
    # 危害不止 CWD：pytest **收集阶段**就会执行 conftest.py 与已注册插件
    # ⇒ 这是一条真实的代码执行通道，且 .pytest_cache/__pycache__ 写进仓内。
    base_dir = _safe_workdir(base_dir, project)
    if base_dir is None:
        return "错误：测试目标必须落在受信代码根内，且不能是项目源码根。"
    base_dir = base_dir.resolve()

    argv = [sys.executable, "-m", "pytest"]
    tgt = (target or "").strip()
    if tgt:
        tp = Path(tgt)
        cand = tp.resolve() if tp.is_absolute() else (base_dir / tgt).resolve()
        if not _canonical_under(cand, base_dir):
            return "错误：测试目标必须在项目根内。"
        argv.append(str(cand) if tp.is_absolute() else tgt)
    extra = _safe_pytest_extra_args(extra_args)
    if extra is None:
        return "错误：extra_args 含不安全的字符（仅允许 pytest 参数）。"
    if not extra:
        extra = ["-q"]
    argv.extend(extra)

    if not isinstance(timeout, int) or timeout <= 0:
        timeout = 120
    timeout = min(int(timeout), 300)

    start = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    job = _job_handle()
    proc = _spawn_with_job(
        job,
        argv,
        cwd=str(base_dir),
        env=_sanitized_env(),
        creationflags=creationflags,
    )
    if proc is None:
        return "运行失败：无法创建子进程（job object 或 Popen 不可用）。"
    # P1-2：等待期间登记「杀进程树」回调，取消 Run 时整棵树立刻停下
    stdout, stderr, timed_out = _communicate_or_cancel(job, proc, timeout)
    if timed_out:
        # job 句柄关闭时 KILL_ON_JOB_CLOSE 已杀掉整棵树（含孙进程）；
        # 无 job 句柄（非 Windows / 缺 pywin32）时降级用 taskkill /T 兜底。
        return f"运行超时（>{timeout}s）：已强制终止 pytest 进程树（含子孙进程）。"

    elapsed = time.monotonic() - start
    stdout, stdout_trunc = _truncate(stdout or "")
    stderr, stderr_trunc = _truncate(stderr or "")
    passed = proc.returncode == 0 and ("passed" in (stdout or "").lower())
    lines = [f"pytest 退出码: {proc.returncode} ｜ 用时: {elapsed:.1f}s ｜ "
             f"{'PASS' if passed else 'FAIL'}"]
    if stdout_trunc or stderr_trunc:
        lines.append("注意：输出过长已截断。")
    if stdout.strip():
        lines.append("\n[stdout]\n" + stdout.rstrip("\n"))
    if stderr.strip():
        lines.append("\n[stderr]\n" + stderr.rstrip("\n"))
    if not stdout.strip() and not stderr.strip():
        lines.append("（没有产生任何输出）")
    return "\n".join(lines)


@function_tool(strict_mode=False)
def run_tests(project: str, target: str = "", extra_args: str = "", timeout: int = 120) -> str:
    """运行项目的 Python 测试（pytest）并返回真实测试结果。

    用途：当任务要求“运行测试 / 验证修改”时，用本工具对项目执行 pytest，得到真实的
    passed/failed、退出码与输出。本工具只读执行测试，**不会修改项目源码**。

    project 指定项目名（必填）；target 可选，指定单个测试文件/用例；extra_args 可选，
    传入 pytest 参数（如 "-q -x"）；timeout 秒后强制终止（默认 120）。

    用法示例：
    run_tests(project='micro_fixture')
    run_tests(project='micro_fixture', target='test_calc.py')
    run_tests(project='micro_fixture', extra_args='-k add -v')"""
    return _trust_wrap_run(run_tests_impl(project, target, extra_args, timeout))


@function_tool(strict_mode=False)
def run_python(project: str, filename: str = "", code: str = "", args: str = "", timeout: int = 40) -> str:
    """在沙箱里运行一个 Python 文件或一段内联代码，并返回结果（stdout/stderr/退出码）。

    filename 指定要运行的文件；code 直接给一段内联代码（二选一，至少提供一个；都用时优先 filename）。
    注意：args 是传给该脚本的命令行参数，**不是 shell 命令**；本工具不执行 pytest/npm 等命令本身。
    timeout 秒后强制终止（默认 40，最大 120）。运行环境不携带任何 API Key；输出会被截断。

    要运行项目测试（pytest/unittest），请用 code 传入调用测试运行器的内联代码，例如：
      code="import pytest, sys; sys.exit(pytest.main(['test_calc.py']))"
    或直接导入并调用测试函数：
      code="from test_calc import test_add; test_add(); print('OK')"

    用法示例：
    run_python(project='demo', filename='hello.py')
    run_python(project='demo', code="print('hi')")"""
    # P1-5：与 tools.py 相同的空转防护——同一轮内用相同代码/参数反复运行 Python
    # 会快速耗尽工具预算；命中时直接返回提醒，让模型基于已有输出作答或修改代码。
    try:
        from tools import _too_repetitive, _REPEAT_HINTS

        _rp_key = f"{(code or '').strip()}|{(args or '').strip() or filename}"
        if _too_repetitive("run_python", _rp_key):
            return f"（提醒）{_REPEAT_HINTS['run_python']}"
    except Exception:
        pass
    return _trust_wrap_run(run_python_impl(project, filename, code, args, timeout))

