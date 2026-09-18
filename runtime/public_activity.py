"""Public projection of existing run/tool facts. Never accepts raw model deltas.

Persisted envelopes use the existing task_events table; transport remains the
existing run SSE session. Tool arguments and observations never leave this layer.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone

from runtime.completion import VERIFY_TOOLS, WRITE_TOOLS, verification_outcome_of

SECRET = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|"
    r"authorization|cookie)\b\s*[\"']?\s*[:=]\s*[^\n,;]+|"
    r"Bearer\s+\S+|\b(?:sk-|tvly-|sk-ant-)[A-Za-z0-9_-]{8,}|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
INTERNAL = re.compile(
    r"(?i)<\s*/?\s*(?:think|thinking|analysis|reasoning|scratchpad|thought)\b|"
    r"我(?:先|首先|现在认为|觉得|需要先|接下来准备)|让我先|根据刚才的工具结果|"
    r"候选方案|逐步推理|chain.of.thought|let me (?:think|reason)|"
    r"I (?:need to|should|will first)|Traceback \(most recent call last\)"
)


def redact(text: object) -> str:
    return SECRET.sub("[已隐藏]", str(text or ""))


def public_text(text: object) -> str:
    """Final fallback protection. Structured execution output is never input here."""
    value = str(text or "")
    value = re.sub(r"(?is)<(think|thinking|analysis|reasoning|scratchpad|thought)\b[^>]*>.*?(?:</\1\s*>|$)", "", value)
    # Strip markdown code fences / raw JSON wrappers so a non-JSON final answer
    # doesn't carry ``` blocks into the public bubble.
    lines = value.splitlines()
    cleaned = [l for l in lines if not re.match(r"^\s*```", l)]
    return redact("\n".join(l for l in cleaned if not INTERNAL.search(l))).strip()


def public_summary(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        return None
    if INTERNAL.search(value) or SECRET.search(value) or len(re.findall(r"[。！？.!?]", value)) > 2:
        return None
    # Speculation and lists are not confirmed findings.
    if re.search(r"可能|也许|大概|假设|首先|其次|```|\n[-*\d]", value):
        return None
    return value.strip()


class ToolActivityPresenter:
    READ = {"read_workspace_file", "read_code_file", "read_note", "read_office_file", "read_spreadsheet", "read_file"}
    SEARCH = {"search_documents", "list_workspace_files", "list_code_files", "search_files", "grep", "web_search"}
    QUIET = {"get_current_datetime", "index_workspace", "recall_memory", "sandbox_snapshot", "list_sandbox_snapshots"}

    @classmethod
    def category(cls, name: str) -> str:
        if name in VERIFY_TOOLS or name in {"tests", "run_tests"}:
            return "verification"
        if name in WRITE_TOOLS or name in {"write_file"}:
            return "editing"
        if name in cls.READ or name in cls.SEARCH:
            return "checking"
        if name in {"run_command", "exec_command"}:
            return "executing"
        return "executing"

    @classmethod
    def running(cls, name: str) -> str:
        if name == "web_search":
            return "正在搜索资料"
        if name in cls.SEARCH:
            return "正在搜索相关代码和文件"
        return {"checking": "正在检查相关文件", "editing": "正在修改文件",
                "verification": "正在运行测试", "executing": "正在执行操作"}[cls.category(name)]

    @staticmethod
    def failed(result: object) -> bool:
        if isinstance(result, dict):
            return result.get("success") is False or result.get("isError") is True or bool(result.get("error")) or result.get("exit_code", 0) != 0
        head = str(result or "")[:1000]
        return bool(re.search(r"(?:^|\n)(?:错误[:：]|Error:|运行失败|循环执行出错)|❌|已拒绝|未开启|运行超时|退出码\s*[:：]\s*-?[1-9]\d*", head))

    @staticmethod
    def file_key(args: dict) -> str | None:
        value = args.get("path") or args.get("filename") or args.get("file")
        return str(args.get("project") or "") + "/" + str(value) if value else None


class RunActivityProjector:
    def __init__(self, run_id, manager, sink=None, *, throttle=0.25):
        self.run_id, self.manager, self.sink = run_id, manager, sink
        self.throttle = throttle
        self.rows: list[dict] = []
        self.calls: dict[str, dict] = {}
        self.files: set[str] = set()
        self.seen: set[tuple] = set()
        self.last_publish = 0.0
        self.pending = None
        self.timer = None
        self.phase = None
        self.retry = False
        self.waiting = False
        self.has_work = False
        self.verification = None
        self.closed = False
        self.secrets: set[str] = set()

    def collect_secrets(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r"(?i)api.?key|token|password|secret|authorization|cookie", str(key)) and isinstance(item, str) and len(item) >= 3:
                    self.secrets.add(item)
                else:
                    self.collect_secrets(item)
        elif isinstance(value, list):
            for item in value:
                self.collect_secrets(item)

    def clean(self, value):
        text = public_text(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[已隐藏]")
        return text

    def emit(self, kind, label="", *, status="running", channel="activity", metadata=None, row_id=None, force=True):
        if self.closed:
            return None
        event = {"event_id": uuid.uuid4().hex, "run_id": self.run_id,
                 "type": kind, "timestamp": datetime.now(timezone.utc).isoformat(),
                 "visibility": "public", "channel": channel, "status": status,
                 "label": redact(label), "metadata": metadata or {}}
        if channel == "activity" and label:
            rid = row_id or event["event_id"]
            event["activity_id"] = rid
            row = {"activity_id": rid, "type": kind, "label": event["label"], "status": status}
            existing = next((r for r in self.rows if r["activity_id"] == rid), None)
            if existing is not None:
                existing.update(row)
            else:
                self.rows.append(row)
            self.rows = self.rows[-40:]
        event["metadata"] = {**event["metadata"], "activities": [dict(r) for r in self.rows],
                             "changed_count": len(self.files), "verification": self.verification,
                             "waiting_for_user": self.waiting}
        # Persist all actual transitions, while batching browser updates for fast tools.
        if self.manager is not None:
            self.manager.add_event(self.run_id, "public." + kind, event)
        if not force and time.monotonic() - self.last_publish < self.throttle:
            self.pending = event
            if self.timer is None:
                try:
                    self.timer = asyncio.get_running_loop().call_later(self.throttle, self.flush)
                except RuntimeError:
                    self.flush()
        else:
            self.flush()
            self._send(event)
        return event

    def _send(self, event):
        self.last_publish = time.monotonic()
        if self.sink:
            self.sink(event["channel"], event)

    def flush(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.pending:
            event, self.pending = self.pending, None
            self._send(event)

    def set_phase(self, phase):
        if phase == self.phase:
            return
        if self.phase:
            self.emit("phase.completed", status="completed", metadata={"phase": self.phase}, force=False)
        self.phase = phase
        self.emit("phase.started", metadata={"phase": phase}, force=False)

    def tool_started(self, name, args, call_id=None):
        self.collect_secrets(args)
        cid = call_id or uuid.uuid4().hex
        if cid in self.calls:
            return cid
        category = ToolActivityPresenter.category(name)
        quiet = name in ToolActivityPresenter.QUIET
        self.has_work = self.has_work or not quiet
        if self.waiting:
            return cid  # Pending approval owns the public state until a resumed Run.
        self.set_phase("修正" if self.retry and category == "editing" else {"checking": "检查", "editing": "执行", "verification": "验证", "executing": "执行"}[category])
        last = self.rows[-1] if self.rows else {}
        group = last.get("activity_id") if last.get("category") == category and last.get("status") != "failed" else uuid.uuid4().hex
        self.calls[cid] = {"name": name, "args": args, "group": group, "quiet": quiet, "done": False, "category": category}
        if quiet:
            return cid
        label = "正在修正" if self.retry and category == "editing" else ToolActivityPresenter.running(name)
        self.emit("tool.started", label, row_id=group, force=False)
        self.rows[-1]["category"] = category
        if category == "verification" and name != "code_loop":
            self.verify_started()
        return cid

    def tool_finished(self, cid, result=None, error=False):
        call = self.calls.get(cid)
        if not call or call["done"]:
            return
        call["done"] = True
        failed = error or ToolActivityPresenter.failed(result)
        call["failed"] = failed
        if call["quiet"] or self.waiting:
            return
        name, cat, group = call["name"], call["category"], call["group"]
        if cat == "verification" and name != "code_loop":
            self.verify_finished(result, failed)
        related = [c for c in self.calls.values() if c["group"] == group]
        completed = [c for c in related if c["done"] and not c.get("failed")]
        active = any(not c["done"] for c in related)
        keys = {ToolActivityPresenter.file_key(c["args"]) for c in completed if c["name"] in ToolActivityPresenter.READ or c["category"] == "editing"} - {None}
        if cat == "editing" and not failed:
            key = ToolActivityPresenter.file_key(call["args"])
            if key:
                self.files.add(key)
                self.emit("files.changed", metadata={"changed_count": len(self.files)}, force=False)
        label = {"checking": f"已检查 {len(keys)} 个文件" if keys else f"已完成 {len(completed)} 次查找",
                 "editing": f"已更新 {len(keys)} 个文件" if keys else "文件操作已完成",
                 "verification": "验证执行完成", "executing": "操作执行完成"}[cat]
        if failed:
            label = "操作未完成" if cat != "verification" else "测试未通过"
        elif active:
            label = ToolActivityPresenter.running(name)
        self.emit("tool.failed" if failed else "tool.completed", label,
                  status="failed" if failed else "running" if active else "completed", row_id=group, force=failed)
        row = next(r for r in self.rows if r["activity_id"] == group)
        row["category"] = cat

    def verify_started(self):
        self.set_phase("验证")
        self.verification = "running"
        self._verify_id = uuid.uuid4().hex
        self.emit("verification.started", "正在重新验证" if self.retry else "正在运行测试", row_id=self._verify_id)

    def verify_finished(self, result, failed=False):
        outcome = verification_outcome_of({"status": "executed", "output": str(result)})
        if isinstance(result, dict) and result.get("exit_code") == 0:
            outcome = "success"
        success = not failed and outcome == "success"
        self.verification = "passed" if success else "failed"
        self.retry = not success
        label = "测试通过" if success else "测试未通过" if failed or outcome == "failed" else "验证未获得通过结果"
        count = re.search(r"\b(\d+) passed\b", str(result)) if success else None
        if count:
            label = f"{count[1]} 项测试通过"
        self.emit("verification.completed" if success else "verification.failed", label,
                  status="completed" if success else "failed", row_id=getattr(self, "_verify_id", None))

    def fixing(self):
        self.retry = True
        self.set_phase("修正")
        self.emit("status.update", "正在修正", row_id="fix")

    def wait(self, label="操作已暂停，需要你的确认"):
        self.waiting = True
        for row in self.rows:
            if row["status"] == "running":
                row["status"] = "waiting_for_user"
        self.emit("status.update", label, status="waiting_for_user", row_id="waiting")
        self.emit("run.waiting_for_user", status="waiting_for_user", channel="control")

    def summary(self, value):
        label = public_summary(value)
        if label and ("summary", label) not in self.seen and self.has_work:
            self.seen.add(("summary", label))
            self.emit("status.update", label)

    def generating(self):
        if self.has_work:
            self.emit("response.generating", "正在生成结果……", row_id="generating")

    def delta(self, value):
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[已隐藏]")
        self.rows = [r for r in self.rows if r["activity_id"] != "generating"]
        self.emit("assistant.delta", channel="assistant_delta", metadata={"delta": value})

    def finish(self, state):
        if self.closed:
            return
        if self.waiting and state == "completed":
            # The existing Runtime completes a clarification turn; the task still
            # needs user information. Preserve that lifecycle, project the wait.
            self.emit("run.completed", status="waiting_for_user", channel="control")
            self.flush()
            self.closed = True
            return
        status = "completed" if state == "completed" else "cancelled" if state == "cancelled" else "failed"
        for row in self.rows:
            if row["status"] == "running":
                row["status"] = "interrupted"
        if self.phase:
            self.emit("phase.completed", status=status, metadata={"phase": self.phase})
        self.rows = [r for r in self.rows if r["activity_id"] != "generating"]
        if self.has_work or state != "completed":
            self.emit("run." + state, {"completed": "已完成", "failed": "执行遇到问题，未能完成", "cancelled": "已停止"}.get(state, "已暂停"), status=status, row_id="terminal")
        self.emit("run." + state, status=status, channel="control")
        self.flush()
        self.closed = True


def current_activity():
    from runtime.runctx import current
    ctx = current()
    return getattr(ctx, "activity", None) if ctx else None


def record_fact(kind, **payload):
    activity = current_activity()
    if activity:
        if kind == "verification.started":
            activity.verify_started()
        elif kind == "verification.result":
            activity.verify_finished(payload.get("result"), payload.get("failed", False))
        elif kind == "fix.started":
            activity.fixing()


def public_approval(row):
    name = str(row.get("tool_name") or row.get("tool") or "")
    label = "需要确认：" + ("删除操作" if re.search(r"delete|remove|forget|drop", name) else ToolActivityPresenter.running(name).removeprefix("正在"))
    args = row.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    target = args.get("path") or args.get("filename") if isinstance(args, dict) else None
    if target and not SECRET.search(str(target)):
        label += " · " + public_text(str(target).replace("\\", "/").split("/")[-1])[:80]
    return {"id": row.get("id"), "task_id": row.get("task_id"), "label": label,
            "status": row.get("status", "pending"), "created_at": row.get("created_at")}


def stored_public_events(manager, run_id):
    # Use the same event store, without the legacy 200-event truncation.
    with manager._connect() as conn:
        rows = conn.execute("SELECT payload_json FROM task_events WHERE task_id=? AND event_type LIKE 'public.%' ORDER BY id", (run_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def public_snapshot(manager, run_id):
    events = stored_public_events(manager, run_id)
    return events[-1].get("metadata", {}) if events else {"activities": [], "changed_count": 0, "verification": None}


def public_wire(run_id, name, payload):
    """Single fail-closed browser boundary, shared by live and terminal replay."""
    if name in {"activity", "assistant_delta", "control"}:
        if payload.get("visibility") != "public" or payload.get("channel") != name or payload.get("run_id") != run_id:
            return None
        return name, payload
    # Compatibility control records never carry provider errors, logs, or args.
    kind = {"done": "runtime.done", "error": "runtime.error", "approval": "approval.required",
            "reply": "assistant.reply"}.get(name, name)
    if kind not in {"runtime.done", "runtime.error", "approval.required", "assistant.reply", "run.started", "run.completed", "run.failed", "run.cancelled", "run.waiting_approval", "run.waiting_for_user", "source.not_ready"}:
        return None
    envelope = {"event_id": uuid.uuid4().hex, "run_id": run_id, "type": kind,
                "timestamp": datetime.now(timezone.utc).isoformat(), "visibility": "public",
                "channel": "control", "status": "waiting_for_user" if "approval" in kind else "failed" if kind.endswith("failed") else "completed" if kind.endswith("completed") else "running",
                "label": "", "metadata": {}}
    if kind == "assistant.reply":
        envelope["metadata"] = {"content": public_text(payload.get("content")), "kind": payload.get("kind", "answer")}
    elif kind == "approval.required":
        envelope["metadata"] = {"approvals": payload.get("approvals", [])}
    elif kind == "runtime.error" or kind == "run.failed":
        envelope["label"] = "执行遇到问题，请查看最终结果或重试。"
    elif kind == "source.not_ready":
        envelope["label"] = "部分参考资料尚未就绪，本次未使用。"
    return "control", envelope


class FinalContentStream:
    """Incremental JSON top-level content decoder, with complete-line redaction.

    Only used for a tools-disabled final model call. Other JSON keys, escaped
    partial secrets, reasoning blocks, and raw unstructured text fail closed.
    """
    def __init__(self, emit):
        self.emit = emit
        self.raw = ""
        self.decoded = ""
        self.sent = ""
        self.in_think = False
        self.complete = False

    def feed(self, delta):
        self.raw += delta
        # Locate top-level keys with JSONDecoder, never regex-match nested content.
        raw = self.raw.lstrip()
        if not raw.startswith("{"):
            # Non-JSON fallback: the model is not emitting {"content":…}. Treat the
            # raw stream as a plain public answer, still filtered for reasoning/secret
            # lines by public_text so we never leak internal text. complete stays False
            # so stream_final can decide the final fallback (it has the full RunResult).
            line = public_text(delta)
            if line.startswith(self.sent):
                emit = line[len(self.sent):]
                if emit:
                    self.emit(emit)
                    self.sent = line
            return
        pos = 1
        decoder = json.JSONDecoder()
        while pos < len(raw):
            while pos < len(raw) and raw[pos] in " \r\n\t,":
                pos += 1
            try:
                key, end = decoder.raw_decode(raw, pos)
            except ValueError:
                return
            pos = end
            while pos < len(raw) and raw[pos].isspace():
                pos += 1
            if pos >= len(raw) or raw[pos] != ":":
                return
            pos += 1
            while pos < len(raw) and raw[pos].isspace():
                pos += 1
            if key != "content":
                try:
                    _, pos = decoder.raw_decode(raw, pos)
                except ValueError:
                    return
                continue
            if pos >= len(raw) or raw[pos] != '"':
                return
            start = pos
            pos += 1
            while pos < len(raw):
                if raw[pos] == "\\":
                    pos += 2
                    continue
                if raw[pos] == '"':
                    try:
                        self.decoded = json.loads(raw[start:pos + 1])
                    except ValueError:
                        return
                    self.complete = True
                    self._publish(True)
                    return
                pos += 1
            fragment = raw[start:]
            # Incomplete JSON escapes must not turn into visible fragments.
            for trim in range(min(7, len(fragment))):
                candidate = fragment if trim == 0 else fragment[:-trim]
                try:
                    self.decoded = json.loads(candidate + '"')
                    break
                except ValueError:
                    continue
            self._publish(False)
            return

    def _publish(self, final):
        # Fail-closed + 真打字机折中：
        # - 非 final 帧（JSON 字符串值尚未闭合）：把 decoded 按「完整行」trim 后 emit，
        #   这样行内逐字符 feed 仍能在每个新完整行上增长 emit（逐行打字机），
        #   且半截行（可能被假闭包 json.loads(fragment+'"') 污染）永远不进 sent，
        #   不会破坏后续 startswith(sent) 前缀假设。
        # - final 帧（JSON 值真正闭合）：把剩余（含未完成行）一次性 emit 补完。
        # 行内打字机：如果某一行特别长（超过 40 字符已累积未发），也在该行边界 emit。
        text = self.decoded if final else self.decoded[:self.decoded.rfind("\n") + 1]
        clean = public_text(text)
        if not final and clean:
            clean += "\n"
        if clean.startswith(self.sent):
            delta = clean[len(self.sent):]
            if delta:
                self.emit(delta)
                self.sent = clean
