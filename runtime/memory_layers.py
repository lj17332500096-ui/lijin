"""过程记忆 + 文件夹记忆（六类记忆中的两类新增）。

过程记忆（run_plan 表）：记录编排器每一步的决策/工具/结果摘要，
让「LLM 比对 → 再派发」的回环能看到「上一步做了什么」，避免重复劳动——
这是「循环有界限且能收敛」的前提。

文件夹记忆（folder_memory 表）：指定文件夹的索引与摘要，供 search_folder 检索。

设计纪律：
- 独立 SQLite 文件（FORGE_MEMORY_DB，默认 agent.db 同目录的 memory.db），
  不侵入 task_manager 的大建表逻辑，保持改动最小、基线零影响。
- WAL 模式 + 单写，与项目既有存储约定一致。
- 纯同步 sqlite3，供编排器在 run 内同步调用；不引入异步复杂度。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

_DEFAULT_DB = Path(os.getenv("FORGE_MEMORY_DB", "")) if os.getenv("FORGE_MEMORY_DB") else None


def default_db_path() -> Path:
    if _DEFAULT_DB is not None:
        return _DEFAULT_DB
    # 默认落在 agent.db 同目录，避免散落
    repo = Path(__file__).resolve().parent.parent
    return repo / "memory.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    decision TEXT,
    tool TEXT,
    args_json TEXT,
    result_summary TEXT,
    loop_count INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_plan_run ON run_plan(run_id);

CREATE TABLE IF NOT EXISTS folder_memory (
    path TEXT PRIMARY KEY,
    indexed_at TEXT,
    doc_count INTEGER,
    summary TEXT
);
"""


def _connect(db: Optional[Path] = None) -> sqlite3.Connection:
    path = db or default_db_path()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 过程记忆（run_plan）
# ---------------------------------------------------------------------------

def record_step(
    run_id: str,
    step_no: int,
    decision: str,
    tool: Optional[str] = None,
    args: Optional[dict] = None,
    result_summary: Optional[str] = None,
    loop_count: int = 0,
    db: Optional[Path] = None,
) -> None:
    """记录编排器一步决策（过程记忆写入）。"""
    import json
    args_json = json.dumps(args, ensure_ascii=False) if args is not None else None
    # 结果摘要截断，避免撑大表
    summary = (result_summary or "")[:2000] if result_summary else None
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO run_plan (run_id, step_no, decision, tool, args_json, result_summary, loop_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, step_no, decision, tool, args_json, summary, loop_count, _now_iso()),
        )


def recent_steps(run_id: str, limit: int = 10, db: Optional[Path] = None) -> list[dict[str, Any]]:
    """取某 run 最近 N 步（供回环时 LLM 看「已做过什么」）。"""
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT step_no, decision, tool, args_json, result_summary, loop_count "
            "FROM run_plan WHERE run_id = ? ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    return [
        {
            "step_no": r[0],
            "decision": r[1],
            "tool": r[2],
            "args": r[3],
            "result_summary": r[4],
            "loop_count": r[5],
        }
        for r in reversed(rows)
    ]


# ---------------------------------------------------------------------------
# 文件夹记忆（folder_memory）
# ---------------------------------------------------------------------------

def index_folder(path: str, summary: str = "", db: Optional[Path] = None) -> int:
    """记录一个指定文件夹的索引摘要，返回该目录下的文件数（尽力而为）。"""
    p = Path(path)
    doc_count = 0
    try:
        if p.is_dir():
            doc_count = sum(1 for _ in p.rglob("*") if _.is_file())
    except OSError:
        doc_count = 0
    with _connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO folder_memory (path, indexed_at, doc_count, summary) "
            "VALUES (?, ?, ?, ?)",
            (str(p), _now_iso(), doc_count, summary[:2000] if summary else ""),
        )
    return doc_count


def search_folder(query: str, k: int = 5, db: Optional[Path] = None) -> list[dict[str, Any]]:
    """按关键字粗匹配已索引文件夹（精确匹配能力交给 RAG 层）。"""
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT path, indexed_at, doc_count, summary FROM folder_memory ORDER BY indexed_at DESC LIMIT 200"
        ).fetchall()
    q = query.strip().lower()
    scored = []
    for path, indexed_at, doc_count, summary in rows:
        hay = f"{path} {summary}".lower()
        if q and q not in hay:
            continue
        scored.append({
            "path": path,
            "indexed_at": indexed_at,
            "doc_count": doc_count,
            "summary": summary,
        })
    return scored[:k]
