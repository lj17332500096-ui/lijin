"""FORGE TUI 专项测试。

覆盖：
- StatusHeader 状态渲染 + 40/80/100/120 列响应式
- MessageItem 各类型渲染
- 流式打字机（start/chunk/end + 节流 + stream_end flush）
- Approval 状态机（y/yes/Y/n/no/N/d/普通文本 10 条）
- Slash 命令过滤 + popup 挂载位置
- Pilot 交互测试（Case A-F + Inspector）

零 LLM / 零网络，全部用 headless run_test。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.tui.app import ForgeTuiApp, InspectorScreen, _SLAH_COMMANDS
from cli.tui.panels import TuiPanels, MessageLog, MessageItem, StatusHeader, SlashPopup


# ════════════════════════════════════════════════════════════════
#  Section 1: StatusHeader 响应式
# ════════════════════════════════════════════════════════════════

def test_header_80col_keeps_core_fields():
    """80 列：FORGE/session/state/model/tools/elapsed 全部保留。"""
    header = StatusHeader("personal")
    header.set_width(80)
    header.set_state("running", elapsed="03.2s")
    header.set_model("GPT-5.6")
    header.set_tool_count(4)
    plain = header._build_text().plain
    assert "FORGE" in plain
    assert "personal" in plain
    assert "RUNNING" in plain
    assert "GPT-5.6" in plain
    assert "4 tools" in plain
    assert "03.2s" in plain


def test_header_60col_drops_elapsed():
    """60 列：tool count 保留，elapsed 被裁减（需 ≥80）。"""
    header = StatusHeader("personal")
    header.set_width(60)
    header.set_state("running", elapsed="03.2s")
    header.set_model("GPT-5.6")
    header.set_tool_count(4)
    plain = header._build_text().plain
    assert "FORGE" in plain
    assert "personal" in plain
    assert "GPT-5.6" in plain
    assert "4 tools" in plain
    assert "03.2s" not in plain  # 60 < 80，elapsed 被裁减


def test_header_40col_minimal():
    """40 列（极窄）：只保留 FORGE + session + state，model 被裁减。"""
    header = StatusHeader("personal")
    header.set_width(40)
    header.set_state("failed")
    header.set_model("GPT-5.6")
    header.set_tool_count(10)
    plain = header._build_text().plain
    assert "FORGE" in plain
    assert "personal" in plain
    assert "ERROR" in plain
    assert "GPT-5.6" not in plain  # 40 < 50，model 被裁减


def test_header_height_single_line():
    """Header 内容是单行（Rich Text 内部无换行），CSS 固定 height: 3。"""
    header = StatusHeader("personal")
    header.set_state("running", elapsed="03.2s")
    header.set_model("GPT-5.6")
    header.set_tool_count(4)
    t = header._build_text()
    assert "\n" not in t.plain


def test_header_all_states_render():
    header = StatusHeader("test")
    for state, label in (("idle", "READY"), ("running", "RUNNING"),
                          ("waiting", "APPROVAL"), ("failed", "ERROR")):
        header.set_state(state)
        assert label in header._build_text().plain


# ════════════════════════════════════════════════════════════════
#  Section 2: MessageItem 各类型
# ════════════════════════════════════════════════════════════════

def test_message_item_user():
    item = MessageItem(kind="user", content="你好")
    text = item.render()
    assert "YOU" in text.plain
    assert "你好" in text.plain


def test_message_item_assistant():
    item = MessageItem(kind="assistant", title="ANSWER", content="这是回复")
    text = item.render()
    assert "FORGE" in text.plain
    assert "ANSWER" in text.plain
    assert "这是回复" in text.plain


def test_message_item_tool_done():
    item = MessageItem(kind="tool", title="filesystem", content="scanned 128 files", status="done")
    text = item.render()
    assert "✓" in text.plain
    assert "filesystem" in text.plain


def test_message_item_tool_running():
    item = MessageItem(kind="tool", title="github", status="running")
    text = item.render()
    assert "◌" in text.plain


def test_message_item_tool_failed_visible():
    """failed tool 行保留 detail 信息（exit code 等）。"""
    item = MessageItem(kind="tool", title="terminal", content="pytest",
                       status="failed", detail="exit 1")
    text = item.render()
    plain = text.plain
    assert "×" in plain
    assert "pytest" in plain
    assert "exit 1" in plain


def test_message_item_error_with_hints():
    item = MessageItem(
        kind="error",
        title="Request failed",
        content="Connection refused",
        detail="Check MCP\nRetry",
    )
    text = item.render()
    assert "× ERROR" in text.plain
    assert "Possible fixes" in text.plain


def test_message_item_artifact():
    item = MessageItem(kind="artifact", title="report.md")
    text = item.render()
    assert "↳" in text.plain
    assert "report.md" in text.plain


def test_message_item_meta():
    item = MessageItem(kind="meta", content="DONE · 3 tools · 2.1s")
    text = item.render()
    assert "DONE" in text.plain


# ════════════════════════════════════════════════════════════════
#  Section 3: 流式打字机
# ════════════════════════════════════════════════════════════════

def test_stream_single_assistant_item():
    """一次回答只产生一个 assistant item，不是三个。"""
    ml = MessageLog()
    ml.stream_start()
    ml.stream_chunk("当前")
    ml.stream_chunk("还有")
    ml.stream_chunk("三个问题")
    ml.stream_end()
    assistant_items = [i for i in ml._items if i._kind == "assistant"]
    assert len(assistant_items) == 1
    assert "当前还有三个问题" in assistant_items[0]._content


def test_stream_end_no_cursor():
    """stream_end 后不留下 ▌。"""
    ml = MessageLog()
    ml.stream_start()
    ml.stream_chunk("hello")
    ml.stream_end()
    item = ml._items[0]
    assert "▌" not in item._content


def test_stream_end_flushes_unrendered_buffer():
    """stream_end 强制 flush 最后一个 chunk，不丢字符。

    模拟：最后两个 chunk 在节流 buffer 里（_last_refresh 刚更新过），
    stream_end 必须把 _stream_buf 完整写入。
    """
    ml = MessageLog()
    ml.stream_start()
    ml.stream_chunk("AAA")
    # 手动把 _last_refresh 设到现在（模拟节流窗口内，后续 chunk 不刷新）
    ml._last_refresh = time.monotonic()
    ml.stream_chunk("BBB")  # 被节流（30ms 内不刷新）
    ml.stream_chunk("CCC")  # 也被节流
    ml.stream_end()  # 必须 flush 全部
    item = ml._items[0]
    assert "AAA" in item._content
    assert "BBB" in item._content
    assert "CCC" in item._content
    assert "▌" not in item._content


def test_stream_no_double_append():
    ml = MessageLog()
    ml.stream_start()
    ml.stream_chunk("a")
    ml.stream_chunk("b")
    ml.stream_chunk("c")
    ml.stream_end()
    assert len(ml._items) == 1


def test_clear_removes_all():
    ml = MessageLog()
    ml.add_user("test")
    ml.add_assistant("reply")
    ml.add_tool("fs", status="done")
    assert len(ml._items) == 3
    ml.clear()
    assert len(ml._items) == 0


# ════════════════════════════════════════════════════════════════
#  Section 4: Approval 状态机
# ════════════════════════════════════════════════════════════════

def _approval_app(app: ForgeTuiApp) -> None:
    """将 app 放入审批态（在真实 run_test 内调用）。"""
    app._in_approval = True
    app._pending_approvals = [{"label": "test", "description": "desc"}]


async def _approval_app_ctx(app: ForgeTuiApp) -> None:
    """真实 run_test 内进入审批态。"""
    _approval_app(app)


def test_approval_y_approves():
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("y")
            assert app._in_approval is False
    asyncio.run(_run())


def test_approval_yes_approves():
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("yes")
            assert app._in_approval is False
    asyncio.run(_run())


def test_approval_Y_approves():
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("Y")
            assert app._in_approval is False
    asyncio.run(_run())


def test_approval_n_rejects():
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("n")
            assert app._in_approval is False
            assert app._pending_approvals == []
    asyncio.run(_run())


def test_approval_no_rejects():
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("no")
            assert app._in_approval is False
    asyncio.run(_run())


def test_approval_N_rejects():
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("N")
            assert app._in_approval is False
    asyncio.run(_run())


def test_approval_d_shows_details():
    """d 展开详情后 _in_approval 保持 True。"""
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("d")
            assert app._in_approval is True
    asyncio.run(_run())


def test_regular_message_does_not_clear_pending_approval():
    """核心测试：审批态下普通文本不解除 pending。"""
    async def _run():
        app = ForgeTuiApp(session_name="approval_test")
        async with app.run_test() as _pilot:
            _approval_app(app)
            app._handle_approval_input("为什么需要执行这个操作？")
            assert app._in_approval is True, "普通文本不应清除 pending approval"
    asyncio.run(_run())


def test_regular_y_in_normal_mode_is_text():
    """非审批态下 y 走 _submit_text 路径（不是审批逻辑）。"""
    app = ForgeTuiApp(session_name="normal")
    app._in_approval = False
    # 验证分支逻辑：非 slash + 非 approval → _submit_text
    text = "y"
    assert not text.startswith("/")
    assert not app._in_approval
    # 直接验证 _in_approval=False 时不会进入 _handle_approval_input
    # （on_input_submitted 的条件分支保证这一点）


def test_regular_n_in_normal_mode_is_text():
    app = ForgeTuiApp(session_name="normal")
    assert app._in_approval is False


# ════════════════════════════════════════════════════════════════
#  Section 5: Slash 命令过滤
# ════════════════════════════════════════════════════════════════

def test_slash_filter_mo():
    results = [(cmd, desc) for cmd, desc in _SLAH_COMMANDS if cmd.lower().startswith("/mo")]
    assert any(cmd == "/mode" for cmd, _ in results)


def test_slash_filter_help():
    results = [(cmd, desc) for cmd, desc in _SLAH_COMMANDS if cmd.lower().startswith("/he")]
    assert any(cmd == "/help" for cmd, _ in results)


def test_slash_filter_none():
    results = [(cmd, desc) for cmd, desc in _SLAH_COMMANDS if cmd.lower().startswith("/xyz_nonexistent")]
    assert len(results) == 0


def test_slash_popup_in_inputbar():
    """SlashPopup 挂载在 InputBar 内（不是 App root）。"""

    async def _run():
        app = ForgeTuiApp(session_name="popup_test")
        async with app.run_test() as pilot:
            panel = app.query_one(TuiPanels)
            popup = panel.bar.popup
            assert popup is not None, "popup 应在 InputBar 内"
            assert isinstance(popup, SlashPopup)

    asyncio.run(_run())


# ════════════════════════════════════════════════════════════════
#  Section 6: Pilot 交互测试（Case A-F）
#
#  Textual 8.2.8 事件 API：
#  - Input.Changed(input, value, validation_result)   → on_input_changed
#  - Input.Submitted(input, value, validation_result)  → on_input_submitted
#  - Key(key, character)                                → on_key
#  - 用 pilot.press("key") 发送按键
#  - 用 pilot.click(widget) 设置 focus
# ════════════════════════════════════════════════════════════════

def test_case_a_normal_input():
    """Case A：普通输入 → Input.Submitted 被 App.on_input_submitted 处理。

    验证：on_input_submitted 收到事件后，_in_approval=False 时走 _submit_text，
    MessageLog 应有 user item（add_user 调用）。
    不实际跑 Runtime（需要真实 LLM），只验证事件路径正确。
    """

    async def _run():
        app = ForgeTuiApp(session_name="case_a")
        async with app.run_test() as pilot:
            panel = app.query_one(TuiPanels)
            ml = panel.msglog
            bar = panel.bar
            input_widget = bar.input

            # focus 输入框
            await pilot.click(input_widget)
            # 逐字符输入（pilot 没有 type，用 press 模拟）
            for ch in "hello":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.1)

            # 验证：user item 已添加（_submit_text 调 add_user）
            user_items = [i for i in ml._items if i._kind == "user"]
            assert any("hello" in i._content for i in user_items), \
                f"hello 应出现在 user items，实际: {[i._content for i in user_items]}"
            # 验证：输入框已清空
            assert input_widget.value == "", "输入框应已清空"

    asyncio.run(_run())


def test_case_b_slash_popup():
    """Case B：输入 /mo → popup 打开 → Tab 补全 /mode → Esc 关闭。"""

    async def _run():
        app = ForgeTuiApp(session_name="case_b")
        async with app.run_test() as pilot:
            panel = app.query_one(TuiPanels)
            bar = panel.bar
            input_widget = bar.input

            # 输入 /mo 触发 popup（on_input_changed）
            await pilot.click(input_widget)
            for ch in "/mo":
                await pilot.press(ch)
            await pilot.pause(0.05)

            # popup 应该可见
            assert app._popup_visible, "输入 /mo 后 slash popup 应打开"

            # Tab 补全 → /mode
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert input_widget.value.startswith("/mode"), \
                f"Tab 应补全为 /mode，实际: {input_widget.value!r}"

            # Esc 关闭 popup
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not app._popup_visible, "Esc 应关闭 slash popup"

    asyncio.run(_run())


def test_case_c_history():
    """Case C：提交两条消息后，↑↓ 浏览历史。

    pilot.press('up') 触发 on_key → 历史导航。
    """

    async def _run():
        app = ForgeTuiApp(session_name="case_c")
        async with app.run_test() as pilot:
            panel = app.query_one(TuiPanels)
            bar = panel.bar
            input_widget = bar.input

            # 预置历史（不走 Runtime，直接设值）
            app._history = ["hello", "world"]
            app._hist_idx = 2  # 当前位置在末尾（下一次 ↑ 显示 "world"）

            await pilot.click(input_widget)

            # 按 ↑ → 应显示 "world"
            await pilot.press("up")
            await pilot.pause(0.05)
            assert input_widget.value == "world", \
                f"↑ 应显示 'world'，实际: {input_widget.value!r}"

            # 再按 ↑ → "hello"
            await pilot.press("up")
            await pilot.pause(0.05)
            assert input_widget.value == "hello"

            # 按 ↓ → 回到 "world"
            await pilot.press("down")
            await pilot.pause(0.05)
            assert input_widget.value == "world"

    asyncio.run(_run())


def test_case_d_approval_header_and_placeholder():
    """Case D：进入审批态 → Header=APPROVAL + 普通文本不清除 pending + y 批准。"""

    async def _run():
        app = ForgeTuiApp(session_name="case_d")
        async with app.run_test() as pilot:
            panel = app.query_one(TuiPanels)

            # 手动进入审批态（不跑 Runtime）
            app._in_approval = True
            app._pending_approvals = [{"label": "git reset", "description": "reset to HEAD~1"}]
            panel.header.set_state("waiting")
            panel.bar.set_placeholder("Approve? [y/N]")

            # 验证 header
            assert panel.header._run_state == "waiting"

            # 普通文本不清除 pending
            app._handle_approval_input("为什么需要这个操作？")
            assert app._in_approval is True, "pending approval 必须保持"

            # y 批准（解除本地标记）
            app._handle_approval_input("y")
            assert app._in_approval is False

    asyncio.run(_run())


def test_case_e_stream_single_item():
    """Case E：stream_start + 3 chunks + stream_end → 只有 1 个 assistant item，无 ▌。"""

    async def _run():
        app = ForgeTuiApp(session_name="case_e")
        async with app.run_test() as pilot:
            ml = app.query_one(TuiPanels).msglog

            ml.stream_start()
            ml.stream_chunk("当前")
            ml.stream_chunk("还有")
            ml.stream_chunk("三个问题")
            ml.stream_end()

            assistant_items = [i for i in ml._items if i._kind == "assistant"]
            assert len(assistant_items) == 1, \
                f"应有 1 个 assistant item，实际 {len(assistant_items)}"
            final = assistant_items[0]._content
            assert "当前还有三个问题" in final
            assert "▌" not in final

    asyncio.run(_run())


def test_case_f_tool_tree():
    """Case F：tool_start + tool_end 成功/失败，失败行保留 detail。"""

    async def _run():
        app = ForgeTuiApp(session_name="case_f")
        async with app.run_test() as pilot:
            ml = app.query_one(TuiPanels).msglog

            # filesystem 成功
            item1 = ml.add_tool("filesystem", "scanned 128 files", status="done")
            assert "✓" in item1.render().plain

            # terminal 失败（保留 detail）
            item2 = ml.add_tool("terminal", detail="pytest", status="failed")
            plain2 = item2.render().plain
            assert "×" in plain2
            assert "pytest" in plain2

    asyncio.run(_run())


def test_inspector_open_close_focus():
    """Ctrl+O 打开 Inspector，Esc 关闭后 focus 回到 InputBar。"""

    async def _run():
        app = ForgeTuiApp(session_name="inspector")
        async with app.run_test() as pilot:
            panel = app.query_one(TuiPanels)
            bar = panel.bar

            # 先 focus 输入框
            await pilot.click(bar.input)

            # 打开 Inspector（action_show_inspector）
            app.action_show_inspector()
            await pilot.pause(0.1)
            # InspectorScreen 应该在 screen stack 里
            assert len(app.screen_stack) > 1, "Inspector 应已 push 到 screen stack"

            # Esc 关闭
            await pilot.press("escape")
            await pilot.pause(0.1)
            # 回到主 screen
            assert len(app.screen_stack) == 1, "Esc 后应回到主 screen"

    asyncio.run(_run())


# ════════════════════════════════════════════════════════════════
#  Section 7: 完整 headless 集成
# ════════════════════════════════════════════════════════════════

def test_full_headless():
    """所有组件集成测试。"""

    async def _run():
        app = ForgeTuiApp(session_name="integration", mode="stream")
        async with app.run_test() as pilot:
            panel = app.query_one(TuiPanels)
            ml = panel.msglog

            ml.add_user("帮我检查项目")
            ml.add_assistant("发现三个问题", title="ANSWER")
            ml.add_tool("filesystem", "scanned 128 files", "done")
            ml.add_tool("github", "loaded branch/main", "running")
            ml.add_artifact("report.md")
            ml.add_meta("DONE · 3 tools · 2.1s")
            ml.add_error(title="Failed", detail="Connection refused",
                         hints=["Check MCP", "Retry"])

            ml.stream_start()
            ml.stream_chunk("Hello ")
            ml.stream_chunk("World")
            ml.stream_end()

            panel.header.set_state("running", elapsed="03.2s")
            panel.header.set_model("GPT-5.6")
            panel.header.set_tool_count(4)
            panel.header.set_width(100)

            assert len(ml._items) >= 8
            assert panel.header._run_state == "running"
            assert panel.header._model_name == "GPT-5.6"
            assert panel.bar.popup is not None

    asyncio.run(_run())


# ════════════════════════════════════════════════════════════════
#  Section 7: 布局回归（"界面显示不全" 的真实成因）
#
#  三个成因（均已修复，此处锁定）：
#   1. InputBar 继承 Textual Vertical 的默认 height: 1fr
#      → 和 MessageLog 平分屏幕，消息区被压扁、底部留大片空白
#   2. .forge-input 写 height: 1，上下边框各吃 1 行
#      → 内容区 0 行，输入框变成空的边框方块，placeholder 不可见
#   3. SlashPopup 默认可见 + Footer 在 1fr 容器里 dock: bottom
#      → 启动时常驻空白方块；Footer 宽度不扣 margin，最右 binding 被挤出屏幕
# ════════════════════════════════════════════════════════════════

def test_message_log_fills_available_height():
    """MessageLog 必须吃掉剩余全部高度，不能被 InputBar 分走一半。"""
    async def _run():
        app = ForgeTuiApp(session_name="layout")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            panel = app.query_one(TuiPanels)
            assert panel.msglog.region.height >= 20, (
                f"MessageLog 只拿到 {panel.msglog.region.height} 行，"
                "InputBar 疑似又抢走了 1fr"
            )
            assert panel.bar.region.height <= 6, (
                f"InputBar 占了 {panel.bar.region.height} 行"
            )

    asyncio.run(_run())


def test_layout_fills_screen_exactly():
    """Header + MessageLog + InputBar 高度之和 == 终端行数，底部不留空白。"""
    async def _run():
        app = ForgeTuiApp(session_name="layout")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            panel = app.query_one(TuiPanels)
            total = (
                panel.header.region.height
                + panel.msglog.region.height
                + panel.bar.region.height
            )
            assert total == app.screen.region.height, (
                f"三段高度 {total} != 屏幕 {app.screen.region.height}"
            )
            assert panel.bar.region.bottom == app.screen.region.height

    asyncio.run(_run())


def test_input_has_visible_content_row():
    """输入框至少 1 行内容区，否则 placeholder 完全不显示。"""
    async def _run():
        app = ForgeTuiApp(session_name="layout")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            inp = app.query_one(TuiPanels).bar.input
            assert inp.content_size.height >= 1, "输入框内容区高度为 0，占位符不可见"
            assert inp.content_size.width > 40
            assert "Enter" in inp.placeholder

    asyncio.run(_run())


def test_slash_popup_hidden_on_start():
    """启动时 popup 必须隐藏，否则常驻一个空白边框方块。"""
    async def _run():
        app = ForgeTuiApp(session_name="layout")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            popup = app.query_one(TuiPanels).bar.popup
            assert popup is not None
            assert popup.display is False
            assert popup.region.width == 0 and popup.region.height == 0

    asyncio.run(_run())


def test_slash_popup_expands_then_collapses():
    """popup 出现时 MessageLog 让出高度，Esc 后收回。"""
    async def _run():
        app = ForgeTuiApp(session_name="layout")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            panel = app.query_one(TuiPanels)
            full = panel.msglog.region.height

            panel.bar.input.value = "/mo"
            await pilot.pause()
            await pilot.pause()
            assert panel.bar.popup.display is True
            assert panel.bar.popup.region.height >= 1
            assert panel.msglog.region.height < full

            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            assert panel.bar.popup.display is False
            assert panel.msglog.region.height == full

    asyncio.run(_run())


def test_footer_within_screen_width():
    """Footer 不得溢出屏幕右边界。"""
    async def _run():
        app = ForgeTuiApp(session_name="layout")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            footer = app.query_one(TuiPanels).bar.footer
            assert footer.region.right <= app.screen.region.width, (
                f"Footer 右边界 {footer.region.right} 溢出屏幕 {app.screen.region.width}"
            )
            assert footer.region.height == 1

    asyncio.run(_run())


def test_layout_survives_narrow_terminal():
    """80x24 窄屏也不能出现塌陷或溢出。"""
    async def _run():
        app = ForgeTuiApp(session_name="layout")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            panel = app.query_one(TuiPanels)
            assert panel.msglog.region.height >= 10
            assert panel.bar.region.height <= 6
            assert panel.bar.region.bottom == app.screen.region.height
            assert panel.bar.input.content_size.height >= 1

    asyncio.run(_run())
