"""FORGE TUI 主 App（Textual 8.x 专业化版本）。

把 `ChatApp.submit` 的事件流接到 Textual 面板：
- 用户输入 → MessageLog.add_user
- run_turn 过程事件（stream_events_cb）→ 流式 + 工具执行树
- 最终答复 → MessageLog.add_assistant
- 错误 → MessageLog.add_error
- 斜杠命令 → SlashPopup（InputBar 内，输入框正上方）
- 审批 → 明确状态机（Y/N/D 仅在 approval 态生效；普通文本不清除 pending）
- Inspector → Ctrl+O（Esc/Ctrl+O 关闭，focus 回到 InputBar）

入口：
    python main.py --tui

Textual 8.2.8 关键约束（勿违反）：
- Widget.classes 是 frozenset，只能构造时传
- Widget.log 是只读 property，自定义属性不能叫 log
- Log.write() 无 end 参数
- Widget.refresh() 替代旧 _invalidate()
- App.run_worker(coro, exclusive=False) 启动后台 worker
"""

from __future__ import annotations

import time

from rich.text import Text
from textual._context import NoActiveAppError
from textual.app import App, ComposeResult, NoScreen, Screen
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.widgets import Footer, Input, Static, Label
from textual.widget import MountError, Widget

from cli.tui.panels import (
    AGENT,
    ARTIFACT,
    BG,
    BORDER,
    ERROR_C,
    MessageItem,
    MessageLog,
    MUTED,
    PANEL,
    PRIMARY,
    SlashPopup,
    StatusHeader,
    SUCCESS,
    SURFACE,
    TuiPanels,
    TEXT_C,
    WARNING,
)


# ── 斜杠命令静态列表（与 cli/commands.py build_registry 同步）──────
_SLAH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "Show available commands"),
    ("/sessions", "List sessions (recent first)"),
    ("/new", "Create and switch to new session"),
    ("/switch", "Switch session"),
    ("/history", "Show recent messages"),
    ("/last", "Show last assistant reply"),
    ("/compact", "Compact conversation (auto summary)"),
    ("/clear", "Clear current session messages"),
    ("/artifacts", "List artifacts"),
    ("/tools", "Show recent tool calls"),
    ("/diag", "Diagnose latest run"),
    ("/debug", "Toggle debug event printing"),
    ("/mode", "Switch execution mode"),
    ("/exit", "Quit CLI"),
]


# ── InspectorScreen ─────────────────────────────────────────────────
class InspectorScreen(Screen):
    """Ctrl+O 打开的 Inspector：展示 run 诊断信息。

    最小版本：展示 run_id / model / state / elapsed / tokens / cwd / tools / events。
    数据从 ChatApp.diagnose() 结果注入；缺失字段显示 "—"。

    键盘：Esc 或 Ctrl+O 关闭（action_close_inspector），
    关闭后 App 层把 focus 还给 InputBar。
    """

    BINDINGS = [
        Binding("ctrl+o", "close_inspector", "Close"),
        Binding("escape", "close_inspector", "Close"),
    ]

    CSS = """
    InspectorScreen {
        background: #0B0E14;
    }
    .inspector-panel {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        border: solid #293142;
        background: #10141C;
    }
    .inspector-title {
        color: #5CC8FF;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }
    .inspector-row {
        height: 1;
        padding-left: 1;
    }
    """

    def __init__(self, data: dict | None = None) -> None:
        super().__init__()
        self._data = data or {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="inspector-panel"):
            yield Label("◆ FORGE — Inspector", classes="inspector-title")
            yield Static(f"  run_id: {self._data.get('run_id', '—')}", classes="inspector-row")
            yield Static(f"  model:  {self._data.get('model', '—')}", classes="inspector-row")
            yield Static(f"  state:  {self._data.get('state', '—')}", classes="inspector-row")
            yield Static(f"  elapsed:{self._data.get('elapsed', '—')}", classes="inspector-row")
            yield Static(f"  tokens: {self._data.get('tokens', '—')}", classes="inspector-row")
            yield Static(f"  cwd:    {self._data.get('cwd', '—')}", classes="inspector-row")
            yield Static(f"  tools:  {self._data.get('tools', [])}", classes="inspector-row")
            yield Static("  ── 事件时间线（最近 5 条）────────────", classes="inspector-row")
            for ev in (self._data.get("events") or [])[:5]:
                yield Static(f"  · {ev}", classes="inspector-row")
            yield Static("  （详细数据请用 /diag 查看）", classes="inspector-row")

    def on_screen_closed(self) -> None:
        """Screen 关闭时由 App 层回调恢复 focus。"""
        pass

    def action_close_inspector(self) -> None:
        self.app.pop_screen()


# ── ForgeTuiApp ─────────────────────────────────────────────────────
class ForgeTuiApp(App):
    """FORGE 全能助手 TUI 专业化版本。

    事件优先级（Esc 键）：
        1. slash popup open → 关闭 popup
        2. approval waiting → 无操作（不解除 pending）
        3. running          → 发送中断信号
        4. idle             → 无操作
    """

    CSS = f"""
    Screen {{
        background: {BG};
    }}
    TuiPanels {{
        height: 100%;
    }}
    StatusHeader {{
        height: 3;
        padding: 0 1;
        background: {SURFACE};
        border-bottom: solid {BORDER};
    }}
    MessageLog {{
        height: 1fr;
        border: solid {BORDER};
        background: {BG};
    }}
    MessageItem {{
        padding: 0 1;
    }}
    InputBar {{
        /* ⚠️ Textual 的 Vertical 默认 CSS 是 height: 1fr，
           不显式写 auto 会让 InputBar 和 MessageLog 各抢一半屏幕，
           表现为消息区被压扁、输入区下留大片空白。
           popup 显示时自动扩展，隐藏时收缩。 */
        height: auto;
        width: 100%;
    }}
    .forge-input {{
        /* Textual 的 height 是边框盒高度：上下边框各占 1 行，
           所以 1 行内容需要 height: 3（写 1 会被边框吃光，内容区变 0 行，
           placeholder 完全不显示）。 */
        height: 3;
        margin: 0 1;
        background: {PANEL};
        border: solid {BORDER};
        color: {TEXT_C};
    }}
    Footer {{
        /* 覆盖 Footer 默认的 dock: bottom。它已经被 auto 高度的 InputBar
           放到屏幕底部，再 dock 一次会让宽度不扣 margin（溢出 1 列），
           并把最右侧的 binding 挤掉。改为跟随 InputBar 内部流式排列。 */
        dock: none;
        height: 1;
        margin: 0 1;
        color: {MUTED};
    }}
    .forge-hint {{
        /* 上下文提示行（idle/running/approval/slash）。显式 height: 1 ——
           Static 默认 auto，空内容时高度不稳会把 Footer 顶出去。 */
        height: 1;
        margin: 0 1;
        color: {MUTED};
    }}
    SlashPopup {{
        /* 默认隐藏；show() 用 inline style 覆盖成 display: block。
           不隐藏的话启动时会常驻一个空的带边框方块。 */
        display: none;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        margin: 0 1;
        padding: 0 1;
    }}
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("ctrl+o", "show_inspector", "Inspector"),
        # priority=True 覆盖 Screen 默认的 tab → app.focus_next
        Binding("tab", "complete_slash", "Tab complete", priority=True),
    ]

    def __init__(
        self,
        session_name: str = "personal",
        mode: str = "stream",
        debug: bool = False,
        max_turns: int = 20,
        history_limit: int | None = None,
        auto_summary: bool = True,
    ) -> None:
        super().__init__()
        self.session_name = session_name or "personal"
        self.mode = mode
        self._debug = debug
        self.max_turns = max_turns
        self.history_limit = history_limit
        self.auto_summary = auto_summary
        self.turns = 0
        self._app_state: TuiPanels | None = None
        # 上下文提示行（InputBar 内的 Static）。挂载前为 None，
        # on_mount 里接上真实 widget —— 此前该属性从未被创建，
        # 导致 _update_footer() 每次调用都是 no-op（被调用 14 次却什么都不显示）。
        self._hint_label: object | None = None
        self._footer_mode: str = "idle"
        self._chat_app = None
        self._pending_compact = None

        # 流式
        self._stream_item: MessageItem | None = None
        self._stream_buf: str = ""
        self._last_tool_item: MessageItem | None = None

        # Approval 状态机
        #   idle → running → waiting_approval
        #   waiting_approval 下：
        #     y/yes  → approve，回到 running
        #     n/no   → reject，回到 idle
        #     d      → 展开详情，保持 waiting_approval
        #     普通文本 → 记录为 clarification，保持 waiting_approval
        #   只有 Runtime 明确返回 approved/rejected/cancelled 才清除
        self._in_approval: bool = False
        self._pending_approvals: list[dict] = []

        # 输入历史（当前 TUI session）
        self._history: list[str] = []
        self._hist_idx: int = 0

        # Slash popup 状态
        self._popup_visible: bool = False

    # ── 依赖装配 ───────────────────────────────────────────────
    def _get_chat_app(self):
        if self._chat_app is None:
            from cli.app import ChatApp
            self._chat_app = ChatApp(
                session_name=self.session_name,
                mode=self.mode,
                debug=self._debug,
                max_turns=self.max_turns,
                history_limit=self.history_limit,
                auto_summary=self.auto_summary,
            )
        return self._chat_app

    def _msglog(self) -> MessageLog:
        return self._app_state.msglog

    def _header(self) -> StatusHeader:
        return self._app_state.header

    def _bar(self):
        return self._app_state.bar

    def _popup(self) -> SlashPopup | None:
        return self._app_state.bar.popup

    # ── 布局 ──────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield TuiPanels(self.session_name)

    def on_mount(self) -> None:
        self._app_state = self.query_one(TuiPanels)
        # 接上 InputBar 的上下文提示行（挂载后 widget 才存在）
        self._hint_label = self._app_state.bar.hint
        # 同步 header 宽度（默认 120 列宽屏）；size 在挂载前可能为 (0, 0)，安全读取
        width = self._app_state.msglog.size[0] or 120
        self._header().set_width(width)
        self._banner()
        self._update_footer("idle")

    def _banner(self) -> None:
        msglog = self._msglog()
        msglog.add_meta("◆ FORGE · 全能助手 TUI")
        msglog.add_meta(f"  session: {self.session_name}   |   mode: {self.mode}")
        msglog.add_meta("  /help 看命令   |   Ctrl+Q 退出   |   Ctrl+O Inspector")
        msglog.add_meta("")
        self._bar().focus_input()

    # ── 输入：slash / 普通 / approval ─────────────────────────
    def on_input_submitted(self, event) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return

        # 保存历史
        self._history.append(text)
        self._hist_idx = len(self._history)

        if text.startswith("/"):
            # 斜杠命令：审批态下也允许（命令是系统操作，不是审批响应）
            self._handle_command(text)
        elif self._in_approval:
            # 审批态：只有 y/yes/n/no/d/details 是快捷操作
            # 其他任何文本 → 作为 clarification 记录，保持 pending
            self._handle_approval_input(text)
        else:
            self._submit_text(text)

    # ── 斜杠命令 ───────────────────────────────────────────────
    def _handle_command(self, text: str) -> None:
        self._msglog().add_user(f"/{text.lstrip('/')}")
        self._run_async(self._dispatch_cmd(text))

    async def _dispatch_cmd(self, text: str) -> None:
        chat = self._get_chat_app()
        try:
            await chat.dispatch(text)
        except Exception as exc:
            self._msglog().add_error(title="命令执行失败", detail=str(exc))

    # ── 普通消息 ────────────────────────────────────────────────
    def _submit_text(self, text: str) -> None:
        self._msglog().add_user(text)
        self._stream_buf = ""
        self._stream_item = None
        self._last_tool_item = None
        self._header().set_state("running")
        self._update_footer("running")
        self._run_async(self._run_turn(text))

    def _run_async(self, coro) -> None:
        try:
            self.run_worker(coro, exclusive=False)
        except (NoActiveAppError, NoScreen):
            # 无活跃 Textual app（如纯同步测试环境）时静默忽略
            import logging
            logging.getLogger(__name__).debug("run_worker skipped: no active app")

    async def _run_turn(self, text: str) -> None:
        chat = self._get_chat_app()
        self.turns += 1
        started = time.monotonic()

        def tui_event_cb(channel: str, payload) -> None:
            self._on_tui_event(channel, payload)

        try:
            result = await chat.runtime.run_turn(
                text,
                session=chat.session,
                session_id=chat.session_name,
                mode=chat.mode,
                debug=chat.debug,
                max_turns=chat.max_turns,
                history_limit=chat.history_limit,
                metadata={"channel": "tui"},
                raise_on_error=True,
                context_guard=chat.auto_summary,
                stream_events_cb=tui_event_cb,
            )
        except KeyboardInterrupt:
            self._stream_item = None
            self._stream_buf = ""
            self._header().set_state("idle")
            self._msglog().add_error(title="已中断", detail="本地等待已中断，后端 Run 可能仍在执行。")
            self._update_footer("idle")
            return
        except Exception as exc:
            self._header().set_state("failed")
            self._render_exception(exc)
            self._update_footer("idle")
            return

        # 审批：Runtime 明确返回 waiting_approval=True 才进入审批态
        if getattr(result, "waiting_approval", False):
            self._enter_approval(result)
            return

        self._render_result(result, started)

    async def _resolve_approvals(self, chat, result):
        from main import _resolve_approvals_interactive
        return await _resolve_approvals_interactive(
            chat.runtime,
            result,
            session=chat.session,
            mode=chat.mode,
            debug=chat.debug,
            max_turns=chat.max_turns,
            history_limit=chat.history_limit,
        )

    # ── Approval 状态机 ─────────────────────────────────────────
    #
    #   IDLE → RUNNING → WAITING_APPROVAL
    #
    #   WAITING_APPROVAL 下：
    #     y / yes   → 批准，→ RUNNING（等待 Runtime 返回）
    #     n / no    → 拒绝，→ IDLE
    #     d         → 展开详情，保持 WAITING_APPROVAL
    #     其他文本  → 记录为 clarification，保持 WAITING_APPROVAL
    #
    #   清除 pending 的唯一合法途径：Runtime 返回 waiting_approval=False

    def _enter_approval(self, result) -> None:
        self._in_approval = True
        approvals = getattr(result, "approvals", []) or []
        self._pending_approvals = list(approvals)
        self._header().set_state("waiting")
        self._update_footer("approval")
        for ap in approvals:
            desc = str(ap.get("description") or ap.get("label") or "高风险操作")
            self._msglog().add_error(
                title="◆ APPROVAL REQUIRED",
                detail=desc,
                hints=["Y 批准", "N 拒绝", "D 查看详情"],
            )
        self._bar().set_placeholder("Approve? [y/N]  —  Y approve   N reject   D details")
        self._bar().focus_input()

    def _exit_approval(self) -> None:
        """仅在 Runtime 明确结束审批时调用（waiting_approval=False）。"""
        self._in_approval = False
        self._pending_approvals = []
        self._bar().set_placeholder("Enter 发送；/ 看命令；Ctrl+Q 退出")
        self._update_footer("idle")
        self._bar().focus_input()

    def _handle_approval_input(self, text: str) -> None:
        """处理审批态下的输入。

        - y / yes   → 批准
        - n / no    → 拒绝
        - d / details → 展开详情（保持审批态）
        - 其他      → 记录为 clarification，保持审批态
        """
        lower = text.lower().strip()
        if lower in ("y", "yes"):
            # 批准：标记为 running，等待 Runtime 继续
            self._in_approval = False  # 本地标记解除（Runtime 会继续返回新状态）
            self._header().set_state("running")
            self._update_footer("running")
            self._bar().set_placeholder("执行中…")
            self._msglog().add_meta("✓ 已批准，继续执行…")
            self._run_async(self._continue_after_approval(approved=True))

        elif lower in ("n", "no"):
            # 拒绝：Runtime 标记 cancelled
            self._in_approval = False
            self._pending_approvals = []
            self._header().set_state("idle")
            self._update_footer("idle")
            self._bar().set_placeholder("Enter 发送；/ 看命令；Ctrl+Q 退出")
            self._msglog().add_meta("× 已拒绝，Run 停止。")
            self._bar().focus_input()

        elif lower in ("d", "details"):
            # 展开详情：保持审批态
            self._msglog().add_meta("── 审批详情 ──")
            for i, ap in enumerate(self._pending_approvals, 1):
                label = str(ap.get("label") or ap.get("description") or f"item {i}")
                desc = str(ap.get("description") or "")
                self._msglog().add_meta(f"  {i}. {label}")
                if desc:
                    self._msglog().add_meta(f"     {desc}")
            self._msglog().add_meta("── 输入 Y 批准 / N 拒绝 / D 再次查看 ──")
            # 注意：_in_approval 保持 True
            self._bar().focus_input()

        else:
            # 普通文本：作为 clarification 记录，不解除 pending
            self._msglog().add_meta(f"  [clarification] {text}")
            self._msglog().add_meta("  （审批仍待处理，请输入 Y / N / D）")
            # _in_approval 保持 True，header/footer 不变
            self._bar().focus_input()

    async def _continue_after_approval(self, approved: bool = True) -> None:
        """批准后调用 Runtime 继续执行；拒绝后仅清理。"""
        if not approved:
            self._exit_approval()
            return

        chat = self._get_chat_app()
        result = getattr(chat, "_last_result", None)
        if result is None:
            self._exit_approval()
            return

        try:
            result = await self._resolve_approvals(chat, result)
        except Exception as exc:
            self._render_exception(exc)
            self._exit_approval()
            return

        if getattr(result, "waiting_approval", False):
            # 还有更多审批（多层）
            self._enter_approval(result)
        else:
            self._exit_approval()
            self._render_result(result, time.monotonic())

    # ── 事件 → 面板 ─────────────────────────────────────────────
    def _on_tui_event(self, channel: str, payload) -> None:
        if not isinstance(payload, dict):
            return
        msglog = self._msglog()

        try:
            if channel == "tool":
                name = str(payload.get("name") or "工具").strip()
                args = str(payload.get("args") or "").strip()
                item = msglog.add_tool(name, detail=args, status="running")
                self._last_tool_item = item

            elif channel == "assistant_delta":
                meta = payload.get("metadata") or {}
                delta = str(meta.get("delta") or "")
                if delta:
                    if self._stream_item is None:
                        self._stream_item = msglog.stream_start()
                    msglog.stream_chunk(delta)

            elif channel == "activity":
                etype = str(payload.get("type") or "")
                meta = payload.get("metadata") or {}
                if etype in ("tool.started", "tool.finished"):
                    label = str(meta.get("label") or etype)
                    status = "done" if etype == "tool.finished" else "running"
                    if self._last_tool_item:
                        msglog.tool_update(self._last_tool_item, status, label)
                    else:
                        self._last_tool_item = msglog.add_tool(label, status=status)
                elif etype in ("run.completed", "run.failed", "run.cancelled"):
                    # 强制 flush 流式 buffer
                    if self._stream_item:
                        msglog.stream_end()
                        self._stream_item = None

            elif channel == "control":
                etype = str(payload.get("type") or "")
                meta = payload.get("metadata") or {}
                if etype == "run.completed":
                    if self._stream_item:
                        msglog.stream_end()
                        self._stream_item = None
                    # Runtime 明确完成 → 清除审批态
                    if self._in_approval:
                        self._exit_approval()
                    self._header().set_state("idle")
                    self._update_footer("idle")
                    self._trigger_compact()
                elif etype == "run.failed":
                    if self._stream_item:
                        msglog.stream_end()
                        self._stream_item = None
                    if self._in_approval:
                        self._exit_approval()
                    self._header().set_state("failed")
                    self._update_footer("idle")
                    msg = str(meta.get("message") or payload.get("message") or "Run 失败")
                    msglog.add_error(title=msg[:200])
                elif etype == "run.waiting_approval":
                    # 事件流里也可能收到（Runtime 中途暂停）
                    if not self._in_approval:
                        approvals = meta.get("approvals") or []
                        self._pending_approvals = list(approvals)
                        self._in_approval = True
                        self._header().set_state("waiting")
                        self._update_footer("approval")
                        self._bar().set_placeholder("Approve? [y/N]")
                        self._bar().focus_input()
                elif etype == "assistant.reply":
                    content = str(meta.get("content") or "")
                    kind = str(meta.get("kind") or "")
                    if content and self._stream_item is None:
                        msglog.add_assistant(content, title=kind)

        except (NoScreen, NoActiveAppError, MountError):
            # 面板渲染失败不打断 Agent 执行；未知异常继续上抛
            import logging
            if self._debug:
                import traceback
                traceback.print_exc()

    # ── 结果渲染 ────────────────────────────────────────────────
    def _render_result(self, result, started: float) -> None:
        from main import _KIND_LABELS, _clean_display_text, coerce_reply
        from schemas import AgentReply

        elapsed = float(getattr(result, "elapsed_seconds", 0) or 0) or (time.monotonic() - started)
        msglog = self._msglog()

        # 强制 flush 流式 buffer
        if self._stream_item:
            msglog.stream_end()
            self._stream_item = None

        reply = coerce_reply(getattr(result, "final_output", None))
        if isinstance(reply, AgentReply):
            content = _clean_display_text(reply.content or "")
            kind_label = _KIND_LABELS.get(reply.kind, reply.kind)
            if content:
                msglog.add_assistant(content, title=kind_label)
            else:
                msglog.add_meta(f"✓ 完成 · {kind_label}")
            if reply.summary:
                msglog.add_meta(f"📌 {_escape_text(reply.summary)}")
            if reply.saved_file:
                msglog.add_artifact(reply.saved_file)
            if reply.next_step:
                msglog.add_meta(f"➡ 下一步: {_escape_text(reply.next_step)}")
        elif reply:
            text = str(reply)
            if text.strip():
                msglog.add_assistant(text)

        error_text = str(getattr(result, "error", "") or "").strip()
        if getattr(result, "ok", True) is False or error_text:
            msglog.add_error(title=error_text[:400] or "本轮未成功")

        for artifact in getattr(result, "artifacts", []) or []:
            msglog.add_artifact(artifact.get("name", "?"))

        task_id = getattr(getattr(result, "task", None), "id", "") or "?"
        msglog.add_meta(f"✓ DONE · {elapsed:.1f}s · run {task_id[:8]}")
        msglog.add_meta("")

        if self.auto_summary:
            chat = self._get_chat_app()
            self._pending_compact = chat.session

        self._exit_approval()  # 安全：result 正常完成时清除（若仍有 pending）
        self._header().set_state("idle")
        self._update_footer("idle")
        self._bar().set_placeholder("Enter 发送；/ 看命令；Ctrl+Q 退出")
        self._bar().focus_input()

    def _render_exception(self, exc: BaseException) -> None:
        from cli.diagnostics import diagnose
        conclusion = diagnose(exc)
        hints = list(conclusion.hints) if hasattr(conclusion, "hints") else []
        self._msglog().add_error(
            title=conclusion.head(),
            hints=hints[:3],
        )
        self._bar().focus_input()

    def _trigger_compact(self) -> None:
        session = self._pending_compact
        if session is None:
            return
        self._pending_compact = None
        self.run_worker(_compact_worker(session, True), exclusive=False)

    # ── 输入历史（↑↓）──────────────────────────────────────────
    def on_input_changed(self, event) -> None:
        text = event.value or ""
        # slash popup：输入 / 开头时过滤
        if text.startswith("/") and len(text) > 1:
            self._show_slash_popup(text)
        elif not text.startswith("/") and self._popup_visible:
            self._hide_slash_popup()

    def key_up(self) -> None:
        self._on_key_press("up")

    def key_down(self) -> None:
        self._on_key_press("down")

    def key_tab(self) -> None:
        self._on_key_press("tab")

    def key_escape(self) -> None:
        self._on_key_press("escape")

    def _on_key_press(self, key: str) -> None:
        """按键处理，优先级：slash popup > approval > history > running。"""
        input_focused = (
            self._bar().input is not None
            and self._bar().input.has_focus
        )
        popup = self._popup()

        # 1. Slash popup 打开时：↑↓/Tab/Esc 全部交给 popup
        if self._popup_visible and popup:
            if key == "down":
                popup.select_next()
            elif key == "up":
                popup.select_prev()
            elif key == "tab":
                sel = popup.selected()
                if sel:
                    self._bar().input.value = sel[0] + " "
                    self._hide_slash_popup()
            elif key == "escape":
                # 第一次 Esc 只关闭 popup，不中断 Runtime
                self._hide_slash_popup()
            return

        # 2. Approval 态：↑↓ 仍然可以浏览历史（不影响 pending）
        # 3. 普通输入历史
        #    hist_idx 语义：当前光标在历史列表中的索引；
        #    初始 hist_idx = len(history)（表示"新输入"位置）
        #    首次按 ↑：先归位到 len(history)-1（最后一条历史），再递减
        if input_focused:
            if key == "up":
                # 归位：如果 hist_idx 超出范围（初始状态），设到末尾
                if self._hist_idx >= len(self._history):
                    self._hist_idx = len(self._history) - 1
                else:
                    self._hist_idx = max(0, self._hist_idx - 1)
                self._bar().input.value = self._history[self._hist_idx]
            elif key == "down":
                if self._hist_idx < len(self._history) - 1:
                    self._hist_idx += 1
                    self._bar().input.value = self._history[self._hist_idx]
                else:
                    # 已到末尾（新输入位置），清空输入框
                    self._hist_idx = len(self._history)
                    self._bar().input.value = ""

        # 4. Esc 优先级
        if key == "escape":
            if self._in_approval:
                # 审批态 Esc：显示提示，不解除 pending
                self._msglog().add_meta("（审批未处理，Esc 不生效；请输入 Y / N / D）")
            elif self._header()._run_state == "running":
                # 运行中：发中断信号
                self._msglog().add_meta("⚠ 已发送中断请求…")

    # ── Slash popup ─────────────────────────────────────────────
    def _show_slash_popup(self, text: str) -> None:
        prefix = text.lower()
        results = [(cmd, desc) for cmd, desc in _SLAH_COMMANDS if cmd.lower().startswith(prefix)]
        if not results:
            self._hide_slash_popup()
            return

        popup = self._popup()
        if popup is None:
            return
        popup.set_results(results, text)
        if not self._popup_visible:
            self._popup_visible = True
            popup.show()
        self._update_footer("slash")

    def _hide_slash_popup(self) -> None:
        popup = self._popup()
        if popup and self._popup_visible:
            popup.hide()
            self._popup_visible = False
        # 根据当前状态恢复 footer
        if self._in_approval:
            self._update_footer("approval")
        elif self._header()._run_state == "running":
            self._update_footer("running")
        else:
            self._update_footer("idle")

    # ── Footer（上下文感知）────────────────────────────────────
    def _update_footer(self, mode: str) -> None:
        """切换底部上下文提示：idle / running / approval / slash。

        挂载前调用只记录 mode，挂载后会立刻补一次（见 on_mount），
        保证任何时序下提示都不会停留在空串。
        """
        self._footer_mode = mode
        hints = {
            "idle": "/ commands   ↑↓ history   Ctrl+L clear   Ctrl+Q quit",
            "running": "Esc interrupt   Ctrl+Q quit",
            "approval": "Y approve   N reject   D details",
            "slash": "↑↓ select   Tab complete   Enter run   Esc close",
        }
        hint = hints.get(mode, hints["idle"])
        label = self._hint_label
        if label is None:
            return
        try:
            label.update(hint)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            # 界面可能正在卸载（NoScreen / NoActiveApp）；提示失败不该影响主流程
            pass

    # ── Inspector ───────────────────────────────────────────────
    def action_show_inspector(self) -> None:
        chat = self._get_chat_app()
        data: dict = {"run_id": "—", "model": "—", "state": "idle"}
        try:
            info = chat.store.diagnose(chat.container_id()) if hasattr(chat, "store") else {}
            latest = info.get("latest_run") or {}
            data["run_id"] = latest.get("id", "—")
            data["state"] = latest.get("state", "—")
            data["elapsed"] = str(info.get("elapsed_s", "—"))
            events = info.get("events") or []
            data["events"] = [
                f"{e.get('type', '?')}  {str(e.get('at', ''))[:19].replace('T', ' ')}"
                for e in events[-5:]
            ]
        except (AttributeError, TypeError, KeyError):
            # store.diagnose 失败时 Inspector 仍用默认占位符
            pass
        self.push_screen(InspectorScreen(data))
        # Inspector 打开时 InputBar 失焦是预期行为

    def on_screen_closed(self) -> None:
        """InspectorScreen 关闭后，focus 回到 InputBar。"""
        self._bar().focus_input()

    # ── 快捷键 ──────────────────────────────────────────────────
    def action_quit(self) -> None:
        try:
            self._get_chat_app()._close_session()
        except (AttributeError, TypeError):
            # 会话未创建或 store 异常时仍需退出 UI
            pass
        self.exit()

    def action_clear_log(self) -> None:
        self._msglog().clear()

    def action_complete_slash(self) -> None:
        """Tab 键：popup 可见时补全选中的 slash command。"""
        popup = self._popup()
        if self._popup_visible and popup:
            sel = popup.selected()
            if sel:
                self._bar().input.value = sel[0] + " "
                self._hide_slash_popup()


def _escape_text(text: str) -> str:
    """转义 Rich markup 字符，防止用户内容触发渲染错误。"""
    return text.replace("[", "【").replace("]", "】")


# ── 顶层 worker ────────────────────────────────────────────────────
async def _compact_worker(session, auto_summary: bool = True) -> None:
    from main import _auto_compact
    await _auto_compact(session, auto_summary)


# ── 入口 ──────────────────────────────────────────────────────────
def run_tui(
    session_name: str = "personal",
    mode: str = "stream",
    debug: bool = False,
    max_turns: int = 20,
    history_limit: int | None = None,
    auto_summary: bool = True,
) -> int:
    app = ForgeTuiApp(
        session_name=session_name,
        mode=mode,
        debug=debug,
        max_turns=max_turns,
        history_limit=history_limit,
        auto_summary=auto_summary,
    )
    try:
        app.run()
        return 0
    except KeyboardInterrupt:
        print("\n已中断，退出。")
        return 130
