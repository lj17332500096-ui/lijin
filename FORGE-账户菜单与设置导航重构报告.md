# FORGE-账户菜单与设置导航重构报告

> 项目：FORGE / my_creative_agent · 日期：2026-09-06 · 前端资源 v28 · 全量 320 tests OK
> 规范：gpt-taste / redesign-existing-projects（本会话已完整读取并遵守；本次以“真实功能不破坏、返回路径不丢状态”为 P0）。

---

## 1. 原账户菜单结构

左下「本地账户」Popover 展开 10 项：设置/外观/通知/回收站 + 管理组（工具与技能/记忆/计划任务/审批记录/Trace·审计）——等同于第二套主导航，高级入口全暴露给普通用户。

## 2. 原无法返回 Conversation 的真实原因

1. 点击账户项后调用旧 `renderSettings`/`switchLegacy`：设置渲染进 `#stream`（清空对话 DOM），高级项切换到独立 legacy section（`#page-memory` 等）——**两条路都没有保存“来源项目”**；
2. 返回只可能靠浏览器 back/重选左栏项目 → openTask 无草稿/滚动恢复、且运行中会 `stopLive()` 断开 SSE（潜在取消 Run）；
3. 首页进入设置后无明确返回目标；`currentProject` 语义与 UI 页面耦合。

## 3. Router / Store 原问题

原生 JS 无 Router：视图 = `state.page` + legacy sections；没有独立的 `uiSurface`，也没有「返回上下文」状态；`state.active`（currentProject）在打开设置时仍在，但没有任何结构保存来源，且 openTask 无条件 stopLive。

## 4. 新账户菜单

精简为 **2 项**：设置、回收站。点击行为只负责打开辅助面；外观/通知/记忆等全部移入设置；`Popover` 保持轻量（固定宽度分组、标题+副标题+两项+页脚）。外部点击关闭；Esc 关闭（T7/T8 验证）。

## 5. 新 Settings IA（统一 Shell）

设置 = 单页 Shell：顶部 **固定「← 返回 <来源>」+ 设置**；一级导航 chips：**常规 / 外观 / 通知 / 记忆 / 数据 / 高级**。
- 常规：账户、使用方式（推荐/安全/自动）
- 外观：主题三态
- 通知：开关（本地偏好）
- 记忆：**长期记忆**开关（写入本地偏好并调后端 `/api/settings/memory` 设置进程门控 `FORGE_MEMORY_ENABLED`，remember/recall 真实拦截）+ **查看长期记忆**（列出并可删除单条 `/api/memories/delete`）
- 数据：数据库路径/大小/备份数
- 高级：只读 Runtime 配置 + 四个入口：**扩展能力**、**定时任务**、**安全与记录**、**诊断与审计**（分别指向既有 tools/schedules/approvals/trace 页面，带悬浮「← 返回 设置」）
- 全局记忆与 Project 记忆范围分开（后者仍在 Project 设置）

## 6. returnTo 实现

不依赖 history.back：模块级 `surfaceCtx`（origin：kind/id/title/scroll/draft），进入辅助面前 `captureOrigin()`；固定返回按钮 `surfaceBack()` 按 origin 恢复：
- project → openTask(原 projectId, keepLive) → 恢复滚动与草稿
- list → 项目列表页
- home → 首页
设置内部导航（外观→记忆→数据→高级）不改变 origin；高级 legacy 页用全局悬浮返回条回到设置，设置再回到来源（测试 4/10 验证：外观/通知/记忆/数据/高级 任意穿行后最终仍回进入前 Project）。

## 7. Project state 保留机制

- `state.active` 与视图解耦：打开 Settings 不清 `currentProject`、不改左栏选中；
- UI 面（conversation/settings/advanced）与 Project state 分离；
- 导航重置：renderHome / renderTaskList / openTask(显式) 会清空 `surfaceCtx`（离开即清除返回上下文，避免串台）。

## 8. Composer Draft 保留机制

新增 `composerDraftMemo`：输入事件实时记忆；进入设置/返回时经 capture+multi-timer 恢复（120/380/800/1600ms，防异步渲染竞态）；发送成功后清空。T2 验证：草稿往返后仍在。

## 9. Scroll 恢复

返回时按进入前 `stream.scrollTop` 恢复（多计时器，内容加载后二次对齐）；精确度受内容回流影响 ±（报告 §未解决）。

## 10. SSE / Running Run 行为

- 进入 Settings 不触碰 SSE：Run 继续执行（设置只改 UI 面，不 unsubscribe/不 cancel）；
- 返回同项目时若该 Run 仍在流（`keepLive` 检测 containerId 相同）：**跳过 stopLive**，仅重取容器并恢复 live 视觉（进度帧/流式气泡按已累积 rawContent 重建）；若 Run 已完成则按真实最新状态渲染，不恢复旧快照。

## 11-15. 入口调整汇总

- 记忆 → 设置/记忆（长期记忆开关+列表）；Project 记忆范围留在 Project 设置
- 工具与技能 → 设置/高级/扩展能力
- 计划任务 → 设置/高级/定时任务
- 审批记录 → 设置/高级/安全与记录
- Trace·审计 → 设置/高级/诊断与审计
（同名既有页面保留；后端 Skills/Tools/Schedules/Approvals/Audit/Trace/Memory 能力零删除）

## 16. 修改文件

- `web/runtime/workspace.js`：surfaceCtx/returnTo、renderSettings→统一 Shell（sections）、高级悬浮返回条、draftMemo、openTask(keepLive)+resumeLiveVisual、账户菜单行为精简、Esc 关闭、renderTrash 返回头、capture 时序修复
- `web/runtime.html`：账户 Popover 精简为 2 项、v28 样式（settings-topbar/settings-nav/chip.active/adv-global 等）
- `webapp.py`：`POST /api/memories/delete`、`POST /api/settings/memory`
- `runtime/task_manager.py`：`delete_memory_row`
- `tools.py`：`FORGE_MEMORY_ENABLED` 门控（remember/recall）

## 17. 自动化测试

全量 **320 tests OK**（记忆/附件/主题等回归无破坏）。新增浏览器行为未纳入离线单测（依赖真实页面），见下 E2E。

## 18. 浏览器 E2E 结果（真实 Chrome/CDP）

- T1：测试111111 → 设置/外观/记忆 → 返回 → 仍是 测试111111，对话正常 ✅
- T2：草稿「这是一个还没发送的草稿」→ 设置/记忆 → 返回 → 草稿仍在 ✅
- T4：外观→通知→记忆→数据→高级 穿行 → 返回进入前 Project ✅
- T5：首页→设置 返回标签「← 返回 首页」→ 回到首页 ✅
- T7/T8：外部点击/Esc 关闭 Popover ✅
- T9：菜单仅 设置|回收站（无 工具/记忆/计划/审批/Trace）✅
- T10：高级→扩展能力 打开 legacy 页并显示「← 返回 设置」，返回设置后再返回项目 ✅
- T6：刷新 Settings 场景未实现持久化（会话内 returnTo）；回落路径=设置页顶返回首页/项目（死路已消除）——见未解决
- T3（20s Running 保持）：已实现 keepLive 机制；端到端长时验证（运行中开设置并等待完成再返回）列为未完成项（需一次真实长任务）

## 19. 未解决问题

1. T6 刷新后 returnTo 持久化：当前刷新回首页（有明确返回入口，非死路）；持久化到 sessionStorage 可后续补
2. 滚动恢复为近似（内容回流改变 scrollHeight 时误差 ≤ 一屏内）；精确 anchor 恢复列入 P2
3. T3 长时 Running 的浏览器级自动化验证待跑（机制已实现并通过 keepLive 代码路径）
4. 高级 legacy 页（tools/schedules/…）顶部仍有各自技术标题（内容来自既有页面），普通文案收敛列为后续 P2
