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

_time = time  # 事件处理里算 elapsed 用 monotonic

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
    HistorySearchDialog,
    MessageItem,
    MessageLog,
    MUTED,
    PANEL,
    PRIMARY,
    SlashPopup,
    Spinner,
    StatusHeader,
    SUCCESS,
    SURFACE,
    ToolGroup,
    TuiPanels,
    TEXT_C,
    WARNING,
    QuestionPanel,
    ApprovalPanel,
    SessionListPopup,
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
    ModelPicker {{
        /* 一行高，显示当前模型；点按 `m` 键打开 popup。 */
        height: 1;
        margin: 0 1;
        padding: 0 1;
        color: {MUTED};
    }}
    .model-popup-header {{
        height: 1;
        padding: 0 1;
        color: {MUTED};
    }}
    .model-popup-body {{
        padding: 0 1;
        color: {TEXT_C};
    }}
    ModelPopup {{
        /* 默认隐藏；按 m 时 display: block，列在输入框正上方 */
        display: none;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        margin: 0 1;
        padding: 0 1;
        max-height: 12;
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
    /* ── Spinner（工具执行中动画）── */
    Spinner {{
        height: 1;
        margin: 0 1;
        padding: 0 1;
        color: {MUTED};
    }}
    /* ── F1 历史搜索对话框 ── */
    HistorySearchDialog {{
        display: none;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        margin: 0 1;
        padding: 0 1;
        max-height: 14;
    }}
    .hist-search-header {{
        height: 1;
        color: {MUTED};
        padding: 0 1;
    }}
    .hist-search-input {{
        height: 1;
        margin: 0 1;
    }}
    .hist-search-body {{
        padding: 0 1;
        color: {TEXT_C};
    }}
    /* ── 提问面板 ── */
    QuestionPanel {{
        display: none;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        margin: 0 1;
        padding: 0 1;
        max-height: 16;
    }}
    .question-header {{
        height: 1;
        color: {WARNING};
        padding: 0 1;
    }}
    .question-body {{
        padding: 0 1;
        color: {TEXT_C};
    }}
    .question-input {{
        height: 1;
        margin: 0 1;
    }}
    /* ── 审批面板 ── */
    ApprovalPanel {{
        display: none;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        margin: 0 1;
        padding: 0 1;
        max-height: 14;
    }}
    .approval-header {{
        height: 1;
        color: {ERROR_C};
        padding: 0 1;
    }}
    .approval-body {{
        padding: 0 1;
        color: {TEXT_C};
    }}
    /* ── 会话列表 ── */
    SessionListPopup {{
        display: none;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        margin: 0 1;
        padding: 0 1;
        max-height: 16;
    }}
    .session-list-header {{
        height: 1;
        color: {MUTED};
        padding: 0 1;
    }}
    .session-list-body {{
        padding: 0 1;
        color: {TEXT_C};
    }}
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("ctrl+o", "show_inspector", "Inspector"),
        Binding("m", "toggle_model_picker", "Models", priority=True),
        # F1 历史搜索对话框（可搜索，替代线性回看）
        Binding("f1", "toggle_history_search", "Search History"),
        # Ctrl+F1 保留为回退（向下）
        Binding("ctrl+f1", "history_next", "History ↓"),
        # 会话列表
        Binding("ctrl+s", "toggle_session_list", "Sessions"),
        # priority=True 覆盖 Screen 默认的 tab → app.focus_next
        Binding("tab", "complete_slash", "Tab complete", priority=True),
    ]

    # ── F1 历史搜索对话框 ──────────────────────────────────────────
    def action_toggle_history_search(self) -> None:
        """F1：打开/关闭可搜索历史对话框。"""
        hs = self._app_state.hist_search if self._app_state else None
        if hs is None:
            return
        if hs.is_visible():
            hs.hide()
            self._bar().focus_input()
            self._update_footer("idle")
            return
        # 构建带时间戳的历史（从 _history 提取，最近 50 条）
        import time as _t
        now = _t.time()
        # _history 是纯字符串列表，没有时间戳，用近似：第 i 条 ≈ now - i*60s
        entries = [
            (self._history[i], now - (len(self._history) - 1 - i) * 60.0)
            for i in range(len(self._history))
        ]
        hs.show(entries)
        self._update_footer("hist_search")

    def _on_hist_search_confirm(self) -> None:
        """F1 搜索对话框确认：把选中的文本填入输入框。"""
        hs = self._app_state.hist_search if self._app_state else None
        if hs is None or not hs.is_visible():
            return
        text = hs.selected_text()
        if text:
            self._bar().input.value = text
        hs.hide()
        self._bar().focus_input()
        self._update_footer("idle")

    def _on_hist_search_cancel(self) -> None:
        """F1 搜索对话框取消。"""
        hs = self._app_state.hist_search if self._app_state else None
        if hs is not None:
            hs.hide()
        self._bar().focus_input()
        self._update_footer("idle")

    # F1 历史专用键：单独 action，不走 _on_key_press 的 slash/approval 优先级
    def action_history_prev(self) -> None:
        if not self._history:
            return
        # 首次 F1 归位到末尾（最后一条），再往前
        if self._hist_idx >= len(self._history):
            self._hist_idx = len(self._history) - 1
        else:
            self._hist_idx = max(0, self._hist_idx - 1)
        self._bar().input.value = self._history[self._hist_idx]

    def action_history_next(self) -> None:
        if not self._history:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
        else:
            self._hist_idx = len(self._history)
            self._bar().input.value = ""
            return
        self._bar().input.value = self._history[self._hist_idx]

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
        # 工具分组：每轮 turn 开始时清零，run 过程中按 activity 累积
        self._tool_group: "ToolGroup" | None = None
        # 后端 activity 行 → 显示状态（tool.started 时建，tool.completed/failed 时定终态）
        # key = activity_id；value = (name, detail, status, elapsed 或 None)
        self._tool_rows: dict[str, tuple[str, str, str, float | None]] = {}
        # 每个 activity 的开始时间戳（monotonic），用于算 elapsed
        self._tool_started_at: dict[str, float] = {}

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
        # 模型选择器状态
        self._model_picker_visible: bool = False
        # ModelPopup 的选中回调：在 on_mount 里把 App._on_model_selected 接进去
        # （ModelPopup 自己不认识 App，所以这里用一个占位，挂载后 patch）
        self._model_popup_callback_set: bool = False

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
        import os
        yield TuiPanels(self.session_name, current_model=os.getenv("AGENT_MODEL", ""))

    def on_mount(self) -> None:
        self._app_state = self.query_one(TuiPanels)
        # 接上 InputBar 的上下文提示行（挂载后 widget 才存在）
        self._hint_label = self._app_state.bar.hint
        # 把 ModelPopup 的选中回调接上 App（popup 是 TuiPanels 持有的，
        # 构造时拿不到 App，所以在挂载时 patch 进去）
        if not self._model_popup_callback_set:
            self._app_state.model_popup._on_select = self._on_model_selected
            self._model_popup_callback_set = True
        # 同步 header 宽度（默认 120 列宽屏）；size 在挂载前可能为 (0, 0)，安全读取
        width = self._app_state.msglog.size[0] or 120
        self._header().set_width(width)
        # Spinner 80ms 动画定时器（参照 dsh-TUI SpinnerAnimationRow）
        self._spinner_timer = self.set_interval(0.08, self._tick_spinner)
        self._banner()
        self._update_footer("idle")

    def _tick_spinner(self) -> None:
        """每 80ms 驱动 Spinner 动画帧推进。"""
        spinner = self._app_state.spinner if self._app_state else None
        if spinner is not None:
            spinner.tick()

    def _banner(self) -> None:
        msglog = self._msglog()
        msglog.add_meta("◆ FORGE · 全能助手 TUI")
        msglog.add_meta(f"  session: {self.session_name}   |   mode: {self.mode}")
        msglog.add_meta("  /help 看命令   |   Ctrl+Q 退出   |   Ctrl+O Inspector")
        msglog.add_meta("  按 m 切换模型   |   ↑↓ 历史   |   Tab 补全")
        msglog.add_meta("")
        self._bar().focus_input()
        # 启动时异步拉取可用模型列表（网关 + 本地）
        self._load_models_async()

    def _load_models_async(self) -> None:
        """后台拉取 /models 端点，把结果写进 ModelPicker。"""
        self._run_async(self._fetch_models())

    async def _fetch_models(self) -> None:
        import os
        picker = self._model_picker()
        popup = self._model_popup()
        if picker is None or popup is None:
            return
        picker.set_loading(True)
        try:
            models = await self._query_gateway_models()
        except Exception:
            models = []
        current = os.getenv("AGENT_MODEL") or "—"
        if models:
            picker.set_models(models)
            # 把当前 AGENT_MODEL 标成已选（如果它不在列表里就补进去）
            if not any(mid == current for mid, _ in models):
                models.append((current, "unknown"))
            picker.set_loading(False)
        else:
            picker.set_models([(current, "unknown")])
            picker.set_loading(False)
        # 同步当前模型显示 + status header
        picker.set_current(current)
        self._header().set_model(current)

    async def _query_gateway_models(self) -> list[tuple[str, str]]:
        """并发拉网关 + 本地 /models，合并去重（网关优先）。

        urllib 是同步阻塞 IO，用 asyncio.to_thread 丢到默认线程池跑，
        不阻塞 Textual 事件循环。两个源并发；任一失败只跳过，不影响另一个。
        """
        import asyncio
        import json
        import os
        import ssl
        import urllib.request

        def _fetch_sync(base_url: str, source: str) -> list[tuple[str, str]]:
            if not base_url:
                return []
            key = os.getenv("OPENAI_API_KEY", "")
            url = base_url.rstrip("/") + "/models"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {key}" if key else "",
                "Content-Type": "application/json",
            })
            try:
                resp = urllib.request.urlopen(
                    req, timeout=5, context=ssl.create_default_context()
                )
                data = json.loads(resp.read())
                return [(m["id"], source) for m in data.get("data", [])]
            except Exception:
                return []

        base = os.getenv("OPENAI_BASE_URL", "")
        local_base = os.getenv("FORGE_LOCAL_MODEL_BASE_URL", "")
        gateway, local = await asyncio.gather(
            asyncio.to_thread(_fetch_sync, base, "gateway"),
            asyncio.to_thread(_fetch_sync, local_base, "local"),
        )
        seen: set[str] = set()
        merged: list[tuple[str, str]] = []
        for mid, src in gateway + local:
            if mid not in seen:
                seen.add(mid)
                merged.append((mid, src))
        return merged

    def _model_picker(self):
        return self._app_state.model_picker if self._app_state else None

    def _model_popup(self):
        return self._app_state.model_popup if self._app_state else None

    # ── 模型选择 action ───────────────────────────────────────
    def action_toggle_model_picker(self) -> None:
        """按 `m` 键打开 / 关闭模型选择 popup。"""
        popup = self._model_popup()
        picker = self._model_picker()
        if popup is None or picker is None:
            return
        if popup.is_visible():
            popup.hide()
            self._model_picker_visible = False
            return
        # 首次打开且模型未加载 → 先拉
        if not picker._models:
            picker.set_loading(True)
            self._run_async(self._fetch_models())
        popup.show(picker._models, picker._current)
        self._model_picker_visible = True

    def _on_model_selected(self, model_id: str, source: str) -> None:
        """ModelPopup 选中后回调：设 AGENT_MODEL 并更新状态栏。"""
        import os
        os.environ["AGENT_MODEL"] = model_id
        # 切到本地模型时，FORGE_MODEL_PREF 也要跟着
        if source == "local":
            os.environ["FORGE_MODEL_PREF"] = "local"
        else:
            os.environ["FORGE_MODEL_PREF"] = "gateway"
        picker = self._model_picker()
        if picker:
            picker.set_current(model_id)
        self._header().set_model(model_id)
        self._msglog().add_meta(f"已切换模型 → {model_id}（{source}）")
        self._model_picker_visible = False

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
        # /sessions 命令 → 弹出会话列表 popup
        if text.strip() in ("/sessions", "/session"):
            self._show_session_list()
            return
        self._run_async(self._dispatch_cmd(text))

    def _show_session_list(self) -> None:
        """/sessions 命令：拉取会话列表并弹出 SessionListPopup。"""
        import time as _t
        now = _t.time()
        sl = self._app_state.session_list if self._app_state else None
        if sl is None:
            return
        # 从 ChatApp 的 store 里取会话摘要（如果可用）
        sessions: list[dict] = []
        try:
            chat = self._get_chat_app()
            if hasattr(chat, "store") and hasattr(chat.store, "list_sessions"):
                raw = chat.store.list_sessions()
                for s in raw[:15]:
                    sessions.append({
                        "title": str(s.get("title") or s.get("name") or "会话"),
                        "updated_at": str(s.get("updated_at") or "")[:16],
                        "model": str(s.get("model") or ""),
                        "status": "stopped",
                        "current": (s.get("name") or s.get("title")) == self.session_name,
                        "pinned": False,
                    })
        except Exception:
            pass
        # 兜底：至少把当前会话列进去
        if not sessions:
            sessions = [{
                "title": self.session_name,
                "updated_at": "",
                "model": "",
                "status": "idle",
                "current": True,
                "pinned": False,
            }]
        sl.show(sessions)
        self._update_footer("idle")

    def _on_session_select(self) -> None:
        """会话列表选中后回调（当前最小版：只记录，不做切换）。"""
        sl = self._app_state.session_list if self._app_state else None
        if sl is not None and sl.is_visible():
            sel = sl.selected_session()
            if sel:
                self._msglog().add_meta(f"→ 选中会话: {sel.get('title', '?')}")
            sl.hide()
            self._bar().focus_input()

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
        # 新一轮 turn 开始：重置工具分组状态
        self._tool_group = None
        self._tool_rows.clear()
        self._tool_started_at.clear()
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
                # 这是旧的「工具调用」行（来自 tool channel 的 {name,args}）。
                # 设计稿 B 用 activity channel 的 tool.started/completed 来建「工具执行」
                # 分组块，那里有 elapsed。tool channel 只作为兜底：如果 activity 没
                # 到达（比如本地调试时关掉事件流），仍要保留一条扁平 tool 行。
                name = str(payload.get("name") or "工具").strip()
                args = str(payload.get("args") or "").strip()
                if self._tool_group is None:
                    item = msglog.add_tool(name, detail=args, status="running")
                    self._last_tool_item = item
                else:
                    self._tool_group.toggle()  # 保证展开
                    self._tool_group.add_tool(name, detail=args, status="running")

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
                aid = str(payload.get("activity_id") or "")
                label = str(meta.get("label") or etype)

                if etype == "tool.started":
                    if not aid:
                        return
                    if self._tool_group is None:
                        self._tool_group = msglog.tool_group_start()
                    self._tool_rows[aid] = (label, label, "running", None)
                    self._tool_started_at[aid] = _time.monotonic()
                    self._tool_group.add_tool(label, detail="", status="running")
                    # Spinner：工具开始执行时点亮动画
                    spinner = self._app_state.spinner if self._app_state else None
                    if spinner is not None:
                        spinner.start(f"正在执行 {label}…")
                        spinner.display = True

                elif etype in ("tool.completed", "tool.failed"):
                    if not aid:
                        return
                    status = "done" if etype == "tool.completed" else "failed"
                    started = self._tool_started_at.pop(aid, None)
                    elapsed = (
                        (_time.monotonic() - started) if started is not None else None
                    )
                    # 用终态 label 覆盖之前 running 的那条
                    prev = self._tool_rows.get(aid)
                    if prev:
                        self._tool_rows[aid] = (prev[0], prev[1], status, elapsed)
                    if self._tool_group is not None:
                        # 追加终态行（保留 running 行作为执行过程）
                        self._tool_group.add_tool(
                            label, detail="", status=status, elapsed=elapsed,
                        )
                    # 没有更多 running 工具时熄灭 Spinner
                    if not any(r[2] == "running" for r in self._tool_rows.values()):
                        spinner = self._app_state.spinner if self._app_state else None
                        if spinner is not None:
                            spinner.stop()
                            spinner.display = False

                elif etype in ("run.completed", "run.failed", "run.cancelled"):
                    # 强制 flush 流式 buffer，把当前工具分组折叠成一行
                    if self._stream_item:
                        msglog.stream_end()
                        self._stream_item = None
                    if self._tool_group is not None and self._tool_group.item_count > 1:
                        self._tool_group.collapse()
                        self._tool_group = None
                    self._tool_rows.clear()
                    # 熄灭 Spinner
                    spinner = self._app_state.spinner if self._app_state else None
                    if spinner is not None:
                        spinner.stop()
                        spinner.display = False

            elif channel == "control":
                etype = str(payload.get("type") or "")
                meta = payload.get("metadata") or {}
                if etype == "run.completed":
                    if self._stream_item:
                        msglog.stream_end()
                        self._stream_item = None
                    # 工具分组在 activity 流里已经折叠，这里不再处理
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

    def key_enter(self) -> None:
        # ModelPopup 的确认键（popup 打开时 focus 在 popup 的 body 上，
        # 不走 Input 的 on_input_submitted，所以这里拦截一次）
        if self._model_picker_visible:
            popup = self._model_popup()
            if popup is not None and popup.is_visible():
                popup.confirm()
        elif self._popup_visible:
            popup = self._popup()
            if popup is not None:
                sel = popup.selected()
                if sel:
                    self._bar().input.value = sel[0] + " "
                    self._hide_slash_popup()
                    self._bar().focus_input()

    def key_escape(self) -> None:
        self._on_key_press("escape")

    def key_m(self) -> None:
        # 按 m 切模型；在 model popup 打开时忽略（Enter 确认 / Esc 取消）
        if self._model_picker_visible:
            return
        self.action_toggle_model_picker()

    def key_f1(self) -> None:
        """F1：打开可搜索历史对话框（参照 dsh-TUI HistorySearchDialog）。"""
        self._on_key_press("f1")

    def key_ctrl_f1(self) -> None:
        """Ctrl+F1：历史回退（向下）。"""
        self._on_key_press("ctrl_f1")

    def _on_key_press(self, key: str) -> None:
        """按键处理，优先级：hist search > model popup > slash popup > approval > history > running。"""
        input_focused = (
            self._bar().input is not None
            and self._bar().input.has_focus
        )
        popup = self._popup()
        hs = self._app_state.hist_search if self._app_state else None

        # -1. 历史搜索对话框打开时：↑↓/Enter/Esc 全部交给对话框
        if hs is not None and hs.is_visible():
            if key == "f1":
                hs.hide()
                self._bar().focus_input()
                self._update_footer("idle")
            elif key == "down":
                hs.select_next()
            elif key == "up":
                hs.select_prev()
            elif key == "enter":
                self._on_hist_search_confirm()
            elif key == "escape":
                self._on_hist_search_cancel()
            return

        # 0. Model popup 打开时：↑↓/Esc 全部交给 popup
        model_popup = self._model_popup()
        if self._model_picker_visible and model_popup is not None and model_popup.is_visible():
            if key == "down":
                model_popup.select_next()
            elif key == "up":
                model_popup.select_prev()
            elif key == "escape":
                model_popup.hide()
                self._model_picker_visible = False
                self._bar().focus_input()
            return

        # 0.5 会话列表打开时：↑↓/Enter/Esc 全部交给会话列表
        sl = self._app_state.session_list if self._app_state else None
        if sl is not None and sl.is_visible():
            if key == "down":
                sl.select_next()
            elif key == "up":
                sl.select_prev()
            elif key == "enter":
                self._on_session_select()
            elif key == "escape":
                sl.hide()
                self._bar().focus_input()
            return

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
        """切换底部上下文提示：idle / running / approval / slash / hist_search。

        挂载前调用只记录 mode，挂载后会立刻补一次（见 on_mount），
        保证任何时序下提示都不会停留在空串。
        """
        self._footer_mode = mode
        hints = {
            "idle": "/ commands   F1 search   Ctrl+S sessions   m model   Ctrl+Q quit",
            "running": "Esc interrupt   Ctrl+Q quit",
            "approval": "Y approve   N reject   D details",
            "slash": "↑↓ select   Tab complete   Enter run   Esc close",
            "hist_search": "↑↓ select   Enter fill   F1/Esc close",
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
