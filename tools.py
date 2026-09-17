"""工具箱：文件产出（备忘/文章）读写、上网搜索、工作区文件读取、安全数学计算。"""

import ast
import html as html_lib
import json
import math
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from agents import function_tool
from dotenv import load_dotenv

import scheduler

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
NOTES_DIR = BASE_DIR / "notes"
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or BASE_DIR.parent).resolve()

# 敏感/无关目录与文件：不让 Agent 读取（例如 .env 里存着 API Key）
_SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".idea"}
_SKIP_FILES = {".env", "apikey.txt"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_DEFAULT_MEMORY_FILE = BASE_DIR / "memory.json"
_MEMORY_FILE = _DEFAULT_MEMORY_FILE
_MEMORY_DB_PATH = BASE_DIR / "agent.db"   # SQLite 记忆后端（可被测试替换）
_MEMORY_SCOPE = ("user", "personal")
_MEMORY_MAX = 200
_last_recall_call = {"key": None, "ts": 0.0, "count": 0}

# v3：记忆作用域绑定（run 期间由 AgentRuntime 设置；None=旧行为=全局个人记忆）
_MEMORY_BINDING = {"task_id": None, "scope": None}


def set_active_memory_binding(task_id: str | None, scope: str | None) -> None:
    _MEMORY_BINDING.update(task_id=task_id, scope=scope)


def clear_active_memory_binding() -> None:
    _MEMORY_BINDING.update(task_id=None, scope=None)



def _memory_enabled() -> bool:
    return os.getenv("FORGE_MEMORY_ENABLED", "1").strip().lower() not in ("0", "off", "false", "no")


def _active_project() -> dict | None:
    """当前 Run 的记忆作用域绑定（P1-C：优先取 RunContext，并发 run 互不覆盖；
    无 RunContext 时回退进程级 _MEMORY_BINDING，兼容旧测试与直接调用）。"""
    try:
        from runtime.runctx import current as _rc

        ctx = _rc()
        if ctx is not None and ctx.container_id and ctx.memory_scope:
            return {"task_id": ctx.container_id, "scope": ctx.memory_scope}
    except Exception:
        pass
    return None if not _MEMORY_BINDING.get("task_id") else dict(_MEMORY_BINDING)


def _project_manager():
    from runtime.task_manager import TaskManager

    return TaskManager(_MEMORY_DB_PATH)

_last_repeat_calls: dict[str, dict] = {}
_REPEAT_HINTS = {
    "web_search": "你已经在同一轮用相同关键词调用过联网搜索。请直接基于已返回的结果作答；若确实没有结果，就直接说明没找到，不要重复调用本工具。",
    "read_workspace_file": "你已经在同一轮用相同路径读取过这个文件。请直接基于已读到的内容作答；若内容不够，请在回复里说明限制，不要重复调用本工具。",
    "list_workspace_files": "你已经在同一轮列过这个目录。请直接基于已列出的内容选择要读的文件或作答，不要反复翻目录。",
    "list_notes": "你已经在同一轮看过文件产出清单。请直接基于列表作答或让用户选择，不要重复调用本工具。",
    "run_python": "你已经在同一轮用相同代码/参数调用过运行 Python。请基于已返回的输出作答；若结果不对，请修改代码或说明限制，不要原样重跑。",
}


def _too_repetitive(tool_name: str, key: str, window: float = 10.0) -> bool:
    """检测同一工具 + 同一关键参数在短时间窗口内被重复调用，防止模型空转耗尽循环上限。"""
    now = time.time()
    rec = _last_repeat_calls.get(tool_name)
    if rec is not None and rec["key"] == key and now - rec["ts"] < window:
        rec["count"] += 1
        rec["ts"] = now
        return rec["count"] >= 2
    _last_repeat_calls[tool_name] = {"key": key, "ts": now, "count": 1}
    return False


_MATH_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_MATH_FUNCS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
}


def _active_read_root_for(target_abs: Path) -> Path:
    """严格 Project 且绑定 WorkLocation 时：落在 wl 内的目标以 wl 为工具根，
    其余路径仍按 WORKSPACE_ROOT 判定（数据/沙箱目录在 WORKSPACE_ROOT 子树内）。"""
    try:
        from runtime.runctx import current as _rc

        ctx = _rc()
        fs = getattr(ctx, "file_scope", None) if ctx else None
        if fs is not None and getattr(fs, "strict", False) and fs.work_location:
            root = fs.work_location.resolve()
            p = target_abs.resolve()
            if p == root or root in p.parents:
                return fs.work_location
    except Exception:
        pass
    return WORKSPACE_ROOT


def _resolve_under_root(path_str: str) -> Path | None:
    """把用户给的路径解析到工作区内；越界返回 None。

    路径解析根是运行期边界：严格 Project（绑 WorkLocation）时落在 wl 内的路径
    以 WorkLocation 为根；其余语境（legacy 个人会话 / 非 wl 路径）仍是 WORKSPACE_ROOT。
    """
    root = WORKSPACE_ROOT
    target = Path(path_str).expanduser()
    if not target.is_absolute():
        target = root / target
    else:
        root = _active_read_root_for(target)
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def _is_protected(path: Path) -> bool:
    """判断文件是否属于敏感/不应读取的文件。"""
    return (
        path.name in _SKIP_FILES
        or any(path.name.startswith(prefix) for prefix in (".env.", "memory.json"))
        or path.name.endswith(".migrated.bak")  # FORGE 内部记忆迁移备份（含 memory.json.*）
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
        or any(part in _SKIP_DIRS for part in path.parts)
    )


def _eval_node(node: ast.AST, depth: int = 0):
    """递归、安全地求值一个算术表达式 AST（只允许数学运算与白名单函数）。"""
    if depth > 30:
        raise ValueError("表达式嵌套太深，已拒绝计算")

    if isinstance(node, ast.Expression):
        return _eval_node(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _MATH_CONSTANTS:
        return _MATH_CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand, depth + 1)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, depth + 1)
        right = _eval_node(node.right, depth + 1)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            return left**right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func = _MATH_FUNCS.get(node.func.id)
        if func and not node.keywords:
            args = [_eval_node(arg, depth + 1) for arg in node.args]
            return func(*args)
    raise ValueError("只支持数字、四则运算、括号和常用数学函数，不支持任意代码")


def _tavily_search(query: str, max_results: int) -> list[dict] | None:
    """通过 Tavily API 搜索（需要 TAVILY_API_KEY）。"""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return None
    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def _ddg_lite_search(query: str, max_results: int) -> list[dict]:
    """用 DuckDuckGo Lite 页面搜索（免费、无需 Key，结果尽力而为）。"""
    resp = requests.get(
        "https://lite.duckduckgo.com/lite/",
        params={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=4,
    )
    resp.raise_for_status()
    page = resp.text

    results: list[dict] = []
    anchor_re = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
    snippet_re = re.compile(r"<td class=['\"]result-snippet['\"]>(.*?)</td>", re.S | re.I)
    snippets = [
        html_lib.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        for s in snippet_re.findall(page)
    ]

    for raw_url, raw_title in anchor_re.findall(page):
        if len(results) >= max_results:
            break
        if not raw_url.startswith("http") or "duckduckgo.com" in raw_url:
            continue
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        if not title:
            continue
        snippet = snippets[len(results)] if len(results) < len(snippets) else ""
        results.append({"title": title, "url": raw_url, "content": snippet})
    return results


def _bing_search(query: str, max_results: int) -> list[dict]:
    """用 Bing 网页搜索（免费、无需 Key，国内网络一般可达）。"""
    resp = requests.get(
        "https://www.bing.com/search",
        params={"q": query, "count": str(max_results * 2), "setlang": "zh-hans"},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        timeout=12,
    )
    resp.raise_for_status()
    page = resp.text

    links = re.findall(
        r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        page,
        re.S | re.I,
    )
    snippets = re.findall(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', page, re.S | re.I)

    results: list[dict] = []
    for i, (url, raw_title) in enumerate(links):
        if len(results) >= max_results:
            break
        if not url.startswith("http"):
            continue
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        if not title:
            continue
        snippet = snippets[i] if i < len(snippets) else ""
        snippet = html_lib.unescape(re.sub(r"<[^>]+>", "", snippet)).strip()
        results.append({"title": title, "url": url, "content": snippet})
    return results


def _format_search_results(results: list[dict]) -> str:
    if not results:
        return "没有搜到结果，换个关键词试试。"
    lines = []
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. {item.get('title', '无标题')}")
        lines.append(f"   链接: {item.get('url', '')}")
        content = (item.get("content") or "").strip()
        if content:
            lines.append(f"   摘要: {content[:300]}")
    return "\n".join(lines)


def _safe_name(name: str) -> str:
    """把标题转成安全的文件名片段。"""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_")
    return (cleaned or "note")[:80]



def _memory_use_sqlite() -> bool:
    """默认走 SQLite（agent.db 结构化记忆）；测试把 _MEMORY_FILE 换成临时文件时回退 JSON。"""
    return Path(_MEMORY_FILE) == _DEFAULT_MEMORY_FILE


def _memory_manager():
    from runtime.task_manager import TaskManager

    return TaskManager(_MEMORY_DB_PATH)


def _memory_classify(text: str) -> str:
    """简单分类：偏好/目标类标记 → preference，否则 semantic（未来可扩展 episodic）。"""
    markers = ("我喜欢", "我偏好", "喜欢", "不喜欢", "习惯", "偏好", "请记住", "我通常", "目标", "打算")
    return "preference" if any(marker in text for marker in markers) else "semantic"


def _memory_gate(text: str) -> str | None:
    """记忆写闸：密钥/空壳类内容拒绝入库（本地规则，零模型调用）。"""
    from runtime.key_patterns import has_key

    if has_key(text):
        return "错误：内容疑似包含 API Key/密钥，不允许写入记忆（凭据请只放 .env）"
    if len(text) < 4 and not any(ch.isalnum() for ch in text):
        return "错误：内容过于空洞，不值得记忆"
    return None


_memory_json_migration_done = False


def _migrate_json_memory_if_needed() -> None:
    """把旧 memory.json 一次性迁入 SQLite（保留 id/时间，之后改名备份）。"""
    global _memory_json_migration_done
    if _memory_json_migration_done:
        return
    _memory_json_migration_done = True
    if not _memory_use_sqlite():
        return
    legacy = Path(_DEFAULT_MEMORY_FILE)
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
        entries = data.get("entries", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        entries = []
    if not entries:
        return
    manager = _memory_manager()
    for entry in entries:
        try:
            manager.memory_upsert_row(
                entry,
                scope_type=_MEMORY_SCOPE[0],
                scope_id=_MEMORY_SCOPE[1],
                memory_type=_memory_classify(str(entry.get("text", ""))),
            )
        except Exception:
            continue
    try:
        legacy.rename(_DEFAULT_MEMORY_FILE.with_suffix(".json.migrated.bak"))
    except OSError:
        pass


def _load_memory() -> list[dict]:
    if _memory_use_sqlite():
        _migrate_json_memory_if_needed()
        return _memory_manager().memory_rows(scope_type=_MEMORY_SCOPE[0], scope_id=_MEMORY_SCOPE[1])
    if not _MEMORY_FILE.exists():
        return []
    try:
        data = json.loads(_MEMORY_FILE.read_text(encoding="utf-8"))
        return data.get("entries", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_memory(entries: list[dict]) -> None:
    if _memory_use_sqlite():
        manager = _memory_manager()
        manager.memory_clear(scope_type=_MEMORY_SCOPE[0], scope_id=_MEMORY_SCOPE[1])
        for entry in entries:
            manager.memory_upsert_row(
                entry,
                scope_type=_MEMORY_SCOPE[0],
                scope_id=_MEMORY_SCOPE[1],
                memory_type=_memory_classify(entry.get("text", "")),
            )
        return
    _MEMORY_FILE.write_text(
        json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@function_tool
def remember(text: str, tags: str = "") -> str:
    """把一条值得长期记住的信息存入跨会话记忆（默认 SQLite，兼容旧 JSON 文件）。
    text 是记忆内容（个人偏好、长期目标、项目背景等，不超过 2000 字）；
    tags 是可选逗号分隔标签，例如“用户偏好,项目背景”。内容重复时会更新原条目。"""
    text = text.strip()
    if not text:
        return "错误：记忆内容不能为空。"
    if len(text) > 2000:
        return "错误：记忆内容太长（超过 2000 字），请精简后重试。"
    gate_reason = _memory_gate(text)
    if gate_reason:
        return gate_reason
    tags_clean = [t.strip() for t in tags.split(",") if t.strip()]

    if not _memory_enabled():
        return "长期记忆已关闭。可在 设置 → 记忆 里重新开启。"
    binding = _active_project()
    if binding and binding.get("scope") == "project_only":
        # 记忆范围=仅此项目：写入本项目记忆，绝不写入全局
        row = _project_manager().add_project_memory(binding["task_id"], text, tags_clean)
        return f"已记住到本项目记忆（id={row['id']}）。当前为“仅此项目”模式，不会写入全局记忆。"

    now = datetime.now().isoformat(timespec="seconds")

    entries = _load_memory()
    for entry in entries:
        if entry["text"].strip() == text:
            merged = list(dict.fromkeys(entry.get("tags", []) + tags_clean))
            entry.update(tags=merged, updated_at=now)
            _save_memory(entries)
            return f"已更新已有记忆（id={entry['id']}），当前共 {len(entries)} 条。"

    if len(entries) >= _MEMORY_MAX:
        return f"错误：长期记忆已达上限 {_MEMORY_MAX} 条，先用 forget_memory 清理再存。"
    entry = {
        "id": "mem_" + uuid.uuid4().hex[:8],
        "text": text,
        "tags": tags_clean,
        "created_at": now,
        "updated_at": now,
    }
    entries.append(entry)
    _save_memory(entries)
    extra = "（同时已记入本项目记忆）" if binding and binding.get("scope") == "global" else ""
    return f"已记住（id={entry['id']}），当前共 {len(entries)} 条。{extra}".strip()


@function_tool
def recall_memory(keyword: str = "") -> str:
    """检索跨会话长期记忆。keyword 为空时返回全部记忆；否则按文字和标签模糊匹配。
    涉及用户偏好、历史背景等信息时，回答前先调用本工具。
    记忆范围=仅此项目时只检索本项目记忆；使用全局记忆时追加全局记忆。"""
    binding = _active_project()
    manager = _project_manager()
    if binding and binding.get("scope") == "project_only":
        rows = (manager.search_project_memories(binding["task_id"], keyword, limit=15)
                if keyword.strip() else manager.list_project_memories(binding["task_id"], limit=15))
        if not rows:
            return ("项目记忆里没有找到相关内容。（当前项目记忆范围=仅此项目，"
                    "不会读取全局记忆或其他项目记忆。）")
        lines = [f"- id={e['id']} [{'、'.join(e.get('tags', [])) or '无标签'}] {e['text']}" for e in rows]
        return "\n".join(lines)
    # 全局（或未绑定=旧行为）：
    global_entries = _load_memory()
    project_rows = []
    if binding and binding.get("scope") == "global":
        project_rows = manager.list_project_memories(binding["task_id"], limit=15)
    entries = list(global_entries)
    entries += [
        {"id": r["id"], "text": r["text"], "tags": r.get("tags", [])} for r in project_rows
    ]
    keyword = keyword.strip()
    if not entries:
        return "长期记忆里还没有任何内容，可以让用户先说一些偏好或背景，再用 remember 保存。"
    if keyword:
        tokens = [tok for tok in re.split(r"[\s,，、;；]+", keyword.lower()) if tok]
        key = " ".join(sorted(tokens)) if tokens else keyword.lower()
        matched = [
            e
            for e in entries
            if any(
                tok in e["text"].lower()
                or any(tok in t.lower() for t in e.get("tags", []))
                for tok in tokens
            )
        ]
        if not tokens:
            matched = []
    else:
        key = "*all*"
        matched = entries

    now = time.time()
    within = now - _last_recall_call["ts"] < 8
    if within and key == _last_recall_call["key"]:
        count = _last_recall_call["count"] + 1
    elif within and _last_recall_call["empty"] and not matched:
        count = _last_recall_call["count"] + 1  # 换关键词连续空结果也算重复空转
    else:
        count = 1
    repeated = count >= 2
    _last_recall_call.update(key=key, ts=now, count=count, empty=not matched)

    if not matched:
        message = f"长期记忆里没有找到与“{keyword}”相关的内容。"
    else:
        lines = []
        for e in matched[-50:]:
            tags = "、".join(e.get("tags", [])) or "无标签"
            lines.append(f"- id={e['id']} [{tags}] {e['text']}")
        message = "\n".join(lines)
    if repeated:
        hint = (
            "连续多次检索长期记忆都没有结果，立即停止换关键词再试："
            "直接告知用户记忆中无相关内容，或需要用 questions 请用户补充，不要重复调用本工具。"
            if not matched
            else "这一轮你已经调用过 recall_memory，结果与上次相同，请直接基于上面的内容作答，不要再重复调用本工具。"
        )
        message = f"（提醒：{hint}）\n\n{message}"
    return message


@function_tool
def forget_memory(entry_id: str) -> str:
    """删除一条长期记忆，entry_id 是 recall_memory 结果里显示的 id（例如 mem_ab12cd34 或 pmem_xxxx）。"""
    entry_id = entry_id.strip()
    binding = _active_project()
    if entry_id.startswith("pmem_"):
        mgr = _project_manager()
        if binding and binding.get("scope") == "project_only":
            # 仅允许删当前项目的项目记忆
            rows = mgr.list_project_memories(binding["task_id"], limit=500)
            own = any(r["id"] == entry_id for r in rows)
            if not own:
                return f"错误：{entry_id} 不属于当前项目，不能删除。"
        ok = mgr.delete_project_memory(entry_id)
        return f"已删除项目记忆（id={entry_id}）。" if ok else f"错误：找不到 id={entry_id} 的项目记忆。"
    entries = _load_memory()
    remaining = [e for e in entries if e["id"] != entry_id]
    if len(remaining) == len(entries):
        return f"错误：找不到 id={entry_id} 的记忆，先用 recall_memory 确认 id。"
    removed = next(e for e in entries if e["id"] == entry_id)
    _save_memory(remaining)
    return f"已删除记忆（id={entry_id}）：{removed['text'][:100]}"


def _trust_wrap(source: str, text: str, uri: object | None = None, max_len: int = 20000) -> str:
    """给外部数据出口包上信任边界（provenance/trust 标记）。"""
    from runtime.trust import tag

    return tag(source, str(uri) if uri else None, text, max_len=max_len)


def web_search_impl(query: str, max_results: int = 5) -> str:
    """web_search 的内部实现（供深度调研等模块直接调用，不经过防重复提醒）。"""
    query = query.strip()
    limit = max(1, min(int(max_results), 10))
    problems: list[str] = []

    # 1) 配了 Tavily Key 时优先用 Tavily（最稳定）
    if os.getenv("TAVILY_API_KEY"):
        try:
            results = _tavily_search(query, limit)
            if results:
                return _trust_wrap("web", _format_search_results(results))
            problems.append("Tavily 没有返回结果")
        except Exception as exc:
            problems.append(f"Tavily 失败（{type(exc).__name__}）")

    # 2) DuckDuckGo（免费）
    try:
        results = _ddg_lite_search(query, limit)
        if results:
            return _trust_wrap("web", _format_search_results(results))
        problems.append("DuckDuckGo 没有返回结果")
    except Exception as exc:
        problems.append(f"DuckDuckGo 失败（{type(exc).__name__}）")

    # 3) Bing（免费，国内网络一般可达）
    try:
        results = _bing_search(query, limit)
        if results:
            return _trust_wrap("web", _format_search_results(results))
        problems.append("Bing 没有返回结果")
    except Exception as exc:
        problems.append(f"Bing 失败（{type(exc).__name__}）")

    return "三个搜索源都失败了。" + "；".join(problems) + "。可配置 TAVILY_API_KEY 提高成功率，或稍后重试。"


@function_tool
def web_search(query: str, max_results: int = 5) -> str:
    """在互联网上搜索最新信息并返回标题、链接和摘要列表。
    query 是搜索关键词（可用中文或英文）；max_results 是希望返回的结果条数，1-10。"""
    if _too_repetitive("web_search", query.strip()):
        return f"（提醒）{_REPEAT_HINTS['web_search']}"
    return web_search_impl(query, max_results)


def save_note_impl(title: str, content: str) -> str:
    """save_note 的内部实现（供深度调研等模块直接调用）。"""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = NOTES_DIR / f"{timestamp}_{_safe_name(title)}.md"
    path.write_text(content, encoding="utf-8")
    return str(path)


@function_tool
def save_note(title: str, content: str) -> str:
    """把一段文字产出（备忘、文章、方案、总结、清单、周报等）保存为 Markdown 文件到本机 notes 目录。
    title 是简短的文件名标题（不含扩展名）；content 是完整正文。返回保存后的完整路径。"""
    return save_note_impl(title, content)


@function_tool
def read_note(filename: str) -> str:
    """读取 notes 目录里的某个文件产出，filename 为文件名（可带 .md）。
    返回文件内容；如果文件不存在则返回错误提示。"""
    target = (NOTES_DIR / filename).resolve()
    if NOTES_DIR.resolve() not in target.parents or target.suffix.lower() != ".md":
        return "错误：只允许读取 notes 目录内的 .md 文件。"
    if not target.exists():
        return f"错误：notes 目录里找不到 {filename}，可先用 list_notes 查看现有文件。"
    from runtime.trust import tag

    return tag("笔记文件", None, target.read_text(encoding="utf-8"))


@function_tool
def list_notes() -> str:
    """列出 notes 目录里保存过的所有文件产出名（按修改时间倒序）。"""
    if _too_repetitive("list_notes", "*all*"):
        return f"（提醒）{_REPEAT_HINTS['list_notes']}"
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(NOTES_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "notes 目录还是空的。"
    return "\n".join(f"- {p.name}" for p in files)


@function_tool
def read_workspace_file(path: str, max_chars: int = 12000) -> str:
    """读取工作区内的文本文件内容（只读，不会修改任何文件）。
    path 可以是相对工作区根目录的路径（如 my_creative_agent/agent.py），也可以是绝对路径；
    二进制文件、超大文件（超过 2MB）以及 .env 等敏感文件会被拒绝。

    path 必须填一个具体的文件路径，不能留空。读取一次拿到内容后直接作答即可；
    除非用户明确要求，否则不要反复读取同一个文件（会被运行时视为空转而阻止）。"""
    if _too_repetitive("read_workspace_file", path.strip()):
        return f"（提醒）{_REPEAT_HINTS['read_workspace_file']}"
    target = _resolve_under_root(path)
    if target is None:
        return f"错误：只能读取工作区 {WORKSPACE_ROOT} 内的文件。"
    if _is_protected(target):
        return "错误：出于安全考虑，这个文件不允许读取。"
    if not target.exists():
        return f"错误：找不到文件 {target}。可先用 list_workspace_files 查看有什么。"
    if target.is_dir():
        return f"错误：{target} 是目录，不是文件。可先用 list_workspace_files 查看内容。"

    size = target.stat().st_size
    if size > 2 * 1024 * 1024:
        return f"错误：文件超过 2MB（实际 {size} 字节），暂不支持整读。"
    try:
        data = target.read_bytes()
    except Exception as exc:
        return f"读取失败：{exc}"
    if b"\x00" in data[:8192]:
        return "这个文件看起来是二进制文件（不是文本），无法直接阅读。"

    text = data.decode("utf-8", errors="replace")
    limit = max(200, min(int(max_chars), 60000))
    head = text[:limit]
    truncated = len(text) > limit
    line_count = text.count("\n") + 1
    note = f"\n……（内容较长，仅显示前 {limit} 字符）" if truncated else ""
    body = f"文件: {target}\n大小: {size} 字节, 约 {line_count} 行\n\n{head}{note}"
    return _trust_wrap("文件", body, uri=target)


@function_tool
def list_workspace_files(directory: str = ".", max_entries: int = 50) -> str:
    """列出工作区内某个目录下的文件和子目录（只列一层，不递归）。
    directory 默认是工作区根目录；返回名称、类型与大小，方便定位要读的文件。

    拿到列表后请立即做出唯一决策（否则就是空转，会被运行时阻止）：
    - 若想深入某个子目录：下次调用时把 directory 设成具体子目录名（如 directory="正文"），
      不要再用默认值重复列同一层；
    - 若已能定位到要读的文件：改用 read_workspace_file(path=具体路径)；
    - 若列表已足以回答用户问题：直接作答，结束工具调用。
    绝对不要在同一层目录上反复调用本工具却不传入新的 directory。"""
    directory = directory.strip() or "."
    if _too_repetitive("list_workspace_files", directory):
        return f"（提醒）{_REPEAT_HINTS['list_workspace_files']}"
    target = _resolve_under_root(directory)
    if target is None:
        return f"错误：只能查看工作区 {WORKSPACE_ROOT} 内的目录。"
    if not target.exists() or not target.is_dir():
        return f"错误：找不到目录 {target}。"

    limit = max(1, min(int(max_entries), 200))
    lines = [f"目录: {target}"]
    count = 0
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception as exc:
        return f"读取目录失败：{exc}"
    for entry in entries:
        if count >= limit:
            lines.append(f"……（还有 {len(entries) - count} 项未显示，可用 max_entries 调大）")
            break
        if entry.is_dir():
            if entry.name in _SKIP_DIRS:
                continue
            lines.append(f"📁 {entry.name}/")
        else:
            if entry.name in _SKIP_FILES:
                continue
            size_kb = entry.stat().st_size / 1024
            size_text = f"{size_kb:.1f} KB" if size_kb >= 1 else f"{entry.stat().st_size} B"
            lines.append(f"📄 {entry.name}  ({size_text})")
        count += 1
    return "\n".join(lines)


@function_tool
def calculate(expression: str) -> str:
    """计算数学表达式，返回结果。
    支持 + - * / // % ** 和括号，以及常用函数：sqrt、sin、cos、tan、log、log10、log2、exp、floor、ceil、abs、round、min、max，常数 pi、e。
    expression 只接受纯数学表达式，不接受任意代码。"""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _eval_node(tree)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError, RecursionError) as exc:
        return f"无法计算“{expression}”：{exc}"
    if isinstance(result, float):
        result = round(result, 10)
        if result == int(result) and abs(result) < 1e15:
            result = int(result)
    return f"{expression} = {result}"


_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


@function_tool
def get_current_datetime() -> str:
    """获取当前本地日期和时间（含星期几与 UTC 偏移）。
    天气、新闻、日程等涉及“今天/明天/本周/最新”的时效性问题，必须先调用本工具，
    用返回的日期作基准再搜索或作答，不要把搜索结果页上的日期当成“今天”。"""
    now = datetime.now().astimezone()
    tz_suffix = now.strftime("%z") or ""
    zone = f"UTC{tz_suffix}" if tz_suffix else "本地时区"
    return (
        f"当前本地时间：{now:%Y-%m-%d %H:%M:%S} "
        f"{_WEEKDAYS[now.weekday()]}（{zone}，按运行电脑的本地时区）"
    )


@function_tool
def schedule_add(name: str, schedule: str, prompt: str, enabled: bool = True) -> str:
    """新建一条定时任务，到点后由常驻进程自动执行。
    name 是任务名称（如“晨间天气”）；schedule 支持三种写法：
    每天“08:30”、指定星期“周一 09:00”或“Mon 09:00”、标准 cron 五段“30 8 * * 1-5”；
    prompt 是到点后要 Agent 执行的具体指令（可要求保存笔记/搜索/发总结）。
    需要先以 python main.py --daemon 常驻运行，任务才会自动触发。"""
    try:
        task = scheduler.add_task(name, schedule, prompt, enabled)
    except ValueError as exc:
        return f"错误：{exc}"
    return (
        f"已创建定时任务 id={task['id']}「{task['name']}」：{task['schedule']} 触发。\n"
        f"下次运行：{task['next_run']}\n"
        f"提示：{task['prompt'][:120]}\n"
        "请保持 python main.py --daemon 常驻运行，到点会自动执行。"
    )


@function_tool
def schedule_list() -> str:
    """列出所有定时任务及其下次运行时间、上次执行结果。"""
    tasks = scheduler.load_tasks()
    if not tasks:
        return "还没有定时任务。可以告诉我“每天早上 9 点提醒我喝水”之类的要求，我会用 schedule_add 创建。"
    return "定时任务：\n\n" + "\n\n".join(scheduler.format_task_line(t) for t in tasks)


@function_tool
def schedule_remove(task_id: str) -> str:
    """删除一条定时任务，task_id 形如 task_ab12cd34（用 schedule_list 查看）。"""
    if scheduler.remove_task(task_id):
        return f"已删除定时任务 {task_id}。"
    return f"错误：找不到 id={task_id} 的任务，可先用 schedule_list 查看现有任务。"


@function_tool
def schedule_set_enabled(task_id: str, enabled: bool) -> str:
    """启用或停用一条定时任务（停用后不会触发，但任务保留）。"""
    task = scheduler.set_task_enabled(task_id, enabled)
    if task is None:
        return f"错误：找不到 id={task_id} 的任务，可先用 schedule_list 查看现有任务。"
    state = "启用" if task["enabled"] else "停用"
    return f"已将定时任务 {task_id}「{task['name']}」设为{state}。"

