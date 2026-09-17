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

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import function_tool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or BASE_DIR.parent).resolve()
SANDBOX_ROOT = WORKSPACE_ROOT / "code_sandbox"

PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_FILE_BYTES = 300 * 1024
MAX_OUTPUT_CHARS = 12000
DEFAULT_TIMEOUT = 40

_SECRET_ENV_KEYS = ("OPENAI", "GITHUB_TOKEN", "TAVILY", "ANTHROPIC", "GOOGLE", "API_KEY", "APITOKEN", "AGENT_TOKEN")
_KEEP_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "COMSPEC", "PATHEXT", "WINDIR", "PYTHONPATH", "HF_ENDPOINT")


def _exec_enabled() -> bool:
    return os.getenv("ALLOW_CODE_EXEC", "").strip().lower() == "true"


def _trusted_code_roots() -> list[Path]:
    """受信评测/本地代码根（环境变量 FORGE_TRUSTED_CODE_ROOTS，逗号分隔绝对路径）。
    只用于让受控的 benchmark fixture 能作为 run_python 的沙箱根执行本地测试；
    不影响生产 Project（默认未配置时行为不变）。"""
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


def _project_dir(project: str) -> Path | None:
    if not PROJECT_RE.match(project or ""):
        return None
    # 受信基准根：project 名命中受信根目录名 → 以该根为沙箱 project（benchmark 本地验证）。
    for root in _trusted_code_roots():
        if root.name.lower() == project.strip().lower():
            return root
    return (SANDBOX_ROOT / project).resolve()


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
    if proj and PROJECT_RE.match(proj):
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
    return f"已写入 {target.relative_to(SANDBOX_ROOT).as_posix()}（{len(content)} 字符）。现在可以 run_python 运行它。"


@function_tool
def read_code_file(project: str, filename: str, max_chars: int = 20000) -> str:
    """读取沙箱项目里的某个代码文件（用于回看/定位问题）。"""
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
    """列出沙箱项目里的所有文件及大小（不含子目录层级限制）。"""
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
    if base_dir is None:
        return "错误：项目名只能包含字母、数字、下划线和连字符。"
    base_dir.mkdir(parents=True, exist_ok=True)

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
        stamp = datetime.now().strftime("%H%M%S_%f")
        run_file = base_dir / f"_inline_{stamp}.py"
        run_file.write_text(code, encoding="utf-8")
    else:
        return "错误：需要提供 filename 或 code 二者之一。"

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
    timed_out = False
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        _release_job(job, proc)
    if timed_out:
        # job 句柄关闭时 KILL_ON_JOB_CLOSE 已杀掉整棵树（含孙进程）；
        # 无 job 句柄（非 Windows / 缺 pywin32）时降级用 taskkill /T 兜底。
        if job is None:
            _kill_tree(proc.pid)
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
    timed_out = False
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        _release_job(job, proc)
    if timed_out:
        # job 句柄关闭时 KILL_ON_JOB_CLOSE 已杀掉整棵树（含孙进程）；
        # 无 job 句柄（非 Windows / 缺 pywin32）时降级用 taskkill /T 兜底。
        if job is None:
            _kill_tree(proc.pid)
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

