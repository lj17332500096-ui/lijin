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

import asyncio
import time

_time = time  # 事件处理里算 elapsed 用 monotonic

from rich.markdown import Markdown
from rich.text import Text
from textual._context import NoActiveAppError
from textual.app import App, ComposeResult, NoScreen, Screen
from textual.binding import Binding
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, Label
from textual.widget import MountError, Widget

from cli.tui.panels import (
    AGENT,
    ARTIFACT,
    ApiSettingsPanel,
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
    ArtifactItem,
)


# ── 斜杠命令静态列表（与 cli/commands.py build_registry 同步）──────
_SLAH_COMMANDS: list[tuple[str, str]] = [
    ("/api", "打开 API 与网关设置"),
    ("/help", "查看可用命令"),
    ("/sessions", "按最近使用顺序列出会话"),
    ("/new", "创建并切换到新会话"),
    ("/attach", "添加本轮要处理的文件"),
    ("/detach", "移除尚未发送的附件"),
    ("/project", "从文件夹创建项目"),
    ("/switch", "切换会话"),
    ("/history", "查看最近消息"),
    ("/last", "查看上一条助手回复"),
    ("/compact", "压缩对话并自动生成摘要"),
    ("/clear", "清空当前会话消息"),
    ("/artifacts", "列出生成的文件"),
    ("/tools", "查看最近的工具调用"),
    ("/diag", "诊断最近一次运行"),
    ("/debug", "切换调试事件输出"),
    ("/mode", "切换执行模式"),
    ("/exit", "退出命令行界面"),
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
        Binding("ctrl+o", "close_inspector", "关闭"),
        Binding("escape", "close_inspector", "关闭"),
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
            yield Label("◆ FORGE · 运行检查", classes="inspector-title")
            yield Static(f"  运行编号：{self._data.get('run_id', '—')}", classes="inspector-row")
            yield Static(f"  模型：    {self._data.get('model', '—')}", classes="inspector-row")
            yield Static(f"  状态：    {self._data.get('state', '—')}", classes="inspector-row")
            yield Static(f"  耗时：    {self._data.get('elapsed', '—')}", classes="inspector-row")
            yield Static(f"  用量：    {self._data.get('tokens', '—')} 个令牌", classes="inspector-row")
            yield Static(f"  工作目录：{self._data.get('cwd', '—')}", classes="inspector-row")
            yield Static(f"  工具：    {self._data.get('tools', [])}", classes="inspector-row")
            yield Static("  ── 最近 5 条事件 ───────────────────", classes="inspector-row")
            for ev in (self._data.get("events") or [])[:5]:
                yield Static(f"  · {ev}", classes="inspector-row")
            yield Static("  （输入 /diag 查看详细诊断）", classes="inspector-row")

    def on_screen_closed(self) -> None:
        """Screen 关闭时由 App 层回调恢复 focus。"""
        pass

    def action_close_inspector(self) -> None:
        self.app.pop_screen()


class ArtifactPreviewScreen(ModalScreen):
    """Bounded, read-only preview for a registered text artifact."""

    BINDINGS = [Binding("escape", "close_preview", "关闭")]
    CSS = f"""
    ArtifactPreviewScreen {{
        align: center middle;
        background: #000000 70%;
    }}
    #artifact-preview-panel {{
        width: 92%;
        max-width: 120;
        height: 86%;
        border: round {BORDER};
        background: {SURFACE};
        padding: 1;
    }}
    #artifact-preview-title {{
        height: 1;
        color: {PRIMARY};
        text-style: bold;
    }}
    #artifact-preview-path {{
        height: 1;
        color: {MUTED};
    }}
    #artifact-preview-scroll {{
        height: 1fr;
        border: solid {BORDER};
        padding: 0 1;
    }}
    #artifact-preview-content {{
        width: 100%;
        color: {TEXT_C};
    }}
    #artifact-preview-close {{
        width: 16;
        height: 3;
        margin: 1 0 0 0;
        dock: right;
    }}
    """

    def __init__(self, name: str, path: str, content: str) -> None:
        super().__init__()
        self.artifact_name = name
        self.path = path
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="artifact-preview-panel"):
            yield Label(f"📄 {self.artifact_name}", id="artifact-preview-title")
            yield Static(self.path, id="artifact-preview-path")
            preview = (Markdown(self.content)
                       if self.artifact_name.lower().endswith((".md", ".markdown"))
                       else Text(self.content))
            with ScrollableContainer(id="artifact-preview-scroll"):
                yield Static(preview, id="artifact-preview-content")
            yield Button("关闭", id="artifact-preview-close", variant="default")

    def action_close_preview(self) -> None:
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
    #tui-toolbar {{
        height: 1;
        width: 100%;
        padding: 0 1;
        background: {SURFACE};
    }}
    #tui-toolbar Button {{
        height: 1;
        min-width: 10;
        margin-right: 1;
        padding: 0 1;
        border: none;
    }}
    #project-label {{
        width: 1fr;
        height: 1;
        content-align: right middle;
        color: {MUTED};
        padding: 0 1;
    }}
    #tui-main {{
        height: 1fr;
        width: 100%;
    }}
    #tui-chat-column {{
        height: 1fr;
        width: 1fr;
    }}
    ConversationSidebar {{
        display: none;
        width: 32;
        min-width: 24;
        max-width: 42;
        height: 1fr;
        border-right: solid {BORDER};
        background: {SURFACE};
        padding: 0 1;
    }}
    .conversation-sidebar-title {{
        height: 1;
        color: {PRIMARY};
        text-style: bold;
        margin-bottom: 1;
    }}
    #conversation-session-rows {{
        width: 100%;
        height: auto;
    }}
    .conversation-session-row {{
        height: 1;
        width: 100%;
    }}
    .conversation-session-select {{
        width: 1fr;
        min-width: 0;
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        content-align: left middle;
        color: {TEXT_C};
        background: transparent;
    }}
    .conversation-session-select.selected {{
        color: {PRIMARY};
        text-style: bold;
        background: {PANEL};
    }}
    .conversation-session-action {{
        width: 10;
        min-width: 10;
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        color: {MUTED};
        background: transparent;
    }}
    .conversation-session-action:hover {{
        color: {ERROR_C};
        background: {PANEL};
    }}
    .conversation-session-action.is-delete {{
        color: {ERROR_C};
        text-style: bold;
    }}
    .conversation-sidebar-empty {{
        padding: 0 1;
        color: {MUTED};
    }}
    MessageLog {{
        height: 1fr;
        border: solid {BORDER};
        background: {BG};
    }}
    MessageItem {{
        padding: 0 1;
    }}
    ArtifactItem {{
        width: 100%;
        height: auto;
        margin: 1 1;
        padding: 0 1;
        border-left: solid {ARTIFACT};
        background: {SURFACE};
    }}
    .artifact-name {{
        height: 1;
        color: {ARTIFACT};
        text-style: bold;
    }}
    .artifact-actions {{
        height: 1;
        width: 100%;
    }}
    .artifact-action {{
        height: 1;
        min-width: 8;
        margin: 0 1 0 0;
        padding: 0 1;
        border: none;
        color: {PRIMARY};
        background: transparent;
    }}
    .artifact-action:hover {{
        color: {TEXT_C};
        background: {PANEL};
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
    .forge-hint {{
        /* 单行上下文快捷提示。 */
        height: 1;
        margin: 0 1;
        color: {MUTED};
    }}
    Footer {{
        dock: none;
        height: 1;
        margin: 0 1;
        color: {MUTED};
    }}
    ModelPicker {{
        /* 一行高，显示当前模型；点按 `m` 键或鼠标点击标签打开 popup。 */
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
    /* ── API/网关设置面板 ── */
    ApiSettingsPanel {{
        display: none;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        margin: 0 1;
        padding: 0 1;
    }}
    .api-header {{
        height: 1;
        padding: 0 1;
        color: {MUTED};
    }}
    .api-label {{
        height: 1;
        padding: 0 1;
        color: {MUTED};
    }}
    .api-in {{
        height: 3;
        margin: 0 1;
        background: {BG};
        border: solid {BORDER};
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
        Binding("ctrl+q", "quit", "退出", show=False),
        Binding("ctrl+l", "clear_log", "清屏", show=False),
        Binding("ctrl+o", "show_inspector", "运行检查", show=False),
        Binding("m", "toggle_model_picker", "切换模型", show=False, priority=True),
        # F1 历史搜索对话框（可搜索，替代线性回看）
        Binding("f1", "toggle_history_search", "搜索历史", show=False),
        # Ctrl+F1 保留为回退（向下）
        Binding("ctrl+f1", "history_next", "历史记录 ↓", show=False),
        # 会话列表
        Binding("ctrl+s", "toggle_session_list", "会话列表", show=False),
        # priority=True 覆盖 Screen 默认的 tab → app.focus_next
        Binding("tab", "complete_slash", "补全命令", priority=True),
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
        _in = self._bar().input
        if _in is not None:
            _in.value = self._history[self._hist_idx]

    def action_history_next(self) -> None:
        if not self._history:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
        else:
            self._hist_idx = len(self._history)
            _in = self._bar().input
            if _in is not None:
                _in.value = ""
            return
        _in = self._bar().input
        if _in is not None:
            _in.value = self._history[self._hist_idx]

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
        self._pending_attachments: list[dict] = []

        # 流式
        self._stream_item: MessageItem | None = None
        self._stream_buf: str = ""
        self._last_tool_item: MessageItem | None = None
        # 标记本轮 run 中 assistant_delta 是否已渲染过流式内容；
        # 若为 True，_render_result 不再重复 add_assistant（避免同一回答出现两次）
        self._streamed_this_turn: bool = False
        # 工具分组：每轮 turn 开始时清零，run 过程中按 activity 累积
        self._tool_group: "ToolGroup" | None = None
        # 后端 activity 行 → 显示状态（tool.started 时建，tool.completed/failed 时定终态）
        # key = activity_id；value = (name, detail, status, elapsed 或 None)
        self._tool_rows: dict[str, tuple[str, str, str, float | None]] = {}
        # 每个 activity 的开始时间戳（monotonic），用于算 elapsed
        self._tool_started_at: dict[str, float] = {}
        # activity_id → ToolGroup 行索引（add_tool 返回），tool.completed 时原地更新
        self._tool_row_idx: dict[str, int] = {}

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
        # 审批后继续执行时需要的 RunResult（waiting_approval=True 那一次）
        self._waiting_result: object = None

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
        self._turn_active: bool = False
        self._cancel_pending: bool = False

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "artifact-preview-close":
            self.pop_screen()
        elif button_id and button_id.startswith("artifact-view-"):
            artifact_id = button_id[len("artifact-view-"):]
            self._run_async(self._open_registered_artifact(artifact_id, reveal=False))
        elif button_id and button_id.startswith("artifact-folder-"):
            artifact_id = button_id[len("artifact-folder-"):]
            self._run_async(self._open_registered_artifact(artifact_id, reveal=True))
        elif button_id and button_id.startswith("session-select-"):
            sidebar = self._app_state.sidebar if self._app_state else None
            if sidebar is not None:
                try:
                    sidebar.select_index(int(button_id.rsplit("-", 1)[1]))
                    self._on_session_select()
                except (TypeError, ValueError):
                    pass
        elif button_id and button_id.startswith("session-menu-"):
            sidebar = self._app_state.sidebar if self._app_state else None
            if sidebar is not None:
                try:
                    sidebar.toggle_actions(int(button_id.rsplit("-", 1)[1]))
                except (TypeError, ValueError):
                    pass
        elif button_id and button_id.startswith("session-delete-"):
            sidebar = self._app_state.sidebar if self._app_state else None
            if sidebar is not None:
                try:
                    sidebar.select_index(int(button_id.rsplit("-", 1)[1]))
                    self._request_delete_selected_session()
                except (TypeError, ValueError):
                    pass
        elif button_id == "sidebar-toggle":
            self._toggle_sidebar()
        elif button_id == "new-chat":
            self._run_async(self._create_new_chat())
        elif button_id == "attach-file":
            self._run_async(self._choose_and_attach_file())
        elif button_id == "attach-folder":
            self._run_async(self._choose_and_create_project())

    def action_toggle_session_list(self) -> None:
        self._toggle_sidebar()

    def _toggle_sidebar(self) -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("当前任务运行或等待确认时不能切换对话。")
            return
        sidebar = self._app_state.sidebar if self._app_state else None
        if sidebar is None:
            return
        if sidebar.is_visible():
            sidebar.hide()
        else:
            self._show_session_list()

    def _show_session_list(self) -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("当前任务运行或等待确认时不能切换对话。")
            return
        sidebar = self._app_state.sidebar if self._app_state else None
        if sidebar is None:
            return
        try:
            chat = self._get_chat_app()
            current_id = chat.container_id()
            sessions = [
                {
                    "container_id": row.container_id,
                    "session_id": row.session_id,
                    "title": row.display_title,
                    "current": row.container_id == current_id,
                    "project": row.session_id.startswith("proj-"),
                }
                for row in chat.store.list(limit=100)
            ]
            sidebar.show(sessions)
            self._update_footer("idle")
        except Exception as exc:
            self._msglog().add_error(title="无法加载对话列表", detail=str(exc))

    def _on_session_select(self) -> None:
        sidebar = self._app_state.sidebar if self._app_state else None
        selected = sidebar.selected_session() if sidebar is not None else None
        if selected is None:
            return
        self._run_async(self._switch_session(str(selected.get("container_id") or "")))

    def _request_delete_selected_session(self) -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("任务运行或等待确认期间不能删除对话。")
            return
        sidebar = self._app_state.sidebar if self._app_state else None
        selected = sidebar.selected_session() if sidebar is not None else None
        if not selected:
            self._msglog().add_meta("请先在左侧选择要删除的对话。")
            return
        container_id = str(selected.get("container_id") or "")
        try:
            manager = self._get_chat_app().runtime.tasks
            if manager.find_active_run(container_id) is not None:
                self._msglog().add_error(
                    title="不能删除正在运行的对话",
                    detail="请等待该 Run 完成或取消后再删除。",
                )
                return
        except Exception as exc:
            self._msglog().add_error(title="无法确认对话状态", detail=str(exc))
            return
        self._run_async(self._delete_session(container_id))

    async def _delete_session(self, container_id: str) -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("任务运行或等待确认期间不能删除对话。")
            return
        chat = self._get_chat_app()
        row = chat.store.get(container_id)
        if row is None:
            self._msglog().add_error(title="删除失败", detail="该对话已经不存在，请刷新列表。")
            self._show_session_list()
            return
        manager = chat.runtime.tasks
        if manager.find_active_run(container_id) is not None:
            self._msglog().add_error(
                title="不能删除正在运行的对话",
                detail="确认期间该对话启动了 Run。请等待或取消后重试。",
            )
            return

        current_id = chat.container_id()
        deleting_current = current_id == container_id
        replacement_id: str | None = None
        try:
            if deleting_current:
                await self._discard_pending_attachments()
                await chat.new_session("新对话")
                replacement_id = chat.container_id()

            if not chat.store.delete(container_id):
                raise RuntimeError("存储层没有确认删除，请重试。")
        except Exception as exc:
            if deleting_current and chat.store.get(container_id) is not None:
                try:
                    await chat.switch_to(container_id)
                    self.session_name = chat.session_name
                    self._header().set_session_name(row.display_title)
                    self._render_container_history(container_id)
                    self._update_project_label(container_id)
                except Exception:
                    pass
            self._msglog().add_error(title="删除对话失败", detail=str(exc))
            return

        if deleting_current and replacement_id:
            self.session_name = chat.session_name
            self._header().set_session_name("新对话")
            self._render_container_history(replacement_id)
            self._update_project_label(replacement_id)

        if self._app_state and self._app_state.sidebar.is_visible():
            self._show_session_list()
        pending_cleanup = any(
            str(item.get("container_id") or "") == container_id
            for item in manager.list_pending_session_cleanups(limit=1000)
        )
        if pending_cleanup:
            self._msglog().add_meta(
                f"已删除对话“{row.display_title}”；模型会话历史清理已排队，将在数据库可用后重试。"
            )
        else:
            self._msglog().add_meta(f"已永久删除对话“{row.display_title}”及其关联数据。")

    async def _switch_session(self, container_id: str) -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("当前任务运行或等待确认时不能切换对话。")
            return
        try:
            await self._discard_pending_attachments()
            chat = self._get_chat_app()
            await chat.switch_to(container_id)
            self.session_name = chat.session_name
            row = chat.store.get(container_id)
            title = row.display_title if row else chat.session_name
            self._header().set_session_name(title)
            self._render_container_history(container_id)
            self._update_project_label(container_id)
            if self._app_state.sidebar and self._app_state.sidebar.is_visible():
                self._show_session_list()
                self._app_state.sidebar.hide()
            self._bar().focus_input()
            self._msglog().add_meta(f"已切换到：{title}")
        except Exception as exc:
            self._msglog().add_error(title="切换对话失败", detail=str(exc))

    def _render_container_history(self, container_id: str) -> None:
        self._msglog().clear()
        try:
            messages = self._get_chat_app().store.messages(container_id, limit=100)
        except Exception as exc:
            self._msglog().add_error(title="读取对话历史失败", detail=str(exc))
            return
        for item in messages:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role == "user":
                try:
                    attachments = self._get_chat_app().store.mgr.list_message_attachments(
                        message_id=int(item.get("id") or 0)
                    )
                    names = [str(a.get("display_name") or "附件") for a in attachments]
                    if names:
                        content += "\n📎 " + "、".join(names)
                except Exception:
                    pass
                self._msglog().add_user(content)
            elif role == "assistant":
                self._msglog().add_assistant(content)

    def _update_project_label(self, container_id: str | None = None) -> None:
        if not self._app_state:
            return
        chat = self._get_chat_app()
        cid = container_id or chat.container_id()
        container = chat.runtime.tasks.get_container(cid) if cid else None
        title = str((container or {}).get("title") or "个人会话")
        prefix = "项目" if chat.session_name.startswith("proj-") else "会话"
        self._app_state.project_label.update(f"{prefix}：{title[:32]}")

    async def _create_new_chat(self, title: str = "新对话") -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("当前任务运行或等待确认时不能新建对话。")
            return
        try:
            await self._discard_pending_attachments()
            chat = self._get_chat_app()
            await chat.new_session(title or "新对话")
            self.session_name = chat.session_name
            self._header().set_session_name(title or "新对话")
            self._render_container_history(chat.container_id())
            self._update_project_label()
            self._refresh_sidebar_selection()
            if self._app_state.sidebar:
                self._app_state.sidebar.hide()
            self._msglog().add_meta(f"已创建新对话：{title or '新对话'}")
            self._bar().focus_input()
        except Exception as exc:
            self._msglog().add_error(title="新建对话失败", detail=str(exc))

    def _refresh_sidebar_selection(self) -> None:
        if self._app_state and self._app_state.sidebar.is_visible():
            self._show_session_list()

    async def _choose_and_attach_file(self, path: str | None = None) -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("当前任务运行或等待确认时不能添加文件。")
            return
        if not path:
            import asyncio
            try:
                path = await asyncio.to_thread(_native_path_dialog, "file")
            except Exception as exc:
                self._msglog().add_error(
                    title="无法打开文件选择器",
                    detail=f"{exc}\n也可以输入 /attach \"完整文件路径\" 添加文件。",
                )
                return
        if not path:
            return
        try:
            from pathlib import Path
            import hashlib
            import mimetypes
            import shutil
            import uuid
            from runtime.task_manager import project_attachments_dir

            source = Path(path).expanduser().resolve(strict=True)
            if not source.is_file():
                raise ValueError("请选择一个文件。")
            if len(self._pending_attachments) >= 8:
                raise ValueError("每条消息最多添加 8 个文件。")
            source_name = source.name.lower()
            if (source_name in {".env", "apikey.txt"}
                    or source_name.startswith(".env.")
                    or source_name.startswith("memory.json")
                    or source.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}):
                raise ValueError("出于安全保护，密钥和证书文件不能作为附件添加。")
            if source.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("单个附件不能超过 25 MB。")
            pending_bytes = sum(int(item.get("size_bytes") or 0)
                                for item in self._pending_attachments)
            if pending_bytes + source.stat().st_size > 50 * 1024 * 1024:
                raise ValueError("待发送附件总量不能超过 50 MB。")
            chat = self._get_chat_app()
            task_id = chat.container_id()
            if not task_id:
                raise RuntimeError("当前对话容器不可用，无法保存附件。")
            target_dir = project_attachments_dir(task_id)
            unique_dir = target_dir / uuid.uuid4().hex[:8]
            unique_dir.mkdir(parents=True, exist_ok=True)
            dest = unique_dir / source.name
            shutil.copy2(source, dest)
            try:
                digest = hashlib.sha256(dest.read_bytes()).hexdigest()
                manager = chat.store.mgr
                attachment = manager.add_message_attachment(
                    task_id=task_id,
                    display_name=source.name,
                    stored_path=str(dest),
                    mime_type=mimetypes.guess_type(source.name)[0],
                    size_bytes=dest.stat().st_size,
                    sha256=digest,
                )
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            self._pending_attachments.append(attachment)
            self._update_attachment_button()
            self._msglog().add_meta(f"已添加本轮附件：{source.name}（发送下一条消息时使用）")
            self._bar().focus_input()
        except Exception as exc:
            self._msglog().add_error(title="添加文件失败", detail=f"{type(exc).__name__}: {exc}")

    async def _choose_and_create_project(self, path: str | None = None) -> None:
        if self._turn_active or self._in_approval:
            self._msglog().add_meta("当前任务运行或等待确认时不能切换项目。")
            return
        if not path:
            import asyncio
            try:
                path = await asyncio.to_thread(_native_path_dialog, "directory")
            except Exception as exc:
                self._msglog().add_error(
                    title="无法打开文件夹选择器",
                    detail=f"{exc}\n也可以输入 /project \"完整文件夹路径\" 创建项目。",
                )
                return
        if not path:
            return
        try:
            from pathlib import Path
            root = Path(path).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError("请选择一个现有文件夹作为项目目录。")
            chat = self._get_chat_app()
            container_id = await chat.new_project(root.name or "新项目", str(root))
            await self._discard_pending_attachments()
            self.session_name = chat.session_name
            self._header().set_session_name(root.name or "新项目")
            self._render_container_history(container_id)
            self._update_project_label(container_id)
            self._refresh_sidebar_selection()
            if self._app_state.sidebar:
                self._app_state.sidebar.hide()
            self._msglog().add_meta(f"已创建项目“{root.name}”，工作文件夹：{root}")
            self._bar().focus_input()
        except Exception as exc:
            self._msglog().add_error(title="创建项目失败", detail=f"{type(exc).__name__}: {exc}")

    async def _discard_pending_attachments(self) -> None:
        pending, self._pending_attachments = self._pending_attachments, []
        self._update_attachment_button()
        if not pending or self._chat_app is None:
            return
        manager = self._chat_app.store.mgr
        for attachment in pending:
            try:
                manager.delete_message_attachment(str(attachment.get("id") or ""))
            except Exception:
                pass

    def _update_attachment_button(self) -> None:
        if self._app_state:
            count = len(self._pending_attachments)
            label = "＋ 文件" if not count else f"＋ 文件（{count}）"
            self._app_state.attach_file_button.label = label

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
            # on_close：popup 的 cancel() 调用此回调，清除 flag + 焦点还给 Input
            self._app_state.model_popup._on_close = self._on_model_popup_closed
            self._model_popup_callback_set = True
        # API 设置面板回调（保存 → _apply_api_settings，取消 → 仅关闭）
        self._app_state.api_settings.attach(self._apply_api_settings, lambda: None)
        # 同步 header 宽度（默认 120 列宽屏）；size 在挂载前可能为 (0, 0)，安全读取
        width = self._app_state.msglog.size[0] or 120
        self._header().set_width(width)
        # Spinner 80ms 动画定时器（参照 dsh-TUI SpinnerAnimationRow）
        self._spinner_timer = self.set_interval(0.08, self._tick_spinner)
        self._banner()
        self._start_laya_warmup()
        self._run_async(self._restore_active_container())
        self._update_footer("idle")

    async def _restore_active_container(self) -> None:
        try:
            chat = self._get_chat_app()
            container_id = chat.container_id()
            row = chat.store.get(container_id) if container_id else None
            if row:
                self._header().set_session_name(row.display_title)
                self._update_project_label(container_id)
                messages = chat.store.messages(container_id, limit=100)
                if messages:
                    self._render_container_history(container_id)
        except Exception as exc:
            self._msglog().add_meta(f"无法恢复当前对话信息：{type(exc).__name__}: {exc}")

    def _start_laya_warmup(self) -> None:
        """Start the paired GGUF server and warm its head before accepting chat input."""
        import os

        if os.getenv("PYTEST_CURRENT_TEST"):
            return
        from runtime.laya_tui_startup import tui_laya_autostart_enabled

        if not tui_laya_autostart_enabled():
            return
        self._bar().input.disabled = True
        self._msglog().add_meta("正在启动并预热 Laya 图形处理器模型，请稍候…")
        self._run_async(self._prepare_laya_backend())

    async def _prepare_laya_backend(self) -> None:
        import asyncio

        try:
            from runtime.laya_tui_startup import prepare_laya_for_tui

            _ready, message = await asyncio.to_thread(prepare_laya_for_tui)
            if self._app_state is not None:
                self._msglog().add_meta(message)
        except Exception as exc:
            if self._app_state is not None:
                self._msglog().add_meta(
                    f"Laya 预热失败：{type(exc).__name__}: {exc}；对话将继续走主 Agent。"
                )
        finally:
            if self._app_state is not None:
                try:
                    self._bar().input.disabled = False
                    self._bar().focus_input()
                except (NoActiveAppError, NoScreen):
                    pass

    def _tick_spinner(self) -> None:
        """每 80ms 驱动 Spinner 动画帧推进。"""
        spinner = self._app_state.spinner if self._app_state else None
        if spinner is not None:
            spinner.tick()

    def _stop_spinner(self) -> None:
        """停止 spinner 动画（turn 结束/失败/中断时调用）。"""
        spinner = self._app_state.spinner if self._app_state else None
        if spinner is not None:
            spinner.stop()
            spinner.display = False

    def _banner(self) -> None:
        msglog = self._msglog()
        msglog.add_meta("◆ FORGE · 全能助手终端界面")
        mode_label = {
            "stream": "流式",
            "async": "异步",
            "sync": "同步",
            "blocking": "阻塞",
        }.get(self.mode, self.mode)
        msglog.add_meta(f"  会话：{self.session_name}   ·   模式：{mode_label}")
        msglog.add_meta("  /help 查看命令   |   Ctrl+Q 退出   |   Ctrl+O 运行检查")
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
        # 若用户已按 m 打开 popup（fetch 时 models 为空、body 是占位提示），
        # 拉取完成后把真实模型列表同步进已打开的 popup，否则一直显示"(未加载模型)"
        if popup.is_visible():
            popup.show(picker._models, picker._current)

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
            if mid in seen:
                continue
            # 只保留可对话的 LLM：网关里 agnes-image-* / agnes-video-* 是
            # 图像/视频生成模型，不能当对话 LLM 选；local 的 GGUF 全保留。
            if src == "gateway" and ("image" in mid or "video" in mid):
                continue
            seen.add(mid)
            merged.append((mid, src))
        return merged

    def _model_picker(self):
        return self._app_state.model_picker if self._app_state else None

    def _model_popup(self):
        return self._app_state.model_popup if self._app_state else None

    def _api_settings(self):
        return self._app_state.api_settings if self._app_state else None

    def _apply_api_settings(self, values: dict[str, str]) -> None:
        """把用户改的 API/网关配置写回 os.environ + 清 agent 缓存 + 持久化 .env。

        网关 provider 按 (OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_USE_RESPONSES)
        做 cache-key、本地 provider 按 (FORGE_LOCAL_MODEL_NAME, ..._BASE_URL, ..._API_KEY)
        做 cache-key（见 agent.py::_gw_config / _local_config）。改完 env 后把对应缓存
        置 None，下一次 run_turn 调用时自动按新 key 重建 Provider——不必重启进程。
        """
        import os
        # 1) 写回环境变量
        env_map = {
            "OPENAI_API_KEY": "OPENAI_API_KEY",
            "OPENAI_BASE_URL": "OPENAI_BASE_URL",
            "FORGE_LOCAL_MODEL_NAME": "FORGE_LOCAL_MODEL_NAME",
            "FORGE_LOCAL_MODEL_BASE_URL": "FORGE_LOCAL_MODEL_BASE_URL",
        }
        for panel_key, env_key in env_map.items():
            if panel_key in values:
                os.environ[env_key] = values[panel_key]
        # 2) 清 agent.py 的两组 provider/agent 缓存，让新 key 生效
        try:
            import agent as _agent_mod
            _agent_mod._GW_PROVIDER_CACHE = None
            _agent_mod._GW_PROVIDER_KEY = None
            _agent_mod._ASSISTANT_AGENT_CACHE = None
            _agent_mod._ASSISTANT_AGENT_KEY = None
            _agent_mod._LOCAL_PROVIDER_CACHE = None
            _agent_mod._LOCAL_PROVIDER_KEY = None
        except Exception:
            pass
        # 3) 持久化到 .env（只更新这几个键，不覆盖其它行）
        self._persist_env_to_dotenv(values)
        # 4) 重拉一次模型列表，让 ModelPicker 反映新的 key/url 可用模型
        self._run_async(self._fetch_models())
        self._msglog().add_meta(
            "已更新 API/网关设置 → 新配置下次 run_turn 立即生效"
        )

    def _persist_env_to_dotenv(self, values: dict[str, str]) -> None:
        """把 4 个键写进项目根 .env（存在则替换、缺失则追加；保持其余行不变）。"""
        try:
            from runtime_paths import PROJECT_ROOT

            env_path = PROJECT_ROOT / ".env"
            lines: list[str] = []
            if env_path.exists():
                with open(env_path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            keys = list(values.keys())
            touched: set[str] = set()
            out: list[str] = []
            for ln in lines:
                stripped = ln.strip()
                hit = None
                if stripped:
                    for k in keys:
                        if stripped.startswith(k + "="):
                            hit = k
                            break
                if hit:
                    out.append(f"{hit}={values.get(hit, '')}")
                    touched.add(hit)
                else:
                    out.append(ln)
            for k in keys:
                if k not in touched:
                    out.append(f"{k}={values.get(k, '')}")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(out) + "\n")
        except Exception:
            # 写 .env 失败不阻塞主流程（env 已改、缓存已清，运行时仍生效）
            pass

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
        # 首次打开且模型未加载 → 先拉。拉到前 popup 显示「加载中」占位，
        # 避免拿空列表 show() 导致没有可选项、Enter 确认落空。
        if not picker._models:
            picker.set_loading(True)
            # 占位：用一个加载提示让 popup 至少能显示，等 _fetch_models 回来再刷真列表
            popup.show([(picker._current or "(加载中…)", "loading")], picker._current)
            self._model_picker_visible = True
            self._run_async(self._fetch_models())
            return
        popup.show(picker._models, picker._current)
        self._model_picker_visible = True

    def _on_model_selected(self, model_id: str, source: str) -> None:
        """ModelPopup 选中后回调：设 AGENT_MODEL 并更新状态栏。"""
        import os
        os.environ["AGENT_MODEL"] = model_id
        # 切到本地模型时，FORGE_MODEL_PREF 也要跟着；同时更新 FORGE_LOCAL_MODEL_NAME
        if source == "local":
            os.environ["FORGE_MODEL_PREF"] = "local"
            # 本地模型的 model name 是 FORGE_LOCAL_MODEL_NAME（不是 AGENT_MODEL）
            os.environ["FORGE_LOCAL_MODEL_NAME"] = model_id
            # 使 local_model_provider 缓存失效（key 变了会重建 Provider）
            import agent as _agent_mod
            _agent_mod._LOCAL_PROVIDER_KEY = None
            _agent_mod._LOCAL_PROVIDER_CACHE = None
        else:
            os.environ["FORGE_MODEL_PREF"] = "gateway"
        picker = self._model_picker()
        if picker:
            picker.set_current(model_id)
        self._header().set_model(model_id)
        source_label = {"gateway": "网关", "local": "本地"}.get(source, source)
        self._msglog().add_meta(f"已切换模型 → {model_id}（{source_label}）")
        self._model_picker_visible = False

    def _on_model_popup_closed(self) -> None:
        """ModelPopup 的 cancel/confirm 后回调：清 flag + 焦点还给 Input。"""
        self._model_picker_visible = False
        try:
            self._bar().focus_input()
        except (NoScreen, NoActiveAppError):
            pass

    # ── 输入：slash / 普通 / approval ─────────────────────────
    def on_input_submitted(self, event) -> None:
        text = event.value.strip()
        sidebar = self._app_state.sidebar if self._app_state else None
        if sidebar is not None and sidebar.is_visible():
            event.stop()
            event.input.clear()
            self._on_session_select()
            return
        # ── popup 拦截（必须 event.stop() 在最前，防止 Input widget 继续消费 Enter）──
        # 模型 popup / slash popup 打开时焦点仍在 Input（Static 不可聚焦，
        # 键盘事件由 App 级 key_enter/key_up/key_down 处理）。
        # 此时按 Enter 应是"确认选择"而非"提交输入"，必须先 stop 再 confirm。
        if self._model_picker_visible:
            popup = self._model_popup()
            if popup is not None and popup.is_visible():
                event.stop()
                event.input.clear()
                popup.confirm()
                return
        if self._popup_visible:
            popup = self._popup()
            if popup is not None:
                event.stop()
                event.input.clear()
                sel = popup.selected()
                if sel:
                    self._bar().input.value = sel[0] + " "
                    self._hide_slash_popup()
                return
        # 空输入：clear 后直接返回（不 stop，让 Input 正常处理）
        if not text:
            event.input.clear()
            return
        event.input.clear()

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
        command, _, argument = text.strip().partition(" ")
        command = command.lower()
        argument = argument.strip()
        if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in "\"'":
            argument = argument[1:-1]
        if command in ("/sessions", "/session"):
            self._show_session_list()
            return
        if command == "/artifacts":
            self._run_async(self._show_artifacts(argument))
            return
        if command == "/new":
            self._run_async(self._create_new_chat(argument or "新对话"))
            return
        if command == "/switch":
            if not argument:
                self._show_session_list()
                return
            try:
                container_id = self._get_chat_app().store.resolve(argument)
                if not container_id:
                    raise ValueError(f"找不到会话：{argument}")
                self._run_async(self._switch_session(container_id))
            except Exception as exc:
                self._msglog().add_error(title="切换对话失败", detail=str(exc))
            return
        if command == "/attach":
            self._run_async(self._choose_and_attach_file(argument or None))
            return
        if command == "/detach":
            self._run_async(self._discard_pending_attachments())
            self._msglog().add_meta("已移除所有尚未发送的附件。")
            return
        if command == "/project":
            self._run_async(self._choose_and_create_project(argument or None))
            return
        if command in ("/api", "/api-settings", "/gateway"):
            self._toggle_api_settings()
            return
        self._run_async(self._dispatch_cmd(text))

    def _toggle_api_settings(self) -> None:
        """打开 / 关闭 API/网关设置面板（首次打开时拉取现有值填充）。"""
        panel = self._api_settings()
        if panel is None:
            return
        if panel.is_visible():
            panel.hide()
            return
        # patch 回调：面板不认识 App，挂载时未接，这里兜底接一次
        panel.attach(self._apply_api_settings, lambda: None)
        panel.show()
        self._update_footer("idle")

    async def _dispatch_cmd(self, text: str) -> None:
        chat = self._get_chat_app()
        # TUI 里 app.print() 写到 stdout（Textual 不显示），
        # 用 StringIO 捕获命令输出，执行后转到 msglog 显示
        import io
        buf = io.StringIO()
        orig_stream = chat.stream
        chat.stream = buf
        try:
            await chat.dispatch(text)
        except Exception as exc:
            self._msglog().add_error(title="命令执行失败", detail=str(exc))
        finally:
            chat.stream = orig_stream
        captured = buf.getvalue().strip()
        if captured:
            # 逐行显示到 msglog（去掉 ANSI 转义码）
            import re
            clean = re.sub(r'\x1b\[[0-9;]*m', '', captured)
            for line in clean.splitlines():
                if line.strip():
                    self._msglog().add_meta(line)

    async def _show_artifacts(self, limit_text: str = "") -> None:
        """Render the current conversation's registered artifacts as actions."""
        chat = self._get_chat_app()
        try:
            limit = max(1, min(int(limit_text), 200)) if limit_text else 20
        except ValueError:
            limit = 20
        try:
            artifacts = await asyncio.to_thread(
                chat.store.artifacts, chat.session_name, limit,
            )
        except Exception as exc:
            self._msglog().add_error(title="无法加载生成文件", detail=str(exc))
            return
        if not artifacts:
            self._msglog().add_meta("当前对话还没有登记的生成文件。")
            return
        self._msglog().add_meta(f"当前对话的生成文件（{len(artifacts)} 个）：")
        for artifact in artifacts:
            self._msglog().add_artifact(artifact)

    def _load_registered_artifact(self, artifact_id: str):
        """Load by database ID and confine the path to Runtime artifact roots."""
        import re

        from cli.tui.artifacts import resolve_registered_artifact

        if not re.fullmatch(r"art_[A-Za-z0-9]+", artifact_id or ""):
            raise ValueError("生成文件编号无效。")
        chat = self._get_chat_app()
        artifact = chat.store.mgr.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError("这个生成文件已不在登记列表中。")
        if str(artifact.get("session_id") or "") != str(chat.session_name or ""):
            raise ValueError("只能打开当前对话登记的生成文件。")
        roots = getattr(chat.runtime, "artifact_dirs", ()) or ()
        path = resolve_registered_artifact(artifact, roots)
        return artifact, path

    async def _open_registered_artifact(self, artifact_id: str, *, reveal: bool) -> None:
        from cli.tui.artifacts import (
            TEXT_PREVIEW_SUFFIXES,
            open_artifact,
            read_artifact_preview,
            reveal_artifact,
        )

        try:
            artifact, path = await asyncio.to_thread(
                self._load_registered_artifact, artifact_id,
            )
            if reveal:
                await asyncio.to_thread(reveal_artifact, path)
                self._msglog().add_meta(f"已在文件夹中定位：{artifact['name']}")
            elif path.suffix.lower() in TEXT_PREVIEW_SUFFIXES:
                content, _truncated = await asyncio.to_thread(read_artifact_preview, path)
                self.push_screen(ArtifactPreviewScreen(
                    str(artifact.get("name") or path.name), str(path), content,
                ))
            else:
                await asyncio.to_thread(open_artifact, path)
                self._msglog().add_meta(f"已用系统默认程序打开：{artifact['name']}")
        except Exception as exc:
            self._msglog().add_error(
                title="打开生成文件失败",
                detail=f"{type(exc).__name__}: {exc}",
                hints=["检查文件是否仍存在，以及是否位于 Agent 的产物目录中。"],
            )

    # ── 普通消息 ────────────────────────────────────────────────
    def _submit_text(self, text: str) -> None:
        if self._turn_active:
            self._msglog().add_meta("当前任务仍在运行；请等待完成或按 Esc 取消后再发送。")
            return
        display_text = text
        if self._pending_attachments:
            names = [str(item.get("display_name") or "附件")
                     for item in self._pending_attachments]
            display_text += "\n📎 " + "、".join(names)
        self._msglog().add_user(display_text)
        self._stream_buf = ""
        self._stream_item = None
        self._last_tool_item = None
        self._streamed_this_turn = False
        # 新一轮 turn 开始：重置工具分组状态
        self._tool_group = None
        self._tool_rows.clear()
        self._tool_started_at.clear()
        self._tool_row_idx.clear()
        self._header().set_state("running")
        self._update_footer("running")
        self._set_turn_active(True)
        # 真实 spinner：braille 动画 80ms/帧 + LLM/工具状态文案
        spinner = self._app_state.spinner if self._app_state else None
        if spinner is not None:
            spinner.start("LLM 推理中…")
            spinner.display = True
        self._run_async(self._run_turn(text))

    def _set_turn_active(self, active: bool) -> None:
        self._turn_active = bool(active)
        try:
            bar = self._bar()
            if bar.input is not None:
                # 审批暂停时必须保留输入，以便用户提交 Y/N/D。
                bar.input.disabled = self._turn_active and not self._in_approval
        except (AttributeError, NoScreen, NoActiveAppError):
            pass

    async def _cancel_active_turn(self) -> None:
        if self._cancel_pending:
            return
        self._cancel_pending = True
        try:
            runtime = self._get_chat_app().runtime
            run_id = getattr(runtime, "_active_run_id", None)
            if not run_id:
                self._msglog().add_meta("当前运行任务尚未登记或已经结束，未发送取消请求。")
                return
            import asyncio

            cancelled = await asyncio.to_thread(runtime.cancel_run, run_id)
            if cancelled:
                self._msglog().add_meta(f"已请求运行时取消任务 {run_id}，等待最终状态…")
            else:
                self._msglog().add_meta(f"任务 {run_id} 已不在执行，取消请求未生效。")
        except Exception as exc:
            self._msglog().add_error(title="取消失败", detail=f"{type(exc).__name__}: {exc}")
        finally:
            self._cancel_pending = False

    def _run_async(self, coro) -> None:
        try:
            self.run_worker(coro, exclusive=False)
        except (NoActiveAppError, NoScreen):
            # 无活跃 Textual app（如纯同步测试环境）时静默忽略
            import logging
            logging.getLogger(__name__).debug("run_worker skipped: no active app")

    async def _run_turn(self, text: str) -> None:
        chat = self._get_chat_app()
        attachment_ids = [
            str(item.get("id") or "") for item in self._pending_attachments
            if item.get("id")
        ]
        container_id = getattr(chat, "container_id", None)
        task_container_id = container_id() if callable(container_id) else None
        self.turns += 1
        started = time.monotonic()

        def tui_event_cb(channel: str, payload) -> None:
            # 更新 spinner 文案：activity 事件到达时显示当前阶段
            if channel == "activity":
                etype = str(payload.get("type") or "")
                label = str(payload.get("label") or etype)
                spinner = self._app_state.spinner if self._app_state else None
                if spinner is not None and spinner._active:
                    spinner.set_message(f"{label}…")
            self._on_tui_event(channel, payload)

        try:
            result = await chat.runtime.run_turn(
                text,
                session=chat.session,
                session_id=chat.session_name,
                task_container_id=task_container_id,
                mode=chat.mode,
                debug=chat.debug,
                max_turns=chat.max_turns,
                history_limit=chat.history_limit,
                metadata={"channel": "tui", "attachment_ids": attachment_ids},
                raise_on_error=False,
                context_guard=chat.auto_summary,
                stream_events_cb=tui_event_cb,
            )
        except KeyboardInterrupt:
            self._stream_item = None
            self._stream_buf = ""
            self._header().set_state("idle")
            self._stop_spinner()
            self._msglog().add_error(title="已中断", detail="本地等待已中断，后端任务可能仍在执行。")
            self._update_footer("idle")
            self._set_turn_active(False)
            return
        except Exception as exc:
            self._header().set_state("failed")
            self._stop_spinner()
            self._render_exception(exc)
            self._update_footer("idle")
            self._set_turn_active(False)
            return
        finally:
            # Only consume files the Runtime actually bound to this user message.
            if attachment_ids:
                manager = chat.store.mgr
                consumed: set[str] = set()
                for attachment_id in attachment_ids:
                    try:
                        row = manager.get_message_attachment(attachment_id)
                        if row and row.get("message_id") is not None:
                            consumed.add(attachment_id)
                    except Exception:
                        pass
                self._pending_attachments = [
                    item for item in self._pending_attachments
                    if str(item.get("id") or "") not in consumed
                ]
                self._update_attachment_button()

        # 审批：Runtime 明确返回 waiting_approval=True 才进入审批态
        if getattr(result, "waiting_approval", False):
            self._stop_spinner()
            self._enter_approval(result)
            return

        self._stop_spinner()
        self._render_result(result, started)
        self._set_turn_active(False)

    async def _resolve_approvals(self, chat, result):
        """TUI 专用：用户已通过 Y/N 键做出决定，直接调 decide_approval 后续跑，
        不走 _resolve_approvals_interactive（那个有 input() 同步阻塞，TUI 里永远卡死）。"""
        from runtime.runner import RunResult

        def _tui_cb(channel: str, payload) -> None:
            """轻量事件回调：复用 App 的 _on_tui_event 渲染逻辑。"""
            try:
                self._on_tui_event(channel, payload)
            except Exception:
                pass

        current = result
        for _round in range(5):
            if not isinstance(current, RunResult) or not current.waiting_approval:
                return current
            task = current.task
            approvals = current.approvals or chat.runtime.tasks.list_pending_approvals(task.id)
            if not approvals:
                return current
            # 批准全部 pending 审批（用户已按 Y）
            for ap in approvals:
                try:
                    chat.runtime.tasks.decide_approval(
                        ap["id"], "approved", actor="user"
                    )
                except Exception:
                    pass
            # 续跑同一任务（带 task_id 恢复）
            current = await chat.runtime.run_turn(
                task.goal,
                session=chat.session,
                session_id=task.session_id,
                mode=chat.mode,
                debug=chat.debug,
                max_turns=chat.max_turns,
                history_limit=chat.history_limit,
                task_id=task.id,
                metadata={"channel": "tui"},
                raise_on_error=False,
                stream_events_cb=_tui_cb,
            )
            if not current.ok and current.error:
                return current
        return current

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
        self._set_turn_active(False)
        approvals = getattr(result, "approvals", []) or []
        self._pending_approvals = list(approvals)
        self._waiting_result = result  # 保存 RunResult，供批准后 resume
        self._header().set_state("waiting")
        self._update_footer("approval")
        for ap in approvals:
            desc = str(ap.get("description") or ap.get("label") or "高风险操作")
            self._msglog().add_error(
                title="◆ 需要审批",
                detail=desc,
                hints=["Y 批准", "N 拒绝", "D 查看详情"],
            )
        self._bar().set_placeholder("需要确认：[Y] 批准  [N] 拒绝  [D] 查看详情")
        self._bar().focus_input()

    def _exit_approval(self) -> None:
        """仅在 Runtime 明确结束审批时调用（waiting_approval=False）。"""
        self._in_approval = False
        self._set_turn_active(False)
        self._pending_approvals = []
        self._bar().set_placeholder("Enter 发送；输入 / 查看命令；Ctrl+Q 退出")
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
            self._set_turn_active(True)
            self._header().set_state("running")
            self._update_footer("running")
            self._bar().set_placeholder("执行中…")
            self._msglog().add_meta("✓ 已批准，继续执行…")
            self._run_async(self._continue_after_approval(approved=True))

        elif lower in ("n", "no"):
            # 拒绝：Runtime 标记 cancelled
            self._in_approval = False
            self._set_turn_active(False)
            self._pending_approvals = []
            self._header().set_state("idle")
            self._update_footer("idle")
            self._bar().set_placeholder("Enter 发送；输入 / 查看命令；Ctrl+Q 退出")
            self._msglog().add_meta("× 已拒绝，任务已停止。")
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
            self._msglog().add_meta(f"  [补充说明] {text}")
            self._msglog().add_meta("  （审批仍待处理，请输入 Y / N / D）")
            # _in_approval 保持 True，header/footer 不变
            self._bar().focus_input()

    async def _continue_after_approval(self, approved: bool = True) -> None:
        """批准后调用 Runtime 继续执行；拒绝后仅清理。"""
        if not approved:
            self._exit_approval()
            return

        result = self._waiting_result
        if result is None:
            self._msglog().add_meta("（未找到等待审批的任务，已忽略）")
            self._exit_approval()
            return
        self._waiting_result = None  # 消费后清除，防重复

        chat = self._get_chat_app()
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
                    self._streamed_this_turn = True

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
                    _row_idx = self._tool_group.add_tool(label, detail="", status="running")
                    if isinstance(_row_idx, int):
                        self._tool_row_idx[aid] = _row_idx
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
                    # 用终态 label 覆盖之前 running 的那条（_tool_rows 内部状态）
                    prev = self._tool_rows.get(aid)
                    if prev:
                        self._tool_rows[aid] = (prev[0], prev[1], status, elapsed)
                    if self._tool_group is not None:
                        _row_idx = self._tool_row_idx.pop(aid, None)
                        if _row_idx is not None:
                            # 原地更新（不追加新行）——修复：同一工具不再显示两行
                            self._tool_group.set_tool(
                                _row_idx, label, detail="", status=status, elapsed=elapsed,
                            )
                        else:
                            # 兼容：add_tool 未返回索引（旧版本）时追加
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
                    if self._tool_group is not None:
                        self._tool_group.collapse()
                        self._tool_group = None
                    self._tool_rows.clear()
                    self._tool_row_idx.clear()
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
                    # 最终错误由 _render_result / _render_exception 统一显示；
                    # 此处只更新状态，避免同一失败事件与 RunResult 各显示一遍。
                elif etype == "run.waiting_approval":
                    # 事件流里也可能收到（Runtime 中途暂停）
                    if not self._in_approval:
                        approvals = meta.get("approvals") or []
                        self._pending_approvals = list(approvals)
                        self._in_approval = True
                        self._header().set_state("waiting")
                        self._update_footer("approval")
                        self._bar().set_placeholder("需要确认：[Y] 批准  [N] 拒绝")
                        self._bar().focus_input()

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
        streamed_item = self._stream_item if self._streamed_this_turn else None

        # 强制 flush 流式 buffer
        if self._stream_item:
            msglog.stream_end()
            self._stream_item = None

        reply = coerce_reply(getattr(result, "final_output", None))
        if isinstance(reply, AgentReply):
            content = _clean_display_text(reply.content or "")
            kind_label = _KIND_LABELS.get(reply.kind, reply.kind)
            if content:
                # 若本轮已流式渲染过内容，跳过重复的 add_assistant
                # （stream_end 已经把完整内容写进 _stream_item，
                #  再 add 一次会导致同一回答显示两次）
                if not self._streamed_this_turn:
                    msglog.add_assistant(content, title=kind_label)
                else:
                    # 把最终类型标签合并到原流式消息头，避免回答结束后再出现
                    # 一条孤立的「FORGE · 回答」元信息，看起来像重复输出。
                    if streamed_item is not None and kind_label:
                        streamed_item.set_title(kind_label)
            else:
                msglog.add_meta(f"✓ 完成 · {kind_label}")
            if reply.summary:
                msglog.add_meta(f"📌 {_escape_text(reply.summary)}")
            artifacts = [
                artifact for artifact in (getattr(result, "artifacts", []) or [])
                if isinstance(artifact, dict) and artifact.get("id")
            ]
            if reply.saved_file:
                saved_label = str(reply.saved_file).strip().replace("\\", "/").lower()
                saved_basename = saved_label.rsplit("/", 1)[-1]
                is_registered = any(
                    saved_label == str(artifact.get("storage_path") or "").replace("\\", "/").lower()
                    or saved_basename == str(artifact.get("name") or "").replace("\\", "/").lower()
                    for artifact in artifacts
                )
                if not is_registered:
                    # A model-provided saved_file string is display-only. Only
                    # database-registered artifacts receive open actions.
                    msglog.add_artifact(str(reply.saved_file))
            if reply.next_step:
                msglog.add_meta(f"➡ 下一步：{_escape_text(reply.next_step)}")
        elif reply:
            text = str(reply)
            if text.strip():
                if not self._streamed_this_turn:
                    msglog.add_assistant(text)

        error_text = str(getattr(result, "error", "") or "").strip()
        if getattr(result, "ok", True) is False or error_text:
            msglog.add_error(title=error_text[:400] or "本轮未成功")

        for artifact in (getattr(result, "artifacts", []) or []):
            if isinstance(artifact, dict) and artifact.get("id"):
                msglog.add_artifact(artifact)

        task_id = getattr(getattr(result, "task", None), "id", "") or "?"
        outcome = str(getattr(result, "outcome", "") or "")
        status_label = {
            "partial": "! 部分完成",
            "blocked": "× 已阻止",
            "failed": "× 失败",
            "cancelled": "■ 已取消",
            "completed": "✓ 已完成",
            "needs_user": "? 需要你处理",
        }.get(outcome, "✓ 已完成" if getattr(result, "ok", True) else "× 失败")
        msglog.add_meta(f"{status_label} · {elapsed:.1f}秒 · 运行编号 {task_id[:8]}")

        if self.auto_summary:
            chat = self._get_chat_app()
            self._pending_compact = chat.session

        self._exit_approval()  # 安全：result 正常完成时清除（若仍有 pending）
        header_state = {
            "completed": "idle",
            "needs_user": "needs_user",
            "partial": "partial",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(outcome)
        if header_state is None:
            header_state = "idle" if getattr(result, "ok", True) else "failed"
        self._header().set_state(header_state)
        self._update_footer("idle")
        self._bar().set_placeholder("Enter 发送；输入 / 查看命令；Ctrl+Q 退出")
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
        # API 设置面板打开时：Enter = 保存
        api = self._api_settings()
        if api is not None and api.is_visible():
            api.save()
            self._bar().focus_input()
            self._update_footer("idle")
            return
        # ModelPopup 的确认键（on_input_submitted 已优先拦截 popup 场景，
        # 这里是兜底：焦点确实在 popup body 上的路径也在此 confirm）
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
        # API 设置面板打开时：Esc = 取消并关闭
        api = self._api_settings()
        if api is not None and api.is_visible():
            api.cancel()
            self._bar().focus_input()
            self._update_footer("idle")
            return
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

        # 左侧对话栏打开时：↑↓选择会话，Enter 真实切换，Esc 收起。
        sidebar = self._app_state.sidebar if self._app_state else None
        if sidebar is not None and sidebar.is_visible():
            if key == "down":
                sidebar.select_next()
            elif key == "up":
                sidebar.select_prev()
            elif key == "enter":
                self._on_session_select()
            elif key == "delete":
                sidebar.toggle_selected_actions()
            elif key == "escape":
                if not sidebar.close_actions():
                    sidebar.hide()
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
            # 无历史时不翻（len([])-1 = -1 会让 _hist_idx=-1 越界 IndexError）
            if not self._history:
                return
            # InputBar.input 是懒挂载的子 widget；刚启动、焦点已落到 InputBar
            # 但 Input 尚未 mount 完成时为 None，此时改值会让 .value 赋值崩溃。
            _in = self._bar().input
            if key == "up":
                # 归位：如果 hist_idx 超出范围（初始状态），设到末尾
                if self._hist_idx >= len(self._history):
                    self._hist_idx = len(self._history) - 1
                else:
                    self._hist_idx = max(0, self._hist_idx - 1)
                if _in is not None:
                    _in.value = self._history[self._hist_idx]
            elif key == "down":
                if self._hist_idx < len(self._history) - 1:
                    self._hist_idx += 1
                    if _in is not None:
                        _in.value = self._history[self._hist_idx]
                else:
                    # 已到末尾（新输入位置），清空输入框
                    self._hist_idx = len(self._history)
                    if _in is not None:
                        _in.value = ""

        # 4. Esc 优先级
        if key == "escape":
            if self._in_approval:
                # 审批态 Esc：显示提示，不解除 pending
                self._msglog().add_meta("（审批未处理，Esc 不生效；请输入 Y / N / D）")
            elif self._header()._run_state == "running":
                # 运行中：请求 Runtime 取消真实 Run，并等待取消终态收口。
                self._run_async(self._cancel_active_turn())

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

    # ── 底部提示（上下文感知）──────────────────────────────────
    def _update_footer(self, mode: str) -> None:
        """切换底部上下文提示：idle / running / approval / slash / hist_search。

        挂载前调用只记录 mode，挂载后会立刻补一次（见 on_mount），
        保证任何时序下提示都不会停留在空串。
        """
        self._footer_mode = mode
        hints = {
            "idle": "Enter 发送   / 查看命令   F1 搜索历史   Ctrl+S 会话   m 切换模型   Ctrl+O 检查   Ctrl+L 清屏   Ctrl+Q 退出",
            "running": "Esc 请求取消   Ctrl+Q 退出",
            "approval": "Y 批准   N 拒绝   D 查看详情",
            "slash": "↑↓ 选择   Tab 补全   Enter 执行   Esc 关闭",
            "hist_search": "↑↓ 选择   Enter 填入   F1/Esc 关闭",
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
            state = str(latest.get("state", "—"))
            data["state"] = {
                "idle": "就绪",
                "running": "运行中",
                "waiting_approval": "等待审批",
                "needs_user": "需要用户处理",
                "completed": "已完成",
                "failed": "失败",
                "blocked": "已阻止",
                "partial": "部分完成",
                "cancelled": "已取消",
            }.get(state, state)
            elapsed_s = info.get("elapsed_s")
            data["elapsed"] = f"{elapsed_s} 秒" if elapsed_s is not None else "—"
            events = info.get("events") or []
            event_labels = {
                "run.started": "运行开始",
                "run.completed": "运行完成",
                "run.failed": "运行失败",
                "run.cancelled": "运行取消",
                "run.waiting_approval": "等待审批",
                "tool.started": "工具开始",
                "tool.completed": "工具完成",
                "tool.failed": "工具失败",
                "assistant_delta": "回答生成中",
            }
            data["events"] = [
                f"{event_labels.get(str(e.get('type', '?')), '事件')}  "
                f"{str(e.get('at', ''))[:19].replace('T', ' ')}"
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
            if self._chat_app is not None and self._pending_attachments:
                manager = self._chat_app.store.mgr
                for attachment in self._pending_attachments:
                    manager.delete_message_attachment(str(attachment.get("id") or ""))
                self._pending_attachments.clear()
        except Exception:
            pass
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


def _native_path_dialog(kind: str) -> str:
    """打开系统文件/文件夹选择器；必须在线程中调用，避免阻塞 Textual 事件循环。"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update_idletasks()
        if kind == "file":
            selected = filedialog.askopenfilename(parent=root, title="选择要添加的文件")
        elif kind == "directory":
            selected = filedialog.askdirectory(parent=root, title="选择项目文件夹", mustexist=True)
        else:
            raise ValueError(f"不支持的选择器类型：{kind}")
        return str(selected or "")
    finally:
        root.destroy()


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
        async def _run_with_mcp_lifecycle() -> None:
            try:
                await app.run_async()
            finally:
                try:
                    import asyncio
                    from runtime.laya_tui_startup import stop_tui_owned_laya_server

                    await asyncio.to_thread(stop_tui_owned_laya_server)
                finally:
                    from integrations.mcp_bridge import close_servers

                    await close_servers()

        import asyncio

        asyncio.run(_run_with_mcp_lifecycle())
        return 0
    except KeyboardInterrupt:
        print("\n已中断，退出。")
        return 130
