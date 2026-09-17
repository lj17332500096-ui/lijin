"""Sources 索引器：parse → chunk → FTS →（可选）向量，含增量与失败语义。"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from sources import parser as P
from sources import store
from sources.schema import INDEX_VERSION

_TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".rst", ".py", ".js", ".ts", ".tsx",
              ".jsx", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".c", ".cpp",
              ".h", ".java", ".go", ".rs"}
_PDF_EXTS = {".pdf"}
_DOCX_EXTS = {".docx"}
SUPPORTED_EXTS = _TEXT_EXTS | _PDF_EXTS | _DOCX_EXTS

_BINARY_PREFIXES = (b"%PDF", b"\x89PNG", b"\xff\xd8", b"PK\x03\x04")


def is_supported(name: str) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_EXTS


def _vectorize(texts: list[str], model_name: str | None) -> tuple[list[list[float]] | None, str | None]:
    """用现有 rag.OnnxEmbedEngine 批量向量化；失败返回 (None, error)（可降级）。"""
    if not texts or not model_name:
        return None, None
    try:
        import rag  # 复用 Runtime 既有本地 ONNX embedding 引擎
        engine, err = rag._try_load_embedder()
        if engine is None:
            return None, err
        return engine.embed_batch([t[:2000] for t in texts]), None
    except Exception as exc:  # noqa: BLE001
        return None, f"向量化失败: {type(exc).__name__}: {exc}"


def _embed_model() -> str | None:
    try:
        import rag
        engine, _err = rag._try_load_embedder()
        if engine is not None:
            return rag.EMBED_MODEL_SETTING or "local-onnx"
    except Exception:
        pass
    return None


def index_source_file(project_id: str, source: dict[str, Any]) -> dict[str, Any]:
    """对单个 Source 执行 parse→chunk→(embed)→索引。返回结果状态信息（不抛业务异常）。"""
    sid = source["id"]
    name = str(source.get("display_name") or "")
    path = Path(str(source.get("stored_path") or ""))
    if not path.is_file():
        store.update_source_status(sid, parse_status="failed", index_status="failed",
                                   parse_error="文件不存在")
        return {"ok": False, "reason": "file_missing"}
    if not is_supported(name):
        store.update_source_status(sid, parse_status="failed", index_status="failed",
                                   parse_error=f"不支持的格式：{Path(name).suffix}")
        return {"ok": False, "reason": "unsupported"}

    # P1：删除/替换竞态守卫 —— 索引前确认 Source 仍存在
    try:
        prev = store.get_source_row(sid)
    except Exception:
        prev = None
    if prev is None:
        return {"ok": False, "reason": "source_gone"}

    # 1) parse（文本/PDF/DOCX）
    store.update_source_status(sid, parse_status="parsing", index_status="indexing")
    started = time.monotonic()
    try:
        if Path(name).suffix.lower() in _PDF_EXTS:
            chunks_raw = P.parse_and_chunk(path, name, "pdf")
        elif Path(name).suffix.lower() in _DOCX_EXTS:
            chunks_raw = P.parse_and_chunk(path, name, "docx")
        else:
            chunks_raw = P.parse_and_chunk(path, name, Path(name).suffix.lstrip("."))
    except ValueError as exc:
        reason = str(exc)
        text_unavailable = int(reason == "text_unavailable")
        store.update_source_status(
            sid, parse_status="failed", index_status="failed",
            text_unavailable=text_unavailable,
            parse_error="扫描版 PDF 无文本层，暂不支持 OCR" if text_unavailable else reason[:400])
        return {"ok": False, "reason": reason, "text_unavailable": bool(text_unavailable)}
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        store.update_source_status(sid, parse_status="failed", index_status="failed",
                                   parse_error=reason)
        return {"ok": False, "reason": reason}

    if not chunks_raw:
        store.update_source_status(sid, parse_status="failed", index_status="failed",
                                   parse_error="解析后无可检索文本")
        return {"ok": False, "reason": "empty_text"}

    # 2) chunk 元数据补全
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    for c in chunks_raw:
        c["id"] = "sch_" + uuid.uuid4().hex[:12]
    # 3) 向量（失败可降级：仅 FTS）
    model = _embed_model()
    try:
        vectors, vector_error = _vectorize(
            [c["content"] for c in chunks_raw], model)
    except Exception:
        vectors, vector_error = None, "向量化异常（降级关键词检索）"
    if vectors is None and model:
        # 向量不可用时仍保留索引但记录；检索自动降级 lexical-only
        store.update_source_status(sid, parse_error=(vector_error or "")[:400])

    # 4) 原子写库（FTS + vectors 同事务）
    try:
        count = store.replace_source_index(project_id, sid, chunks_raw, vectors, model,
                                           INDEX_VERSION)
    except Exception as exc:  # noqa: BLE001
        # P1：更新失败 → 保留旧 ready 版本（若旧版本曾 ready），状态不得变成
        # “failed 但旧 chunk 仍可检索”的自相矛盾
        if str(prev.get("index_status") or "") == "ready" and str(prev.get("parse_status") or "") == "ok":
            store.update_source_status(
                sid, parse_status="ok", index_status="ready",
                parse_error="索引更新失败，已保留上一版本：%s" % str(exc)[:200],
                indexed_at=prev.get("indexed_at") or now,
                index_version=prev.get("index_version") or INDEX_VERSION,
            )
        else:
            store.update_source_status(sid, index_status="failed",
                                       parse_error=f"索引写入失败: {str(exc)[:300]}")
        return {"ok": False, "reason": "index_write_failed"}
    # P1：写入后再次确认 Source 未被并发删除（晚完成索引不得重插孤儿 chunk）
    try:
        after = store.get_source_row(sid)
    except Exception:
        after = None
    if after is None:
        try:
            store.delete_source_index(project_id, sid)
        except Exception:
            pass
        return {"ok": False, "reason": "deleted_while_indexing"}
    store.update_source_status(
        sid, parse_status="ok", index_status="ready", indexed_at=now,
        index_version=INDEX_VERSION,
        text_unavailable=0, parse_error=None)
    return {"ok": True, "chunks": count,
            "vectors": 0 if vectors is None else len(vectors),
            "elapsed_s": round(time.monotonic() - started, 3),
            "embedding_model": model}


def recover_interrupted_indexes() -> int:
    """启动恢复：把崩溃遗留的 parsing/indexing/pending 行收口为 failed 并清掉半成品索引。

    返回处理条数。行若已被删除则忽略；不会自动重建索引（需要用户重新上传/触发）。
    """
    import sqlite3

    try:
        rows = store.list_all_source_rows_pending()
    except Exception:
        return 0
    count = 0
    for row in rows:
        sid = row.get("id")
        pid = row.get("task_id")
        try:
            store.delete_source_index(pid, sid)
        except Exception:
            pass
        try:
            store.update_source_status(
                sid, parse_status="failed", index_status="failed",
                parse_error="索引在进程重启时被中断，请删除后重新上传，或等待下次处理")
        except Exception:
            pass
        count += 1
    return count


def index_source_async(project_id: str, source: dict[str, Any]) -> None:
    """webapp 上传后后台异步索引（无 Worker 系统：asyncio.create_task + to_thread）。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        index_source_file(project_id, source)
        return

    async def _run() -> None:
        await asyncio.to_thread(index_source_file, project_id, source)

    try:
        asyncio.create_task(_run())
    except RuntimeError:  # pragma: no cover
        index_source_file(project_id, source)
