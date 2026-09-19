"""把一轮 Run 的过程事件实时渲染成终端输出。

事件来源
--------
`AgentRuntime.run_turn(..., stream_events_cb=cb)` 会把**面向用户**的事件推给 `cb`，
签名统一为 `cb(channel, payload)`：

  channel="activity"        payload = 公开活动事件（tool.started / tool.finished /
                            phase.started / run.* 等，metadata 里带 activities 快照）
  channel="assistant_delta" payload = {"type": "assistant.delta",
                            "metadata": {"delta": "增量文本"}}（终稿正文的流式分片）
  channel="control"         payload = 控制事件（run.started / run.completed /
                            run.failed / assistant.reply / assistant.reset 等）
  channel="tool"            payload = {"name": ..., "args": ...}（上下文整理等
                            非工具调用的过程提示）

渲染原则
--------
1. **只渲染公开信息**：活动用 `label`（已经是「正在检查相关文件」这类人话），
   正文用解码后的 `delta`，绝不打印原始 JSON 分片或推理文本；
2. **不重复正文**：流式分片已经展示过正文，最终答复只在与分片不一致时补打；
3. 活动行按 `activity_id` 去重、按快照增量渲染，重复节流事件不会刷屏。
"""

from __future__ import annotations

import sys

from cli import theme

#: 会写入结果但不值得单独占一行的活动类别（与 runtime 的 QUIET 语义对齐）
_LOW_KEY_TYPES = {"phase.started", "phase.completed"}


class TurnRenderer:
    """单轮渲染器：一个 run_turn 对应一个实例，不要跨轮复用。"""

    def __init__(
        self,
        stream=None,
        *,
        show_activity: bool = True,
        show_phases: bool = False,
        show_other: bool = False,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.show_activity = show_activity
        self.show_phases = show_phases
        self.show_other = show_other

        self.run_id: str | None = None
        self.terminal_state: str | None = None
        self.error_message: str = ""
        self.assistant_reply: str = ""
        self.assistant_kind: str = ""

        self.activity_lines: list[str] = []
        self.failed_activities: list[str] = []
        self.other_events: list[tuple[str, dict]] = []

        self._rows: dict[str, str] = {}
        self._deltas: list[str] = []
        self._stream_started = False
        self._at_line_start = True
        self._verification: object = None

    # ── 对外接口 ────────────────────────────────────────────────
    def on_event(self, channel: str, payload: object) -> None:
        """stream_events_cb 直接接这里；任何异常都不应打断主流程。"""
        if not isinstance(payload, dict):
            return
        try:
            if channel == "tool":
                self._on_tool(payload)
                return
            meta = payload.get("metadata")
            meta = meta if isinstance(meta, dict) else {}
            etype = str(payload.get("type") or "")
            if channel == "assistant_delta":
                self._on_delta(str(meta.get("delta") or ""))
            elif channel == "activity":
                self._on_activity(etype, payload, meta)
            elif channel == "control":
                self._on_control(etype, payload, meta)
            else:
                self._on_other(channel, payload)
        except Exception:
            # 渲染失败绝不能影响 Agent 执行
            return

    def streamed_text(self) -> str:
        """本轮已流式展示的正文（用于和最终答复比对，避免重复打印）。"""
        return "".join(self._deltas)

    def finish(self) -> None:
        """收尾：给流式正文补一个换行。"""
        if self._stream_started:
            self._write("\n")
            self._stream_started = False
        self._flush()

    # ── 内部渲染 ────────────────────────────────────────────────
    def _on_tool(self, payload: dict) -> None:
        name = str(payload.get("name") or "").strip() or "工具"
        args = str(payload.get("args") or "").strip()
        line = f"{name}：{args}" if args else name
        self.activity_lines.append(line)
        if self.show_activity:
            self._newline_if_needed()
            self._write(theme.dim(f"  ⚙ {line}") + "\n")

    def _on_activity(self, etype: str, payload: dict, meta: dict) -> None:
        if etype in _LOW_KEY_TYPES and not self.show_phases:
            return
        if etype in _LOW_KEY_TYPES:
            phase = str(meta.get("phase") or "")
            if phase:
                self._newline_if_needed()
                self._write(theme.dim(f"  · {etype.split('.')[0]}：{phase}") + "\n")
            return
        verification = meta.get("verification")
        if verification and verification != self._verification:
            self._verification = verification
        self._render_activity_rows(meta.get("activities"))

    def _render_activity_rows(self, rows: object) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            if not label:
                continue
            rid = str(row.get("activity_id") or label)
            status = str(row.get("status") or "")
            previous = self._rows.get(rid)
            if previous is None:
                self._rows[rid] = status
                self.activity_lines.append(label)
                if self.show_activity:
                    self._newline_if_needed()
                    self._write(theme.dim(f"  ⚙ {label}") + "\n")
            elif previous != status:
                self._rows[rid] = status
                if status == "failed":
                    self.failed_activities.append(label)
                    self._newline_if_needed()
                    self._write(theme.err(f"  ✗ {label}") + "\n")

    def _on_delta(self, text: str) -> None:
        if not text:
            return
        if not self._stream_started:
            self._newline_if_needed()
            self._write("\n" + theme.head("助手 >") + "\n")
            self._stream_started = True
            self._at_line_start = True
        self._deltas.append(text)
        self._write(text)
        self._flush()

    def _on_control(self, etype: str, payload: dict, meta: dict) -> None:
        if etype == "run.started":
            self.run_id = str(payload.get("run_id") or self.run_id or "") or None
        elif etype == "run.completed":
            self.terminal_state = "completed"
        elif etype == "run.failed":
            self.terminal_state = "failed"
            self.error_message = str(meta.get("message") or payload.get("message") or "")
            self._newline_if_needed()
            self._write(theme.err(f"  ✗ 运行失败：{self.error_message}") + "\n")
        elif etype == "run.waiting_approval":
            self.terminal_state = "waiting_approval"
        elif etype == "run.waiting_for_user":
            self.terminal_state = "waiting_user"
        elif etype == "run.cancelled":
            self.terminal_state = "cancelled"
        elif etype == "assistant.reply":
            self.assistant_reply = str(meta.get("content") or "")
            self.assistant_kind = str(meta.get("kind") or "")
        elif etype == "assistant.reset":
            self._newline_if_needed()
            self._write(theme.warn("  ↺ 本轮输出已被重置（安全闸拦截后重试）") + "\n")

    def _on_other(self, channel: str, payload: dict) -> None:
        self.other_events.append((channel, payload))
        if self.show_other:
            self._newline_if_needed()
            self._write(theme.dim(f"  · [{channel}] {str(payload)[:120]}") + "\n")

    # ── 输出原语 ────────────────────────────────────────────────
    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
        except Exception:
            return
        if text:
            self._at_line_start = text.endswith("\n")

    def _newline_if_needed(self) -> None:
        if not self._at_line_start:
            self._write("\n")

    def _flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:
            pass
