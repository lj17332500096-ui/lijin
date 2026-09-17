"""深度调研工具：把一个主题变成一份带来源的调研报告。

流程（全部通过当前网关的同一个模型 + 现有 web_search 实现完成）：
1. 规划：模型按主题拆出若干个检索词与调研大纲；
2. 第一轮检索：逐词调用 web_search（内部自动回退 Tavily → DuckDuckGo → Bing）；
3. 追问：把第一轮片段交给模型，让它补最多 3 个更聚焦的检索词再搜一轮；
4. 综合：模型把全部片段写成结构化 Markdown 报告，经 save_note 保存到 notes/。

成本提醒：一次调研 ≈ 若干次 web_search（免费源）+ 3 次模型调用。
"""

import json
import os
import re
import time
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MAX_TOPIC_CHARS = 300
PLAN_QUERIES = 6          # 第一轮生成的检索词数量
REFINE_QUERIES = 3        # 第二轮追问数量
SEARCH_TOP = 5            # 每个检索词取几条结果
RETURN_CAP = 2000         # 返回给 Agent 的报告节选长度（全文落盘）

_PLAN_PROMPT = """你是一名资深调研规划师。围绕主题拆解检索任务。
要求：
1. 输出 6 个检索关键词，覆盖：背景与定义、现状与数据、不同角度/对立观点、中文与英文关键词、具体案例；
2. 同时给一句话调研范围（要弄清什么、不做什么）。
只输出 JSON：{{"scope": "...", "queries": ["...", ...]}}，不要多余文字、不要代码块。"""

_REFINE_PROMPT = """你正在做深度调研。基于已检索到的片段，判断哪些关键信息还缺失或矛盾。
请输出最多 3 个更聚焦的补充检索关键词（去重，与已有查询不重复）。
只输出 JSON：{{"queries": ["...", ...]}}，最多 3 个；没有需要补充的就输出空数组。"""

_SYNTHESIS_PROMPT = """你是调研报告撰写者。根据提供的全部检索片段，写一份中文调研报告（Markdown）。
要求：
1. 结构：标题(#)、一句话结论、正文小节（每节先说结论再给论据，可标注来源序号 [1]）、
   存疑/矛盾之处、参考来源列表（编号+标题+链接）；
2. 只依据片段写，不编造；片段不足的明确写“资料有限”；
3. 全文 800-1800 字。只输出报告正文，不要代码块。"""


def _chat_model() -> str:
    model = os.getenv("AGENT_MODEL") or os.getenv("VISION_MODEL") or ""
    if not model:
        raise ValueError(".env 里未配置 AGENT_MODEL")
    return model


def _llm_text(system: str, user: str, max_tokens: int = 1200) -> str:
    from openai import OpenAI
    from runtime.provider_errors import call_with_provider_retry

    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        timeout=120,
    )
    response = call_with_provider_retry(
        lambda: client.chat.completions.create(
            model=_chat_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
        )
    )
    return (response.choices[0].message.content or "").strip()


def _provider_reason(exc: Exception) -> str:
    """返回已分类的用户可读 provider 文案；非 provider 失败返回空串。"""
    from runtime.provider_errors import provider_public_text

    return provider_public_text(exc) or ""


def _parse_queries(text: str) -> list[str]:
    """从模型输出里解析检索词数组：容忍 ```json 围栏、前后杂文、纯行列表。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    queries: list[str] = []
    try:
        obj = json.loads(cleaned[cleaned.find("{") : cleaned.rfind("}") + 1])
        queries = obj.get("queries") or []
    except Exception:
        queries = re.findall(r'"([^"]+)"', cleaned)
    if not queries:
        queries = [
            line.lstrip("-*0123456789.、 ").strip()
            for line in cleaned.splitlines()
            if line.strip() and len(line.strip()) > 3
        ]
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = (q or "").strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _parse_search_page(page: str) -> list[dict]:
    """解析 web_search 返回的文本：编号/标题 + 链接 + 摘要。"""
    results: list[dict] = []
    blocks = re.split(r"(?m)^(\d+)\. ", page.strip())
    # blocks: ['', '1', 'title...\n链接...\n摘要...', '2', ...]
    for i in range(1, len(blocks) - 1, 2):
        body = blocks[i + 1]
        link = re.search(r"链接[:：]\s*(\S+)", body)
        title_line = body.splitlines()[0] if body.splitlines() else ""
        title = re.sub(r"^#+\s*", "", title_line).strip()
        url = link.group(1) if link else ""
        if url.startswith("(") and url.endswith(")"):
            url = url[1:-1]
        m = re.search(r"摘要[:：]\s*(.+)", body, re.S)
        summary = (m.group(1).strip() if m else "")[:300]
        if title and url:
            results.append({"title": title, "url": url, "snippet": summary})
    return results


def _run_searches(queries: list[str]) -> list[dict]:
    """执行一组检索并收集片段（调用 web_search 内部实现）。"""
    from tools import web_search_impl

    hits: list[dict] = []
    seen_urls: set[str] = set()
    for query in queries:
        page = web_search_impl(query, SEARCH_TOP)
        for item in _parse_search_page(page):
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            hits.append(
                {
                    "query": query,
                    "title": item["title"],
                    "url": item["url"],
                    "snippet": item["snippet"],
                }
            )
    return hits


def _snippet_block(hits: list[dict], limit: int = 6000) -> str:
    """把片段压成发给模型的文本（每条截短、总量有上限）。"""
    lines: list[str] = []
    total = 0
    for i, hit in enumerate(hits, 1):
        seg = (
            f"[{i}] 标题：{hit['title']}\n来源：{hit['url']}\n"
            f"由检索词“{hit['query']}”得到\n摘要：{hit['snippet'][:400]}\n"
        )
        if total + len(seg) > limit:
            lines.append(f"……（共 {len(hits)} 条，其余略）")
            break
        lines.append(seg)
        total += len(seg)
    return "\n".join(lines)


def _dedupe_hits(hits: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        url = (h.get("url") or "").rstrip("/")
        if url and url in seen:
            continue
        seen.add(url)
        out.append(h)
    return out


def _format_return(title: str, path: str, report: str, n_queries: int, n_sources: int, elapsed: float) -> str:
    cap = min(RETURN_CAP, len(report))
    head = (
        f"深度调研完成：{title}（用时 {elapsed:.0f}s，{n_queries} 个检索词、"
        f"{n_sources} 个来源）\n报告全文已保存：{path}\n"
    )
    tail = "\n……（完整报告见保存的文件）" if cap < len(report) else ""
    return head + "\n--- 报告节选 ---\n\n" + report[:cap] + tail


def _clean_topic(topic: str) -> str:
    topic = re.sub(r"\s+", " ", (topic or "").strip())
    return topic[:MAX_TOPIC_CHARS]


def deep_research_impl(topic: str, max_queries: int = 12) -> str:
    """深度调研内部实现：拆词→两轮检索→综合成报告并落盘。"""
    topic = _clean_topic(topic)
    if not topic:
        return "错误：调研主题不能为空。"
    start = time.monotonic()

    plan_raw = _llm_text(_PLAN_PROMPT, f"调研主题：{topic}", max_tokens=800)
    queries = _parse_queries(plan_raw)[: max(4, min(max_queries, 12))]
    if not queries:
        return "规划失败：模型没有给出检索词，请换个说法或稍后重试。"

    hits = _run_searches(queries)
    executed = len(queries)

    # 第二轮：基于中间发现追问补充
    if hits:
        try:
            refine_raw = _llm_text(
                _REFINE_PROMPT,
                f"调研主题：{topic}\n\n已检索片段（部分）：\n{_snippet_block(hits[:20], limit=4000)}",
                max_tokens=500,
            )
            extra = _parse_queries(refine_raw)[:REFINE_QUERIES]
        except Exception:
            extra = []
        if extra:
            hits.extend(_run_searches(extra))
            executed += len(extra)

    hits = _dedupe_hits(hits)
    if not hits:
        return f"深度调研失败：多个检索词都没有搜到可用结果（主题：{topic}）。可稍后重试或换主题。"

    try:
        report = _llm_text(
            _SYNTHESIS_PROMPT,
            f"调研主题：{topic}\n\n检索片段：\n{_snippet_block(hits)}",
            max_tokens=2600,
        )
    except Exception as exc:
        reason = _provider_reason(exc)
        return f"深度调研失败：{reason}" if reason else "深度调研失败：写报告时模型调用出错，稍后重试。"
    if len(report.strip()) < 80:
        return "深度调研失败：报告过短，模型可能未按要求输出，稍后重试。"

    from tools import save_note_impl

    safe_topic = re.sub(r'[\\/:*?"<>|\r\n]+', "_", topic)[:40]
    path = save_note_impl(f"调研_{safe_topic}", report)
    elapsed = time.monotonic() - start
    return _format_return(topic, path, report, executed, len(hits), elapsed)


@function_tool
def deep_research(topic: str, max_queries: int = 12) -> str:
    """对一个主题做多轮联网深度调研，输出带来源的 Markdown 调研报告并保存到 notes/。
    topic 是调研主题（一句话，可中文）；max_queries 是检索词总数上限，1-12。
    内部会：拆成多个检索词分两轮搜索（自动回退 Tavily/DuckDuckGo/Bing），再综合成报告。
    适合“帮我调研 X / 全面了解一下 Y / 对比 A 和 B 的现状”这类需要多角度信息的需求。
    注意：一次性跑完，不要为同一主题重复调用本工具；耗时约 1-3 分钟属正常。"""
    try:
        from runtime.trust import tag

        return tag("调研资料(联网来源)", None, deep_research_impl(topic, max_queries))
    except Exception as exc:
        reason = _provider_reason(exc)
        if reason:
            return f"深度调研失败：{reason}"
        return f"深度调研出错：{type(exc).__name__}: {str(exc)[:300]}"

