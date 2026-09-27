"""按 trace_id（= run_id）聚合导出一次 Run 的完整观测轨迹。

设计原则
--------
- **零改主链路**：run_id 已是 Runtime 全链路唯一事实键（task_events / messages /
  provider_attempts / 审计表全用它），本模块只把它重命名为 trace_id 供消费，
  不动任何表结构与事件写入。
- **两个事实源**：
  1. Runtime 事件表（task_events，结构化事实）
  2. OTel JSONL 轨迹（var/traces/traces.jsonl，observability 埋点）
- 导出 JSON 形如：
  {
    "trace_id": "<run_id>",
    "container_id": "...",
    "state": "completed",
    "spans": [ {otel jsonl record} ... ],
    "events": [ {event_type, payload, created_at} ... ],
    "tool_calls": [ {name, status, invocation_id} ... ],
    "provider_attempts": [ {kind, model, latency_ms, error} ... ],
    "messages": [ {role, content} ... ]
  }

用法
----
    python -m runtime.trace_export <run_id> [--jsonl var/traces/traces.jsonl] [--db <state database>]
    python -m runtime.trace_export <run_id> --stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from runtime.task_manager import DEFAULT_DB_PATH
from runtime_paths import TRACE_DIR


def _load_events(run_id: str, db_path: str) -> list[dict]:
    """从 Runtime 事件表读 run 的所有事件。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT event_type, payload_json, created_at FROM task_events "
        "WHERE task_id = ? ORDER BY id ASC LIMIT 5000",
        (run_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            payload = {}
        out.append({"event_type": r["event_type"], "payload": payload,
                    "created_at": r["created_at"]})
    return out


def _load_container(run_id: str, db_path: str) -> str | None:
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return row["task_id"] if row else None


def _load_provider_attempts(run_id: str, db_path: str) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT kind, model, latency_ms, error, detail_json FROM provider_attempts "
        "WHERE task_id = ? ORDER BY id ASC", (run_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({"kind": r["kind"], "model": r["model"],
                    "latency_ms": r["latency_ms"], "error": r["error"],
                    "detail": r["detail_json"]})
    return out


def _load_messages(container_id: str, run_id: str, db_path: str) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content, meta_json FROM messages "
        "WHERE task_id = ? AND run_id = ? ORDER BY id ASC",
        (container_id, run_id)).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
        except Exception:
            meta = {}
        out.append({"role": r["role"], "content": (r["content"] or "")[:5000],
                    "kind": meta.get("kind", "")})
    return out


def _load_spans(run_id: str, jsonl_path: str | None) -> list[dict]:
    """从 OTel JSONL 按 Run id 过滤 span。

    B8（P1-6，2026-09-22）：过滤键是 ``run_id`` 而不是 ``trace_id``。
    旧实现拿 Run id 去比 SDK 生成的 OTel ``trace_id``，两者从无关联，
    条件恒为假 —— 导出结果里 ``spans`` 永远是空的（"跨层追踪"实际不存在）。
    新记录都带 ``run_id`` 字段；``trace_id`` 作为旧文件的回退，保持向后兼容。
    """
    if not jsonl_path:
        return []
    out = []
    primary = Path(jsonl_path)
    paths = [primary.with_name(primary.name + f".{i}") for i in range(1, 4)] + [primary]
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if str(rec.get("run_id") or rec.get("trace_id") or "") == run_id:
                out.append(rec)
    return out


def export(run_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
           jsonl_path: str | Path | None = TRACE_DIR / "traces.jsonl") -> dict:
    container = _load_container(run_id, db_path) or ""
    return {
        "trace_id": run_id,
        "container_id": container,
        "spans": _load_spans(run_id, jsonl_path),
        "events": _load_events(run_id, db_path),
        "tool_calls": [
            {"name": (e["payload"] or {}).get("tool_name")
                     or (e["payload"] or {}).get("name"),
             "status": (e["payload"] or {}).get("status", "executed"),
             "invocation_id": (e["payload"] or {}).get("invocation_id")}
            for e in _load_events(run_id, db_path)
            if e["event_type"] == "tool.invocation"
        ],
        "provider_attempts": _load_provider_attempts(run_id, db_path),
        "messages": _load_messages(container, run_id, db_path),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trace_export")
    p.add_argument("run_id")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH))
    p.add_argument("--jsonl", default=str(TRACE_DIR / "traces.jsonl"))
    p.add_argument("--stdout", action="store_true")
    p.add_argument("--out", default="")
    args = p.parse_args(argv)

    data = export(args.run_id, db_path=args.db, jsonl_path=args.jsonl or None)
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if args.stdout:
        print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"trace {args.run_id} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
