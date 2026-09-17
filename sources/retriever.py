"""Hybrid Retriever：FTS5(BM25) + 本地 ONNX 向量 → RRF 合并 → 轻量符号精确性 rerank。

- 强制 project_id 过滤（Source Scope，绝不跨 Project）；
- 区分 NO_RESULTS 与 RETRIEVAL_FAILED；
- 向量引擎/向量化失败自动降级 lexical-only（复用 rag.OnnxEmbedEngine）。
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from sources import store

RRF_K = 60
RRF_TOP = 60
DEFAULT_TOP_K = 6

_CJK = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize_query(query: str) -> tuple[list[str], list[str]]:
    """返回 (可检索 terms, 精确符号 terms)。CJK 整段按短语；英文按词并拆 camelCase。"""
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
    terms, exact = tokenize_query(query)
    pieces: list[str] = []
    for t in terms:
        cleaned = _escape_fts(t)
        if cleaned:
            pieces.append(f'"{cleaned}"')
    if not pieces:
        return ""
    return " OR ".join(pieces)


def _lexical_search(project_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    expr = build_match_expr(query)
    if not expr:
        return [], False
    try:
        return store.fts_search(project_id, expr, limit=limit), False
    except Exception:
        return [], True


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
