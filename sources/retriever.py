"""Hybrid Retriever：FTS5(BM25) + 本地 ONNX 向量 → RRF 合并 → 轻量符号精确性 rerank。

- 强制 project_id 过滤（Source Scope，绝不跨 Project）；
- 区分 NO_RESULTS 与 RETRIEVAL_FAILED；
- 向量引擎/向量化失败自动降级 lexical-only（复用 rag.OnnxEmbedEngine）。
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Any

from sources import store

RRF_K = 60
RRF_TOP = 60
DEFAULT_TOP_K = 6

_CJK = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
#: 单个 CJK 字符（用于相邻二元组切分）
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def cjk_bigrams(text: str) -> list[str]:
    """中文按**相邻二元组**切分（与 rag.py::_tokens 同策略，无需分词库）。

    ⚠️ **为什么需要它**（10-05 CI 实据，已最小复现）：FTS5 的 `unicode61`
    分词器把中文**整段当一个 token** —— 内容「按钮必须放在右下角且带确认弹窗。」
    用 `"按钮"` / `"右下角"` / `"确认弹窗"` 查，命中数一律为 **0**。
    所以 FTS 那条路对中文**恒失效**（不是查询写法问题，是分词器问题）。
    相邻二元组（按钮/钮必/必须/…）能被 unicode61 正常切出，因此**在 Python 侧
    自行评分**即可补上中文召回。
    """
    chars = _CJK_CHAR.findall(text.lower())
    return ["".join(pair) for pair in zip(chars, chars[1:])]


def tokenize_query(query: str) -> tuple[list[str], list[str]]:
    """返回 (可检索 terms, 精确符号 terms)。CJK 整段按短语；英文按词并拆 camelCase。

    注：CJK 整段 term 是给 **Python 侧二元组评分**用的（`_python_lexical_search`）；
    送进 FTS5 的只有 ASCII 词与二元组（见 `build_match_expr`）。
    """
    ascii_parts = _WORD.findall(query)
    words: list[str] = []
    for part in ascii_parts:
        sub = _CAMEL.split(part)
        words.extend(sub)
        words.append(part)
    cjk_runs = _CJK.findall(query)
    terms = [w.lower() for w in words if len(w) >= 1] + cjk_runs
    exact = [w for w in words if len(w) >= 4]
    return terms, exact


def _escape_fts(term: str) -> str:
    for ch in ('"', "*", "(", ")", "{", "}", ":", " ", "\\"):
        term = term.replace(ch, " ")
    return term.strip()


def build_match_expr(query: str) -> str:
    """构造 FTS5 MATCH 表达式。

    ⚠️ **只送 FTS 能命中的 term**（10-05 CI 实据）：CJK 整段 term（如「确认弹窗」）
    送进 FTS5 **必然命中 0** —— `unicode61` 把中文整段当一个 token，而索引里存
    的是长句，中文子串不存在于任何单个 token。已最小复现：内容
    「按钮必须放在右下角且带确认弹窗。」用 `"按钮" OR "确认弹窗" OR "右下角"`
    查，命中数= 0。
    所以 CJK 一律改送**相邻二元组**（能被 unicode61 切出），中文召回由
    `_python_lexical_search` 兜底。ASCII 词/符号仍走 FTS5 原生 BM25。
    """
    _terms, _exact = tokenize_query(query)
    pieces: list[str] = []
    # ASCII 词：FTS5 原生分词能正常命中
    for w in _WORD.findall(query):
        cleaned = _escape_fts(w.lower())
        if cleaned:
            pieces.append(f'"{cleaned}"')
    # CJK：改送二元组（unicode61 切得出来）
    for bg in cjk_bigrams(query):
        cleaned = _escape_fts(bg)
        if cleaned:
            pieces.append(f'"{cleaned}"')
    if not pieces:
        return ""
    return " OR ".join(dict.fromkeys(pieces))  # 去重且保序


def _python_lexical_search(project_id: str, query: str,
                           limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Python 侧 BM25 兜底（中文召回的主力，见 `cjk_bigrams` 的说明）。

    与 `rag.py::_bm25_rank` 同策略：相邻二元组 + k1/b BM25，不依赖 FTS5 分词器。
    任何异常都退化为 `([], True)`（=该路失败），不拖垮整体检索。
    """
    terms = set(cjk_bigrams(query)) | {w.lower() for w in _WORD.findall(query)}
    terms = {t for t in terms if len(t) >= 2}
    if not terms:
        return [], False
    try:
        rows = store.all_ready_chunks(project_id)
    except Exception:
        return [], True
    if not rows:
        return [], False
    try:
        counters: dict[int, Counter] = {}
        for r in rows:
            text = " ".join(str(r.get(k) or "") for k in ("title", "heading", "content"))
            counters[r["cid"]] = Counter(
                list(cjk_bigrams(text)) + [w.lower() for w in _WORD.findall(text)]
            )
        n_docs = max(len(rows), 1)
        avg_len = (sum(sum(c.values()) for c in counters.values()) / n_docs) or 1.0
        k1, b = 1.5, 0.75
        scores: dict[int, float] = {}
        for token in terms:
            postings = [cid for cid, c in counters.items() if c.get(token)]
            if not postings:
                continue
            df = len(postings)
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
            for cid in postings:
                tf = counters[cid][token]
                norm = sum(counters[cid].values()) / avg_len
                scores[cid] = scores.get(cid, 0.0) + idf * (tf * (k1 + 1)) / (
                    tf + k1 * (1 - b + b * norm))
        if not scores:
            return [], False
        by_cid = {r["cid"]: r for r in rows}
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        out = []
        for cid, score in ranked:
            row = dict(by_cid[cid])
            row["score"] = -score  # 与 FTS5 bm25() 同向（越小越靠前）
            out.append(row)
        return out, False
    except Exception:
        return [], True


def _lexical_search(project_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """FTS5 与 Python BM25 两路合并（任一路成功即算成功）。

    两路都保留：FTS5 对英文/符号有效且快，Python 侧对中文有效。
    合并时按 `cid` 去重，Python 侧结果**补进** FTS5 结果（不覆盖排序位次，
    避免改变 FTS5 已有的英文排序行为）。
    """
    expr = build_match_expr(query)
    rows: list[dict[str, Any]] = []
    fts_failed = False
    if expr:
        try:
            rows = list(store.fts_search(project_id, expr, limit=limit))
        except Exception:
            fts_failed = True
    py_rows, py_failed = _python_lexical_search(project_id, query, limit=limit)
    seen = {r["cid"] for r in rows}
    for r in py_rows:
        if r["cid"] not in seen:
            rows.append(r)
            seen.add(r["cid"])
    # 两路都没命中**且**都没出错 ⇒ 真的没结果（不是失败）
    return rows[:limit], fts_failed and py_failed


def _semantic_search(project_id: str, query: str, limit: int) -> tuple[list[int], str | None]:
    """返回 (cids 排序, model)。向量不可用时返回 (None, reason)？约定：向量未启用 → ([], None)。"""
    vectors = store.load_vectors(project_id)
    if not vectors:
        return [], None
    try:
        import rag
        engine, err = rag._try_load_embedder()
        if engine is None:
            return [], err or "embedder_unavailable"
        qv = rag._embed_query(engine, query)
        if qv is None:
            return [], "embed_query_failed"
    except Exception as exc:  # noqa: BLE001
        return [], f"semantic_failed:{type(exc).__name__}"
    scored = []
    for item in vectors:
        sim = _cosine(qv, item["vector"])
        scored.append((item["cid"], sim))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [cid for cid, _ in scored[:limit]], None


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _rrf_scores(lexical: list[dict[str, Any]], semantic_cids: list[int]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for rank, row in enumerate(lexical[:RRF_TOP]):
        scores[row["cid"]] = scores.get(row["cid"], 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, cid in enumerate(semantic_cids[:RRF_TOP]):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def _rerank_bonus(content: str, query: str) -> float:
    """轻量 score-based rerank：精确符号（≥4 字符 token）出现即加分；不用 LLM。"""
    _terms, exact = tokenize_query(query)
    bonus = 0.0
    for token in exact:
        if token in content:
            bonus += 0.05
    return bonus


def retrieve(project_id: str, query: str, *, source_ids: list[str] | None = None,
             top_k: int = DEFAULT_TOP_K,
             audit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hybrid retrieve。返回统一结果 dict（含 mode / error / results）。"""
    started = time.monotonic()
    result: dict[str, Any] = {"ok": True, "mode": "hybrid", "error": None,
                              "results": [], "count": 0}

    lexical, lexical_error = _lexical_search(project_id, query, limit=RRF_TOP)
    if lexical_error:
        result["mode"] = "lexical_failed"

    semantic_cids, semantic_error = _semantic_search(project_id, query, limit=RRF_TOP)
    if semantic_error:
        result["mode"] = "lexical_only"
        result["error"] = semantic_error
    elif not semantic_cids:
        result["mode"] = "lexical_only" if lexical else result["mode"]

    if not lexical and not semantic_cids:
        result["ok"] = True
        result["mode"] = "no_results" if not result["error"] else "retrieval_failed"
        return result

    merged = _rrf_scores(lexical, semantic_cids)
    # 过滤 source_ids（可选，显式工具指定时）
    if source_ids is not None:
        allowed = set(source_ids)
        if allowed:
            lexical = [r for r in lexical if r["source_id"] in allowed]
            merged = {cid: score for cid, score in merged.items()
                      if cid in {r["cid"] for r in lexical}}
    if not merged:
        result["mode"] = "no_results"
        return result

    ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    cids = [cid for cid, _ in ranked[: max(top_k * 2, 10)]]
    rows = {r["cid"]: r for r in store.chunks_by_ids(project_id, cids)}

    out: list[dict[str, Any]] = []
    for cid, rrf in ranked:
        row = rows.get(cid)
        if row is None:
            continue
        content = row["content"]
        lexical_score = _lex_score(lexical, cid)
        semantic_score = _sem_score(semantic_cids, cid)
        combined = rrf + _rerank_bonus(content, query)
        out.append({
            "chunk_id": row["id"], "source_id": row["source_id"],
            "title": row.get("title"), "heading": row.get("heading"),
            "section_path": row.get("section_path"),
            "start_line": row.get("start_line"), "end_line": row.get("end_line"),
            "page": row.get("page_number"),
            "content": content[:2000],
            "lexical_score": round(lexical_score, 4),
            "semantic_score": round(semantic_score, 4),
            "combined_score": round(combined, 4),
            "token_count": row.get("token_count"),
        })
        if len(out) >= top_k:
            break

    result["results"] = out
    result["count"] = len(out)
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    result["embedding_model"] = store.vector_model_used(project_id)
    result["query"] = query[:200]
    if audit is not None:
        audit.update(result)
    return result


def _lex_score(lexical: list[dict[str, Any]], cid: int) -> float:
    for rank, row in enumerate(lexical):
        if row["cid"] == cid:
            return 1.0 / (rank + 1)
    return 0.0


def _sem_score(cids: list[int], cid: int) -> float:
    if cid in cids:
        return 1.0 / (cids.index(cid) + 1)
    return 0.0
