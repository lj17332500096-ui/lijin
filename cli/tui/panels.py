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

import re as _re
import time as _time

from rich.text import Text
from rich.markdown import Markdown as _RichMarkdown
from textual._context import NoActiveAppError
from textual.app import ComposeResult, NoScreen
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Static
from textual.widget import MountError


class ForgeInput(Input):
    """Input 子类：放行 `m` 键事件，让它冒泡到 App 层触发模型 popup。

    Textual 8.x 的 Input._on_key 对**所有**可打印字符调用 event.stop()，
    且 Input.check_consume_key 对所有可打印字符返回 True，导致 binding 系统
    从 binding_chain 中过滤掉 `m` 的 binding（认为 Input 会消费它）。
    ForgeInput 做两件事：
      1. 重写 _on_key：对 `m` 键不 stop / 不 prevent_default，让事件继续冒泡
      2. 重写 check_consume_key：对 `m` 键返回 False，让 binding 系统保留
         App 级 `Binding("m", "toggle_model_picker", priority=True)`
    其他可打印字符行为与原 Input 完全一致。
    """

    async def _on_key(self, event) -> None:  # type: ignore[override]
        self._restart_blink()
        if event.is_printable:
            # m 键且输入框为空 → 放行（让 binding 触发模型 popup）
            if event.character == "m" and not self.value:
                return
            event.stop()
            assert event.character is not None
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(event.character)
            else:
                self.replace(event.character, *selection)
            event.prevent_default()

    def check_consume_key(self, key: str, character: str | None) -> bool:
        """m 键在输入框为空时不消费（让 App 级 binding 生效），
        有内容时仍消费（m 作为普通字符写入，避免意外打开模型 popup）。
        """
        if character == "m":
            # 输入框有内容 → 正常消费 m 字符
            if self.value:
                return True
            # 输入框为空 → 不消费，让 Binding("m", priority=True) 生效
            return False
        return character is not None and character.isprintable()


# ── 设计令牌（对齐 dsh-TUI design-system）────────────────────────
# dsh-TUI 的 design-system 目录定义了 Pane / Divider / HintLine / StatusIcon /
# ProgressBar 五个原语。FORGE TUI 已有等价的 CSS 类（.model-popup-body /
# .forge-hint 等），这里把设计令牌统一收敛到模块级常量，供所有组件引用。
#
# 色彩体系（沿用 FORGE 既有暗色主题）：
#   背景层：BG → SURFACE → PANEL → BORDER（由深到浅 4 级）
#   文字层：TEXT_C（正文）→ MUTED（弱化）→ 各状态色
#   状态色：SUCCESS / WARNING / ERROR_C / PRIMARY / AGENT / ARTIFACT
#
# 对齐 dsh-TUI 的语义命名：
#   dsh "permission"   → FORGE PRIMARY（权限/选中/焦点）
#   dsh "suggestion"   → FORGE AGENT（建议/助手消息）
#   dsh "remember"     → FORGE SUCCESS（记忆/完成/固定）
#   dsh "error"        → FORGE ERROR_C（错误/危险）
#   dsh "warning"      → FORGE WARNING（警告/运行中）

BG       = "#0B0E14"  # 最底层背景
SURFACE  = "#10141C"  # 卡片/面板底
PANEL    = "#161B25"  # 弹出层/浮层底
BORDER   = "#293142"  # 边框
TEXT_C   = "#DCE4F2"  # 正文
MUTED    = "#778196"  # 弱化文字/提示
PRIMARY  = "#5CC8FF"  # 焦点/选中/主要交互
AGENT    = "#A78BFA"  # 助手消息/建议
SUCCESS  = "#6EE7A8"  # 成功/完成
WARNING  = "#F6C177"  # 警告/运行中
ERROR_C  = "#FF7185"  # 错误/危险
ARTIFACT = "#7DD3FC"  # 产出物/链接


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
            "partial": WARNING,
            "blocked": ERROR_C,
            "needs_user": WARNING,
            "cancelled": MUTED,
        }.get(state, MUTED)
        state_label = {
            "idle": "● 就绪",
            "running": "◌ 运行中",
            "waiting": "◆ 等待审批",
            "failed": "× 出错",
            "partial": "! 部分完成",
            "blocked": "× 已阻止",
            "needs_user": "? 需要你处理",
            "cancelled": "■ 已取消",
        }.get(state, "· 状态更新")

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
            t.append(f"  {self._tool_count} 项工具", style=MUTED)

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

    def set_session_name(self, name: str) -> None:
        self._session_name = str(name or "personal")[:48]
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
                 elapsed: float | None = None) -> int:
        """追加一条 tool 子项。status: running / done / failed。
        返回该行的索引（供 set_tool 更新用）。"""
        idx = len(self._rows)
        self._rows.append((name, detail, status, elapsed))
        self._refresh()
        return idx

    def set_tool(self, idx: int, name: str, detail: str = "",
                 status: str = "done", elapsed: float | None = None) -> None:
        """按索引原地更新一条 tool 子项（不追加新行）。

        修复：tool.started 时 add_tool(running) 追加一条，
        tool.completed 时再用 set_tool 覆盖为终态，
        避免同一工具在 UI 里显示两行。
        """
        if 0 <= idx < len(self._rows):
            self._rows[idx] = (name, detail, status, elapsed)
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
        count = ""
        if self._rows:
            parts = [f"{len(self._rows)} 项"]
            if self._collapsed:
                totals = {
                    status: sum(1 for row in self._rows if row[2] == status)
                    for status in ("done", "failed", "running")
                }
                if totals["done"]:
                    parts.append(f"✓{totals['done']}")
                if totals["failed"]:
                    parts.append(f"×{totals['failed']}")
                if totals["running"]:
                    parts.append(f"◌{totals['running']}")
                if all(row[3] is not None for row in self._rows):
                    parts.append(f"{sum(row[3] or 0.0 for row in self._rows):.1f}秒")
            count = "  · " + "  ".join(parts)
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
                t.append(f"  {elapsed:.1f}秒", style=MUTED)
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
            t.append("你", style=f"bold {PRIMARY}")
            t.append("\n")
            for line in (self._content or "").splitlines() or [""]:
                t.append(f"  {_escape(line)}\n")

        elif k == "assistant":
            t.append("FORGE", style=f"bold {AGENT}")
            if self._title:
                t.append(f" · {_escape(self._title)}", style=MUTED)
            t.append("\n")
            # 流式 Markdown 渲染（参照 dsh-TUI StreamingMarkdown）：
            # 不等全部到齐，每个 chunk 到达就实时解析为富文本
            md_content = self._content or ""
            if md_content:
                md_lines = md_content.splitlines() or [""]
                for line in md_lines:
                    t.append(f"  {self._render_markdown_line(line)}\n")
            else:
                t.append("\n")

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
            t.append("× 出错", style=f"bold {ERROR_C}")
            t.append("\n")
            if self._title:
                t.append(f"  {_escape(self._title)}\n", style=ERROR_C)
            for line in (self._content or "").splitlines() or [""]:
                t.append(f"  {_escape(line)}\n", style=TEXT_C)
            if self._detail:
                t.append("  可尝试的处理方式\n", style=MUTED)
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

    def set_title(self, title: str) -> None:
        self._title = title
        self._refresh()

    def set_status(self, status: str, detail: str = "") -> None:
        self._status = status
        if detail:
            self._detail = detail
        self._refresh()

    # ── 流式 Markdown 行内渲染（参照 dsh-TUI StreamingMarkdown）──
    @staticmethod
    def _render_markdown_line(line: str) -> str:
        """轻量 Markdown 行内渲染：**加粗** / `代码` / 列表 / 代码块 标记。

        返回转义后的安全字符串（仍用 _escape 防止 markup 注入）。
        流式渲染时每个 chunk 都走这里，不等全文到齐。
        """
        # 行内代码 `...`
        line = _re.sub(r"`([^`]+)`", r"『\1』", line)
        # 加粗 **...**
        line = _re.sub(r"\*\*(.+?)\*\*", r"【\1】", line)
        # 斜体 *...*（非 **）
        line = _re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"⟨\1⟩", line)
        # 列表标记
        line = _re.sub(r"^(\s*)-\s", r"\1· ", line)
        line = _re.sub(r"^(\s*)\*\s", r"\1· ", line)
        # 转义剩余 Rich markup 特殊字符
        return _escape(line)


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
    """底部：SlashPopup + Input + 上下文提示 + 精简快捷键栏。

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
        self.input = ForgeInput(
            placeholder="Enter 发送；输入 / 查看命令；Ctrl+Q 退出",
            classes="forge-input",
        )
        # 单行上下文提示由 App 层按 idle / running / approval / slash 状态更新。
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

    def on_click(self, event) -> None:
        """鼠标点击标签 → 打开模型 popup（与按 m 键等价）。"""
        self.open_popup()

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


class _ModelPopupBody(Static):
    """可聚焦的 ModelPopup body：拦截 ↑↓/Enter/Esc，阻止事件冒泡到 Input。

    Textual 8.x 中 Input widget 的 _on_key 对可打印字符调用 event.stop()，
    焦点在 Input 上时 App 级 key_m/key_up/key_down 都收不到。
    让 body 可聚焦后，焦点移到 popup 内部，所有按键由 body 的 on_key 处理。
    """

    can_focus = True

    def __init__(self, text, classes: str | None = None) -> None:
        super().__init__(text, classes=classes)
        self._popup_ref: "ModelPopup | None" = None  # 构造后 patch

    def on_key(self, event) -> None:
        """拦截按键：↑↓ 移动选区，Enter 确认，Esc 取消。event.stop() 阻止冒泡。"""
        if self._popup_ref is None:
            return
        p = self._popup_ref
        if event.key == "up":
            p.select_prev()
            event.stop()
        elif event.key == "down":
            p.select_next()
            event.stop()
        elif event.key == "enter":
            p.confirm()
            event.stop()
        elif event.key == "escape":
            p.cancel()
            event.stop()


class ModelPopup(Vertical, can_focus=False):
    """模型选择 popup（单层平铺直选）。

    一屏列出所有可对话 LLM（网关 + 本地，已剔除 image/video 非 LLM 模型），
    每行带 source 标记（[网关]/[本地]），↑↓ 移动选区、Enter **直接切换模型**、Esc 取消。

    焦点管理：show() 时焦点移到 body（_ModelPopupBody，can_focus=True），
    body 的 on_key 拦截所有按键并 event.stop()，彻底绕过 Input widget 的事件消费。
    """

    def __init__(self, on_select: "callable | None" = None,
                 on_close: "callable | None" = None) -> None:
        super().__init__()
        self._on_select = on_select
        self._on_close = on_close
        # 全量模型：(model_id, source)
        self._models: list[tuple[str, str]] = []
        self._selected: int = 0
        self._body = _ModelPopupBody(self._build_text(), classes="model-popup-body")
        self._body._popup_ref = self  # patch 双向引用
        self._body.display = True
        self._header = Static("选择模型  （↑↓ 选择 / Enter 切换 / Esc 取消）",
                              classes="model-popup-header")

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    def show(self, models: list[tuple[str, str]], current: str) -> None:
        self._models = list(models)
        self._selected = 0
        # 定位当前模型
        for i, (mid, _src) in enumerate(self._models):
            if mid == current:
                self._selected = i
                break
        # 用 Widget.display 属性显示（setter 走 styles.display，能覆盖 CSS 类选择器）。
        self.display = True
        try:
            self._body.update(self._build_text())
            self._header.update(self._build_header())
            # 焦点移到 body（可聚焦的 _ModelPopupBody），Input 不再抢按键
            self._body.focus()
        except (NoScreen, NoActiveAppError, MountError):
            # 未挂载/无活跃 app（如纯同步单元环境）时安全跳过渲染，
            # 但 display 已置 True，is_visible 返回 True
            pass

    def hide(self) -> None:
        self._models = []
        self._selected = 0
        self.display = False
        try:
            self.refresh()
        except (NoScreen, NoActiveAppError, MountError):
            pass

    def is_visible(self) -> bool:
        return self.display

    # ── 键盘操作 ────────────────────────────────────────────
    def _paint(self) -> None:
        """刷新 body/header 渲染；未挂载或无活跃 app（纯同步单元环境）时安全跳过。"""
        try:
            self._body.update(self._build_text())
            self._header.update(self._build_header())
        except (NoScreen, NoActiveAppError, MountError):
            pass

    def select_next(self) -> None:
        if self._models:
            self._selected = (self._selected + 1) % len(self._models)
            self._paint()

    def select_prev(self) -> None:
        if self._models:
            self._selected = (self._selected - 1) % len(self._models)
            self._paint()

    def confirm(self) -> None:
        """选中当前行的模型，回调并关闭。"""
        if not self._models:
            self.hide()
            if self._on_close is not None:
                self._on_close()
            return
        mid, source = self._models[self._selected]
        self.hide()
        if self._on_close is not None:
            self._on_close()
        if self._on_select is not None:
            self._on_select(mid, source)

    def cancel(self) -> None:
        self.hide()
        if self._on_close is not None:
            self._on_close()
        # 焦点还给 Input（self.app 是 Textual 标准属性，无需 get_current_app）
        try:
            if hasattr(self, "app") and self.app:
                app = self.app
                if hasattr(app, "_bar"):
                    app._bar().focus_input()
        except Exception:
            pass

    def _build_header(self) -> str:
        return "选择模型  （↑↓ 选择 / Enter 切换 / Esc 取消）"

    def _build_text(self) -> Text:
        if not self._models:
            return Text("（模型列表未加载，按 m 重新获取）", style=MUTED)
        t = Text()
        for i, (mid, source) in enumerate(self._models):
            marker = "●" if i == self._selected else " "
            color = PRIMARY if i == self._selected else TEXT_C
            tag = {"gateway": "[网关]", "local": "[本地]",
                   "unknown": "", "loading": ""}.get(source, "[其他来源]")
            t.append(f"{marker} {mid}", style=f"bold {color}")
            if tag:
                t.append(f"  {tag}", style=MUTED)
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


# ── 面板：F1 历史搜索对话框（参照 dsh-TUI HistorySearchDialog）────
class HistorySearchDialog(Vertical, can_focus=False):
    """F1 可搜索历史对话框。

    参照 dsh-TUI 的 HistorySearchDialog：
    - 搜索框（Input）实时过滤历史
    - 列表行：● 选中 / 相对时间标签
    - 键盘：↑↓ 选择、Enter 填入、Esc 关闭
    - 默认显示最近 20 条；输入关键词后过滤
    """

    _MAX_VISIBLE = 20

    def __init__(self) -> None:
        super().__init__()
        self._history: list[tuple[str, float]] = []  # (text, timestamp)
        self._query: str = ""
        self._selected: int = 0
        self._input: Input | None = None
        self._body = Static(self._build_text(), classes="hist-search-body")
        self._header = Static("⌕ 搜索历史  （↑↓ 选择 / Enter 填入 / Esc 关闭）",
                               classes="hist-search-header")
        self.display = False  # 默认隐藏

    def compose(self) -> ComposeResult:
        yield self._header
        self._input = Input(
            placeholder="输入关键词过滤…",
            classes="hist-search-input",
        )
        yield self._input
        yield self._body

    def set_history(self, history: list[tuple[str, float]]) -> None:
        """注入历史条目（text, timestamp）。timestamp 为 epoch 秒。"""
        self._history = list(history)
        self._query = ""
        self._selected = 0
        self._refresh()

    def show(self, history: list[tuple[str, float]]) -> None:
        self.set_history(history)
        self._query = ""
        self._selected = 0
        if self._input:
            self._input.value = ""
        self.display = True
        self._refresh()
        if self._input:
            self._input.focus()

    def hide(self) -> None:
        self.display = False
        self._query = ""

    def is_visible(self) -> bool:
        return self.display

    def on_input_changed(self, event) -> None:
        """搜索框输入变化时实时过滤。"""
        self._query = (event.value or "").lower().strip()
        self._selected = 0
        self._refresh()

    def select_next(self) -> None:
        n = len(self._filtered())
        if n:
            self._selected = (self._selected + 1) % n
            self._refresh()

    def select_prev(self) -> None:
        n = len(self._filtered())
        if n:
            self._selected = (self._selected - 1) % n
            self._refresh()

    def selected_text(self) -> str:
        """当前选中条目的文本。"""
        matches = self._filtered()
        if not matches:
            return ""
        return matches[min(self._selected, len(matches) - 1)]

    def _filtered(self) -> list[str]:
        if not self._query:
            return [text for text, _ in self._history[-self._MAX_VISIBLE:]]
        return [text for text, _ in self._history if self._query in text.lower()]

    @staticmethod
    def _relative_age(ts: float) -> str:
        """以中文显示历史记录的相对时间。"""
        elapsed = _time.time() - ts
        if elapsed < 60:
            return "刚刚"
        if elapsed < 3600:
            return f"{int(elapsed // 60)} 分钟前"
        if elapsed < 86400:
            return f"{int(elapsed // 3600)} 小时前"
        return f"{int(elapsed // 86400)} 天前"

    def _build_text(self) -> Text:
        matches = self._filtered()
        if not matches:
            return Text("（没有匹配项）", style=MUTED)
        # 新→旧排列
        matches = list(reversed(matches))
        t = Text()
        for i, text in enumerate(matches):
            marker = "●" if i == self._selected else " "
            color = PRIMARY if i == self._selected else TEXT_C
            # 取匹配文本（新→旧后，原始索引需映射）
            ts = self._history[-1 - i][1] if (self._history and i < len(self._history)) else 0
            age = self._relative_age(ts) if ts else ""
            display = text if len(text) <= 50 else text[:47] + "…"
            t.append(f"{marker} {_escape(display)}", style=color)
            if age:
                t.append(f"  {age}", style=MUTED)
            t.append("\n")
        return t

    def _refresh(self) -> None:
        try:
            self._body.update(self._build_text())
        except (NoScreen, NoActiveAppError):
            pass


# ── 面板：Spinner 动画（参照 dsh-TUI SpinnerAnimationRow）─────────
class Spinner(Static):
    """工具执行中的动画 spinner + 状态文案。

    参照 dsh-TUI SpinnerAnimationRow：
    - 动画字符序列：⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏（braille spinner）
    - 每 80ms 换一帧
    - 状态文案由 App 层驱动（"正在搜索…" / "正在读取…"）
    - 固定 2 列槽位（保持文案对齐）
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self) -> None:
        self._frame: int = 0
        self._message: str = ""
        self._active: bool = False
        self._text = self._build_text()
        super().__init__(self._text)

    def set_message(self, msg: str) -> None:
        self._message = msg
        self._refresh()

    def start(self, msg: str = "") -> None:
        self._active = True
        self._frame = 0
        self._message = msg
        self._refresh()

    def stop(self) -> None:
        self._active = False
        self._refresh()

    def tick(self) -> None:
        """每帧调用（由 App 层 80ms 定时器驱动）。"""
        if self._active:
            self._frame += 1
            self._refresh()

    def _build_text(self) -> Text:
        t = Text()
        if self._active:
            glyph = self._FRAMES[self._frame % len(self._FRAMES)]
            t.append(f"{glyph} ", style=f"bold {PRIMARY}")
            if self._message:
                t.append(self._message, style=MUTED)
        else:
            t.append("● ", style=MUTED)
            if self._message:
                t.append(self._message, style=MUTED)
        return t

    def _refresh(self) -> None:
        self._text = self._build_text()
        try:
            self.update(self._text)
        except (NoScreen, NoActiveAppError):
            pass

    def render(self) -> Text:
        return self._text


# ── 面板：提问面板（参照 dsh-TUI AskUserQuestionPanel）───────────
class QuestionPanel(Vertical, can_focus=False):
    """模型通过 ask_user 工具弹出结构化提问面板。

    参照 dsh-TUI AskUserQuestionPanel：
    - 单选/多选选项列表 + 自由文本输入
    - 键盘：↑↓ 选择、Enter 确认、输入文本补充
    - 批量提问（多题顺序推进）
    """

    def __init__(self) -> None:
        super().__init__()
        self._questions: list[dict] = []  # 每题: {question, options, multi_select, header}
        self._current_q: int = 0
        self._selected: list[str] = []  # 当前题选中的选项
        self._custom_input: str = ""
        self._input_widget: Input | None = None
        self._body = Static("", classes="question-body")
        self._header = Static("", classes="question-header")
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body
        self._input_widget = Input(placeholder="补充说明（可选）", classes="question-input")
        yield self._input_widget

    def show(self, questions: list[dict]) -> None:
        """显示提问面板。questions: [{question, options, multi_select, header}]"""
        self._questions = questions
        self._current_q = 0
        self._selected = []
        self._custom_input = ""
        if self._input_widget:
            self._input_widget.value = ""
        self.display = True
        self._render_current()

    def hide(self) -> None:
        self.display = False
        self._questions = []

    def is_visible(self) -> bool:
        return self.display

    def _current_question(self) -> dict | None:
        if 0 <= self._current_q < len(self._questions):
            return self._questions[self._current_q]
        return None

    def _render_current(self) -> None:
        q = self._current_question()
        if q is None:
            return
        total = len(self._questions)
        pos = self._current_q + 1
        title = q.get("header", "问题")
        self._header.update(f"◆ {title}  （{pos}/{total}）")

        t = Text()
        t.append(_escape(q.get("question", "")), style=TEXT_C)
        t.append("\n\n")

        options = q.get("options", [])
        multi = q.get("multi_select", False)
        if options:
            for i, opt in enumerate(options):
                label = str(opt.get("label", f"选项 {i+1}"))
                desc = str(opt.get("description", ""))
                checked = label in self._selected
                symbol = "◉" if checked else "○"
                color = SUCCESS if checked else TEXT_C
                t.append(f"  {symbol} {label}", style=color)
                if desc:
                    t.append(f"  {_escape(desc)}", style=MUTED)
                t.append("\n")
            if multi:
                t.append(f"  （多选：↑↓ 切换选中，Enter 确认）", style=MUTED)
            else:
                t.append(f"  （↑↓ 选择，Enter 确认）", style=MUTED)
        else:
            t.append("  （直接输入回答，Enter 确认）", style=MUTED)

        try:
            self._body.update(t)
        except (NoScreen, NoActiveAppError):
            pass

    def select_next(self) -> None:
        q = self._current_question()
        if q and q.get("options"):
            opts = [str(o.get("label", f"选项 {i+1}")) for i, o in enumerate(q["options"])]
            if not q.get("multi_select"):
                if self._selected:
                    idx = opts.index(self._selected[0])
                    self._selected = [opts[(idx + 1) % len(opts)]]
                else:
                    self._selected = [opts[0]]
            else:
                if self._selected:
                    idx = opts.index(self._selected[0])
                else:
                    idx = -1
                next_idx = (idx + 1) % len(opts)
                self._selected = [opts[next_idx]]
            self._render_current()

    def select_prev(self) -> None:
        q = self._current_question()
        if q and q.get("options"):
            opts = [str(o.get("label", f"选项 {i+1}")) for i, o in enumerate(q["options"])]
            if self._selected:
                idx = opts.index(self._selected[0])
            else:
                idx = 0
            self._selected = [opts[(idx - 1) % len(opts)]]
            self._render_current()

    def confirm(self) -> list[str]:
        """确认当前题，返回所有答案。多题时推进到下一题。"""
        q = self._current_question()
        if q is None:
            return []
        answer = self._selected[0] if self._selected else self._custom_input
        if self._current_q < len(self._questions) - 1:
            self._current_q += 1
            self._selected = []
            self._custom_input = ""
            if self._input_widget:
                self._input_widget.value = ""
            self._render_current()
        return [answer]

    def get_all_answers(self) -> list[str]:
        return [self._selected[0] if self._selected else self._custom_input
                for _ in self._questions]


# ── 面板：审批面板（参照 dsh-TUI ApprovalPanel）─────────────────
class ApprovalPanel(Vertical, can_focus=False):
    """审批面板：多条审批时显示为面板，支持批量操作。

    参照 dsh-TUI ApprovalPanel：
    - 单条审批：Yes/No 两行
    - 多条审批：列出所有 + "全部批准" / "全部拒绝" 批量操作
    - 键盘：↑↓ 选择、1/2 快捷、Enter 确认、Esc 拒绝
    """

    def __init__(self) -> None:
        super().__init__()
        self._approvals: list[dict] = []
        self._selected: int = 0
        self._body = Static(self._build_text(), classes="approval-body")
        self._header = Static("◆ 需要审批", classes="approval-header")
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    def show(self, approvals: list[dict]) -> None:
        """显示审批面板。approvals: [{label, description, tool}]"""
        self._approvals = approvals
        self._selected = 0
        self.display = True
        self._refresh()

    def hide(self) -> None:
        self.display = False
        self._approvals = []

    def is_visible(self) -> bool:
        return self.display

    def _refresh(self) -> None:
        try:
            self._body.update(self._build_text())
        except (NoScreen, NoActiveAppError):
            pass

    def _build_text(self) -> Text:
        t = Text()
        if not self._approvals:
            return Text("（无待审批项）", style=MUTED)

        n = len(self._approvals)
        for i, ap in enumerate(self._approvals):
            label = str(ap.get("label") or ap.get("description") or f"第 {i+1} 项")
            desc = str(ap.get("description") or "")
            tool = str(ap.get("tool") or "")
            marker = "●" if i == self._selected else " "
            color = PRIMARY if i == self._selected else TEXT_C
            t.append(f"{marker} {i+1}. ", style=f"bold {color}")
            t.append(_escape(label), style=f"bold {color}")
            if tool:
                t.append(f"  [{_escape(tool)}]", style=MUTED)
            t.append("\n")
            if desc:
                t.append(f"    {_escape(desc)}\n", style=MUTED)

        t.append("\n", style=MUTED)
        if n > 1:
            t.append("  ↑↓ 选择  ·  Y 批准选中  ·  N 拒绝选中  ·  A 全部批准  ·  R 全部拒绝\n", style=MUTED)
        else:
            t.append("  Y 批准  ·  N 拒绝  ·  Esc 取消\n", style=MUTED)
        return t

    def select_next(self) -> None:
        if self._approvals:
            self._selected = (self._selected + 1) % len(self._approvals)
            self._refresh()

    def select_prev(self) -> None:
        if self._approvals:
            self._selected = (self._selected - 1) % len(self._approvals)
            self._refresh()

    def selected_index(self) -> int:
        return self._selected

    def approve_selected(self) -> None:
        """批准当前选中的审批项。"""
        pass  # 由 App 层处理

    def reject_selected(self) -> None:
        """拒绝当前选中的审批项。"""
        pass  # 由 App 层处理

    def approve_all(self) -> None:
        """批准所有审批项。"""
        pass  # 由 App 层处理

    def reject_all(self) -> None:
        """拒绝所有审批项。"""
        pass  # 由 App 层处理


# ── 面板：会话列表（参照 dsh-TUI SessionListRow）────────────────
class SessionListPopup(Vertical, can_focus=False):
    """/session list 弹出的会话列表。

    参照 dsh-TUI SessionListRow：
    - 两行布局：标题行 + 元数据行
    - 固定列宽：pin 2 列 + 状态 2 列 + 标题自适应
    - 键盘：↑↓ 选择、Enter 恢复会话、Esc 关闭
    """

    _MAX_VISIBLE = 15

    def __init__(self) -> None:
        super().__init__()
        self._sessions: list[dict] = []
        self._selected: int = 0
        self._body = Static(self._build_text(), classes="session-list-body")
        self._header = Static("◆ 会话列表  （↑↓ 选择 / Enter 恢复 / Esc 关闭）",
                               classes="session-list-header")
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    def show(self, sessions: list[dict]) -> None:
        """显示会话列表。sessions: [{title, updated_at, model, status, current}]"""
        self._sessions = sessions[:self._MAX_VISIBLE]
        self._selected = 0
        self.display = True
        self._refresh()

    def hide(self) -> None:
        self.display = False

    def is_visible(self) -> bool:
        return self.display

    def select_next(self) -> None:
        if self._sessions:
            self._selected = (self._selected + 1) % len(self._sessions)
            self._refresh()

    def select_prev(self) -> None:
        if self._sessions:
            self._selected = (self._selected - 1) % len(self._sessions)
            self._refresh()

    def selected_session(self) -> dict | None:
        if 0 <= self._selected < len(self._sessions):
            return self._sessions[self._selected]
        return None

    @staticmethod
    def _status_glyph(status: str) -> tuple[str, str]:
        return {
            "working": ("✽", WARNING),
            "completed": ("✓", SUCCESS),
            "failed": ("✕", ERROR_C),
            "idle": ("∙", MUTED),
            "stopped": ("∙", MUTED),
        }.get(status, ("∙", MUTED))

    def _build_text(self) -> Text:
        if not self._sessions:
            return Text("（无会话）", style=MUTED)
        t = Text()
        for i, s in enumerate(self._sessions):
            title = s.get("title", "")[:40]
            updated = s.get("updated_at", "")
            model = s.get("model", "")
            status = s.get("status", "stopped")
            current = s.get("current", False)
            pinned = s.get("pinned", False)

            pin_mark = "★" if pinned else "☆"
            glyph, color = self._status_glyph(status)

            marker = "●" if i == self._selected else " "
            sel_color = PRIMARY if i == self._selected else TEXT_C

            # 行 1：pin + 状态 + 标题
            t.append(f"  {marker} {pin_mark} {glyph} ", style=color)
            t.append(_escape(title), style=f"bold {sel_color}")
            if current:
                t.append("  [当前]", style=SUCCESS)
            t.append("\n")
            # 行 2：元数据
            facts = []
            if updated:
                facts.append(updated)
            if model:
                facts.append(model)
            if facts:
                t.append(f"  {'  ·  '.join(_escape(f) for f in facts)}\n", style=MUTED)
        return t

    def _refresh(self) -> None:
        try:
            self._body.update(self._build_text())
        except (NoScreen, NoActiveAppError):
            pass


class ConversationEntry(Horizontal):
    """侧栏中的单条对话：点击标题切换，展开行内删除操作。"""

    def __init__(self, index: int, item: dict, *, selected: bool, show_actions: bool) -> None:
        super().__init__(classes="conversation-session-row")
        title = str(item.get("title") or "未命名会话")[:24]
        if item.get("project"):
            title = "📁 " + title
        marker = "▸ " if selected else "  "
        if item.get("current") and not selected:
            title += " · 当前"
        self.select_button = Button(
            marker + title,
            id=f"session-select-{index}",
            classes="conversation-session-select" + (" selected" if selected else ""),
            variant="default",
        )
        if show_actions:
            self.action_button = Button(
                "删除对话",
                id=f"session-delete-{index}",
                classes="conversation-session-action is-delete",
                variant="default",
            )
        else:
            self.action_button = Button(
                "⋯",
                id=f"session-menu-{index}",
                classes="conversation-session-action",
                variant="default",
            )

    def compose(self) -> ComposeResult:
        yield self.select_button
        yield self.action_button


class ConversationSidebar(VerticalScroll, can_focus=False):
    """左侧会话栏，保留容器 ID 以便 App 真实切换与恢复历史。"""

    def __init__(self) -> None:
        super().__init__(id="conversation-sidebar")
        self._sessions: list[dict] = []
        self._selected = 0
        self._action_index: int | None = None
        self._rows = Vertical(id="conversation-session-rows")
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("对话", classes="conversation-sidebar-title")
        yield self._rows

    def show(self, sessions: list[dict]) -> None:
        self._sessions = list(sessions[:100])
        self._action_index = None
        self._selected = next(
            (i for i, item in enumerate(self._sessions) if item.get("current")), 0
        )
        self.display = True
        self._refresh()

    def hide(self) -> None:
        self.display = False

    def is_visible(self) -> bool:
        return bool(self.display)

    def select_next(self) -> None:
        if self._sessions:
            self._selected = (self._selected + 1) % len(self._sessions)
            self._action_index = None
            self._refresh()

    def select_prev(self) -> None:
        if self._sessions:
            self._selected = (self._selected - 1) % len(self._sessions)
            self._action_index = None
            self._refresh()

    def select_index(self, index: int) -> None:
        if 0 <= index < len(self._sessions):
            self._selected = index
            self._action_index = None
            self._refresh()

    def toggle_actions(self, index: int) -> None:
        if not 0 <= index < len(self._sessions):
            return
        self._selected = index
        self._action_index = None if self._action_index == index else index
        self._refresh()

    def toggle_selected_actions(self) -> None:
        self.toggle_actions(self._selected)

    def close_actions(self) -> bool:
        if self._action_index is None:
            return False
        self._action_index = None
        self._refresh()
        return True

    def selected_session(self) -> dict | None:
        if 0 <= self._selected < len(self._sessions):
            return self._sessions[self._selected]
        return None

    def _refresh(self) -> None:
        if not self._rows.is_mounted:
            return
        self.run_worker(self._render_rows(), group="conversation-sidebar", exclusive=True)

    async def _render_rows(self) -> None:
        await self._rows.remove_children()
        if not self._sessions:
            await self._rows.mount(Static("（暂无对话）", classes="conversation-sidebar-empty"))
            return
        entries = [
            ConversationEntry(
                index,
                item,
                selected=index == self._selected,
                show_actions=index == self._action_index,
            )
            for index, item in enumerate(self._sessions)
        ]
        await self._rows.mount(*entries)

# ── 面板：API / 网关自定义配置（参照 dsh-TUI SettingsPanel）───────
class ApiSettingsPanel(Vertical, can_focus=False):
    """自定义 API/网关配置面板。

    可编辑 4 项（保存后写 os.environ + 清 agent.py provider 缓存 + 持久化 .env，
    后续 run_turn 立即生效，不必重启进程）：
      - 网关 API Key  (OPENAI_API_KEY)
      - 网关 Base URL (OPENAI_BASE_URL)
      - 本地模型名    (FORGE_LOCAL_MODEL_NAME)
      - 本地 Base URL (FORGE_LOCAL_MODEL_BASE_URL)

    键盘：焦点在某个输入框时直接改值；Enter=保存，Esc=取消并关闭。
    """

    _KEY_API = "OPENAI_API_KEY"
    _KEY_URL = "OPENAI_BASE_URL"
    _KEY_LNAME = "FORGE_LOCAL_MODEL_NAME"
    _KEY_LBASE = "FORGE_LOCAL_MODEL_BASE_URL"

    def __init__(self) -> None:
        super().__init__()
        self.display = False
        self._fields: dict[str, Input] = {}
        self._on_save: "callable | None" = None  # (values dict) -> None，由 App patch
        self._on_cancel: "callable | None" = None

    # 由 App 在挂载时注入回调（面板自己不认识 App）
    def attach(self, on_save, on_cancel) -> None:
        self._on_save = on_save
        self._on_cancel = on_cancel

    def compose(self) -> ComposeResult:
        import os
        self._fields[self._KEY_API] = Input(
            value=os.getenv(self._KEY_API, ""),
            placeholder="sk-…（网关访问密钥）",
            classes="api-in",
        )
        self._fields[self._KEY_URL] = Input(
            value=os.getenv(self._KEY_URL, ""),
            placeholder="https://host/v1",
            classes="api-in",
        )
        self._fields[self._KEY_LNAME] = Input(
            value=os.getenv(self._KEY_LNAME, ""),
            placeholder="本地 GGUF 路径或模型名",
            classes="api-in",
        )
        self._fields[self._KEY_LBASE] = Input(
            value=os.getenv(self._KEY_LBASE, ""),
            placeholder="http://localhost:8080/v1",
            classes="api-in",
        )
        yield Static("◆ 接口 / 网关设置  （Enter 保存 / Esc 取消）",
                     classes="api-header")
        for label, key in [
            ("网关访问密钥  ", self._KEY_API),
            ("网关接口地址  ", self._KEY_URL),
            ("本地模型名    ", self._KEY_LNAME),
            ("本地接口地址  ", self._KEY_LBASE),
        ]:
            yield Static(label, classes="api-label")
            yield self._fields[key]
        self._fields[self._KEY_API].focus()

    def show(self) -> None:
        import os
        for key, inp in self._fields.items():
            inp.value = os.getenv(key, "")
        self.display = True
        self._fields.get(self._KEY_API).focus()
        self._refresh()

    def hide(self) -> None:
        self.display = False

    def is_visible(self) -> bool:
        return self.display

    def current_values(self) -> dict[str, str]:
        return {k: (v.value or "").strip() for k, v in self._fields.items()}

    def save(self) -> dict[str, str]:
        vals = self.current_values()
        # 调 App 层回调（写 os.environ + 清缓存 + 持久化 .env）
        if self._on_save is not None:
            self._on_save(vals)
        self.hide()
        return vals

    def cancel(self) -> None:
        if self._on_cancel is not None:
            self._on_cancel()
        self.hide()

    def _refresh(self) -> None:
        # 无 body 渲染，仅确保 layout 刷新
        try:
            self.refresh()
        except (NoScreen, NoActiveAppError, MountError):
            pass


# ── 主 App 容器 ───────────────────────────────────────────────────
class TuiPanels(Vertical, can_focus=False):
    """TUI 主布局：状态栏、操作工具栏、会话侧栏/消息区和输入栏。

    新增：
    - Spinner：工具执行中动画状态行（默认隐藏）
    - HistorySearchDialog：F1 可搜索历史对话框（默认隐藏）
    - QuestionPanel：ask_user 提问面板（默认隐藏）
    - ApprovalPanel：批量审批面板（默认隐藏）
    - SessionListPopup：/session list 会话列表（默认隐藏）
    """

    def __init__(self, session_name: str = "personal",
                 current_model: str = "") -> None:
        super().__init__()
        self.header = StatusHeader(session_name)
        self.msglog = MessageLog()
        self.spinner = Spinner()
        self.model_picker = ModelPicker(current_model)
        self.model_popup = ModelPopup()
        self.api_settings = ApiSettingsPanel()
        self.hist_search = HistorySearchDialog()
        self.question_panel = QuestionPanel()
        self.approval_panel = ApprovalPanel()
        self.session_list = SessionListPopup()
        self.sidebar = ConversationSidebar()
        self.sidebar_button = Button("☰ 对话", id="sidebar-toggle", variant="default")
        self.new_chat_button = Button("＋ 新对话", id="new-chat", variant="default")
        self.attach_file_button = Button("＋ 文件", id="attach-file", variant="default")
        self.attach_folder_button = Button("＋ 项目文件夹", id="attach-folder", variant="default")
        self.project_label = Static("个人会话", id="project-label")
        self.bar = InputBar()
        self.model_picker.attach_popup(self.model_popup)
        # 所有 popup 默认隐藏
        self.model_popup.display = False
        self.api_settings.display = False
        self.spinner.display = False
        self.hist_search.display = False
        self.question_panel.display = False
        self.approval_panel.display = False
        self.session_list.display = False

    def compose(self) -> ComposeResult:
        yield self.header
        with Horizontal(id="tui-toolbar"):
            yield self.sidebar_button
            yield self.new_chat_button
            yield self.attach_file_button
            yield self.attach_folder_button
            yield self.project_label
        with Horizontal(id="tui-main"):
            yield self.sidebar
            with Vertical(id="tui-chat-column"):
                yield self.msglog
                yield self.spinner
                yield self.model_picker
                yield self.model_popup
                yield self.api_settings
                yield self.hist_search
                yield self.question_panel
                yield self.approval_panel
                yield self.session_list
        yield self.bar
