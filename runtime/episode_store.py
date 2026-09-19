"""Episode 存储层：把每一次 Run 的事实复盘写进 SQLite，并支持按指纹召回。

挂载方式（重要）：
    **完全不动 runner.py**。所有 Run 终态都已经落在 ``task_events`` 的
    ``run.terminal`` 事件里，本模块采用**后置采集**：``ingest_pending()`` 扫描
    尚未采集过的 terminal 事件，回溯 runs / tool_calls / model_calls 聚合成 episode。

这带来三个好处：
    1. 零侵入主链 —— 不碰 Socket律 Approve / FileScope / Completion Gate 任何一条路径；
    2. 天然幂等 —— 同一 run_id 只在 episodes 里出现一次（PRIMARY KEY + INSERT OR IGNORE）；
    3. **可回填历史** —— agent.db 里已躺着 448 条 run.terminal，跑一次 ingest
       就能立刻拿到种子数据，不用等新数据慢慢攒。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from runtime.episode import (
    Episode,
    SCHEMA_VERSION,
    classify_intent,
    digest_goal,
    fingerprint,
    is_informative,
    normalize_outcome,
    similarity,
    tokenize,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    run_id            TEXT PRIMARY KEY,
    fingerprint       TEXT NOT NULL,
    intent            TEXT NOT NULL,
    topic_tokens      TEXT NOT NULL DEFAULT '[]',
    outcome           TEXT NOT NULL,
    terminal_kind     TEXT NOT NULL DEFAULT '',
    tool_sequence     TEXT NOT NULL DEFAULT '[]',
    tool_count        INTEGER NOT NULL DEFAULT 0,
    failed_tools      TEXT NOT NULL DEFAULT '[]',
    rounds            INTEGER NOT NULL DEFAULT 0,
    produced_artifact INTEGER NOT NULL DEFAULT 0,
    error_excerpt     TEXT NOT NULL DEFAULT '',
    goal_digest       TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    schema_version    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_episodes_fp ON episodes(fingerprint, outcome);
CREATE INDEX IF NOT EXISTS idx_episodes_intent ON episodes(intent, created_at);
"""

# 已经 ingest 过的 run_id 不再重复处理（即便 episode 表被清空也能通过游标表得知）
_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS episode_ingest_cursor (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    last_pk    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

TERMINAL_EVENT = "run.terminal"


# ---------------------------------------------------------------- 默认库解析
#
# 为什么要可覆盖：``DEFAULT_DB_PATH`` 是模块级常量（项目根的 agent.db），
# 而评测（benchmark.eval_runner）会用 **临时库** 隔离真实数据。如果不覆盖，
# 记忆层就会去读生产库 —— 这是两个方向上的问题：
#   1. 隔离泄漏：run 写临时库、记忆读生产库，评测结果不可复现；
#   2. 答案泄漏：生产库里的 447 条 episode 大部分就是这批评测题的历史执行，
#      模型一查就能看到"上次怎么做的"，等于把标准答案递回去。
# 所以评测必须先 ``set_default_db_path(临时库)`` 再做种子注入。

_OVERRIDE_DB_PATH: Path | None = None


def set_default_db_path(path: str | Path | None) -> None:
    """覆盖记忆层默认库（评测隔离用）。传 None 可恢复为生产库。"""
    global _OVERRIDE_DB_PATH
    _OVERRIDE_DB_PATH = Path(path) if path is not None else None


def get_default_db_path() -> Path:
    if _OVERRIDE_DB_PATH is not None:
        return _OVERRIDE_DB_PATH
    from runtime.task_manager import DEFAULT_DB_PATH

    return Path(DEFAULT_DB_PATH)


# ---------------------------------------------------------------- 召回期排除
#
# 为什么需要全局排除，而不是只靠 recall(exclude_fingerprints=...)：
#   主动注入（push）时，调用方知道自己是谁，传参排除即可；
#   按需拉取（pull）时，是**模型**拿着任意关键词来查，调用方不在场，
#   没法把"当前是哪道题"告诉它。所以挂一个进程级的排除集合，
#   由评测 runner 在每个 case 开始前设置、结束后清空。
#
# 粒度是"只排除当前这一道题"，不是"排除整个评测集" ——
# 后者会把 447 条种子砍到 112 条，导致典型查询召回为 0，实验直接失去灵敏度。

_RECALL_EXCLUSION: set[str] = set()


def set_recall_exclusion(fingerprints: Iterable[str] | None) -> None:
    """设置召回期全局排除（传 None/空 表示清空）。"""
    global _RECALL_EXCLUSION
    _RECALL_EXCLUSION = set(fingerprints or ())


def get_recall_exclusion() -> set[str]:
    return set(_RECALL_EXCLUSION)


def ensure_tables(conn: sqlite3.Connection) -> None:
    """幂等建表。可被 TaskManager.__init__ 或外部脚本调用。"""
    conn.executescript(_SCHEMA)
    conn.executescript(_CURSOR_SCHEMA)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class EpisodeStore:
    """SQLite DAO。db_path 缺省跟随 ``runtime.task_manager.DEFAULT_DB_PATH``。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_default_db_path()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            ensure_tables(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 写入

    def upsert(self, ep: Episode) -> bool:
        row = ep.to_row()
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        with self._connect() as conn:
            cur = conn.execute(f"INSERT OR IGNORE INTO episodes ({cols}) VALUES ({marks})", row)
            return cur.rowcount > 0

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        r = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return r is not None

    def pending_terminal_rows(self, conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
        """取出尚未采集的终态事件（按 task_events.id 游标推进）。"""
        if not (self._table_exists(conn, "task_events") and self._table_exists(conn, "runs")):
            return []
        try:
            last = conn.execute(
                "SELECT last_pk FROM episode_ingest_cursor WHERE id=1"
            ).fetchone()
        except Exception:
            last = None
        last_pk = int(last["last_pk"]) if last else 0
        return list(
            conn.execute(
                "SELECT te.id AS pk, te.task_id AS run_id, te.payload_json, te.created_at "
                "FROM task_events te WHERE te.event_type=? AND te.id > ? "
                "ORDER BY te.id ASC LIMIT ?",
                (TERMINAL_EVENT, last_pk, limit),
            )
        )

    def ingest_pending(self, limit: int = 1000) -> dict[str, int]:
        """把未采集的终态 Run 变成 episode。返回统计，供 CLI/日志打印。

        任何单条失败都被吞掉（by design）：记忆层是**增益项**，绝不能因为它
        出错而阻断主流程 —— 这与本项目"清理路径绝不能抛异常"的一贯纪律一致。
        """
        stats = {"scanned": 0, "inserted": 0, "skipped": 0, "failed": 0}
        max_pk = 0
        with self._connect() as conn:
            rows = self.pending_terminal_rows(conn, limit=limit)
            stats["scanned"] = len(rows)
            for r in rows:
                pk = int(r["pk"])
                max_pk = max(max_pk, pk)
                try:
                    ep = self._build_episode(conn, r)
                    if ep is None:
                        stats["skipped"] += 1
                        continue
                    if self._insert(conn, ep):
                        stats["inserted"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    stats["failed"] += 1
            if rows:
                conn.execute(
                    "INSERT INTO episode_ingest_cursor (id, last_pk, updated_at) VALUES (1,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET last_pk=excluded.last_pk, "
                    "updated_at=excluded.updated_at",
                    (max_pk, _now_iso()),
                )
        return stats

    def _insert(self, conn: sqlite3.Connection, ep: Episode) -> bool:
        row = ep.to_row()
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        cur = conn.execute(f"INSERT OR IGNORE INTO episodes ({cols}) VALUES ({marks})", row)
        return cur.rowcount > 0

    # ------------------------------------------------------------ 种子注入

    def copy_from(
        self,
        source: "EpisodeStore",
        *,
        exclude_fingerprints: Iterable[str] = (),
    ) -> int:
        """把另一个库的 episode 复制进来，可按指纹排除。

        评测用的入口：把生产库积累的经验**搬到**隔离的临时库，
        同时排除评测题自身的指纹（否则等于给模型看标准答案）。

        返回实际写入条数。源库不存在 episodes 表时返回 0（静默）。
        """
        from runtime.episode import Episode as _Ep  # 局部导入，避免循环依赖

        skip = set(exclude_fingerprints or ())
        try:
            with source._connect() as sconn:
                if not self._table_exists(sconn, "episodes"):
                    return 0
                rows = list(sconn.execute("SELECT * FROM episodes"))
        except Exception:
            return 0

        written = 0
        with self._connect() as conn:
            for r in rows:
                try:
                    ep = _Ep.from_row(r)
                except Exception:
                    continue
                if skip and ep.fingerprint in skip:
                    continue
                if self._insert(conn, ep):
                    written += 1
        return written

    # ------------------------------------------------------------ 聚合

    def _build_episode(self, conn: sqlite3.Connection, ev_row: sqlite3.Row) -> Episode | None:
        """从一个 run.terminal 事件回溯出完整 episode。"""
        run_id = ev_row["run_id"]
        payload = _json_load(ev_row["payload_json"])
        kind = str(payload.get("kind") or "")
        state = str(payload.get("state") or "")

        goal = ""
        created_at = ev_row["created_at"] or ""
        try:
            r = conn.execute("SELECT goal, created_at FROM runs WHERE id=?", (run_id,)).fetchone()
            if r is not None:
                goal = r["goal"] or ""
                created_at = created_at or (r["created_at"] or "")
        except Exception:
            pass

        # 没有 goal 的 run 无法归类，直接跳过（避免污染指纹空间）
        if not goal.strip():
            return None

        tools: list[str] = []
        failed: list[str] = []
        try:
            ts = conn.execute(
                "SELECT tool_name, status FROM tool_calls WHERE task_id=? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
            for t in ts:
                name = str(t["tool_name"] or "")
                if name and name not in tools:
                    tools.append(name)
                if name and str(t["status"] or "").lower() in ("failed", "error", "denied"):
                    if name not in failed:
                        failed.append(name)
        except Exception:
            pass

        rounds = 0
        try:
            rr = conn.execute(
                "SELECT MAX(turn_number) AS m FROM model_calls WHERE task_id=?", (run_id,)
            ).fetchone()
            if rr is not None and rr["m"] is not None:
                rounds = int(rr["m"])
        except Exception:
            pass

        produced = False
        try:
            ar = conn.execute(
                "SELECT 1 FROM artifacts WHERE task_id=? LIMIT 1", (run_id,)
            ).fetchone()
            produced = ar is not None
        except Exception:
            pass

        outcome = normalize_outcome(kind, state)

        # model_calls 有时落不上（provider_error 提前终断时 turn_number 没写库），
        # 此时用工具调用次数兜底估轮数，避免"明明干了 4 步却显示 0 轮"。
        effective_rounds = rounds or len(tools)

        return Episode(
            run_id=run_id,
            fingerprint=fingerprint(goal),
            intent=classify_intent(goal),
            topic_tokens=tokenize(goal)[:12],
            outcome=outcome,
            terminal_kind=kind,
            tool_sequence=tools[:12],
            tool_count=len(tools),
            failed_tools=failed[:8],
            rounds=effective_rounds,
            produced_artifact=produced,
            error_excerpt=str(payload.get("error") or "")[:300],
            goal_digest=digest_goal(goal),
            created_at=created_at or _now_iso(),
        )

    # ------------------------------------------------------------ 召回

    def recall(
        self,
        goal: str,
        *,
        limit: int = 5,
        min_sim: float = 0.25,
        exclude_fingerprints: "set[str] | None" = None,
    ) -> list[tuple[Episode, float]]:
        """按请求文本召回最相近的历史 episode，返回 [(episode, similarity)]，按相似度降序。

        exclude_fingerprints：留一法（leave-one-out）排除。
        评测时必须传当前 case 自身的指纹 —— 否则同一道题会召回它上一轮的执行记录，
        等于把标准答案递回去，Behavior Pass Rate 的提升是假的。
        """
        if not (goal or "").strip():
            return []
        intent = classify_intent(goal)
        toks = tokenize(goal)
        fp = fingerprint(goal)
        # 默认**不**排除自身指纹：生产场景下用户重复问同一件事，
        # 召回它自己的历史恰恰是最有价值的。排除只在留一法评测时显式开启。
        skip_fps = set(exclude_fingerprints or ()) | _RECALL_EXCLUSION

        scored: list[tuple[Episode, float]] = []
        with self._connect() as conn:
            # 不做 intent 硬过滤：实验证明硬过滤会让大量相关样本漏召回
            # （问"读 README 并总结"落在 doc 桶，而真实的读文件 episode 全在 file 桶）。
            # 改为全池排序 + 同 intent 加权；447 条规模下全表扫描仍是毫秒级。
            rows = list(
                conn.execute("SELECT * FROM episodes ORDER BY created_at DESC LIMIT 600")
            )
            for r in rows:
                ep = Episode.from_row(r)
                if ep.fingerprint in skip_fps:
                    continue  # 留一法：排除题目自身的既往执行
                if not is_informative(ep):
                    continue  # 丢弃零信息量的寒暄样本
                sim = similarity(toks, ep.topic_tokens, same_intent=(ep.intent == intent))
                if ep.fingerprint == fp:
                    sim = max(sim, 0.95)  # 完全同指纹：置顶
                if sim >= min_sim:
                    scored.append((ep, round(sim, 3)))

        # 去重：同一个请求历史上跑过多次时，只保留最有代表性的一条
        # （相似度优先，其次取最近）。否则注入窗口会被同一条记录刷满。
        scored = _dedupe(scored)

        # 同 outcome 过度集中时做一次截断：成功样本最多占 2/3，
        # 保证"失败教训"不会被成功样本淹没 —— 教训往往比成功路径更有指导价值。
        scored = _balance_outcomes(scored, limit)
        scored.sort(key=lambda x: (-x[1], x[0].created_at))
        return scored[:limit]

    # ------------------------------------------------------------ 运维

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]
            by_outcome = {
                r["outcome"]: r["c"]
                for r in conn.execute("SELECT outcome, COUNT(*) c FROM episodes GROUP BY 1")
            }
            by_intent = {
                r["intent"]: r["c"]
                for r in conn.execute(
                    "SELECT intent, COUNT(*) c FROM episodes GROUP BY 1 ORDER BY 2 DESC"
                )
            }
        return {
            "total": total,
            "by_outcome": by_outcome,
            "by_intent": by_intent,
            "schema_version": SCHEMA_VERSION,
            "db_path": str(self.db_path),
        }

    def clear(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM episodes")
            conn.execute("DELETE FROM episode_ingest_cursor")
            return cur.rowcount

    def reindex(self, limit: int = 100_000) -> dict[str, int]:
        """全量重建：清表 → 从原始事件重新聚合。

        什么时候需要：``episode.py`` 的分词规则 / 意图规则改过之后，
        库存的老 episode 仍是按旧规则算出的 topic_tokens 与 fingerprint，
        此时单纯 ingest 不会生效（run_id 已存在会被 IGNORE 跳过）。

        之所以必须从原始事件而非 episodes 表重建：episode **刻意不保存用户请求原文**，
        因此无法离线重算指纹 —— 这是一开始就接受的代价，换来的是注入安全性。
        """
        self.clear()
        return self.ingest_pending(limit=limit)


def _balance_outcomes(
    scored: list[tuple[Episode, float]], limit: int
) -> list[tuple[Episode, float]]:
    """限制成功样本占比，避免失败教训被淹没。

    真实场景里成功远多于失败（本地库 268 成功 / 90 失败），纯按相似度排会
    让"这条路走得通"刷满注入窗口，而真正该学的"上次在这里栽过"永远排不进来。
    """
    if not scored:
        return scored
    max_success = max(2, (limit * 2) // 3)
    success_idx = [i for i, (ep, _) in enumerate(scored) if ep.outcome == "completed"]
    if len(success_idx) <= max_success:
        return scored
    drop = set(success_idx[max_success:])
    return [pair for i, pair in enumerate(scored) if i not in drop]


def _dedupe(scored: list[tuple[Episode, float]]) -> list[tuple[Episode, float]]:
    """同 fingerprint 且同 outcome 只留一条（相似度优先，其次最近）。"""
    best: dict[tuple[str, str], int] = {}
    keep: list[int] = []
    for i, (ep, sim) in enumerate(scored):
        key = (ep.fingerprint, ep.outcome)
        j = best.get(key)
        if j is None:
            best[key] = i
            keep.append(i)
            continue
        prev_sim = scored[j][1]
        newer = ep.created_at > scored[j][0].created_at
        if sim > prev_sim or (sim == prev_sim and newer):
            keep[keep.index(j)] = i
            best[key] = i
    return [scored[i] for i in keep]


def _json_load(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}
