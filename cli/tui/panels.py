"""FORGE TUI 面板组件（Textual 8.x 专业化）。

所有组件为纯渲染层，不持有 Agent 状态（状态由 ForgeTuiApp 管理）。

Textual 8.2.8 已知约束（设计基准）：
- Widget.classes 是 frozenset → 只能构造时传入，不能 .add()
- Widget.log 是只读 property → 自定义属性不能叫 log
- Log.__init__ 只接受 highlight / max_lines / auto_scroll
- Log.write() 没有 end 参数，只接受 str
- Widget 没有 _invalidate()，用 self.refresh()
- Widget 没有 scroll_to_end()，用 self.scroll_end()
"""

from __future__ import annotations

import time as _time

from rich.text import Text
from textual._context import NoActiveAppError
from textual.app import ComposeResult, NoScreen
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Input, Static
from textual.widget import MountError


# ── 设计令牌 ─────────────────────────────────────────────────────
BG       = "#0B0E14"
SURFACE  = "#10141C"
PANEL    = "#161B25"
BORDER   = "#293142"
TEXT_C   = "#DCE4F2"
MUTED    = "#778196"
PRIMARY  = "#5CC8FF"
AGENT    = "#A78BFA"
SUCCESS  = "#6EE7A8"
WARNING  = "#F6C177"
ERROR_C  = "#FF7185"
ARTIFACT = "#7DD3FC"


def _tool_symbol(status: str) -> str:
    return {"done": "✓", "running": "◌", "failed": "×"}.get(status, "·")


def _escape(text: str) -> str:
    """转义 Rich markup 特殊字符，避免用户输入触发 markup 渲染错误。"""
    return text.replace("[", "【").replace("]", "】")


# ── 面板：顶部状态栏 ──────────────────────────────────────────────
class StatusHeader(Static):
    """◆ FORGE + session + state（必填）+ model + tools + elapsed（可选）。

    响应式：根据 terminal width 按优先级裁减字段：
    优先级 1（始终保留）: FORGE / session / state
    优先级 2（width ≥ 60）: model
    优先级 3（width ≥ 80）: tool count / elapsed

    用 `set_*` 方法更新字段，触发 self.refresh()。
    """

    # 各字段最小列宽估算
    _BASE_MIN = 25   # "◆ FORGE  personal  ● READY"  大约 25 列
    _MODEL_MIN = 15  # "  GPT-5.6"
    _TOOLS_MIN = 10  # "  4 tools"

    def __init__(self, session_name: str = "personal") -> None:
        self._session_name = session_name
        self._model_name: str = ""
        self._tool_count: int = 0
        self._run_state: str = "idle"
        self._elapsed: str = ""
        self._width: int = 120  # 默认宽屏
        self._text = self._build_text()
        super().__init__(self._text)

    def set_width(self, width: int) -> None:
        """App 布局时调用，传入 MessageLog 可用宽度。"""
        self._width = max(40, width)
        self._refresh()

    def _build_text(self) -> Text:
        state = self._run_state
        color = {
            "idle": PRIMARY,
            "running": WARNING,
            "waiting": AGENT,
            "failed": ERROR_C,
        }.get(state, MUTED)
        state_label = {
            "idle": "● READY",
            "running": "◌ RUNNING",
            "waiting": "◆ APPROVAL",
            "failed": "× ERROR",
        }.get(state, f"· {state.upper()}")

        # 始终保留：FORGE + session + state
        w = self._width
        t = Text()
        t.append("◆ FORGE", style=f"bold {PRIMARY}")
        t.append(f"  {self._session_name}", style=MUTED)

        # 优先级 2：model（width ≥ 50 时显示）
        if self._model_name and w >= 50:
            t.append(f"  {self._model_name}", style=MUTED)

        # 优先级 3：tool count + elapsed（width ≥ 60）
        if self._tool_count and w >= 60:
            t.append(f"  {self._tool_count} tools", style=MUTED)

        t.append(f"  {state_label}", style=f"bold {color}")
        if self._elapsed and state == "running" and w >= 80:
            t.append(f" {self._elapsed}", style=MUTED)

        return t

    def _refresh(self) -> None:
        """重建 Text 并刷新。widget 未挂载 / 无活跃 app 时只更新 _text。"""
        self._text = self._build_text()
        try:
            self.update(self._text)
        except (NoScreen, NoActiveAppError):
            pass  # widget 未挂载或无活跃 app，_text 已更新，下次挂载时生效

    def set_state(self, state: str, elapsed: str = "") -> None:
        self._run_state = state
        if elapsed:
            self._elapsed = elapsed
        self._refresh()

    def set_model(self, model: str) -> None:
        self._model_name = model
        self._refresh()

    def set_tool_count(self, n: int) -> None:
        self._tool_count = n
        self._refresh()

    def render(self) -> Text:
        return self._text


# ── 面板：消息条目 ────────────────────────────────────────────────
class ToolGroup(Static):
    """设计稿 B 里的「工具执行」分组块：组头 + 多条 tool 子项 + 折叠箭头。

    渲染规则：
    - 展开时：组头一行 + 每条 tool 子项各占一行
    - 折叠时：组头一行，子项全部收起（再点一次可展开）

    耗时来自后端 `metadata.elapsed_seconds`（tool.completed/failed 事件）。
    没有耗时的（tool.started 还没结束）不显示耗时，只显示「进行中」。
    """

    def __init__(self, title: str = "工具执行") -> None:
        self._title = title
        self._collapsed = False
        # 每条 tool 的状态行：(name, detail, status, elapsed)
        self._rows: list[tuple[str, str, str, float | None]] = []
        self._text = self._build_text()
        super().__init__(self._text)

    # ── 对外 API ──────────────────────────────────────────────
    def add_tool(self, name: str, detail: str = "", status: str = "done",
                 elapsed: float | None = None) -> None:
        """追加一条 tool 子项。status: running / done / failed。"""
        self._rows.append((name, detail, status, elapsed))
        self._refresh()

    def collapse(self) -> None:
        self._collapsed = True
        self._refresh()

    def expand(self) -> None:
        self._collapsed = False
        self._refresh()

    def toggle(self) -> None:
        if self._collapsed:
            self.expand()
        else:
            self.collapse()

    @property
    def item_count(self) -> int:
        return len(self._rows)

    # ── 渲染 ──────────────────────────────────────────────────
    @staticmethod
    def _tool_symbol(status: str) -> str:
        return {"done": "✓", "running": "◌", "failed": "×"}.get(status, "·")

    @staticmethod
    def _tool_color(status: str) -> str:
        return {"done": SUCCESS, "running": WARNING, "failed": ERROR_C}.get(status, MUTED)

    def _build_text(self) -> Text:
        t = Text()
        arrow = "▸" if self._collapsed else "▼"
        count = f"  · {len(self._rows)} 项" if self._rows else ""
        t.append(f"{arrow} ", style=MUTED)
        t.append(self._title, style=f"bold {PRIMARY}")
        t.append(count, style=MUTED)
        t.append("\n")

        if self._collapsed:
            return t

        for name, detail, status, elapsed in self._rows:
            symbol = self._tool_symbol(status)
            color = self._tool_color(status)
            t.append("  ┃ ", style=BORDER)
            t.append(f"{symbol} ", style=color)
            if name:
                t.append(_escape(name), style=f"bold {TEXT_C}")
            if detail:
                t.append(f"  {_escape(detail)}", style=MUTED)
            if elapsed is not None:
                t.append(f"  {elapsed:.1f}s", style=MUTED)
            elif status == "running":
                t.append("  进行中", style=MUTED)
            t.append("\n")
        return t

    def _refresh(self) -> None:
        self._text = self._build_text()
        try:
            self.update(self._text)
        except (NoScreen, NoActiveAppError):
            pass  # 未挂载时（单测）只更新 _text

    def render(self) -> Text:
        return self._text

class MessageItem(Static):
    """单条消息条目。kind 决定视觉呈现。

    kind: user / assistant / tool / error / artifact / meta
    """

    def __init__(
        self,
        kind: str = "user",
        title: str = "",
        content: str = "",
        status: str = "",
        detail: str = "",
    ) -> None:
        self._kind = kind
        self._title = title
        self._content = content
        self._status = status
        self._detail = detail
        self._text = self._build_text()
        super().__init__(self._text)

    def _build_text(self) -> Text:
        t = Text()
        k = self._kind

        if k == "user":
            t.append("YOU", style=f"bold {PRIMARY}")
            t.append("\n")
            for line in (self._content or "").splitlines() or [""]:
                t.append(f"  {_escape(line)}\n")

        elif k == "assistant":
            t.append("FORGE", style=f"bold {AGENT}")
            if self._title:
                t.append(f" · {_escape(self._title)}", style=MUTED)
            t.append("\n")
            for line in (self._content or "").splitlines() or [""]:
                t.append(f"  {_escape(line)}\n")

        elif k == "tool":
            symbol = _tool_symbol(self._status)
            color = {"done": SUCCESS, "running": WARNING, "failed": ERROR_C}.get(
                self._status, MUTED
            )
            t.append("┃ ", style=BORDER)
            t.append(f"{symbol} ", style=color)
            if self._title:
                t.append(_escape(self._title), style=f"bold {TEXT_C}")
            if self._content:
                t.append(f"  {_escape(self._content)}", style=MUTED)
            if self._detail:
                t.append(f"  {_escape(self._detail)}", style=MUTED)
            t.append("\n")

        elif k == "error":
            t.append("× ERROR", style=f"bold {ERROR_C}")
            t.append("\n")
            if self._title:
                t.append(f"  {_escape(self._title)}\n", style=ERROR_C)
            for line in (self._content or "").splitlines() or [""]:
                t.append(f"  {_escape(line)}\n", style=TEXT_C)
            if self._detail:
                t.append("  Possible fixes\n", style=MUTED)
                for hint in self._detail.split("\n"):
                    if hint.strip():
                        t.append(f"  ├─ {_escape(hint.strip())}\n", style=TEXT_C)

        elif k == "artifact":
            t.append(f"↳ {_escape(self._title or self._content)}", style=f"bold {ARTIFACT}")
            t.append("\n")

        elif k == "meta":
            t.append(_escape(self._content), style=MUTED)
            t.append("\n")

        else:
            t.append(_escape(self._content), style=TEXT_C)
            t.append("\n")

        return t

    def _refresh(self) -> None:
        self._text = self._build_text()
        try:
            self.update(self._text)
        except (NoScreen, NoActiveAppError):
            pass  # MessageItem 未挂载时

    def render(self) -> Text:
        return self._text

    def set_content(self, text: str) -> None:
        self._content = text
        self._refresh()

    def set_status(self, status: str, detail: str = "") -> None:
        self._status = status
        if detail:
            self._detail = detail
        self._refresh()


# ── 面板：消息流 ──────────────────────────────────────────────────
class MessageLog(VerticalScroll, can_focus=False):
    """中部：MessageItem 列表，支持流式打字机效果。

    流式打字机（Textual 8.x 适配）：
    - stream_start()  创建 assistant item，初始内容 "▌"
    - stream_chunk()  累积到 _stream_buf，节流 30ms 刷新
    - stream_end()    强制 flush 最后一个 chunk，移除光标

    重要：stream_end() 必须把 _stream_buf 全部写进去再移除 ▌，
    防止节流导致最后几个字符丢失。
    """

    def __init__(self) -> None:
        super().__init__()
        self._items: list["MessageItem | ToolGroup"] = []
        self._stream_item: MessageItem | None = None
        self._stream_buf: str = ""
        self._last_refresh: float = 0.0
        self._REFRESH_INTERVAL: float = 0.030  # 30 ms
        self._last_tool_group: "ToolGroup" | None = None

    def _append_item(self, item) -> "MessageItem | ToolGroup":
        self._items.append(item)
        # 非 App 上下文（单测）时 mount 会抛 MountError / NoActiveAppError，允许静默跳过
        try:
            self.mount(item)
            self.scroll_end()
        except (MountError, NoActiveAppError):
            pass
        return item

    # ── 工具分组 ──────────────────────────────────────────────
    def tool_group_start(self, title: str = "工具执行") -> ToolGroup:
        """开始一个工具分组块（默认展开）。

        返回的 ToolGroup 实例可直接 mount 到 MessageLog。
        """
        group = ToolGroup(title=title)
        self._last_tool_group = group
        self._append_item(group)
        return group

    def tool_group_add(self, name: str, detail: str = "", status: str = "done",
                       elapsed: float | None = None) -> None:
        """在当前工具分组里追加一条 tool 行。"""
        if self._last_tool_group is None:
            return
        self._last_tool_group.add_tool(name, detail=detail, status=status, elapsed=elapsed)

    def tool_group_toggle(self) -> None:
        """折叠 / 展开当前工具分组。"""
        if self._last_tool_group is not None:
            self._last_tool_group.toggle()

    # ── 对外 API ──────────────────────────────────────────────

    def add_user(self, text: str) -> None:
        self._append_item(MessageItem(kind="user", content=text))

    def add_assistant(self, text: str, title: str = "") -> None:
        self._append_item(MessageItem(kind="assistant", title=title, content=text))

    def add_tool(self, name: str, detail: str = "", status: str = "done") -> MessageItem:
        """追加一条 tool 行。

        Args:
            name:   tool 名（如 "filesystem"）
            detail: 说明文字（如 "scanned 128 files"）
            status: "done" / "running" / "failed"
        """
        item = MessageItem(kind="tool", title=name, content=detail, status=status)
        return self._append_item(item)

    def add_error(
        self,
        title: str = "",
        detail: str = "",
        hints: list[str] | None = None,
    ) -> None:
        self._append_item(
            MessageItem(
                kind="error",
                title=title,
                content=detail,
                detail="\n".join(hints or []),
            )
        )

    def add_artifact(self, name: str) -> None:
        self._append_item(MessageItem(kind="artifact", title=name))

    def add_meta(self, text: str) -> None:
        self._append_item(MessageItem(kind="meta", content=text))

    # ── 流式打字机 ────────────────────────────────────────────

    def stream_start(self) -> MessageItem:
        item = MessageItem(kind="assistant", content="▌")
        self._stream_item = item
        self._stream_buf = ""
        self._last_refresh = 0.0
        self._append_item(item)
        return item

    def stream_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        self._stream_buf += chunk
        now = _time.monotonic()
        if now - self._last_refresh < self._REFRESH_INTERVAL:
            return  # 节流：30ms 内不刷新
        self._last_refresh = now
        if self._stream_item:
            self._stream_item.set_content(self._stream_buf + " ▌")

    def stream_end(self) -> None:
        """强制 flush _stream_buf，移除 ▌ 光标。

        这是关键方法：即使最后一次 chunk 还在节流 buffer 里，
        这里也会把它写进 MessageItem，保证不丢字符。
        """
        if self._stream_item:
            self._stream_item.set_content(self._stream_buf)
            self._stream_item = None
            self._stream_buf = ""

    def tool_update(self, item: MessageItem, status: str, detail: str = "") -> None:
        """更新已有 tool item 的状态（不重复追加）。"""
        item.set_status(status, detail)

    def clear(self) -> None:
        for item in self._items:
            try:
                item.remove()
            except (NoActiveAppError, NoScreen):
                pass
        self._items.clear()
        self._stream_item = None
        self._stream_buf = ""


# ── 面板：底部输入栏 ──────────────────────────────────────────────
class InputBar(Vertical, can_focus=False):
    """底部：SlashPopup + Input + Footer。

    SlashPopup 在 InputBar 内，显示在 Input 正上方；隐藏时 display:none，
    不占用高度。
    """

    def __init__(self) -> None:
        super().__init__()
        self.input: Input | None = None
        self.footer: Footer | None = None
        self.hint: Static | None = None
        self.popup: "SlashPopup" | None = None  # type: ignore[name-defined]

    def compose(self) -> ComposeResult:
        from cli.tui.panels import SlashPopup
        self.popup = SlashPopup()
        self.input = Input(
            placeholder="Enter 发送；/ 看命令；Ctrl+Q 退出",
            classes="forge-input",
        )
        # 上下文提示行：idle / running / approval / slash 四态由 App 层
        #（ForgeTuiApp._update_footer）驱动。放在输入框与 Footer 之间 ——
        # 两者职责不同：Footer 是静态键位表，hint 是"此刻能做什么"。
        self.hint = Static("", classes="forge-hint")
        self.footer = Footer()
        yield self.popup
        yield self.input
        yield self.hint
        yield self.footer

    def focus_input(self) -> None:
        if self.input:
            self.input.focus()

    def clear_input(self) -> None:
        if self.input:
            self.input.value = ""
            self.input.cursor_position = 0

    def get_value(self) -> str:
        return self.input.value if self.input else ""

    def set_placeholder(self, text: str) -> None:
        if self.input:
            self.input.placeholder = text

    def set_hint(self, text: str) -> None:
        """更新上下文提示行（由 App 层按 idle/running/approval/slash 调用）。"""
        if self.hint is not None:
            self.hint.update(text)


# ── 面板：模型选择下拉（真实 HTTP 驱动）──────────────────────────
class ModelPicker(Static):
    """输入栏右下角的 `agnes-2.5-flash ▾`。

    点击或按 `Tab`/`Enter` 打开 popup，列出租户账号真实可用的模型
    （调网关 /models 端点 + 本地 /v1/models 合并去重）。

    选择模型 → 设 AGENT_MODEL 环境变量 → 后续 run_turn 用它（不重启进程）。
    """

    def __init__(self, current: str = "") -> None:
        self._current = current or "—"
        self._models: list[tuple[str, str]] = []  # (id, source)  source: gateway/local/unknown
        self._loading = False
        self._popup: "ModelPopup | None" = None
        self._text = self._build_text()
        super().__init__(self._text)

    def _build_text(self) -> Text:
        t = Text()
        if self._loading:
            t.append("模型加载中…", style=MUTED)
        else:
            t.append(f" {self._current} ", style=f"bold {PRIMARY}")
            t.append(" ▾", style=MUTED)
        return t

    def set_current(self, model_id: str) -> None:
        self._current = model_id
        # 同步到 status header（header 的 model 字段也是这个名字）
        self._refresh()

    def set_models(self, models: list[tuple[str, str]]) -> None:
        self._models = models
        self._refresh()

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        self._refresh()

    def attach_popup(self, popup: "ModelPopup") -> None:
        self._popup = popup

    def open_popup(self) -> None:
        if self._popup is not None:
            self._popup.show(self._models, self._current)

    def _refresh(self) -> None:
        self._text = self._build_text()
        try:
            self.update(self._text)
        except (NoScreen, NoActiveAppError):
            pass

    def render(self) -> Text:
        return self._text


class ModelPopup(Vertical, can_focus=False):
    """模型选择 popup：列可用模型，选中后回调。

    键盘：↑↓ 切换、Enter 确认、Esc 取消。
    """

    def __init__(self, on_select: "callable | None" = None) -> None:
        super().__init__()
        self._on_select = on_select
        self._models: list[tuple[str, str]] = []
        self._selected: int = 0
        self._body = Static(self._build_text(), classes="model-popup-body")
        self._body.display = True
        self._header = Static("选择模型  （↑↓ 选 / Enter 确认 / Esc 取消）",
                              classes="model-popup-header")

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    def show(self, models: list[tuple[str, str]], current: str) -> None:
        self._models = models
        self._selected = next(
            (i for i, (mid, _) in enumerate(models) if mid == current), 0
        )
        # 用 CSS 类覆盖默认 display:none；Textual 的 inline style 优先级低
        try:
            self.styles.display = "block"
            self.refresh()
        except (NoScreen, NoActiveAppError):
            pass
        self._body.update(self._build_text())
        self._body.focus()

    def hide(self) -> None:
        self._models = []
        try:
            self.styles.display = "none"
            self.refresh()
        except (NoScreen, NoActiveAppError):
            pass

    def is_visible(self) -> bool:
        try:
            return self.styles.display != "none"
        except (NoScreen, NoActiveAppError):
            return False

    def select_next(self) -> None:
        if self._models:
            self._selected = (self._selected + 1) % len(self._models)
            self._body.update(self._build_text())

    def select_prev(self) -> None:
        if self._models:
            self._selected = (self._selected - 1) % len(self._models)
            self._body.update(self._build_text())

    def confirm(self) -> None:
        if not self._models:
            return
        mid, source = self._models[self._selected]
        self.hide()
        if self._on_select is not None:
            self._on_select(mid, source)

    def cancel(self) -> None:
        self.hide()

    def _build_text(self) -> Text:
        if not self._models:
            return Text("(未加载模型，按 m 重新拉取)", style=MUTED)
        t = Text()
        for i, (mid, source) in enumerate(self._models):
            marker = "●" if i == self._selected else " "
            color = PRIMARY if i == self._selected else TEXT_C
            tag = {"gateway": "[网关]", "local": "[本地]"}.get(source, "")
            t.append(f"{marker} {mid:<32} ", style=f"bold {color}")
            if tag:
                t.append(tag, style=MUTED)
            if i < len(self._models) - 1:
                t.append("\n")
        return t


# ── SlashPopup（独立类，在 InputBar.compose 里 import）─────────────
class SlashPopup(Static):
    """斜杠命令自动补全 popup。

    显示在 InputBar 内，位于 Input 正上方；
    隐藏时 display:none，不影响 MessageLog 高度。

    键盘操作由 App 层（on_key）驱动，Popup 本身不处理按键。
    """

    _MAX_POPUP = 6

    def __init__(self) -> None:
        self._results: list[tuple[str, str]] = []
        self._selected: int = 0
        self._input_text: str = ""
        self._text = self._build_text()
        super().__init__(self._text)

    def set_results(self, results: list[tuple[str, str]], input_text: str) -> None:
        self._results = results[: self._MAX_POPUP]
        self._selected = 0
        self._input_text = input_text
        self._text = self._build_text()
        self._refresh()

    def select_next(self) -> None:
        if self._results:
            self._selected = (self._selected + 1) % len(self._results)
            self._refresh()

    def select_prev(self) -> None:
        if self._results:
            self._selected = (self._selected - 1) % len(self._results)
            self._refresh()

    def selected(self) -> tuple[str, str] | None:
        if self._results:
            return self._results[self._selected]
        return None

    def show(self) -> None:
        """显示 popup（display: block）。"""
        self._text = self._build_text()
        self._refresh()
        self.display = True

    def hide(self) -> None:
        """隐藏 popup（display: none，不占高度）。"""
        self._results = []
        self._text = self._build_text()
        self._refresh()
        self.display = False

    def _build_text(self) -> Text:
        if not self._results:
            return Text()
        t = Text()
        for i, (cmd, desc) in enumerate(self._results):
            marker = "●" if i == self._selected else " "
            color = PRIMARY if i == self._selected else TEXT_C
            t.append(f"{marker} {cmd:<12} ", style=f"bold {color}")
            t.append(_escape(desc), style=MUTED)
            if i < len(self._results) - 1:
                t.append("\n")
        return t

    def _refresh(self) -> None:
        self._text = self._build_text()
        try:
            self.update(self._text)
        except (NoScreen, NoActiveAppError):
            pass  # SlashPopup 未挂载时

    def render(self) -> Text:
        return self._text


# ── 主 App 容器 ───────────────────────────────────────────────────
class TuiPanels(Vertical, can_focus=False):
    """四行布局：StatusHeader / MessageLog / ModelPicker 行 / InputBar。

    ModelPicker 行把 `agnes-2.5-flash ▾` 放在 MessageLog 和 InputBar 之间，
    设计稿把它画在输入栏右侧 —— 终端里独立占一行更易点选，布局不冲突。
    隐藏时 display:none，不占高度（由 App 层控制显隐）。
    """

    def __init__(self, session_name: str = "personal",
                 current_model: str = "") -> None:
        super().__init__()
        self.header = StatusHeader(session_name)
        self.msglog = MessageLog()
        self.model_picker = ModelPicker(current_model)
        self.model_popup = ModelPopup()
        self.bar = InputBar()
        self.model_picker.attach_popup(self.model_popup)
        # 模型 popup 默认隐藏；App 层按 "m" 键显示
        self.model_popup.display = False

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.msglog
        yield self.model_picker
        yield self.model_popup
        yield self.bar
