# FORGE TUI 界面设计 Brief（给 GPT 用）

## 项目背景

FORGE 是一个本地 AI Agent 全能助手，后端是 Starlette + 多 MCP 工具链（GitHub、浏览器、文件系统、终端、数据库等 6 个 MCP 服务器），前端是一个终端 TUI（Textual 8.x 框架，Rich 作者出品）。

当前 TUI 是一个**最小骨架原型**（约 400 行），功能上可用但视觉粗糙，需要重新设计。

---

## 技术约束（必须遵守）

1. **框架**：Textual 8.2.8（Python TUI 框架，Rich 风格）
2. **可用组件**：
   - `Static` — 静态文本渲染（支持 Rich markup）
   - `Log` — 自动滚动的消息日志（8.x API：`Log(highlight=True)`，`write(str)` 只接受纯字符串 + Rich markup 标签）
   - `Input` — 单行输入框
   - `Vertical` / `VerticalScroll` / `Horizontal` — 布局容器
   - `Footer` — 底部快捷键提示栏
   - `Label` / `Button` / `TabbedContent` — 可扩展
   - `Screen` — 全屏模态
3. **CSS 语法**：Textual 自有 CSS 子集，支持属性：`width/height`（数字或 `1fr`）、`margin/padding`、`background`（颜色变量 `$primary` `$surface` `$panel`）、`border`（`solid`/`rounded`/`heavy`/`double`/`panel`）、`color`、`text-align`、`content-overflow`
4. **8.x 已知坑**（踩过的，设计时注意）：
   - `Widget.classes` 是 `frozenset`，构造时传 `classes="foo"`，不能运行时 `.add()`
   - `Widget.log` 是只读 property，不能自定义实例属性名 `log`
   - `Log.write()` 没有 `end=""` 参数（Rich 旧版有），流式打字机效果需要自己 buffer
   - `Log.__init__` 只接受 `highlight=True/False`、`max_lines`、`auto_scroll`
   - `App.debug` 是只读 property，不能用 `debug` 做实例属性名
   - `App.workers.run_worker(coro, exclusive=False)` 是 8.x 正确的异步任务方式

---

## 当前布局结构（可重新设计，但要保留这些功能区域）

```
┌─────────────────────────────────────────────────────────┐
│ StatusHeader (h=3)                                       │
│  "FORGE · 全能助手 TUI   session=personal   [running]"  │
├─────────────────────────────────────────────────────────┤
│ MessageLog (h=1fr, 可滚动)                              │
│  [bold]你[/bold]                                         │
│  你好，帮我搜索一下...                                    │
│                                                        │
│  ⚙ 正在调用：GitHub                                        │
│  ✓ 搜索完成                                               │
│                                                        │
│  [green]助手 · ANSWER[/green]                             │
│  这是回复内容...                                          │
│  ✓ 完成 · 回答                                            │
│  🗂 产物: report.xlsx (spreadsheet)                      │
│  ⏱ 3.2s  run=abc-123                                     │
├─────────────────────────────────────────────────────────┤
│ InputBar (h=5)                                          │
│  [ 输入框（placeholder 文字）]                           │
│  Footer: Ctrl+Q 退出  |  Ctrl+L 清屏  |  /help 帮助      │
└─────────────────────────────────────────────────────────┘
```

---

## 需要设计决策的问题

1. **配色方案**：目前是默认 Textual 配色，建议设计一套 FORGE 专属配色（深色系，赛博朋克/科技感），支持亮暗模式
2. **StatusHeader 信息密度**：当前只有 session 名 + 状态，可以加：当前模型名、本轮耗时、工具数量
3. **MessageLog 视觉层次**：用户消息、助手消息、工具活动、错误、产物、耗时 6 类信息，如何视觉区分（颜色/边框/缩进）
4. **流式打字机效果**：当前实现是完整一行才显示，可以设计更好的逐字效果（如光标跟随、进度指示）
5. **工具活动面板**：当前是内嵌在消息流里的单行，是否要独立面板（侧边栏显示当前工具调用进度）
6. **命令系统（斜杠命令）**：16 条斜杠命令，当前只显示在输入框 placeholder，是否需要命令补全、历史、提示
7. **错误展示**：当前 `diagnose()` 返回 head + hints 列表，如何视觉呈现（折叠/展开？颜色？）
8. **等待审批状态**：`waiting_approval` 状态下，用户需要输入 yes/no，如何设计交互（当前是回退到普通输入框）
9. **Footer 快捷键提示**：当前只有 2 个，扩展后如何显示（Tab 切换？？）
10. **整体视觉风格**：赛博朋克/极简/终端原生？

---

## 现有代码结构（供参考，改的时候别破坏）

```python
# cli/tui/app.py
class ForgeTuiApp(App):
    CSS = "..."          # Textual CSS 字符串
    BINDINGS = [...]    # 快捷键绑定
    
    def compose(self) -> ComposeResult:
        yield TuiPanels(session_name)
    
    def on_input_submitted(self, event): ...   # 输入处理入口
    def _on_tui_event(self, channel, payload): ...  # 事件→面板映射
    def _render_result(self, result, started): ...  # 结果渲染
    def _render_exception(self, exc): ...          # 异常渲染

# cli/tui/panels.py
class StatusHeader(Static):       # 顶部状态栏
class MessageLog(VerticalScroll): # 消息流（核心面板）
class InputBar(Vertical):        # 底部输入栏
class TuiPanels(Vertical):       # 容器（三栏布局）
```

---

## 设计输出期望

请输出：
1. **视觉 mockup**（ASCII art 或描述性 wireframe，深色/浅色各一套）
2. **配色方案**（具体颜色 hex 值，Textual CSS 变量映射）
3. **布局 CSS**（完整的 Textual CSS 字符串，可直接贴进 `ForgeTuiApp.CSS`）
4. **各面板组件**（StatusHeader / MessageLog / InputBar 的 `render()` 方法代码）
5. **交互说明**（流式效果、审批交互、命令补全的实现思路）

设计目标：让 FORGE 从一个"能用的骨架"升级到一个"有视觉识别度的专业 TUI"。
