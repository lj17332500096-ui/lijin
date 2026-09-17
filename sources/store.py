"""source_chunks / FTS / vectors 的最小存储层（agent.db，SQLite 单连接封装）。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from sources.schema import ensure_tables, pack_vector, unpack_vector

DB_PATH = Path(__file__).resolve().parent.parent / "agent.db"
_DB_PATH = DB_PATH


def set_db_path(path: str | Path) -> None:
    """与 TaskManager 对齐同一 agent.db（测试用临时库时由 TaskManager 注入）。"""
    global _DB_PATH
    _DB_PATH = Path(path)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    ensure_tables(conn)
    return conn


# ---------------- 状态 / 元数据 ----------------

def update_source_status(source_id: str, *, parse_status: str | None = None,
                         index_status: str | None = None, text_unavailable: int | None = None,
                         parse_error: str | None = None, indexed_at: str | None = None,
                         index_version: str | None = None) -> None:
    sets: list[str] = []
    params: list[Any] = []
    mapping = {
        "parse_status": parse_status, "index_status": index_status,
        "text_unavailable": text_unavailable, "parse_error": parse_error,
        "indexed_at": indexed_at, "index_version": index_version,
    }
    for col, val in mapping.items():
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    params.append(source_id)
    with _conn() as conn:
        conn.execute(f"UPDATE project_sources SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()


def list_source_index_rows(project_id: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM project_sources WHERE task_id = ? AND index_status='ready' "
            "AND parse_status='ok'",
            (project_id,)).fetchall()
        return [dict(r) for r in rows]


def list_all_source_rows(project_id: str) -> list[dict[str, Any]]:
    """全部 Source 行（含状态），供“未就绪信号”判断。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, task_id, display_name, parse_status, index_status, parse_error "
            "FROM project_sources WHERE task_id = ?",
            (project_id,)).fetchall()
        return [dict(r) for r in rows]


def list_all_source_rows_pending() -> list[dict[str, Any]]:
    """崩溃遗留的 parsing/indexing/pending 行（启动恢复用）。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, task_id, display_name, parse_status, index_status "
            "FROM project_sources "
            "WHERE parse_status IN ('pending','parsing') OR index_status = 'indexing'"
        ).fetchall()
        return [dict(r) for r in rows]


def get_source_row(source_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, task_id, parse_status, index_status, index_version, indexed_at "
            "FROM project_sources WHERE id = ?", (source_id,)).fetchone()
        return dict(row) if row else None


def source_chunk_count(project_id: str) -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM source_chunks WHERE project_id=?",
                            (project_id,)).fetchone()[0]


# ---------------- 索引写入（事务内批量） ----------------

def replace_source_index(project_id: str, source_id: str, chunks: list[dict[str, Any]],
                         vectors: list[list[float]] | None, model: str | None,
                         index_version: str) -> int:
    """原子替换某 Source 的全部分块（先清后写，含 FTS/向量）。"""
    conn = _conn()
    try:
        conn.execute("BEGIN")
        cids = [r["cid"] for r in conn.execute(
            "SELECT cid FROM source_chunks WHERE project_id=? AND source_id=?",
            (project_id, source_id)).fetchall()]
        if cids:
            conn.executemany(
                "DELETE FROM source_chunks_fts WHERE rowid = ?", [(c,) for c in cids])
            conn.execute(
                "DELETE FROM source_chunk_vectors WHERE cid IN (%s)" %
                ",".join("?" * len(cids)), cids)
            conn.execute("DELETE FROM source_chunks WHERE project_id=? AND source_id=?",
                         (project_id, source_id))
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        chunk_ids: list[str] = []
        for seq, chunk in enumerate(chunks):
            cid = conn.execute(
                "INSERT INTO source_chunks (id, project_id, source_id, title, chunk_type, "
                "heading, section_path, start_line, end_line, page_number, content, "
                "content_hash, token_count, trust_level, seq, index_version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (chunk["id"], project_id, source_id, chunk.get("title") or "",
                 chunk.get("chunk_type") or "text", chunk.get("heading"),
                 chunk.get("section_path"), chunk.get("start_line"),
                 chunk.get("end_line"), chunk.get("page"), chunk.get("content"),
                 chunk.get("content_hash"), int(chunk.get("token_count") or 0),
                 "source", seq, index_version, now)).lastrowid
            chunk_ids.append(chunk["id"])
            conn.execute("INSERT INTO source_chunks_fts (rowid, content) VALUES (?,?)",
                         (cid, chunk.get("content") or ""))
            if vectors and seq < len(vectors):
                conn.execute(
                    "INSERT INTO source_chunk_vectors (cid, project_id, dim, model, blob, "
                    "index_version, created_at) VALUES (?,?,?,?,?,?,?)",
                    (cid, project_id, len(vectors[seq]), model or "",
                     pack_vector(vectors[seq]), index_version, now))
        conn.commit()
        return len(chunks)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_source_index(project_id: str, source_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("BEGIN")
        cids = [r["cid"] for r in conn.execute(
            "SELECT cid FROM source_chunks WHERE project_id=? AND source_id=?",
            (project_id, source_id)).fetchall()]
        if cids:
            conn.executemany("DELETE FROM source_chunks_fts WHERE rowid = ?",
                             [(c,) for c in cids])
            conn.execute(
                "DELETE FROM source_chunk_vectors WHERE cid IN (%s)" %
                ",".join("?" * len(cids)), cids)
            conn.execute("DELETE FROM source_chunks WHERE project_id=? AND source_id=?",
                         (project_id, source_id))
        conn.commit()
    finally:
        conn.close()


def delete_project_index(project_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("BEGIN")
        cids = [r["cid"] for r in conn.execute(
            "SELECT cid FROM source_chunks WHERE project_id=?", (project_id,)).fetchall()]
        if cids:
            conn.executemany("DELETE FROM source_chunks_fts WHERE rowid = ?",
                             [(c,) for c in cids])
            conn.execute("DELETE FROM source_chunk_vectors WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM source_chunks WHERE project_id=?", (project_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------- 读取（Retriever 用） ----------------

def fts_search(project_id: str, match_expr: str, limit: int = 60) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT c.cid, c.id, c.project_id, c.source_id, c.title, c.content, c.heading, "
            "c.section_path, c.start_line, c.end_line, c.page_number, c.token_count, "
            "c.seq, c.content_hash, bm25(source_chunks_fts) AS score "
            "FROM source_chunks_fts JOIN source_chunks c ON c.cid = source_chunks_fts.rowid "
            "WHERE source_chunks_fts MATCH ? AND c.project_id = ? "
            "AND EXISTS (SELECT 1 FROM project_sources s WHERE s.id = c.source_id "
            "            AND s.task_id = c.project_id AND s.parse_status='ok' "
            "            AND s.index_status='ready') "
            "ORDER BY score LIMIT ?",
            (match_expr, project_id, limit)).fetchall()
        return [dict(r) for r in rows]


def load_vectors(project_id: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT cid, dim, model, blob FROM source_chunk_vectors "
            "WHERE project_id=? AND EXISTS "
            "(SELECT 1 FROM source_chunks c WHERE c.cid = source_chunk_vectors.cid "
            " AND c.project_id = source_chunk_vectors.project_id "
            " AND EXISTS (SELECT 1 FROM project_sources s WHERE s.id = c.source_id "
            "             AND s.task_id = c.project_id AND s.parse_status='ok' "
            "             AND s.index_status='ready'))",
            (project_id,)).fetchall()
        out = []
        for r in rows:
            try:
                out.append({"cid": r["cid"], "dim": r["dim"], "model": r["model"],
                            "vector": unpack_vector(r["blob"])})
            except Exception:
                continue
        return out


def chunks_by_ids(project_id: str, cids: Iterable[int]) -> list[dict[str, Any]]:
    cid_list = list(cids)
    if not cid_list:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, cid, project_id, source_id, title, content, heading, section_path, "
            "start_line, end_line, page_number, token_count, seq, content_hash "
            "FROM source_chunks WHERE project_id=? AND cid IN (%s) "
            "AND EXISTS (SELECT 1 FROM project_sources s WHERE s.id = source_chunks.source_id "
            "            AND s.task_id = source_chunks.project_id AND s.parse_status='ok' "
            "            AND s.index_status='ready')" %
            ",".join("?" * len(cid_list)), [project_id, *cid_list]).fetchall()
        return [dict(r) for r in rows]


def vector_model_used(project_id: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT model FROM source_chunk_vectors WHERE project_id=? AND model <> '' "
            "LIMIT 1", (project_id,)).fetchone()
        return row["model"] if row else None
