"""Chat with GitHub：把 GitHub 仓库抓成本地工作区文件夹，供 RAG 检索问答。

流程（纯 Python，不需要本机装 git）：
1. 解析用户给的 GitHub 地址（支持 https://github.com/owner/repo 或 owner/repo）；
2. 确定默认分支（调 GitHub API，公开仓库免鉴权）；
3. 下载官方归档：公开仓库走 codeload zip；配了 GITHUB_TOKEN 走 API tarball（可抓私有仓库）；
4. 安全解压到 工作区/github_repos/<owner>__<repo>/（防路径穿越、超限中断），
   之后用 index_workspace 索引即可对该仓库问答。

安全与限制：
- 只允许 github.com 域名；单仓库归档 ≤200MB，防止拖爆磁盘；
- 解压时拒绝 ../ 越界路径、绝对路径、符号链接；
- .env 等敏感文件不会被 RAG 索引（rag.py 已内置跳过规则）。
"""

import io
import json
import os
import re
import tarfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agents import function_tool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or BASE_DIR.parent).resolve()
REPO_ROOT_DIR = WORKSPACE_ROOT / "github_repos"

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024   # 单仓库归档上限
MAX_FILES = 20000                        # 解压文件数上限
UA = "Mozilla/5.0 (chat-with-github; personal agent)"
GITHUB_PATTERN = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$"
)


def parse_repo(repo_url: str) -> tuple[str, str] | None:
    """从 URL 或 owner/repo 里解析出 (owner, repo)；非法返回 None。"""
    text = (repo_url or "").strip().rstrip("/")
    m = GITHUB_PATTERN.match(text)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.lower() in ("tree", "issues", "pull", "releases", "archive", "blob"):
        return None
    return owner, repo


def _http_get(url: str, token: str = "", timeout: int = 30) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data


def _api_default_branch(owner: str, repo: str, token: str = "") -> str:
    """调 GitHub API 查默认分支；失败返回空串。公开仓库匿名可用。"""
    try:
        meta = json.loads(_http_get(f"https://api.github.com/repos/{owner}/{repo}", token, timeout=20))
        branch = meta.get("default_branch")
        return str(branch) if branch else ""
    except Exception:
        return ""


def _branch_candidates(owner: str, repo: str, requested: str, token: str = "") -> list[str]:
    """决定尝试的分支顺序：显式指定 > API 默认分支 > main > master。"""
    candidates: list[str] = []
    if requested:
        candidates.append(requested)
    api_branch = _api_default_branch(owner, repo, token)
    if api_branch:
        candidates.append(api_branch)
    candidates.extend(["main", "master"])
    seen: list[str] = []
    for b in candidates:
        if b not in seen:
            seen.append(b)
    return seen


def _download_archive(owner: str, repo: str, branch: str, token: str = "") -> tuple[bytes, str]:
    """下载仓库归档；返回 (原始字节, 格式: zip|tgz)。公开走 codeload，带 token 走 API tarball。"""
    try:
        if token:
            data = _http_get(
                f"https://api.github.com/repos/{owner}/{repo}/tarball/{quote(branch)}",
                token,
                timeout=120,
            )
            return data, "tgz"
    except HTTPError:
        pass
    data = _http_get(
        f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{quote(branch)}",
        timeout=180,
    )
    return data, "zip"


def _safe_extract_zip(raw: bytes, target: Path) -> int:
    count = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if count >= MAX_FILES:
                raise ValueError(f"仓库文件超过 {MAX_FILES} 个，已中止（防止撑爆磁盘）")
            dest = _member_target(target, info.filename)
            if dest is None:
                raise ValueError(f"归档包含越界路径，已中止：{info.filename}")
            if info.file_size > MAX_ARCHIVE_BYTES:
                raise ValueError("归档内单个文件异常巨大，已中止")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
            count += 1
    return count


def _safe_extract_tgz(raw: bytes, target: Path) -> int:
    count = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            if count >= MAX_FILES:
                raise ValueError(f"仓库文件超过 {MAX_FILES} 个，已中止（防止撑爆磁盘）")
            dest = _member_target(target, member.name)
            if dest is None:
                raise ValueError(f"归档包含越界路径，已中止：{member.name}")
            if member.size > MAX_ARCHIVE_BYTES:
                raise ValueError("归档内单个文件异常巨大，已中止")
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, dest.open("wb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
            count += 1
    return count


def _member_target(target: Path, name: str) -> Path | None:
    """把归档成员名解析为 target 内路径。

    顺序：先做穿越/绝对路径校验，再去掉 GitHub 归档第一层 "owner-repo-<sha>" 根目录，
    最后再做一次越界校验；任何一步不合法都返回 None。
    """
    parts = name.replace("\\", "/").split("/")
    parts = [p for p in parts if p not in ("", ".")]
    if not parts:
        return None
    if any(p == ".." for p in parts) or re.match(r"^[A-Za-z]:", parts[0]):
        return None
    if len(parts) > 1:
        parts = parts[1:]  # 去 GitHub 归档根目录层
    return _resolve_entry(target, "/".join(parts))


def _strip_archive_root(name: str) -> str:
    """GitHub 归档第一层是 "owner-repo-<sha>" 目录，去掉它。"""
    parts = name.split("/")
    if len(parts) > 1:
        return "/".join(parts[1:])
    return name


def _resolve_entry(target: Path, name: str) -> Path | None:
    """把归档内的相对路径解析到 target 内；越界/绝对路径返回 None。"""
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = name.split("/")
    if any(p in ("..", "", ".") for p in parts):
        return None
    dest = (target / name).resolve()
    try:
        dest.relative_to(target.resolve())
    except ValueError:
        return None
    return dest


def fetch_github_repo_impl(repo_url: str, branch: str = "", token: str = "") -> str:
    """抓取 GitHub 仓库到工作区 github_repos/ 目录（供 RAG 检索）。"""
    parsed = parse_repo(repo_url)
    if not parsed:
        return "错误：无法识别的 GitHub 地址，请提供形如 https://github.com/用户/仓库 的链接。"
    owner, repo = parsed
    token = token or os.getenv("GITHUB_TOKEN", "").strip()

    start = time.monotonic()
    requested = (branch or "").strip()
    raw: bytes | None = None
    fmt = ""
    last_error = ""
    for cand in _branch_candidates(owner, repo, requested, token):
        try:
            raw, fmt = _download_archive(owner, repo, cand, token)
            branch_used = cand
            break
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (URLError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            break  # 网络问题换分支也没用
    if raw is None:
        hint = "" if token else "，或私有仓库需要 .env 里配置 GITHUB_TOKEN"
        return (
            f"下载失败（{last_error}）：{owner}/{repo}{hint}。"
            f"请确认仓库名/分支是否正确、仓库是否公开。"
        )
    if len(raw) > MAX_ARCHIVE_BYTES:
        return f"仓库归档超过 {MAX_ARCHIVE_BYTES // (1024 * 1024)}MB，已拒绝下载，请换小仓库。"

    target = REPO_ROOT_DIR / f"{owner}__{repo}"
    if target.exists() and any(target.iterdir()):
        return (
            f"目录已存在：{target}（之前抓过）。"
            f"如需重新抓取请先删除该目录，或换一个仓库。"
        )
    target.mkdir(parents=True, exist_ok=True)
    try:
        if fmt == "tgz":
            n_files = _safe_extract_tgz(raw, target)
        else:
            n_files = _safe_extract_zip(raw, target)
    except ValueError as exc:
        return f"解压中止：{exc}（目录 {target} 可能不完整，可删除后重试）"
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        return f"解压失败：{type(exc).__name__}: {str(exc)[:200]}"

    elapsed = time.monotonic() - start
    return (
        f"已抓取 GitHub 仓库 {owner}/{repo}（分支 {branch_used}，{n_files} 个文件，用时 {elapsed:.0f}s）。\n"
        f"位置：{target}\n"
        f"下一步：想对仓库内容问答时，先对我说：index_workspace(directory='github_repos/{owner}__{repo}')，"
        f"然后就可以问源码/文档/README 的问题了。"
    )


@function_tool
def fetch_github_repo(repo_url: str, branch: str = "") -> str:
    """把 GitHub 仓库抓取到工作区 github_repos/ 目录，供本地文档问答（RAG）检索。
    repo_url 是仓库链接或 owner/repo（如 https://github.com/octocat/Hello-World）；
    branch 可选指定分支，不填用仓库默认分支。公开仓库无需凭据；
    私有仓库需在 .env 配置 GITHUB_TOKEN。之后用 index_workspace 索引该目录即可对仓库问答。"""
    try:
        from runtime.trust import tag

        return tag("GitHub 仓库内容", None, fetch_github_repo_impl(repo_url, branch))
    except Exception as exc:
        return f"抓取出错：{type(exc).__name__}: {str(exc)[:300]}"

