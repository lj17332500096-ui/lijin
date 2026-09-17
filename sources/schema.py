"""Sources 深度 RAG：schema 与最小存储访问（agent.db 内追加表，无平行库）。

设计（与 Runtime Stable Baseline 的关系）：
- Source = 参考资料（READ ONLY Reference Data，不是 Workspace）；
- 索引全部存 agent.db（个人规模、可随现有 Snapshot/Restore 一起备份）；
- 检索强制 project_id 过滤（Source Scope），绝不跨 Project；
- 所有 Retrieved Chunk 进 Context 前由调用方套 runtime.trust.tag（本模块不绕过）。

表：
- project_sources（既有表，追加列：enabled/text_unavailable/parse_error/indexed_at/index_version）
- source_chunks（结构化分块；cid 为 INTEGER rowid，供 FTS external content）
- source_chunks_fts（FTS5 external content，content 列影子）
- source_chunk_vectors（本地 ONNX embedding，BLOB float32）
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Any

INDEX_VERSION = "sources-v1"       # chunk 策略/表结构版本（与 embedding model 分开记录）
CHUNK_STRATEGY = "structure-v1"

DDL_CHUNKS = """
CREATE TABLE IF NOT EXISTS source_chunks (
    cid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    chunk_type TEXT NOT NULL DEFAULT 'text',
    heading TEXT,
    section_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    page_number INTEGER,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    trust_level TEXT NOT NULL DEFAULT 'source',
    seq INTEGER NOT NULL DEFAULT 0,
    index_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_srcchunks_project ON source_chunks(project_id, source_id);
CREATE INDEX IF NOT EXISTS idx_srcchunks_source ON source_chunks(source_id);
"""

DDL_VECTORS = """
CREATE TABLE IF NOT EXISTS source_chunk_vectors (
    cid INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL,
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    blob BLOB NOT NULL,
    index_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_srcvec_project ON source_chunk_vectors(project_id);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    """无条件确保 Sources 索引表与 project_sources 追加列（幂等）。"""
    conn.executescript(DDL_CHUNKS)
    conn.executescript(DDL_VECTORS)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS source_chunks_fts USING fts5("
        "content, content='source_chunks', content_rowid='cid')"
    )
    _ensure_columns(conn, "project_sources", {
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "text_unavailable": "INTEGER NOT NULL DEFAULT 0",
        "parse_error": "TEXT",
        "indexed_at": "TEXT",
        "index_version": "TEXT",
    })


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))
