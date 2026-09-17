# FORGE 消息渲染与 Markdown 改造报告

> 项目：FORGE / my_creative_agent · 日期：2026-09-06 · 前端资源 v21
> 目标：解决「Agent 输出缺乏格式与视觉层级」——Markdown 原样裸显、过程与正文混流、无安全渲染层。

---

## 1. 原问题位置

- `web/runtime/workspace.js::messageRow`：assistant 内容用 `textContent` 整段直插 → `**`、`*`、`#`、``` ``` 全部原样显示；
- SSE 流式：`liveEls.streamText.textContent += delta` 逐 token 追加（v18 逐字流实现），Markdown 跨 chunk 不成立；
- 过程（activity）与正文（assistant）共用 `.msg-text/.act-empty` 近似字号颜色，无视觉层级；
- 无 Markdown 解析器，全部渲染点各自实现（workspace/Drawer/交付卡/失败卡）。

## 2. 原渲染链路

```
reply SSE / reply_delta → workspace.onEvent
  → textContent 追加（流式）或 messageRow(textContent)（历史）
  → 与 act-card（过程）混排，同字号同色
  → 无任何解析/消毒/结构（也因 textContent 无 XSS，但同样无格式）
```

## 3. 新 MessageRenderer（统一内容类型）

`web/runtime/markdown.js`（安全渲染器）+ workspace 统一调用：

| 类型 | 渲染 | 说明 |
|---|---|---|
| user_message | 纯文本气泡 | 用户输入不做 Markdown |
| assistant_message | `.agent-markdown` ← `RT.markdown.renderTo` | 最终正文/解释/问题/总结 |
| activity | `.act-card`（13-14px 灰蓝辅助色） | 搜索/读取/修改/运行/验证过程，独立于正文 |
| approval | `.approval-inline`（需要你确认 + [看看会改什么][暂不][继续]） | 独立组件，新增“看看会改什么”→Drawer 修改 Tab |
| result | `.deliver-card`（✓ 已完成 + 完成事项/修改/生成/验证/下一步） | 独立 ResultCard |
| error | `.fail-card`（遇到一个问题 + [继续尝试][查看问题]） | 独立 ErrorCard |
| code/file | `.code-block`（语言标签+复制按钮+水平滚动）/ Drawer 文件行 | 代码块独立样式 |

## 4. Markdown Renderer（安全白名单 AST）

- 解析：`parseMarkdown(src) → AST`（纯数据，可序列化测试）：
  heading h1-h6 / paragraph / **bold** / *italic* / _underline_ / ul / ol / blockquote / inline code / fenced code（含语言）/ link / table / hr；
- 渲染：`renderTo(container, raw)` —— **仅 `document.createElement` + `textContent`**，无任何 `innerHTML`；
- 链接白名单 `cleanHref`：http/https/mailto/#/相对路径；`javascript:`/`data:`/`vbscript:` → `#`；
- 图片语法 `![]()` 按链接文本降级（不渲染 `<img>`，防跟踪/onerror）；
- 模型过程性列表行（`* 正在… / 接下来… / 我现在…`）自动加 `agent-thought-line` 弱化为辅助色——真实过程只来自 Runtime 事件（Activity）。

## 5. SSE 流式处理

- `reply_delta` → `streamRaw += chunk`（rawContent 语义），**60ms debounce** 全量 `renderTo` 重建；
- 不做 `innerHTML += parse(chunk)`；跨 chunk 的 `**粗体**`/列表/标题/fence/table 均正确（有专项测试：分块拼接渲染 == 整段渲染）；
- `assistant.reply` 到达后用最终 content 重渲染同一气泡（rawContent 最终一致）；DB 只存原始 Markdown，无 HTML 回写。

## 6. Activity 渲染（视觉分层 §六）

- 正文：15px / line-height 1.7 / 正文色；
- Activity：13-14px / 灰蓝辅助色 / line-height 1.5；标题 14px/600；
- 交付/错误卡独立标题层级（16px/15px）；用户消息右对齐独立底色。

## 7. 安全处理（§十一）

XSS 防护保持并加强：解析产出 AST，渲染只建白名单节点，模型输出永不进入 innerHTML/属性；链接协议白名单。实测：
`<script>`、`<img onerror>`、`<iframe>`、`<svg onload>`、`<style>` 均作为纯文本显示且不执行（`window.__xss` 未触发、DOM 无 script 元素）；`javascript:` 链接 → `#`。

## 8. 测试结果

- 新增 `tests/test_markdown_js.py`（21 用例，经 Node 运行 `tests/markdown_runner.js`）：普通段落 / 粗斜体 / 无序·有序列表 / 标题 / 引用 / 行内代码 / 代码块 / 表格 / 链接 / 分隔线 / 中文+代码混排 / 超长回答 / **SSE 跨 chunk 分块==整段** / 恶意 HTML（script·img·iframe·svg·style）/ 恶意链接（javascript·data·vbscript）/ 安全链接保留；
- CDP 真机：真实对话渲染出 h2/strong/ul（innerText 无裸 `**`）；确定性注入验证 code-block（含语言标签+复制按钮）、table、`javascript:`→`#`、XSS 零执行；正文 15px 与活动 13-14px 分层生效；
- 全量离线回归：**292/292 通过**。

## 9. 修改的文件清单

- `web/runtime/markdown.js`（新增：安全 Markdown AST 解析 + 白名单 DOM 渲染 + 链接白名单 + thought-line 弱化）
- `web/runtime/workspace.js`（messageRow 走渲染器；SSE rawContent+debounce 重渲染；stream_reset/reply 同步；审批卡加“看看会改什么”；stopLive 清理流式状态）
- `web/runtime.html`（引入 markdown.js；`.agent-markdown`/`.code-block`/`.md-table`/act 分层全套排版 + 浅色适配；资源 v21）
- `tests/markdown_runner.js`（新增 Node 运行器）
- `tests/test_markdown_js.py`（新增 21 用例）

## 附：未引入第三方依赖的说明

项目为原生 JS 无构建链，未引入 marked/DOMPurify；自研解析器为纯函数 AST（白名单渲染天然免疫 XSS），既满足“Parser→Sanitizer→Safe DOM”的要求，又可通过 Node 直接做离线单测。
