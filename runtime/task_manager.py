"""TaskManager：Task/Run 的唯一权威来源（agent.db SQLite）。

领域映射（Project → Task → Message → Run → Event）落地方式（v2 schema）：
- `projects`：工作环境容器（id/name/root_path）。
- `tasks`：**持续存在的 Task 容器**（一件事可多轮：标题/摘要/会话底键/归档时间）。
- `messages`：Task 内的对话消息（role=user/assistant/system；不存工具日志）。
- `runs`：单轮执行记录（由 v1 的 `tasks` 表迁移而来，行 id 即旧 task id，
  保留 session_id/goal/state/budget/usage/error 等原列，新增 task_id(容器) 与 run_index）。
- 子表 task_events/checkpoints/approvals/artifacts/model_calls/tool_calls 全部挂在
  **run id**（列名仍叫 task_id，语义为 run_id）下；schedules/memories 不变。

兼容策略：
- 方法名（create_task/get_task/list_tasks/transition/mark_success...）语义 = 操作一个 Run，
  返回 Task 对象（Run DTO），旧调用方与测试不变；
- 容器由 get_or_create_container / add_message / list_messages / list_containers 等新方法操作；
- 迁移：检测到 v1 旧 `tasks` 表（含 goal 列）且无 `runs` 表 → ALTER RENAME + 按 session_id
  归并创建容器并回填 task_id/run_index；PRAGMA user_version 置 1。不丢任何历史行。

PRAGMA：WAL / synchronous=NORMAL / busy_timeout。上层不直接写 SQL——
本类之外任何代码都不允许改 runs.state / tasks.*。
"""

import json
import logging
import math
import os
import sqlite3
import time
import uuid
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from runtime.errors import AgentError
from runtime_paths import PROJECT_ROOT, state_db_path
from runtime.state_machine import RESUMABLE_FROM, assert_transition
from runtime.task import RunBudget, Task, TaskEvent, TaskState, TaskUsage, utcnow_iso

_logger = logging.getLogger(__name__)

# 审计 D3：结构化日志代理（opt-in，FORGE_STRUCTURED_LOG=1）。
from runtime.structured_log import slog

DEFAULT_DB_PATH = state_db_path(
    "agent.db", env_vars=("FORGE_AGENT_DB", "FORGE_DB_PATH"),
    legacy_path=PROJECT_ROOT / "agent.db",
)
FORGE_DATA_DIR = Path(os.getenv("FORGE_DATA_DIR") or PROJECT_ROOT / "forge_data")


def project_sources_dir(container_id: str) -> Path:
    return FORGE_DATA_DIR / "projects" / container_id / "sources"


def project_attachments_dir(container_id: str) -> Path:
    """临时消息附件的托管目录，位于该容器 FileScope 的项目数据根下。"""
    return FORGE_DATA_DIR / "projects" / container_id / "attachments"

_SCHEMA_V2 = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

-- ===== v2：Project / Task(容器) / Message =====
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(name);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_thread_session ON tasks(session_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_thread_project ON tasks(project_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id);

-- ===== Runs：v1 tasks 的物理承接（同名列语义不变） =====
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    run_index INTEGER,
    parent_task_id TEXT,
    agent_name TEXT NOT NULL,
    goal TEXT NOT NULL,
    state TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    usage_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_container ON runs(task_id, run_index);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, id);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_key TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    actor TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id, status);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    session_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    schedule_expr TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    session_id TEXT,
    next_run TEXT,
    last_run TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled, next_run);

CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    status TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER,
    cost_usd REAL,
    pricing_version TEXT,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_calls_task ON model_calls(task_id, turn_number);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    result_excerpt TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name);

-- 客户端幂等台账：同一容器 + client_message_id 唯一，重复提交返回既有 run
CREATE TABLE IF NOT EXISTS client_messages (
    container_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (container_id, client_message_id)
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL DEFAULT 'user',
    scope_id TEXT NOT NULL DEFAULT 'personal',
    memory_type TEXT NOT NULL DEFAULT 'semantic',
    key TEXT,
    value_text TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    importance REAL NOT NULL DEFAULT 0.7,
    source_type TEXT NOT NULL DEFAULT 'agent-tool',
    source_id TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);

-- 幂等台账：schedule_id + 计划触发时间 唯一，杜绝重启后重复执行
CREATE TABLE IF NOT EXISTS schedule_runs (
    schedule_id TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, fire_at)
);

-- P1-4：provider 尝试记录跨进程持久化（进程崩溃后 resume 仍保有审计链）
CREATE TABLE IF NOT EXISTS provider_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT,
    latency_ms INTEGER,
    error TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_attempts_task ON provider_attempts(task_id);

-- Session DB is a separate durability domain. Keep cleanup intent here until
-- the SDK database confirms the corresponding operation.
CREATE TABLE IF NOT EXISTS session_cleanup_queue (
    container_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('clear','delete')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_cleanup_updated ON session_cleanup_queue(updated_at);

-- Compact Run-level latency samples survive router log rotation.
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT PRIMARY KEY,
    container_id TEXT,
    state TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    sample_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_metrics_sample ON run_metrics(sample_at);
"""

_SCHEMA_V1_LEGACY_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_task_id TEXT,
    agent_name TEXT NOT NULL,
    goal TEXT NOT NULL,
    state TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    usage_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _column_names(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_db(conn: sqlite3.Connection) -> None:
    """把 v1 agent.db（tasks=单轮运行）升级到 v2（runs + 持续 Task 容器）。

    - 旧表 rename 后，行 id 不变 → 历史 task/approval/artifact 关联全部保留；
    - 每个旧行按 session_id 归并进新容器，runs.task_id/runs.run_index 回填；
    - PRAGMA user_version: 0 → 1（幂等：已迁移库直接跳过）。
    """
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        version = 0
    if int(version) >= 1:
        return
    legacy_tasks = _table_exists(conn, "tasks") and "goal" in _column_names(conn, "tasks")
    if legacy_tasks and not _table_exists(conn, "runs"):
        conn.execute("ALTER TABLE tasks RENAME TO runs")
        conn.execute("DROP INDEX IF EXISTS idx_tasks_session")
        conn.execute("DROP INDEX IF EXISTS idx_tasks_state")
    if _table_exists(conn, "runs"):
        _ensure_columns(conn, "runs", {"task_id": "TEXT", "run_index": "INTEGER"})
    conn.execute("PRAGMA user_version = 1")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3（Project 产品模型）：
    1) 旧 projects（=工作位置/文件夹占位）→ work_locations；
    2) tasks 容器表加项目语义列 instructions / memory_scope / work_location_id；
    3) 新表 project_sources / project_memories；forge_data 源目录。
    存量容器默认 memory_scope='global'（保留旧行为），新项目默认 'project_only'。
    """
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except Exception:
        version = 0
    if version >= 3:
        return
    _ensure_columns(conn, "tasks", {
        "instructions": "TEXT NOT NULL DEFAULT ''",
        "memory_scope": "TEXT NOT NULL DEFAULT 'project_only'",
        "work_location_id": "TEXT",
    })
    if not _table_exists(conn, "work_locations"):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS work_locations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                local_path TEXT,
                permission_profile TEXT NOT NULL DEFAULT 'read-write',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            )"""
        )
    if _table_exists(conn, "projects"):
        rows = conn.execute("SELECT * FROM projects").fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO work_locations (id, name, local_path, permission_profile, created_at, updated_at, last_used_at) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (row["id"], row["name"] or "未命名工作位置", row["root_path"], "read-write",
                 row["created_at"], row["created_at"]),
            )
    if not _table_exists(conn, "project_sources"):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS project_sources (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                source_type TEXT NOT NULL DEFAULT 'file',
                parse_status TEXT NOT NULL DEFAULT 'ok',
                index_status TEXT NOT NULL DEFAULT 'none',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_sources_task ON project_sources(task_id)")
    if not _table_exists(conn, "project_memories"):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS project_memories (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                text TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_memories_task ON project_memories(task_id, updated_at)")
    if not _table_exists(conn, "message_attachments"):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS message_attachments (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                message_id TEXT,
                run_id TEXT,
                display_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                attachment_scope TEXT NOT NULL DEFAULT 'message_only',
                promoted_to_source_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_task ON message_attachments(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_message ON message_attachments(message_id)")
    # 存量容器：旧行为用全局记忆 → global；仅有少量容器是新创建 → project_only 也可接受
    conn.execute("UPDATE tasks SET memory_scope = 'global' WHERE memory_scope = 'project_only' AND session_id NOT LIKE 'proj-%'")
    conn.execute("PRAGMA user_version = 3")


def _repair_multi_active_runs(conn: sqlite3.Connection, active_states: tuple[str, ...]) -> int:
    """把「同一容器下多个活跃 Run」收敛为一个，返回被收敛的行数。

    B4（P1-5，2026-09-22）：这不是新加的业务规则，而是**修历史漏洞留下的脏数据**。
    旧谓词漏掉 ``waiting_user``，于是「一个 waiting_user + 一个新 Run」可以共存在
    同一容器里。唯一索引一旦把 waiting_user 纳入，CREATE 会因重复键直接失败
    → TaskManager 构造抛异常 → 整个服务起不来。所以必须先消冲突再建索引。

    收敛策略：**保留最新的那个 Run**（它是用户此刻真正在等的那一个），其余置为
    ``cancelled``。选 cancelled 而不是 failed 的理由：这些 Run 既没有执行失败，也
    不是用户主动取消，而是被同容器里更新的 Run 取代了 —— 从「还能不能继续」的
    角度它与 cancelled 同义（不再可恢复），而 failed 会污染失败率统计。
    每一次收敛都留一条 ``run.superseded`` 事件，说明是迁移修复而非真实业务动作。
    """
    if not active_states or not _table_exists(conn, "runs"):
        return 0
    ph = ",".join("?" for _ in active_states)
    dupes = conn.execute(
        f"SELECT task_id FROM runs WHERE task_id IS NOT NULL AND state IN ({ph}) "
        "GROUP BY task_id HAVING COUNT(*) > 1",
        active_states,
    ).fetchall()
    if not dupes:
        return 0
    now = utcnow_iso()
    repaired = 0
    for row in dupes:
        container_id = row["task_id"]
        keeper = conn.execute(
            f"SELECT id FROM runs WHERE task_id = ? AND state IN ({ph}) "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (container_id, *active_states),
        ).fetchone()
        if keeper is None:  # pragma: no cover - 与上面的查询互斥
            continue
        stale = conn.execute(
            f"SELECT id, state FROM runs WHERE task_id = ? AND state IN ({ph}) AND id <> ?",
            (container_id, *active_states, keeper["id"]),
        ).fetchall()
        for s in stale:
            conn.execute(
                "UPDATE runs SET state = 'cancelled', updated_at = ?, completed_at = ? "
                "WHERE id = ? AND state = ?",
                (now, now, s["id"], s["state"]),
            )
            conn.execute(
                "UPDATE approvals SET status = 'expired', decided_at = ? "
                "WHERE task_id = ? AND status = 'pending'",
                (now, s["id"]),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?)",
                (s["id"], "run.superseded", json.dumps({
                    "run_id": s["id"],
                    "container_id": container_id,
                    "from": s["state"],
                    "to": "cancelled",
                    "reason": "single_active_run_migration",
                    "kept_run_id": keeper["id"],
                    "note": "同容器只允许一个活跃 Run；本 Run 被更新的 Run 取代"
                            "（B4 索引迁移修复，非业务动作）",
                }, ensure_ascii=False), now),
            )
            repaired += 1
    return repaired


def _backfill_containers(conn: sqlite3.Connection) -> None:
    """runs.task_id IS NULL 的行（含迁移前遗留）→ 按 session_id 归并进容器。"""
    if not _table_exists(conn, "runs"):
        return
    rows = conn.execute(
        "SELECT id, session_id, goal, created_at FROM runs WHERE task_id IS NULL ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    session_counter: dict[str, int] = {}
    for row in rows:
        session_id = row["session_id"] or "personal"
        task_row = conn.execute(
            "SELECT id, title FROM tasks WHERE session_id = ? AND archived_at IS NULL "
            "ORDER BY created_at ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        now = utcnow_iso()
        if task_row is None:
            container_id = "tk_" + uuid.uuid4().hex[:8]
            conn.execute(
                "INSERT INTO tasks (id, project_id, session_id, title, summary, status, created_at, updated_at, archived_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (container_id, None, session_id, "", "", "active", now, now, None),
            )
            container_id_for = container_id
        else:
            container_id_for = task_row["id"]
        session_counter[session_id] = session_counter.get(session_id, 0) + 1
        conn.execute(
            "UPDATE runs SET task_id = ?, run_index = ? WHERE id = ?",
            (container_id_for, session_counter[session_id], row["id"]),
        )
    # 容器标题：取容器内最早一条 run 的 goal 前 60 字（仅当标题为空）
    if rows:
        containers = conn.execute("SELECT id FROM tasks WHERE title = ''").fetchall()
        for c in containers:
            first = conn.execute(
                "SELECT goal FROM runs WHERE task_id = ? ORDER BY run_index ASC, rowid ASC LIMIT 1",
                (c["id"],),
            ).fetchone()
            if first is not None:
                conn.execute(
                    "UPDATE tasks SET title = ?, updated_at = ? WHERE id = ?",
                    ((first["goal"] or "")[:60], utcnow_iso(), c["id"]),
                )


class TaskManager:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        try:
            from sources.schema import ensure_tables as _ensure_sources_tables
            from sources.store import set_db_path as _set_sources_db

            _set_sources_db(self.db_path)
        except Exception:
            _ensure_sources_tables = None  # type: ignore[assignment]
        with self._connect() as conn:
            _migrate_db(conn)  # v1→v2：先把旧 tasks 改名 runs（若有）
            conn.executescript(_SCHEMA_V2)  # 幂等建全部 v2 表
            _ensure_columns(conn, "tasks", {"pinned": "INTEGER NOT NULL DEFAULT 0"})  # 收藏列（老库补加）
            _ensure_columns(conn, "tasks", {"model_pref": "TEXT NOT NULL DEFAULT ''"})  # 模型选择列（老库补加）
            _migrate_v3(conn)  # v3：Project 产品模型（work_locations/project_sources/项目记忆/说明列）
            _ensure_v31_tables(conn)  # v3.1：message_attachments（无条件确保，兼容早先已到 v3 的库）
            # v4：tool_calls.invocation_id（Write-Ahead 副作用台账用）+ client_messages 幂等表
            _ensure_columns(conn, "tool_calls", {"invocation_id": "TEXT"})
            # v5/6: separate approval execution claim from confirmed execution.
            _ensure_columns(conn, "approvals", {"executed": "INTEGER NOT NULL DEFAULT 0"})
            _ensure_columns(conn, "approvals", {
                "execution_claimed": "INTEGER NOT NULL DEFAULT 0"
            })
            # B7（P1-8，2026-09-22）：tool_calls 补 4 列。此前这张表只有
            # (tool_name, arguments, status, result_excerpt)，无法回答「这次调用花了多久 /
            # 属于哪一轮模型 / 参数是否与上次同形 / 结果是否与上次同形」——
            # 也就是 benchmark 与收敛分析真正需要的三个维度：
            #   latency_ms          单次调用耗时（同步工具卡死时长只能靠它发现）
            #   turn_number         产生该调用的模型轮次
            #   normalized_arguments 规范化参数（脱敏后），用于「同形不同值」判定
            #   fingerprint         结果指纹（sha1 前 16 位），用于「换词绕过去重」判定
            _ensure_columns(conn, "tool_calls", {
                "latency_ms": "INTEGER",
                "turn_number": "INTEGER",
                "normalized_arguments": "TEXT",
                "fingerprint": "TEXT",
            })
            _ensure_columns(conn, "model_calls", {
                "cost_usd": "REAL",
                "pricing_version": "TEXT",
                "currency": "TEXT NOT NULL DEFAULT 'USD'",
            })
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_invocation "
                "ON tool_calls(invocation_id) WHERE invocation_id IS NOT NULL"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS client_messages ("
                " container_id TEXT NOT NULL, client_message_id TEXT NOT NULL,"
                " run_id TEXT NOT NULL, created_at TEXT NOT NULL,"
                " PRIMARY KEY (container_id, client_message_id))"
            )
            # P1：同容器单 Active Run 的 DB 级兜底（并发双发也只会有一个成功）
            # B4：谓词由 ACTIVE_RUN_STATES 派生；旧定义（漏 waiting_user）必须先 DROP
            # 再建 —— CREATE ... IF NOT EXISTS 对已存在的索引是 no-op，不会更新谓词。
            # 建索引前先消冲突，否则历史脏数据会让 CREATE 抛异常、整个服务起不来。
            try:
                _repaired = _repair_multi_active_runs(conn, self.ACTIVE_RUN_STATES)
                if _repaired:
                    _logger.warning(
                        "B4 迁移：收敛同容器多活跃 Run %d 条（保留最新，其余置 cancelled）",
                        _repaired)
                    slog.warning("B4 迁移收敛多活跃 Run", repaired=_repaired)
                conn.execute("DROP INDEX IF EXISTS idx_runs_single_active")
                conn.execute(self._single_active_index_sql())
            except Exception:
                # 兜底不能阻止服务启动；但必须留下明确信号 —— 此时 DB 级「单活跃」
                # 约束失效，只剩 find_active_run 的代码级检查。
                _logger.exception("B4：单活跃 Run 唯一索引重建失败，DB 级约束未生效")
            try:
                if _ensure_sources_tables is not None:
                    _ensure_sources_tables(conn)  # Sources RAG 表（无条件确保，幂等）
            except Exception:
                pass
            _backfill_containers(conn)  # 空壳任务在（旧）库连接期间存在性保证后回填
        # A prior process may have stopped while SDK Session cleanup was pending.
        # Retry once on manager startup; failures stay durable in the queue.
        try:
            self.retry_pending_session_cleanups(limit=100)
        except Exception:
            _logger.warning("启动时重试 SDK Session 清理失败", exc_info=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ================= Project（工作环境） =================

    def ensure_default_project(self, name: str = "默认项目") -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects ORDER BY created_at ASC LIMIT 1").fetchone()
            if row is not None:
                return dict(row)
            project_id = "prj_" + uuid.uuid4().hex[:8]
            conn.execute(
                "INSERT INTO projects (id, name, root_path, created_at) VALUES (?,?,?,?)",
                (project_id, name[:120], None, utcnow_iso()),
            )
        return self.get_project(project_id) or {"id": project_id, "name": name, "root_path": None}

    def create_project(self, name: str, root_path: str | None = None) -> dict:
        project_id = "prj_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, root_path, created_at) VALUES (?,?,?,?)",
                (project_id, (name or "未命名项目")[:120], root_path, utcnow_iso()),
            )
        return self.get_project(project_id) or {}

    def get_project(self, project_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    # ================= Task 容器（持续对话的一件事情） =================

    def get_or_create_container(
        self, session_id: str, project_id: str | None = None, title: str | None = None
    ) -> dict:
        """同 session_id（底层上下文键）复用一个未归档的 Task 容器。"""
        session_id = (session_id or "personal").strip() or "personal"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND archived_at IS NULL "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is not None:
                return dict(row)
            container_id = "tk_" + uuid.uuid4().hex[:8]
            now = utcnow_iso()
            conn.execute(
                "INSERT INTO tasks (id, project_id, session_id, title, summary, status, created_at, updated_at, archived_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (container_id, project_id, session_id, (title or "")[:120], "", "active", now, now, None),
            )
        result = self.get_container(container_id)
        assert result is not None
        return result

    def get_container(self, container_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (container_id,)).fetchone()
        return dict(row) if row else None

    def list_containers(
        self, *, session_id: str | None = None, project_id: str | None = None,
        archived: bool | None = False, limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if archived is not None:
            clauses.append("archived_at IS " + ("NOT NULL" if archived else "NULL"))
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def touch_container(self, container_id: str, summary_tail: str | None = None) -> None:
        with self._connect() as conn:
            if summary_tail:
                conn.execute(
                    "UPDATE tasks SET updated_at = ?, summary = ? WHERE id = ?",
                    (utcnow_iso(), str(summary_tail)[:300], container_id),
                )
            else:
                conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (utcnow_iso(), container_id))

    def set_container_title(self, container_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET title = ?, updated_at = ? WHERE id = ?",
                ((title or "").strip()[:120], utcnow_iso(), container_id),
            )

    def archive_container(self, container_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET archived_at = ?, status = 'archived', updated_at = ? WHERE id = ?",
                (utcnow_iso(), utcnow_iso(), container_id),
            )
        return cursor.rowcount > 0

    def set_container_pinned(self, container_id: str, pinned: bool) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, utcnow_iso(), container_id),
            )
        return cursor.rowcount > 0

    def container_stat(self, container_id: str) -> dict:
        """容器聚合：runs/messages 数量、最近一次 run 状态。"""
        with self._connect() as conn:
            runs = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE task_id = ?", (container_id,)
            ).fetchone()
            messages = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE task_id = ?", (container_id,)
            ).fetchone()
            latest = conn.execute(
                "SELECT id, state, created_at, goal FROM runs WHERE task_id = ? ORDER BY run_index DESC, rowid DESC LIMIT 1",
                (container_id,),
            ).fetchone()
        result = {
            "runs": int(runs["n"]) if runs else 0,
            "messages": int(messages["n"]) if messages else 0,
        }
        if latest is not None:
            result["latest_run"] = {"id": latest["id"], "state": latest["state"], "created_at": latest["created_at"],
                                    "goal": (latest["goal"] or "")[:200]}
        return result

    # ================= Message =================

    def add_message(
        self, container_id: str, role: str, content: str, *, run_id: str | None = None, meta: dict | None = None
    ) -> dict:
        if role not in ("user", "assistant", "system"):
            raise AgentError(f"非法消息角色：{role}")
        now = utcnow_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (task_id, run_id, role, content, meta_json, created_at) VALUES (?,?,?,?,?,?)",
                (container_id, run_id, role, str(content)[:200000], json.dumps(meta or {}, ensure_ascii=False), now),
            )
            mid = int(cursor.lastrowid)
        return {"id": mid, "task_id": container_id, "run_id": run_id, "role": role,
                "content": str(content)[:200000], "meta": meta or {}, "created_at": now}

    def list_messages(self, container_id: str, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, run_id, role, content, meta_json, created_at FROM messages "
                "WHERE task_id = ? ORDER BY id ASC LIMIT ?",
                (container_id, max(1, min(int(limit), 2000))),
            ).fetchall()
        result = []
        for row in rows:
            try:
                meta = json.loads(row["meta_json"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            result.append(
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "meta": meta,
                    "created_at": row["created_at"],
                }
            )
        return result

    def clear_container_history(self, container_id: str) -> dict:
        """清空容器的对话历史（消息 + 事件 + 消息附件），返回删除计数。

        为什么保留 run/tool_call 等审计表：那些是「执行发生过什么」的证据，
        清空对话不应该毁掉审计链。要真正重置模型记忆，调用方还需额外清掉
        LLM 侧的上下文会话（`SQLiteSession.clear_session()`）。
        """
        deleted = {"messages": 0, "events": 0, "attachments": 0}
        with self._connect() as conn:
            for table, key in (("message_attachments", "attachments"),
                               ("task_events", "events"),
                               ("messages", "messages")):
                cursor = conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (container_id,))
                deleted[key] = int(cursor.rowcount or 0)
        return deleted

    # ---------- 创建 / 读取（Run 语义；v1 方法名保持兼容） ----------

    def create_task(
        self,
        session_id: str,
        goal: str,
        *,
        agent_name: str = "assistant",
        parent_task_id: str | None = None,
        budget: RunBudget | None = None,
        metadata: dict | None = None,
        thread_id: str | None = None,
    ) -> Task:
        """创建一个 Run（v1 语义里曾叫 Task）。thread_id 给容器归属。"""
        metadata = dict(metadata or {})
        if thread_id:
            metadata.setdefault("container_id", thread_id)
        task = Task(
            id="task_" + uuid.uuid4().hex[:8],
            session_id=session_id,
            goal=goal[:2000],
            agent_name=agent_name,
            parent_task_id=parent_task_id,
            budget=budget or RunBudget(),
            metadata=metadata,
        )
        with self._connect() as conn:
            try:
                if thread_id:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(run_index), 0) AS n FROM runs WHERE task_id = ?", (thread_id,)
                    ).fetchone()
                    run_index = int(row["n"]) + 1 if row else 1
                else:
                    run_index = None
                conn.execute(
                    """INSERT INTO runs
                       (id, session_id, task_id, run_index, parent_task_id, agent_name, goal, state,
                        budget_json, usage_json, metadata_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task.id,
                        task.session_id,
                        thread_id,
                        run_index,
                        task.parent_task_id,
                        task.agent_name,
                        task.goal,
                        task.state.value,
                        # B9（2026-09-22）：此前是 `task.budget.__dict__ if hasattr(..., "__dict__")`，
                        # 而 RunBudget 是 slots=True 的 dataclass（无 __dict__）→ 恒落 "{}"，
                        # budget_json 成了只写不读的死列。改用显式 to_dict()。
                        json.dumps(task.budget.to_dict() if hasattr(task.budget, "to_dict") else {},
                                   ensure_ascii=False),
                        "{}",
                        json.dumps(task.metadata, ensure_ascii=False),
                        task.created_at,
                        task.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc)
                if thread_id and "UNIQUE constraint failed" in msg and "runs.task_id" in msg:
                    raise AgentError(
                        f"容器 {thread_id} 已有正在处理的 Run（同一容器同一时间只允许一个活跃任务："
                        f"请等待完成、取消或先处理待确认操作后再继续）。"
                    ) from None
                raise AgentError(f"创建 Run 失败：{exc}") from None
        self.add_event(task.id, "task.created", {"goal": task.goal[:200], "agent": task.agent_name})
        return task

    def get_task(self, task_id: str) -> Task | None:
        """取一个 Run。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def get_run_container_id(self, run_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row["task_id"] if row else None

    def list_tasks(
        self,
        *,
        session_id: str | None = None,
        state: TaskState | str | None = None,
        limit: int = 50,
        container_id: str | None = None,
    ) -> list[Task]:
        """列 Runs；container_id 过滤 = 某 Task 容器下的一串 Run。"""
        sql = "SELECT * FROM runs"
        clauses: list[str] = []
        params: list = []
        if container_id is not None:
            clauses.append("task_id = ?")
            params.append(container_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value if isinstance(state, TaskState) else state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ---------- Run 语义（容器） ----------

    #: 视为“容器正在处理中”的 Run 状态（同容器新 Run 应被拒绝/排队）
    #:
    #: B4（P1-5，2026-09-22）：补入 ``waiting_user``。
    #: 旧的 4 元组漏了它 —— 而 waiting_user 既不是终态、也不在活跃集，语义上悬空，
    #: 后果是「追问态下再发一条消息」可以并起第二个 Run，两个 Run 共享同一份会话
    #: 历史 ⇒ 消息交错、状态互相覆盖（多标签页 / 双击时真实可复现）。
    #: 它与 waiting_approval 同属「等外部输入」的挂起态，本来就该同等对待。
    #:
    #: **这是唯一来源**：DB 侧的部分唯一索引谓词由本元组派生（见 ``_single_active_index_sql``），
    #: 不再手抄第二份字符串 —— 二者漂移过一次，正是这个 bug 的成因。
    ACTIVE_RUN_STATES: tuple[str, ...] = (
        "submitted", "running", "waiting_approval", "waiting_user", "paused",
    )

    def _single_active_index_sql(self) -> str:
        """由 ``ACTIVE_RUN_STATES`` 派生的部分唯一索引 DDL。"""
        values = ",".join("'%s'" % s for s in self.ACTIVE_RUN_STATES)
        return (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_single_active ON runs(task_id) "
            f"WHERE state IN ({values})"
        )

    def find_active_run(self, container_id: str) -> Task | None:
        """返回同容器内仍在处理的 Run（ACTIVE_RUN_STATES 中的任一状态）。"""
        if not container_id:
            return None
        placeholders = ",".join("?" for _ in self.ACTIVE_RUN_STATES)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM runs WHERE task_id = ? AND state IN ({placeholders}) "
                "ORDER BY created_at DESC LIMIT 1",
                (container_id, *self.ACTIVE_RUN_STATES),
            ).fetchone()
        return self._row_to_task(row) if row else None

    # ---------- 客户端幂等（client_message_id） ----------

    def find_run_by_client_message(self, container_id: str, client_message_id: str) -> str | None:
        """幂等查询：返回该容器下已登记的 client_message_id → run_id（无则 None）。"""
        if not container_id or not client_message_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM client_messages WHERE container_id = ? AND client_message_id = ?",
                (container_id, client_message_id),
            ).fetchone()
        return row["run_id"] if row else None

    def record_client_message(self, container_id: str, client_message_id: str, run_id: str) -> str | None:
        """登记 client_message_id→run 映射；已存在时返回既有 run_id（幂等命中），否则 None。"""
        if not container_id or not client_message_id:
            return None
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT run_id FROM client_messages WHERE container_id = ? AND client_message_id = ?",
                (container_id, client_message_id),
            ).fetchone()
            if existing is not None:
                return existing["run_id"]
            conn.execute(
                "INSERT INTO client_messages (container_id, client_message_id, run_id, created_at) "
                "VALUES (?,?,?,?)",
                (container_id, client_message_id, run_id, utcnow_iso()),
            )
        return None

    # ---------- 工具 Write-Ahead 副作用台账（P0：崩溃一致性） ----------

    def begin_side_effect_tool_call(
        self, *, task_id: str, tool_name: str, arguments: dict,
        invocation_id: str, turn_number: int | None = None,
    ) -> bool:
        """Atomically claim a unique invocation before allowing a side effect.

        False means this invocation id was already claimed. Database errors are
        raised so the runtime can fail closed instead of executing unjournaled.
        """
        if not invocation_id:
            raise AgentError("side-effect invocation requires a durable invocation_id")
        from runtime.audit import redact_text, redact_value

        args_json = json.dumps(redact_value(arguments or {}), ensure_ascii=False)[:8000]
        row_id = "tc_" + uuid.uuid4().hex[:8]
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO tool_calls (id, task_id, tool_name, arguments_json, status, "
                    "invocation_id, turn_number, normalized_arguments, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (row_id, task_id, str(tool_name)[:200], args_json, "pending",
                     invocation_id, turn_number,
                     redact_text(json.dumps(redact_value(arguments or {}), ensure_ascii=False))[:8000],
                     utcnow_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            # Only treat a uniqueness collision as a duplicate. Other constraint
            # failures are durability failures and must stop execution.
            existing = self.find_tool_call_by_invocation(task_id, invocation_id)
            if existing is not None:
                return False
            raise

    def pending_side_effect_rows(self, task_id: str) -> list[dict]:
        """status='pending' 的副作用行（恢复时视为执行结果 UNKNOWN）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, tool_name, invocation_id, arguments_json, created_at FROM tool_calls "
                "WHERE task_id = ? AND status = 'pending' AND invocation_id IS NOT NULL "
                "ORDER BY rowid ASC",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def interrupt_pending_side_effects(self, task_id: str, *, reason: str = "interrupted") -> int:
        """Mark unresolved side effects UNKNOWN at crash/cancel; never claim they were undone."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tool_calls SET status = 'unknown', result_excerpt = ? "
                "WHERE task_id = ? AND status = 'pending' "
                "AND invocation_id IS NOT NULL",
                (("unresolved after " + str(reason))[:1000], task_id),
            )
            return int(cursor.rowcount or 0)

    def update_tool_call_status(
        self, task_id: str, invocation_id: str, *, status: str, result_excerpt: str | None = None
    ) -> bool:
        """CAS the Write-Ahead row from pending to one terminal invocation status."""
        if not invocation_id:
            return False
        if status not in {"executed", "error", "blocked", "cancelled", "interrupted", "unknown"}:
            raise AgentError(f"invalid side-effect invocation status: {status}")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tool_calls SET status = ?, result_excerpt = ? "
                "WHERE task_id = ? AND invocation_id = ? AND status = 'pending'",
                (status, (result_excerpt or "")[:4000], task_id, invocation_id),
            )
            return cursor.rowcount == 1

    def find_tool_call_by_invocation(self, task_id: str, invocation_id: str) -> dict | None:
        if not invocation_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, tool_name, status, invocation_id FROM tool_calls "
                "WHERE task_id = ? AND invocation_id = ?",
                (task_id, invocation_id),
            ).fetchone()
        return dict(row) if row else None

    # ---------- 状态流转（唯一修改处；作用于 Run） ----------

    def transition(
        self,
        task_id: str,
        target: TaskState,
        *,
        reason: str | None = None,
    ) -> Task:
        """Run 状态流转的唯一写入口。

        B1（P0-6a，2026-09-22）：改为 CAS（compare-and-swap）。

        旧实现是「先读 → 校验 → 凭空写 ``state=?``」的两步非原子操作，而且读与写
        用的还是**两条不同连接**：并发的 cancel 与 fail 可以各自通过
        ``assert_transition``，随后后写的一方直接覆盖先写的一方 —— 终态被篡改
        （典型症状：用户取消的 Run 显示成 failed，或 failed 被 completed 覆盖）。

        现在把「取写锁 → 读当前状态 → 校验 → 带旧状态条件写」收进单个
        ``BEGIN IMMEDIATE`` 事务：
        - ``BEGIN IMMEDIATE`` 让并发写在**读之前**就排队，而不是先读一份陈旧快照
          再尝试升级锁（后者在 SQLite 下会 SQLITE_BUSY 或读到过期状态）；
        - ``WHERE id=? AND state=<刚读到的旧状态>`` 让「写」对「读」负责：若状态
          在两者之间被动过，rowcount 就是 0，而不是安静地覆盖掉别人的终态。

        权威状态流转事件与状态更新在同一事务提交，避免出现 state 已变而
        task.<state> 事件缺失。辅助事件仍由 ``add_event`` 独立写入。
        """
        now = utcnow_iso()
        old_state: TaskState
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise AgentError(f"task not found: {task_id}")
            current = self._row_to_task(row)
            old_state = current.state
            assert_transition(old_state, target)

            sets = ["state = ?", "updated_at = ?"]
            params: list = [target.value, now]
            if target == TaskState.RUNNING and current.started_at is None:
                sets.append("started_at = ?")
                params.append(now)
            if target in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
                sets.append("completed_at = ?")
                params.append(now)
            params.extend([task_id, old_state.value])
            cur = conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = ? AND state = ?", params
            )
            if cur.rowcount != 1:
                # 持 IMMEDIATE 写锁期间不可能有其它写者插进来；走到这里说明同一进程内
                # 有嵌套/重入的写（例如在 transition 事务里又调用了 transition）。
                # 宁可显式失败，也不要静默覆盖状态。
                raise AgentError(
                    f"状态流转冲突：{task_id} {old_state.value} → {target.value} "
                    f"未生效（并发修改或事务嵌套）"
                )
            # P2（状态一致性）：进入不可继续终态时，未决审批必须关闭，
            # 否则 failed/cancelled run 会继续显示“等待用户批准”。
            if target in (TaskState.FAILED, TaskState.CANCELLED, TaskState.COMPLETED):
                conn.execute(
                    "UPDATE approvals SET status='expired', decided_at=? "
                    "WHERE task_id=? AND status='pending'",
                    (now, task_id),
                )
            transition_payload = {"from": old_state.value, "reason": reason}
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?)",
                (task_id, f"task.{target.value}",
                 json.dumps(transition_payload, ensure_ascii=False), now),
            )
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def mark_success(self, task_id: str, summary: str = "") -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise AgentError(f"task not found: {task_id}")
        task = self.transition(task_id, TaskState.COMPLETED)
        self.add_event(task_id, "task.result", {"summary": (summary or "")[:500]})
        container_id = self.get_run_container_id(task_id)
        if container_id:
            self.touch_container(container_id, summary_tail=(summary or "")[:300])
        return task

    def mark_failure(self, task_id: str, error: str) -> Task:
        task = self.transition(task_id, TaskState.FAILED)
        with self._connect() as conn:
            conn.execute("UPDATE runs SET error_message = ? WHERE id = ?", (str(error)[:1000], task_id))
        self.add_event(task_id, "task.failed", {"error": str(error)[:400]})
        return self.get_task(task_id) or task

    def update_usage(self, task_id: str, **fields: int | float) -> None:
        allowed = {"turns", "tool_calls", "failures", "input_tokens", "output_tokens", "cost_usd"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        task = self.get_task(task_id)
        if task is None:
            return
        usage = {
            "turns": task.usage.turns,
            "tool_calls": task.usage.tool_calls,
            "failures": task.usage.failures,
            "input_tokens": task.usage.input_tokens,
            "output_tokens": task.usage.output_tokens,
            "cost_usd": task.usage.cost_usd,
        }
        usage.update({k: (float(v) if k == "cost_usd" else int(v))
                      for k, v in updates.items()})
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET usage_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(usage), utcnow_iso(), task_id),
            )

    # ---------- 事件 ----------

    def add_event(self, task_id: str, event_type: str, payload: dict | None = None) -> TaskEvent:
        event = TaskEvent(task_id=task_id, event_type=event_type, payload=payload or {})
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json, created_at) VALUES (?,?,?,?)",
                (task_id, event_type, json.dumps(event.payload, ensure_ascii=False), event.created_at),
            )
        if event_type == "run.terminal":
            self._auto_ingest_episode_memory()
        return event

    def _auto_ingest_episode_memory(self) -> None:
        """Best-effort post-commit episode collection; never changes Run outcome."""
        try:
            from runtime.memory_policy import episode_ingest_enabled

            if not episode_ingest_enabled():
                return
            from runtime.episode_store import EpisodeStore

            raw_limit = os.getenv("EPISODE_INGEST_BATCH_SIZE", "1000")
            try:
                limit = max(1, min(int(raw_limit), 5000))
            except (TypeError, ValueError):
                limit = 1000
            stats = EpisodeStore(self.db_path).ingest_pending(limit=limit)
            if stats.get("failed"):
                logging.getLogger(__name__).warning(
                    "episode auto-ingest had row failures: scanned=%s inserted=%s failed=%s",
                    stats.get("scanned"), stats.get("inserted"), stats.get("failed"),
                )
        except Exception:
            logging.getLogger(__name__).exception("episode auto-ingest failed")

    def add_terminal_event(
        self, task_id: str, expected_state: TaskState, payload: dict
    ) -> TaskEvent:
        """Persist terminal evidence only if it matches the durable Run state.

        The state transition itself already writes ``task.<state>`` atomically.
        This transaction closes the race between reading that state and writing
        the richer terminal evidence snapshot.
        """
        event = TaskEvent(task_id=task_id, event_type="run.terminal", payload=payload)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM runs WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise AgentError(f"task not found: {task_id}")
            if str(row["state"]) != expected_state.value:
                raise AgentError(
                    f"terminal evidence state mismatch: expected={expected_state.value} "
                    f"actual={row['state']}"
                )
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?)",
                (task_id, event.event_type,
                 json.dumps(payload, ensure_ascii=False), event.created_at),
            )
        self._auto_ingest_episode_memory()
        return event

    # ---------- P1-4：provider attempt 跨进程持久化 ----------

    def record_provider_attempt(
        self, task_id: str, kind: str, meta: dict | None = None
    ) -> None:
        """把一条 provider 尝试记录落库（kind=ok/limited/failed；detail_json 存完整 meta）。

        进程崩溃后 resume 仍保有审计链（内存 _ATTEMPTS 之外的兜底）。"""
        meta = meta or {}
        with self._connect() as conn:
            attempt_id = meta.get("attempt_id")
            if attempt_id:
                for row in conn.execute(
                    "SELECT detail_json FROM provider_attempts WHERE task_id = ?",
                    (task_id,),
                ):
                    try:
                        if json.loads(row["detail_json"]).get("attempt_id") == attempt_id:
                            return
                    except (TypeError, json.JSONDecodeError):
                        continue
            conn.execute(
                "INSERT INTO provider_attempts "
                "(task_id, kind, model, latency_ms, error, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    kind,
                    str(meta.get("model") or "")[:120],
                    int(meta.get("latency_ms") or 0),
                    str(meta.get("error") or "")[:500],
                    json.dumps(meta, ensure_ascii=False, default=str)[:4000],
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                ),
            )

    def list_provider_attempts(self, task_id: str, limit: int = 200) -> list[dict]:
        """按时间升序取本 task 的 provider 尝试记录（audit 用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, model, latency_ms, error, detail_json, created_at "
                "FROM provider_attempts WHERE task_id = ? "
                "ORDER BY id ASC LIMIT ?",
                (task_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                detail = json.loads(r["detail_json"])
            except json.JSONDecodeError:
                detail = {}
            out.append({
                "kind": r["kind"],
                "model": r["model"],
                "latency_ms": r["latency_ms"],
                "error": r["error"],
                "created_at": r["created_at"],
                "detail": detail,
            })
        return out

    def list_events(self, task_id: str, limit: int = 200) -> list[TaskEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, event_type, payload_json, created_at FROM task_events "
                "WHERE task_id = ? ORDER BY id ASC LIMIT ?",
                (task_id, max(1, min(int(limit), 2000))),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            events.append(
                TaskEvent(
                    task_id=row["task_id"],
                    event_type=row["event_type"],
                    payload=payload,
                    created_at=row["created_at"],
                )
            )
        return events

    # ---------- Checkpoint / 恢复 ----------

    def write_checkpoint(self, task_id: str, schema_version: int, snapshot: dict) -> None:
        """在 turn 边界写一个检查点（加速点，非唯一真相源）。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints (task_id, schema_version, snapshot_json, created_at) "
                "VALUES (?,?,?,?)",
                (task_id, schema_version, json.dumps(snapshot, ensure_ascii=False), utcnow_iso()),
            )
        self.add_event(task_id, "task.checkpoint", {"schema_version": schema_version})

    def read_latest_checkpoint(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT schema_version, snapshot_json, created_at FROM checkpoints "
                "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(row["snapshot_json"])
        except json.JSONDecodeError:
            snapshot = {}
        return {
            "schema_version": row["schema_version"],
            "snapshot": snapshot,
            "created_at": row["created_at"],
        }

    def count_checkpoints(self, task_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def recover_stale_tasks(
        self,
        *,
        states: tuple[TaskState, ...] = (TaskState.RUNNING, TaskState.SUBMITTED),
        max_age_seconds: int = 900,
        budget_relative: bool = False,
    ) -> list[str]:
        """把超时未推进的 Run 标记为 failed（疑似进程崩溃），并追加恢复事件。

        判定依据是 updated_at 距今超过 max_age_seconds；终态与等待用户/审批的不处理
        （那些本来就该长期挂起，可跨重启继续）。返回被恢复的 run id。

        P2（状态一致性）：默认同时回收崩溃于 create→transition 之间的 stale SUBMITTED；
        正常排队中的 Run 由调用方显式指定 states 过滤（排队时间远小于阈值即不受影响）。

        P1/B9（比例回收阈值）：budget_relative=True 时，阈值 = max(max_age_seconds,
        Run 预算的 2 倍)。复杂任务（如 3000s 预算的 run）用固定 900s 会被误判为崩溃
        而强制 failed；相对阈值让"预算内未推进"成为更合理的判据。调用方
        （auto_recover）显式 opt-in，保持向后兼容默认。
        注：初版写成 min()，方向是反的 —— 对 3000s 预算仍然只给 900s，修复等于
        没生效（详见下方逐行阈值处的注释）。
        """
        import datetime as _dt

        def _row_cutoff(age_seconds: int) -> str:
            return (_dt.datetime.now(_dt.timezone.utc)
                    - _dt.timedelta(seconds=age_seconds)).isoformat(timespec="seconds")

        state_values = tuple(s.value for s in states)
        placeholders = ",".join("?" for _ in state_values)
        if not budget_relative:
            cutoff = _row_cutoff(max_age_seconds)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT id, state FROM runs WHERE state IN ({placeholders}) AND updated_at < ?",
                    (*state_values, cutoff),
                ).fetchall()
        else:
            # P1：逐行阈值 = min(max_age_seconds, 预算的 2 倍)。复杂任务（3000s 预算）
            # 用固定 900s 会被误判崩溃而强制 failed；相对阈值以预算为判据更合理。
            import re as _re
            with self._connect() as conn:
                raw = conn.execute(
                    f"SELECT id, state, goal, updated_at, budget_json FROM runs WHERE state IN ({placeholders})",
                    state_values,
                ).fetchall()
                now = _dt.datetime.now(_dt.timezone.utc)
                rows = []
                for r in raw:
                    # 墙钟预算优先取 Run 持久化的 budget_json（runner 传入的真实预算），
                    # 其次回退到 goal 文本里的「预算N秒」。都没有时用 max_age_seconds。
                    rel_budget = 0
                    try:
                        bj = json.loads(r["budget_json"] or "{}")
                        rel_budget = int(bj.get("max_wall_seconds") or 0)
                    except Exception:
                        rel_budget = 0
                    if rel_budget <= 0:
                        goal = str(r["goal"] or "")
                        m = _re.search(r"预算\s*(\d+)\s*秒", goal)
                        if m:
                            rel_budget = int(m.group(1))
                    if rel_budget <= 0:
                        threshold = max_age_seconds
                    else:
                        # B9 修正（2026-09-22）：这里原本是 min()，方向是反的 ——
                        # 对 3000s 预算的 Run，min(900, 6000) 仍然等于 900，
                        # 与「固定 900s」完全等价，等于修复从未生效（一个 3000s
                        # 预算的 Run 只要 901s 没动 updated_at 就会被误判崩溃并强制
                        # failed，而它本来还有 2000s 可跑）。
                        # 正确判据：Run 合法的「最长无进展时长」上界就是它的墙钟预算
                        # （到点 runner 自己会中止），所以阈值应随预算**放大**，
                        # max_age_seconds 只作为「预算未知」时的兜底下限。
                        # 代价方向是安全的：阈值偏大只会让僵尸 Run 多留一会儿，
                        # 而不是把还活着的长跑任务杀掉。
                        threshold = max(max_age_seconds, rel_budget * 2)
                    try:
                        upd = _dt.datetime.fromisoformat(r["updated_at"])
                        if upd.tzinfo is None:
                            upd = upd.replace(tzinfo=_dt.timezone.utc)
                        age = (now - upd).total_seconds()
                    except Exception:
                        continue
                    if age < threshold:
                        continue
                    rows.append(r)
        recovered: list[str] = []
        for row in rows:
            task_id = row["id"]
            try:
                interrupted = self.interrupt_pending_side_effects(task_id, reason="interrupted")
                if interrupted:
                    self.add_event(task_id, "tool.side_effect_unknown", {
                        "reason": "进程重启恢复：存在未决副作用行（执行结果 UNKNOWN），不自动重放",
                        "rows": interrupted,
                    })
            except Exception:
                pass
            try:
                self.transition(task_id, TaskState.FAILED, reason="stale/recovery")
            except AgentError:
                # B1 之后 transition 是 CAS：状态在两次调用之间被改（例如另一个进程
                # 已经恢复过这一行）会抛冲突。这种情况本就无需再收口 —— 逐个吞掉，
                # 不能让一行冲突中断整个启动恢复流程。
                continue
            was = str(row["state"])
            self.add_event(task_id, "task.recovered", {"reason": f"进程重启恢复：{was} 超时未推进"})
            recovered.append(task_id)
        return recovered

    # ---------- Approval ----------

    def create_approval(
        self,
        task_id: str,
        tool_name: str,
        args_key: str,
        arguments: dict,
        *,
        status: str = "pending",
        actor: str | None = None,
    ) -> str:
        row_id = "approv_" + uuid.uuid4().hex[:8]
        from runtime.audit import redact_value

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO approvals (id, task_id, tool_name, args_key, arguments_json, status, actor, "
                "reason, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    task_id,
                    tool_name,
                    args_key,
                    json.dumps(redact_value(arguments), ensure_ascii=False)[:4000],
                    status,
                    actor,
                    None,
                    utcnow_iso(),
                    utcnow_iso() if status != "pending" else None,
                ),
            )
        self.add_event(task_id, f"task.approval.{status}", {"approval_id": row_id, "tool": tool_name})
        return row_id

    def find_approval(self, task_id: str, tool_name: str, args_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, tool_name, args_key, status, arguments_json FROM approvals "
                "WHERE task_id = ? AND tool_name = ? AND args_key = ? ORDER BY created_at DESC LIMIT 1",
                (task_id, tool_name, args_key),
            ).fetchone()
        if row is None:
            return None
        try:
            arguments = json.loads(row["arguments_json"])
        except json.JSONDecodeError:
            arguments = {}
        return {
            "id": row["id"],
            "tool_name": row["tool_name"],
            "args_key": row["args_key"],
            "status": row["status"],
            "arguments": arguments,
        }

    def list_pending_approvals(self, task_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, tool_name, arguments_json, created_at, executed "
                "FROM approvals "
                "WHERE task_id = ? AND status = 'pending' ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                arguments = json.loads(row["arguments_json"])
            except json.JSONDecodeError:
                arguments = {}
            result.append(
                {
                    "id": row["id"],
                    "tool_name": row["tool_name"],
                    "arguments": arguments,
                    "created_at": row["created_at"],
                    "executed": bool(row["executed"]),
                }
            )
        return result

    def list_pending_approvals_all(self, limit: int = 100) -> list[dict]:
        """全部待审批行（跨 run；通知中心用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, tool_name, created_at FROM approvals "
                "WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        actor: str = "user",
        reason: str | None = None,
    ) -> dict:
        if decision not in ("approved", "denied"):
            raise AgentError(f"非法审批决定：{decision}")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, tool_name, status FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise AgentError(f"approval not found: {approval_id}")
            if row["status"] != "pending":
                raise AgentError(f"审批 {approval_id} 已处理（{row['status']}）")
            # 原子决策：UPDATE 带 WHERE status='pending'，行数=1 才是本决定生效；
            # 并发 approve/deny 只有一个能成功（另一连接 rowcount=0 → 读侧已非 pending）。
            cursor = conn.execute(
                "UPDATE approvals SET status = ?, actor = ?, reason = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (decision, actor, reason, utcnow_iso(), approval_id),
            )
            if cursor.rowcount != 1:
                raise AgentError(f"审批 {approval_id} 已被并发处理，无法再次决定")
        self.add_event(
            row["task_id"],
            f"task.approval.{decision}",
            {"approval_id": approval_id, "tool": row["tool_name"], "actor": actor, "reason": reason},
        )
        return {"id": approval_id, "status": decision}

    def claim_approval_execution(self, approval_id: str) -> bool:
        """Atomically claim an approved invocation before executing it.

        External side effects cannot be made exactly-once by SQLite. Claiming
        before execution prevents silent replay; a crash after the claim is an
        uncertain outcome and requires reconciliation.

        Returns True only for the process that claimed it.
        Returns False if already executed or not approvable.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_id, tool_name FROM approvals WHERE id = ? AND status = 'approved'",
                (approval_id,),
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                "UPDATE approvals SET execution_claimed = 1 "
                "WHERE id = ? AND status = 'approved' AND executed = 0 AND execution_claimed = 0",
                (approval_id,),
            )
            if cursor.rowcount != 1:
                return False
            payload = {"approval_id": approval_id, "tool": row["tool_name"],
                       "execution_claimed": True}
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?)",
                (row["task_id"], "task.approval.execution_claimed",
                 json.dumps(payload, ensure_ascii=False), utcnow_iso()),
            )
            return True

    def mark_approval_executed(self, approval_id: str) -> bool:
        """Record confirmed execution after the wrapper returns successfully."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_id, tool_name FROM approvals WHERE id = ? AND status = 'approved' "
                "AND execution_claimed = 1 AND executed = 0",
                (approval_id,),
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                "UPDATE approvals SET executed = 1 "
                "WHERE id = ? AND status = 'approved' AND execution_claimed = 1 AND executed = 0",
                (approval_id,),
            )
            if cursor.rowcount != 1:
                return False
            payload = {"approval_id": approval_id, "tool": row["tool_name"],
                       "invocation_id": f"approval:{approval_id}",
                       "execution_status": "executed"}
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?)",
                (row["task_id"], "task.approval.execution_committed",
                 json.dumps(payload, ensure_ascii=False), utcnow_iso()),
            )
            return True

    def expire_stale_approvals(
        self,
        task_id: str,
        *,
        max_age_seconds: int | float,
        auto_deny: bool = True,
    ) -> list[dict]:
        """P1：WAITING_APPROVAL TTL 自动拒绝。

        超过 max_age_seconds 仍为 pending 的审批 → 置为 denied（actor='ttl'），
        避免无人处理的审批永久挂起（重启后仍可跨 Run 挂起，但应有时效上限）。
        返回被 TTL 拒绝的审批行。auto_deny=False 时只标记、不改状态（审计用）。
        """
        import datetime as _dt
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(seconds=int(max_age_seconds))).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, tool_name, created_at FROM approvals "
                "WHERE task_id = ? AND status = 'pending' AND created_at < ?",
                (task_id, cutoff),
            ).fetchall()
        expired = []
        for row in rows:
            status = "pending" if not auto_deny else "denied"
            base = {"id": row["id"], "tool_name": row["tool_name"],
                    "created_at": row["created_at"], "expired": True, "status": status,
                    "actor": "ttl" if auto_deny else None}
            if not auto_deny:
                expired.append(base)
                continue
            try:
                self.decide_approval(row["id"], "denied", actor="ttl",
                                     reason=f"审批 TTL {int(max_age_seconds)}s 过期，自动拒绝")
                expired.append(base)
            except AgentError:
                # 并发已决定 → 跳过
                continue
        return expired

    def get_approved_unexecuted(self, task_id: str) -> list[dict]:
        """Phase 38: find approvals that are approved but not yet executed.

        Used on resume to auto-execute pending approved invocations before
        handing control back to the model.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, tool_name, arguments_json, args_key FROM approvals "
                "WHERE task_id = ? AND status = 'approved' AND executed = 0 "
                "AND execution_claimed = 0 "
                "ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                arguments = json.loads(row["arguments_json"])
            except json.JSONDecodeError:
                arguments = {}
            result.append({
                "id": row["id"],
                "tool_name": row["tool_name"],
                "arguments": arguments,
                "args_key": row["args_key"],
            })
        return result

    # ---------- Artifacts ----------

    def register_artifact(
        self,
        *,
        task_id: str | None,
        session_id: str | None,
        name: str,
        kind: str,
        storage_path: str,
        sha256: str,
        size_bytes: int,
        metadata: dict | None = None,
    ) -> dict:
        artifact_id = "art_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO artifacts (id, task_id, session_id, name, kind, storage_path, sha256, "
                "size_bytes, metadata_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    task_id,
                    session_id,
                    name,
                    kind,
                    storage_path,
                    sha256,
                    int(size_bytes),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utcnow_iso(),
                ),
            )
        return self.get_artifact(artifact_id)  # type: ignore[return-value]

    def get_artifact(self, artifact_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, task_id, session_id, name, kind, storage_path, sha256, size_bytes, "
                "metadata_json, created_at FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            meta = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            meta = {}
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "name": row["name"],
            "kind": row["kind"],
            "storage_path": row["storage_path"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "metadata": meta,
            "created_at": row["created_at"],
        }

    def list_artifacts(self, *, task_id: str | None = None, session_id: str | None = None, limit: int = 50) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        sql = "SELECT id, task_id, session_id, name, kind, storage_path, sha256, size_bytes, created_at FROM artifacts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "session_id": row["session_id"],
                "name": row["name"],
                "kind": row["kind"],
                "storage_path": row["storage_path"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ---------- 审计明细（model_calls / tool_calls） ----------

    def insert_model_call(
        self,
        *,
        task_id: str,
        turn_number: int = 0,
        model: str | None = None,
        status: str = "succeeded",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int | None = None,
        cost_usd: float | None = None,
        pricing_version: str | None = None,
        currency: str = "USD",
    ) -> str:
        row_id = "mc_" + uuid.uuid4().hex[:8]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO model_calls (id, task_id, turn_number, model, status, input_tokens, "
                "output_tokens, latency_ms, cost_usd, pricing_version, currency, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row_id, task_id, int(turn_number), model, status, int(input_tokens),
                 int(output_tokens), latency_ms, cost_usd, pricing_version,
                 currency, utcnow_iso()),
            )
        return row_id

    def insert_tool_call(
        self,
        *,
        task_id: str,
        tool_name: str,
        arguments: dict | None = None,
        status: str = "succeeded",
        result_excerpt: str | None = None,
        invocation_id: str | None = None,
        latency_ms: int | None = None,
        turn_number: int | None = None,
        normalized_arguments: str | None = None,
        fingerprint: str | None = None,
    ) -> str:
        from runtime.audit import redact_text, redact_value

        row_id = "tc_" + uuid.uuid4().hex[:8]
        if invocation_id:
            existing = self.find_tool_call_by_invocation(task_id, invocation_id)
            if existing is not None:
                return existing["id"]  # 幂等：同一 invocation 只保留一行
        _args_json = json.dumps(redact_value(arguments or {}), ensure_ascii=False)[:8000]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tool_calls (id, task_id, tool_name, arguments_json, status, "
                "result_excerpt, invocation_id, latency_ms, turn_number, "
                "normalized_arguments, fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row_id, task_id, str(tool_name)[:200], _args_json, status,
                 redact_text(result_excerpt or "")[:1000], invocation_id,
                 int(latency_ms) if latency_ms is not None else None,
                 int(turn_number) if turn_number is not None else None,
                 # B7：normalized_arguments 是独立入口，必须自己走脱敏（同一份敏感
                 # 数据不得因为「换了列」而丢掉防护）。调用方若没给就沿用 arguments。
                 redact_text(normalized_arguments)[:8000] if normalized_arguments
                 else _args_json,
                 fingerprint, utcnow_iso()),
            )
        return row_id

    def list_model_calls(self, task_id: str, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, turn_number, model, status, input_tokens, output_tokens, latency_ms, "
                "cost_usd, pricing_version, currency, "
                "created_at FROM model_calls WHERE task_id = ? ORDER BY rowid ASC LIMIT ?",
                (task_id, max(1, min(int(limit), 2000))),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_tool_calls(self, task_id: str, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, tool_name, arguments_json, status, result_excerpt, invocation_id, "
                "latency_ms, turn_number, normalized_arguments, fingerprint, created_at "
                "FROM tool_calls WHERE task_id = ? ORDER BY rowid ASC LIMIT ?",
                (task_id, max(1, min(int(limit), 2000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["arguments"] = json.loads(row["arguments_json"])
            except json.JSONDecodeError:
                item["arguments"] = {}
            result.append(item)
        return result

    def list_approvals(
        self,
        *,
        task_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if state is not None:
            clauses.append("status = ?")
            params.append(state)
        sql = "SELECT id, task_id, tool_name, arguments_json, status, actor, reason, created_at, decided_at " \
              "FROM approvals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            try:
                arguments = json.loads(row["arguments_json"])
            except json.JSONDecodeError:
                arguments = {}
            result.append(
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "tool": row["tool_name"],
                    "arguments": arguments,
                    "status": row["status"],
                    "actor": row["actor"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                    "decided_at": row["decided_at"],
                }
            )
        return result

    # ---------- Schedules（config/tasks.json 的 SQLite 镜像 + 幂等台账） ----------

    def mirror_tasks_json(self, tasks: list[dict]) -> int:
        """把 scheduler 的 config/tasks.json 条目镜像进 schedules 表（按原 id 幂等 upsert）。

        tasks 形如 scheduler.load_tasks() 的返回值。返回本轮 upsert 条数。
        """
        count = 0
        now = utcnow_iso()
        for task in tasks or []:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO schedules (id, name, schedule_expr, prompt, enabled, session_id,
                       next_run, last_run, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                       schedule_expr=excluded.schedule_expr, prompt=excluded.prompt,
                       enabled=excluded.enabled, session_id=excluded.session_id,
                       next_run=excluded.next_run, last_run=excluded.last_run, updated_at=excluded.updated_at""",
                    (
                        task.get("id", ""),
                        str(task.get("name", "") or "")[:120],
                        str(task.get("schedule", "") or "")[:200],
                        str(task.get("prompt", "") or "")[:3000],
                        1 if task.get("enabled", True) else 0,
                        str(task.get("session_id") or "")[:100],
                        task.get("next_run") or None,
                        task.get("last_run") or None,
                        now,
                        now,
                    ),
                )
            count += 1
        return count

    def list_schedules(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, schedule_expr, prompt, enabled, next_run, last_run FROM schedules "
                "ORDER BY enabled DESC, next_run ASC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "schedule": row["schedule_expr"],
                "prompt": row["prompt"],
                "enabled": bool(row["enabled"]),
                "next_run": row["next_run"],
                "last_run": row["last_run"],
            }
            for row in rows
        ]

    def begin_schedule_run(self, schedule_id: str, fire_at: str, task_id: str | None = None) -> bool:
        """登记一次计划触发（幂等键 schedule_id+fire_at）；已存在返回 False（防重复执行）。"""
        now = utcnow_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO schedule_runs (schedule_id, fire_at, task_id, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (schedule_id, str(fire_at), task_id, "running", now, now),
            )
        return cursor.rowcount > 0

    def finalize_schedule_run(self, schedule_id: str, fire_at: str, status: str, task_id: str | None = None, detail: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedule_runs SET status=?, task_id=?, detail=?, updated_at=? "
                "WHERE schedule_id=? AND fire_at=?",
                (status, task_id, detail, utcnow_iso(), schedule_id, str(fire_at)),
            )

    def list_schedule_runs(self, schedule_id: str | None = None, limit: int = 100) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if schedule_id is not None:
            clauses.append("schedule_id = ?")
            params.append(schedule_id)
        sql = "SELECT schedule_id, fire_at, task_id, status, detail, updated_at FROM schedule_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY fire_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "schedule_id": row["schedule_id"],
                "fire_at": row["fire_at"],
                "task_id": row["task_id"],
                "status": row["status"],
                "detail": row["detail"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    # ---------- Memories（SQLite 版 memory.json，结构化列 + 写闸字段） ----------

    def memory_rows(self, *, scope_type: str = "user", scope_id: str = "personal") -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, memory_type, key, value_text, confidence, importance, source_type, "
                "tags_json, created_at, updated_at FROM memories "
                "WHERE scope_type = ? AND scope_id = ? ORDER BY updated_at DESC",
                (scope_type, scope_id),
            ).fetchall()
        entries = []
        for row in rows:
            try:
                tags = json.loads(row["tags_json"])
            except (json.JSONDecodeError, TypeError):
                tags = []
            entries.append(
                {
                    "id": row["id"],
                    "text": row["value_text"],
                    "tags": tags if isinstance(tags, list) else [],
                    "memory_type": row["memory_type"],
                    "confidence": float(row["confidence"]),
                    "importance": float(row["importance"]),
                    "source": row["source_type"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return entries

    def delete_memory_row(self, entry_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
        return cursor.rowcount > 0

    def memory_clear(self, *, scope_type: str = "user", scope_id: str = "personal") -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE scope_type = ? AND scope_id = ?", (scope_type, scope_id)
            )
        return cursor.rowcount

    def memory_upsert_row(
        self,
        entry: dict,
        *,
        scope_type: str = "user",
        scope_id: str = "personal",
        memory_type: str = "semantic",
        confidence: float = 1.0,
        importance: float = 0.7,
        source_type: str = "agent-tool",
        source_id: str | None = None,
        key: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO memories (id, scope_type, scope_id, memory_type, key, value_text,
                   confidence, importance, source_type, source_id, tags_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET value_text=excluded.value_text,
                   memory_type=excluded.memory_type, confidence=excluded.confidence,
                   importance=excluded.importance, tags_json=excluded.tags_json,
                   updated_at=excluded.updated_at""",
                (
                    entry["id"],
                    scope_type,
                    scope_id,
                    memory_type,
                    key,
                    entry["text"],
                    float(confidence),
                    float(importance),
                    source_type,
                    source_id,
                    json.dumps(entry.get("tags", []), ensure_ascii=False),
                    entry.get("created_at") or utcnow_iso(),
                    entry.get("updated_at") or utcnow_iso(),
                ),
            )

    # ---------- 工具 ----------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        def load_json(text: str, default: object) -> object:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return default

        task = Task(
            id=row["id"],
            session_id=row["session_id"],
            goal=row["goal"],
            state=TaskState(row["state"]),
            agent_name=row["agent_name"],
            parent_task_id=row["parent_task_id"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
        usage = load_json(row["usage_json"], {}) or {}
        task.usage = TaskUsage(
            turns=int(usage.get("turns", 0) or 0),
            tool_calls=int(usage.get("tool_calls", 0) or 0),
            failures=int(usage.get("failures", 0) or 0),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
        )
        task.metadata = load_json(row["metadata_json"], {}) or {}
        return task

    @staticmethod
    def is_resumable(task: Task) -> bool:
        return task.state in RESUMABLE_FROM


    # ---------- 搜索 / 回收站（P2） ----------

    def search_containers(self, q: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, summary, updated_at FROM tasks "
                "WHERE archived_at IS NULL AND (title LIKE ? OR summary LIKE ?) "
                "ORDER BY updated_at DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", min(int(limit), 50)),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_runs(self, q: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, goal, state, error_message, created_at FROM runs "
                "WHERE goal LIKE ? OR error_message LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", min(int(limit), 50)),
            ).fetchall()
        return [
            {"id": r["id"], "container_id": r["task_id"], "goal": (r["goal"] or "")[:160],
             "state": r["state"], "error": (r["error_message"] or "")[:160], "created_at": r["created_at"]}
            for r in rows
        ]

    def search_messages(self, q: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, run_id, role, content, created_at FROM messages "
                "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{q}%", min(int(limit), 50)),
            ).fetchall()
        return [
            {"id": r["id"], "container_id": r["task_id"], "role": r["role"],
             "content": (r["content"] or "")[:160], "created_at": r["created_at"]}
            for r in rows
        ]

    def search_artifacts(self, q: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, kind, created_at FROM artifacts WHERE name LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{q}%", min(int(limit), 50)),
            ).fetchall()
        return [dict(r) for r in rows]



    def clear_container_history(self, container_id: str) -> int:
        """清空某容器（Project）的 UI 对话消息并复位 summary（Run/事件/产物保留）。

        Runtime 语义（Phase 5）：只清 agent.db.messages（产品原始记录）。
        模型 Runtime Session（sessions.sqlite）由适配层配对调用
        runtime.context.delete_session_history(session_id) 一并清理。
        """
        with self._connect() as conn:
            row = conn.execute("SELECT session_id FROM tasks WHERE id = ?", (container_id,)).fetchone()
            cur = conn.execute("DELETE FROM messages WHERE task_id = ?", (container_id,))
            conn.execute("UPDATE tasks SET summary = '' WHERE id = ?", (container_id,))
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (utcnow_iso(), container_id))
            if row and row["session_id"]:
                now = utcnow_iso()
                conn.execute(
                    "INSERT INTO session_cleanup_queue(container_id,session_id,operation,created_at,updated_at) "
                    "VALUES(?,?, 'clear', ?, ?) ON CONFLICT(container_id) DO UPDATE SET "
                    "session_id=excluded.session_id,operation='clear',retry_count=0,last_error=NULL,updated_at=excluded.updated_at",
                    (container_id, row["session_id"], now, now),
                )
        self.retry_pending_session_cleanups(container_id=container_id)
        return cur.rowcount or 0

    def delete_container(self, container_id: str) -> bool:
        """硬删除容器及其全部子数据（回收站「清除」用，不可恢复）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, session_id FROM tasks WHERE id = ?", (container_id,)
            ).fetchone()
            if row is None:
                pending = conn.execute(
                    "SELECT 1 FROM session_cleanup_queue WHERE container_id = ?", (container_id,)
                ).fetchone()
                if not pending:
                    return False
                session_id = None
            else:
                session_id = row["session_id"]
                if session_id:
                    now = utcnow_iso()
                    conn.execute(
                        "INSERT INTO session_cleanup_queue(container_id,session_id,operation,created_at,updated_at) "
                        "VALUES(?,?, 'delete', ?, ?) ON CONFLICT(container_id) DO UPDATE SET "
                        "session_id=excluded.session_id,operation='delete',retry_count=0,last_error=NULL,updated_at=excluded.updated_at",
                        (container_id, session_id, now, now),
                    )
            if row is None:
                # A prior deletion left durable cleanup work. Retry below.
                pass
            else:
                run_ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM runs WHERE task_id = ?", (container_id,)).fetchall()]
                ids = run_ids + [container_id]
                ph = ",".join("?" * len(ids))
                for table, col in [("task_events", "task_id"), ("checkpoints", "task_id"),
                                   ("approvals", "task_id"), ("model_calls", "task_id"),
                                   ("tool_calls", "task_id"), ("message_attachments", "task_id")]:
                    conn.execute(f"DELETE FROM {table} WHERE {col} IN ({ph})", ids)
                if run_ids:
                    rph = ",".join("?" * len(run_ids))
                    conn.execute(f"DELETE FROM artifacts WHERE task_id IN ({rph})", run_ids)
                conn.execute("DELETE FROM messages WHERE task_id = ?", (container_id,))
                conn.execute("DELETE FROM project_sources WHERE task_id = ?", (container_id,))
                conn.execute("DELETE FROM project_memories WHERE task_id = ?", (container_id,))
                # Sources RAG tables may not exist in older installations.
                try:
                    chunk_cids = [r["cid"] for r in conn.execute(
                        "SELECT cid FROM source_chunks WHERE project_id = ?", (container_id,)).fetchall()]
                    if chunk_cids:
                        conn.executemany("DELETE FROM source_chunks_fts WHERE rowid = ?", [(c,) for c in chunk_cids])
                    conn.execute("DELETE FROM source_chunk_vectors WHERE project_id = ?", (container_id,))
                    conn.execute("DELETE FROM source_chunks WHERE project_id = ?", (container_id,))
                except Exception:
                    pass
                conn.execute("DELETE FROM runs WHERE task_id = ?", (container_id,))
                conn.execute("DELETE FROM tasks WHERE id = ?", (container_id,))
        if session_id or row is None:
            self.retry_pending_session_cleanups(container_id=container_id)
        # 物理清理项目文件区（attachments/sources 由行删除时已删文件，这里兜底清空目录树）
        try:
            from runtime.task_manager import FORGE_DATA_DIR

            root = FORGE_DATA_DIR / "projects" / container_id
            if root.exists():
                import shutil as _shutil

                _shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass
        return True

    def retry_pending_session_cleanups(self, *, container_id: str | None = None, limit: int = 100) -> dict:
        """Retry durable SDK Session clear/delete intents; failed rows remain inspectable."""
        sql = "SELECT container_id,session_id,operation FROM session_cleanup_queue"
        params: list = []
        if container_id:
            sql += " WHERE container_id = ?"
            params.append(container_id)
        sql += " ORDER BY updated_at LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        succeeded = 0
        for item in rows:
            try:
                sessions_path = state_db_path(
                    "sessions.sqlite", env_vars=("FORGE_SESSIONS_DB",),
                    legacy_path=PROJECT_ROOT / "sessions.sqlite",
                )
                from runtime.context import clear_session_history, delete_session_history
                if item["operation"] == "clear":
                    clear_session_history(item["session_id"], str(sessions_path))
                else:
                    delete_session_history(item["session_id"], str(sessions_path))
                with self._connect() as conn:
                    conn.execute("DELETE FROM session_cleanup_queue WHERE container_id = ?", (item["container_id"],))
                succeeded += 1
            except Exception as exc:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE session_cleanup_queue SET retry_count=retry_count+1,last_error=?,updated_at=? WHERE container_id=?",
                        (f"{type(exc).__name__}: {exc}"[:1000], utcnow_iso(), item["container_id"]),
                    )
                _logger.warning("Session 清理待重试 container=%s session=%s operation=%s",
                                item["container_id"], item["session_id"], item["operation"], exc_info=True)
        return {"attempted": len(rows), "succeeded": succeeded, "pending": len(rows) - succeeded}

    def list_pending_session_cleanups(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT container_id,session_id,operation,retry_count,last_error,created_at,updated_at "
                "FROM session_cleanup_queue ORDER BY updated_at LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_run_latency(self, run_id: str, container_id: str | None, state: str, latency_ms: int) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            run = conn.execute("SELECT started_at,created_at FROM runs WHERE id = ?", (run_id,)).fetchone()
            start_value = (run["started_at"] or run["created_at"]) if run else None
            if start_value:
                try:
                    wall_ms = int((datetime.fromisoformat(now) - datetime.fromisoformat(start_value)).total_seconds() * 1000)
                    # Timestamps are persisted at second precision in legacy rows; for fast
                    # Runs their difference can be 0 even though Runner supplied monotonic
                    # elapsed time. Keep the monotonic sample as the floor, while the wall
                    # clock still captures the full span of a resumed Run.
                    latency_ms = max(int(latency_ms), wall_ms)
                except (TypeError, ValueError):
                    pass
            conn.execute(
                "INSERT INTO run_metrics(run_id,container_id,state,latency_ms,sample_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET container_id=excluded.container_id,state=excluded.state,"
                "latency_ms=excluded.latency_ms,sample_at=excluded.sample_at",
                (run_id, container_id, state, max(0, int(latency_ms)), now),
            )
            # Bounded raw sample retention; latest percentile still survives router log rotation.
            conn.execute("DELETE FROM run_metrics WHERE run_id IN (SELECT run_id FROM run_metrics ORDER BY sample_at DESC LIMIT -1 OFFSET 20000)")

    def run_latency_summary(self, *, limit: int = 20000) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT latency_ms,state FROM run_metrics ORDER BY sample_at DESC LIMIT ?",
                (max(1, min(int(limit), 20000)),),
            ).fetchall()
        values = sorted(int(r["latency_ms"]) for r in rows)
        def percentile(p: float) -> int | None:
            if not values:
                return None
            return values[max(0, math.ceil(p * len(values)) - 1)]
        return {"count": len(values), "p50_ms": percentile(.50), "p95_ms": percentile(.95),
                "average_ms": round(sum(values) / len(values)) if values else None,
                "states": {state: sum(1 for r in rows if r["state"] == state)
                           for state in sorted({r["state"] for r in rows})}}

    def restore_container(self, container_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET archived_at = NULL, status = 'active', updated_at = ? WHERE id = ?",
                (utcnow_iso(), container_id),
            )
        return cursor.rowcount > 0

    # ================= v3：WorkLocation =================

    def list_work_locations(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM work_locations ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def create_work_location(self, name: str, local_path: str | None = None, permission_profile: str = "read-write") -> dict:
        row_id = "wl_" + uuid.uuid4().hex[:8]
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO work_locations (id, name, local_path, permission_profile, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (row_id, (name or "未命名工作位置")[:120], local_path, permission_profile, now, now),
            )
        result = self.get_work_location(row_id)
        assert result is not None
        return result

    def get_work_location(self, wl_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM work_locations WHERE id = ?", (wl_id,)).fetchone()
        return dict(row) if row else None



    def update_project(self, container_id: str, *, name: str | None = None,
                       instructions: str | None = None, memory_scope: str | None = None,
                       work_location_id: str | None = None, detach_work_location: bool = False,
                       model_pref: str | None = None) -> dict | None:
        """Project（容器）编辑：名称/说明/记忆范围/工作位置绑定/模型选择。"""
        sets = ["updated_at = ?"]
        params: list = [utcnow_iso()]
        if name is not None:
            sets.append("title = ?"); params.append((name or "")[:120])
        if instructions is not None:
            sets.append("instructions = ?"); params.append(str(instructions or "")[:8000])
        if memory_scope is not None:
            scope = str(memory_scope or "project_only")
            if scope not in ("project_only", "global"):
                scope = "project_only"
            sets.append("memory_scope = ?"); params.append(scope)
        if detach_work_location:
            sets.append("work_location_id = ?"); params.append(None)
        elif work_location_id is not None:
            sets.append("work_location_id = ?"); params.append(work_location_id)
        if model_pref is not None:
            pref = str(model_pref or "").strip().lower()
            if pref not in ("", "gateway", "local"):
                pref = ""
            sets.append("model_pref = ?"); params.append(pref)
        params.append(container_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_container(container_id)

    # ================= v3：Project Sources =================

    def add_project_source(self, *, task_id: str, display_name: str, stored_path: str,
                           mime_type: str | None = None, size_bytes: int = 0, sha256: str | None = None,
                           parse_status: str = "ok", source_type: str = "file") -> dict:
        row_id = "src_" + uuid.uuid4().hex[:8]
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO project_sources (id, task_id, display_name, stored_path, original_name, mime_type, "
                "size_bytes, sha256, source_type, parse_status, index_status, metadata_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row_id, task_id, str(display_name)[:255], str(stored_path)[:2000], str(display_name)[:255],
                 (mime_type or "")[:120], int(size_bytes), sha256, source_type, parse_status, "none",
                 "{}", now, now),
            )
        return self.get_project_source(row_id) or {}

    def get_project_source(self, source_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM project_sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            out["metadata"] = json.loads(row["metadata_json"])
        except Exception:
            out["metadata"] = {}
        return out

    def list_project_sources(self, task_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM project_sources WHERE task_id = ? ORDER BY created_at ASC",
                                (task_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(row["metadata_json"])
            except Exception:
                item["metadata"] = {}
            out.append(item)
        return out

    def delete_project_source(self, source_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM project_sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                return None
            path = Path(row["stored_path"])
            # 引用它的临时附件（已加入来源）随源删除一并移除，避免悬空引用
            conn.execute("DELETE FROM message_attachments WHERE promoted_to_source_id = ?", (source_id,))
            conn.execute("DELETE FROM project_sources WHERE id = ?", (source_id,))
        try:
            if path.exists():
                path.unlink()
        except (OSError, SystemExit):
            # 物理文件删除属收尾副作用：失败（含运行环境安全护栏抛出的
            # SystemExit）不得冒泡，否则会终止整个 Web 服务进程。
            pass
        return dict(row)

    # ================= v3.1：Message Attachments（临时附件） =================

    def add_message_attachment(self, *, task_id: str, display_name: str, stored_path: str,
                               mime_type: str | None = None, size_bytes: int = 0, sha256: str | None = None,
                               scope: str = "message_only") -> dict:
        row_id = "att_" + uuid.uuid4().hex[:8]
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO message_attachments (id, task_id, message_id, run_id, display_name, stored_path, "
                "mime_type, size_bytes, sha256, attachment_scope, promoted_to_source_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row_id, task_id, None, None, str(display_name)[:255], str(stored_path)[:2000],
                 (mime_type or "")[:120], int(size_bytes), sha256, scope, None, now, now),
            )
        return self.get_message_attachment(row_id) or {}

    def get_message_attachment(self, attachment_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM message_attachments WHERE id = ?", (attachment_id,)).fetchone()
        return dict(row) if row else None

    def bind_message_attachments(self, attachment_ids: list[str], message_id: int,
                                 run_id: str | None = None, *,
                                 task_id: str | None = None) -> int:
        now = utcnow_iso()
        count = 0
        for att_id in attachment_ids or []:
            with self._connect() as conn:
                sql = (
                    "UPDATE message_attachments SET message_id = ?, run_id = ?, updated_at = ? "
                    "WHERE id = ? AND message_id IS NULL"
                )
                params: tuple[Any, ...] = (message_id, run_id, now, att_id)
                if task_id:
                    sql += " AND task_id = ?"
                    params += (task_id,)
                cursor = conn.execute(
                    sql, params,
                )
                count += cursor.rowcount
        return count

    def list_message_attachments(self, message_id: int | None = None, task_id: str | None = None) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if message_id is not None:
            clauses.append("message_id = ?")
            params.append(message_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        sql = "SELECT * FROM message_attachments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def attachments_for_run(self, run_id: str) -> list[dict]:
        """某 Run 触发消息的附件（Context 注入用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.* FROM message_attachments a "
                "JOIN messages m ON a.message_id = m.id WHERE m.run_id = ? ORDER BY a.created_at ASC",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_message_attachment(self, attachment_id: str) -> dict | None:
        """删除附件：仅当它是独立副本（无 promoted source）时同时删除物理文件。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM message_attachments WHERE id = ?", (attachment_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            conn.execute("DELETE FROM message_attachments WHERE id = ?", (attachment_id,))
        stored = row["stored_path"]
        keep_file = bool(row.get("promoted_to_source_id")) or row.get("attachment_scope") == "project_source"
        if not keep_file and stored and Path(stored).exists():
            try:
                Path(stored).unlink()
            except (OSError, SystemExit):
                # 同上：物理文件删除失败不得冒泡终止服务。
                pass
        return row

    def promote_attachment_to_source(self, attachment_id: str) -> dict:
        """临时附件 → 项目来源（move/reference/dedupe，按 sha256 去重，不无脑复制）。

        返回更新后的附件行（scope=project_source，promoted_to_source_id 指向来源）。
        """
        from runtime.task_manager import project_sources_dir

        row = self.get_message_attachment(attachment_id)
        if row is None:
            raise AgentError(f"attachment not found: {attachment_id}")
        task_id = row["task_id"]
        sha = row.get("sha256")
        existing = [s for s in self.list_project_sources(task_id) if s.get("sha256") and s["sha256"] == sha]
        now = utcnow_iso()
        if existing:
            src = existing[0]
            # 去重：引用既有来源，移除独立副本文件
            old_path = row["stored_path"]
            with self._connect() as conn:
                conn.execute(
                    "UPDATE message_attachments SET stored_path = ?, attachment_scope = 'project_source', "
                    "promoted_to_source_id = ?, updated_at = ? WHERE id = ?",
                    (src["stored_path"], src["id"], now, attachment_id),
                )
            if old_path and Path(old_path).exists() and Path(old_path).resolve() != Path(src["stored_path"]).resolve():
                try:
                    Path(old_path).unlink()
                except (OSError, SystemExit):
                    # 同上：去重时清理旧副本失败不得冒泡终止服务。
                    pass
            return self.get_message_attachment(attachment_id) or row
        # 复制入 sources 目录（物理一份在 sources；数据库记录来源关系）
        src_dir = project_sources_dir(task_id)
        src_dir.mkdir(parents=True, exist_ok=True)
        name = Path(row["display_name"]).name
        dest = src_dir / name
        src_path = Path(row["stored_path"])
        if dest.exists() and dest.resolve() != src_path.resolve():
            dest = src_dir / (uuid.uuid4().hex[:6] + "_" + name)
        if src_path.exists() and src_path.resolve() != dest.resolve():
            dest.write_bytes(src_path.read_bytes())
        source = self.add_project_source(task_id=task_id, display_name=name, stored_path=str(dest),
                                         mime_type=row.get("mime_type"), size_bytes=row.get("size_bytes") or 0,
                                         sha256=sha, parse_status="ok")
        with self._connect() as conn:
            conn.execute(
                "UPDATE message_attachments SET stored_path = ?, attachment_scope = 'project_source', "
                "promoted_to_source_id = ?, updated_at = ? WHERE id = ?",
                (str(dest), source["id"], now, attachment_id),
            )
        return self.get_message_attachment(attachment_id) or row

    def create_source_reference_attachments(self, task_id: str, source_ids: list[str]) -> list[dict]:
        """把项目已有 Source 引用为当前（待发送）消息附件：只建引用，不复制不重复索引。"""
        out: list[dict] = []
        for sid in source_ids or []:
            src = self.get_project_source(sid)
            if src is None or src.get("task_id") != task_id:
                continue
            out.append(self.add_message_attachment(
                task_id=task_id, display_name=src["display_name"], stored_path=src["stored_path"],
                mime_type=src.get("mime_type"), size_bytes=src.get("size_bytes") or 0,
                sha256=src.get("sha256"), scope="project_source",
            ))
            with self._connect() as conn:
                conn.execute(
                    "UPDATE message_attachments SET promoted_to_source_id = ? WHERE id = ?",
                    (sid, out[-1]["id"]),
                )
            out[-1] = self.get_message_attachment(out[-1]["id"]) or out[-1]
        return out

    # ================= v3：Project Memory =================

    def add_project_memory(self, task_id: str, text: str, tags: list[str] | None = None) -> dict:
        row_id = "pmem_" + uuid.uuid4().hex[:8]
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO project_memories (id, task_id, text, tags_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (row_id, task_id, str(text)[:4000], json.dumps((tags or [])[:20], ensure_ascii=False), now, now),
            )
        return {"id": row_id, "task_id": task_id, "text": str(text)[:4000], "tags": (tags or [])[:20],
                "created_at": now, "updated_at": now}

    def upsert_project_memory(self, task_id: str, text: str,
                              tags: list[str] | None = None) -> dict:
        """Create or update the exact project-memory copy of a remembered fact."""
        value = str(text)[:4000]
        safe_tags = list(dict.fromkeys(str(tag)[:120] for tag in (tags or [])))[:20]
        tags_json = json.dumps(safe_tags, ensure_ascii=False)
        now = utcnow_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, created_at FROM project_memories "
                "WHERE task_id = ? AND text = ? ORDER BY updated_at DESC LIMIT 1",
                (task_id, value),
            ).fetchone()
            if row:
                row_id = str(row["id"])
                created_at = str(row["created_at"])
                conn.execute(
                    "UPDATE project_memories SET tags_json = ?, updated_at = ? WHERE id = ?",
                    (tags_json, now, row_id),
                )
            else:
                row_id = "pmem_" + uuid.uuid4().hex[:8]
                created_at = now
                conn.execute(
                    "INSERT INTO project_memories "
                    "(id, task_id, text, tags_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (row_id, task_id, value, tags_json, now, now),
                )
        return {"id": row_id, "task_id": task_id, "text": value, "tags": safe_tags,
                "created_at": created_at, "updated_at": now}

    def list_project_memories(self, task_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, text, tags_json, created_at, updated_at FROM project_memories "
                "WHERE task_id = ? ORDER BY updated_at DESC LIMIT ?",
                (task_id, max(1, min(int(limit), 500))),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(row["tags_json"])
            except Exception:
                item["tags"] = []
            out.append(item)
        return out

    def search_project_memories(self, task_id: str, keyword: str, limit: int = 10) -> list[dict]:
        kw = f"%{keyword}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, text, tags_json, created_at, updated_at FROM project_memories "
                "WHERE task_id = ? AND (text LIKE ? OR tags_json LIKE ?) ORDER BY updated_at DESC LIMIT ?",
                (task_id, kw, kw, max(1, min(int(limit), 50))),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(row["tags_json"])
            except Exception:
                item["tags"] = []
            out.append(item)
        return out

    def delete_project_memory(self, memory_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM project_memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    # ================= v3：Artifacts 按 Project =================

    def list_project_artifacts(self, container_id: str, limit: int = 100) -> list[dict]:
        """某 Project 下各 Run 产生的产物（Sources 不在此）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.name, a.kind, a.storage_path, a.sha256, a.size_bytes, a.created_at, r.id AS run_id "
                "FROM artifacts a JOIN runs r ON a.task_id = r.id "
                "WHERE r.task_id = ? ORDER BY a.created_at DESC LIMIT ?",
                (container_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(r) for r in rows]


def _ensure_v31_tables(conn: sqlite3.Connection) -> None:
    """v3.1：message_attachments 无条件确保（兼容 user_version 已=3 的库）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS message_attachments (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            message_id TEXT,
            run_id TEXT,
            display_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            mime_type TEXT,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT,
            attachment_scope TEXT NOT NULL DEFAULT 'message_only',
            promoted_to_source_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_task ON message_attachments(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_message ON message_attachments(message_id)")


def auto_recover(db_path: str | Path | None = None) -> list[str]:
    """启动钩子：把上次进程崩溃遗留的超时 RUNNING 任务标为 failed；返回恢复的 run id。

    同时把崩溃遗留的 Sources 半成品索引（parsing/indexing/pending）收口为 failed
    （不自动重建；避免状态永久卡住与"failed 却仍可检索"的不一致）。

    B9（P2-7，2026-09-22）：显式开启 ``budget_relative=True``。此前这个能力虽然实现
    了，调用点却没开，默认固定 900s 阈值 —— 任何预算大于 450s 的 Run 只要 900s 没
    刷新 updated_at 就会被判定为「崩溃遗留」并强制 failed，而它可能完全健康地在跑
    （例如一次长网页抓取）。相对阈值以 Run 自己的墙钟预算为判据，避免这种误杀。
    """
    recovered = TaskManager(db_path).recover_stale_tasks(budget_relative=True)
    try:
        from sources.indexer import recover_interrupted_indexes

        recover_interrupted_indexes()
    except Exception:
        pass
    return recovered
