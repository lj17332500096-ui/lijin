"""Codex 循环闭环：写代码 → 运行验证 →（失败≤3 次）自动修复重跑 → 四段总结。

只作用于 code_sandbox/<project>/ 内文件；未开启 ALLOW_CODE_EXEC 时直接返回提示。
修复由当前网关模型完成（上下文 = 文件内容前 8k + 最近一次 stderr 尾 2k），
模型只输出替换后的完整文件代码（禁止透传任意风险内容——落盘前做基础校验）。
"""

import os
import re
import time
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv

from code_exec import _exec_enabled, _file_target, _project_dir, run_python_impl
from runtime.key_patterns import has_key
from runtime.provider_errors import call_with_provider_retry, provider_public_text

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MAX_ATTEMPTS = 3
CODE_CTX = 8000
ERR_TAIL = 2000

_FIX_SYSTEM = """你是代码修复助手。用户会给你一个 Python 文件（可能不完整）与它最近一次运行的错误输出。
任务：给出修复后的**完整文件源码**，要求：
1. 保持原文件的结构与意图，只修导致当下错误的代码；
2. 输出纯代码，不要解释、不要 markdown 代码块；
3. 如果错误无法修复（需求不明/环境问题），只输出一行 `__NO_FIX__`。"""


def _validate_fix(text: str) -> bool:
    """修复源码落盘前的安全校验：无密钥格式、无空、可写长度。"""
    if not text:
        return False
    if has_key(text):
        return False
    return True


def _fix_with_llm(code: str, error_tail: str) -> str | None:
    """用网关模型生成修复后的完整源码；无法修复返回 None。"""
    from openai import OpenAI

    model = os.getenv("AGENT_MODEL", "")
    if not model:
        return None
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        timeout=120,
    )
    user = f"# 文件源码（可能不完整）\n```python\n{code[:CODE_CTX]}\n```\n\n# 最近一次运行错误\n{error_tail[:ERR_TAIL]}"
    try:
        resp = call_with_provider_retry(
            lambda: client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _FIX_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=3000,
            )
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        public = provider_public_text(exc)
        return f"__FIX_ERROR__:{public or type(exc).__name__}"
    text = re.sub(r"^```(?:python)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    if not text or text.startswith("__NO_FIX__"):
        return None
    if not _validate_fix(text):
        return None  # 疑似夹带密钥或空内容，拒绝自动写入
    return text


def _safe_write(project_dir: Path, filename: str, content: str) -> bool:
    target = _file_target(project_dir, filename)
    if target is None:
        return False
    if len(content) > 300_000 or "\x00" in content:
        return False
    target.write_text(content, encoding="utf-8")
    return True


def _ok_runs(out: str) -> bool:
    return "退出码: 0" in out and "运行超时" not in out and "运行失败" not in out


def _strip_output(out: str) -> str:
    return " ".join(out.split())[-350:]


def code_loop_impl(project: str, filename: str, max_attempts: int = MAX_ATTEMPTS, args: str = "") -> str:
    """Codex 循环内部实现；返回带四段字段的结构化结果文本。"""
    if not _exec_enabled():
        return "错误：代码执行未开启。请在 .env 加 ALLOW_CODE_EXEC=true 后重试。"
    project = (project or "").strip()
    project_dir = _project_dir(project)
    target = _file_target(project_dir, filename.strip()) if project_dir else None
    if target is None or not target.exists():
        return f"错误：找不到代码文件 {filename}（先用 write_code_file 写它）。"

    attempts = max(1, min(int(max_attempts), 5))
    try:  # 循环开始前自动拍快照，失败不阻塞循环
        from runtime.sandbox_snapshot import snapshot_impl

        snapshot_impl(project, note="code_loop 自动快照")
    except Exception:
        pass
    history: list[str] = []
    workspace_changed = False
    start = time.monotonic()
    for attempt in range(1, attempts + 1):
        from runtime.public_activity import record_fact
        record_fact("verification.started")
        out = run_python_impl(project, filename=filename, args=args)
        record_fact("verification.result", result=out, failed=not _ok_runs(out))
        history.append(f"第{attempt}次：{_strip_output(out)}")
        if _ok_runs(out):
            elapsed = round(time.monotonic() - start, 1)
            return (
                f"【循环结果】✅ 通过（{attempt}/{attempts} 次尝试，用时 {elapsed}s）\n"
                f"做了什么：代码 {filename}，经运行验证后通过。\n"
                f"为何这样做：验证通过（退出码 0），符合 Codex 循环「失败自动修复重跑」的验证要求。\n"
                f"验证结果：{_strip_output(out)}\n"
                f"下一步：可继续补充测试用例，或把该文件复制到正式项目。\n"
                f"[Phase24-outcome] workspace_changed={workspace_changed} verification_passed=true"
            )
        error_tail = out
        code = target.read_text(encoding="utf-8", errors="replace")
        record_fact("fix.started")
        fix = _fix_with_llm(code, error_tail)
        if not fix or str(fix).startswith("__FIX_ERROR__"):
            joined = "；".join(history)
            if str(fix).startswith("__FIX_ERROR__"):
                detail = str(fix).split(":", 1)[1] if ":" in str(fix) else ""
                reason = f"（模型服务故障：{detail[:200]}；不是代码本身无法修复）"
            elif fix is None:
                reason = "（模型认为无法修复或内容疑似含密钥）"
            else:
                reason = ""
            return (
                f"【循环结果】❌ 失败（尝试 {attempt - 1}/{attempts} 次后停止）{reason}\n"
                f"做了什么：尝试自动修复 {filename} 并重跑 {attempt - 1} 次。\n"
                f"为何这样做：每次失败后用最近报错让模型生成修复后源码再重试。\n"
                f"验证结果：{joined}\n"
                f"下一步：建议人工查看文件与最近一次报错；若问题在需求/环境，请在对话中补充背景。"
            )
        if not _safe_write(project_dir, filename, fix):
            return f"【循环结果】❌ 失败：修复内容写入被拒（第 {attempt} 次）。\n{'；'.join(history)}"

    joined = "；".join(history)
    return (
        f"【循环结果】❌ 失败（达到 {attempts} 次上限）\n"
        f"做了什么：连续 {attempts} 次“运行→自动修复→重跑”仍未通过。\n"
        f"为何这样做：每次失败后的修复尝试见下，均未消除错误。\n"
        f"验证结果：{joined}\n"
        f"下一步：请把最后一段报错发给我人工分析；或调整需求后重新发起。"
    )


@function_tool
def code_loop(project: str, filename: str, max_attempts: int = 3, args: str = "") -> str:
    """代码验证闭环（Codex 循环）：运行代码→失败则自动修复重跑（≤3 次）→返回四段总结。
    project/filename 指 code_sandbox 项目与文件（先用 write_code_file 写好）；
    args 是传给脚本的参数。返回：做了什么/为何/验证结果/下一步。需要 ALLOW_CODE_EXEC=true。"""
    try:
        return _codex_trust_wrap(code_loop_impl(project, filename, max_attempts, args))
    except Exception as exc:
        return f"循环执行出错：{type(exc).__name__}: {str(exc)[:300]}"


def _codex_trust_wrap(text: str) -> str:
    from runtime.trust import tag

    return tag("程序输出(代码循环)", None, text)

