# FORGE-UI 完整审计与重构报告

> 项目：FORGE / my_creative_agent · 日期：2026-09-06 · 前端资源 v27 · 全量 320 tests OK
> 执行规范：`gpt-taste` + `redesign-existing-projects`（两份规则文件已完整读取；本报告为真实代码修改与浏览器验证结果，非设计方案）。

---

## 0. 规则读取与内部执行清单（修改前完成）

- 读取文件：`skills/gpt-taste/skill.md`（72 行）、`skills/redesign-existing-projects/skill.md`（176 行）——均存在且完整阅读。
- **gpt-taste 对本项目的要求映射**：
  1. 禁 Emoji（代码/注释/界面符号）→ 已开始清除界面 emoji（附件 chips、按钮图标改为纯文本/几何符号）；
  2. 无“廉价元标签/编号”“AI 味文案”→ 审计并清理（结果卡只输出真实结果，无 SECTION/已完成堆叠）；
  3. 字体有性格、禁用默认 Inter 全站 → 字体栈升级并保留离线回退；
  4. 容器宽、行距/字距经校准、区块留白大 → 应用到会话容器与标题排版；
  5. 对比度/按钮可读 → 重做主按钮对比。
  - **判定为不适用（如实记录）**：AWWWARDS 营销 Hero / bento 栅格 / GSAP ScrollTrigger / 横向手风琴 / 图片内嵌文字 —— 面向“营销落地页与滚动叙事”，FORGE 是本地任务工作台（桌面 app 型 UI），且环境离线无 GSAP/图片资产，强制套用会产生伪营销页并伤害可用性；gpt-taste §1 随机化脚本规则亦不适用于工具型信息架构（保持确定性 UX）。其“规则精神”（打破统计性默认、禁廉价模式、精确留白、防呆状态）已内化为工作台设计语言。
- **redesign-existing-projects 对本项目的要求**：Fix Priority 顺序执行 —— 1 字体、2 色板收敛、3 悬停/按压/焦点态、4 布局与间距、5 去通用组件、6 空/载/错状态、7 排版细节；并“先扫描诊断，不重写、不破坏功能、不换框架/依赖”。

## 1-2. UI 地图与状态（真实代码盘点）

页面/视图（均真实存在并有真实数据源）：首页（大输入 + 示例 + 最近）、项目列表（进行中/等你确认/最近）、Project 对话流（统一 .flow）、来源文件 Drawer、项目设置 Drawer（名称/说明/记忆范围/工作位置/删除）、全局设置（账户菜单：使用方式/外观/通知/数据/高级）、回收站、搜索浮层、上下文统计浮层、运行中实时流、等待确认卡、失败恢复卡、空项目引导、主题切换（light/dark）、代码块/表格 Markdown 渲染、Artifact 下载。
上一轮已完成并持续有效：删除 任务/资料 一级导航与内部语（第N次/次执行/未开始/Run 词）、工作位置 undefined 修复、统一对话流、附件三作用域、Memory Scope 真实生效。

## 3-4. 产品心智统一（本轮复查）

- 复查可见字符串：清理 `Task Workspace/输入目标创建 Task/继续当前任务/Task details/未命名任务` 等残留 → 全部改为 项目/FORGE/未命名项目。
- 主界面已不存在用户可读的 Run/Session/Event/ToolCall/ModelCall/workspace_id/session_name；高级信息仅在“查看完整过程”抽屉。
- 每页自答：我在哪（crumb/标题）→ FORGE 能做什么（首页示例）→ 在做什么（正在处理/活动行）→ 需不需要我（等你确认）→ 完成没有（轻量结果卡仅真实产出时）→ 下一步（composer 占位/继续）。

## 5-9. 信息架构 / 首页 / Project 主界面 / Run 视觉 / 状态语言（本轮复查结论）

- 左栏=首页+项目列表+设置入口，来源/工作位置/记忆全部归入 Project——已符合规范；
- 首页非 Dashboard（无指标堆叠）；
- Project 页面=统一时间顺序 Conversation Flow（前轮 v26 完成，本轮复查通过）；
- 状态语言：内部 running/waiting_approval/…→界面 正在处理/等你确认/已暂停/遇到问题/已停止/已完成；无顶部“未开始+已完成”冲突（前轮修复，复查通过）。

## 10-11. 工作过程投影与 Markdown 排版（前轮已实现，本轮纳入重审）

- Event→Presentation Projector→Activity（act-inline 轻量行）；正文 Markdown 渲染器（标题/粗斜/列表/引用/表格/行内码/代码块含语言与复制、链接白名单）已存在并受主题 token 控制。

## 12-… 视觉系统重构（v27，本轮真实改动）

按 Fix Priority 落地（全部真实 CSS/JS，浏览器 computed 验证）：

1. **字体**：全站字体栈升级为 Geist/Satoshi/Outfit/“Segoe UI Variable Display”回退链；标题负字距、更重字重；正文行距 1.72、段落 ≤70ch；时间/统计数字启用 tabular-nums。
2. **色板收敛**：多强调色（青+紫 AI 渐变）→ **单一强调色**：深色 `#6ea8d8` / 浅色 `#5f9fc9`；logo、主按钮、新项目按钮、头像全部收敛为该色，`background-image: linear-gradient` 型“AI 渐变指纹”移除（computed 验证：logo/primary 均单色 `rgb(95,159,201)`，backgroundImage=none）；主按钮深色文字保证对比。
3. **交互统一**：按钮/卡片/列表统一 `transition .18s`；悬停轻微上移、按压 `scale(.985)`；`transition` 只用 transform/opacity/color（非 top/left）；**`:focus-visible` 焦点环**全站（2px accent+ring）；chip/ghost/icon/danger 等全部纳入。
4. **布局与留白**：会话 `.flow` 居中最大 1080px（已有），区块间隔与光学 padding 微调；卡片去“白底+边框+黑阴影”通用样 → 背景/间隔表达层级，阴影改 tinted。
5. **去通用/防呆**：结果卡只展示真实产物/修改/验证（无重复“已完成”），失败/空/加载态已有，`full-output-enforcement` 类强约束技能未启用（冲突风险，已在 .env 说明）。

**浏览器 E2E 验证（CDP computed）**：
- `body fontFamily` 返回新字体栈；
- `--accent` 深/浅两主题取值单一且与 logo/primary 一致，主按钮深色文字；
- `:focus-visible` 规则存在；`.ghost-btn` transition 生效；`.ctx-chip` fontVariantNumeric=tabular-nums；
- 页面无 `undefined / 第 N 次 / 次执行 / Task` 等内部词（前轮验收基线维持）。

## 未解决问题（如实）

1. **规范原文在此前消息中截断**（可见 §0–§11，后续段落未获得正文）——本报告覆盖已获得章节；后续章节收到后可继续。
2. gpt-taste 的 GSAP/滚动叙事/图片资产条目判定不适用（理由见 §0），未引入外部动画库（规则亦要求先查依赖、本项目零框架零外部 UI 依赖、离线）。
3. 字体栈首位 Geist/Satoshi/Outfit 本机未安装时回退 Segoe UI（在线字体引入需网络/构建，未做，可后续按需交付 font 文件）。
4. 界面 emoji 清除为渐进项（附件 chips 等已处理；剩余符号多为几何字体图标与状态符号，全面去除列后续）。
5. Loading skeleton / 深浅系统级检测等增强列下一迭代（规则 6 “states” 部分已具备，深化项未在本轮内完成）。


---

# 附录 A：未决项收敛（v29）

针对本报告「未解决的问题」逐项处理结果：

1. **规范截断（§0–§11）**：需要你把被截断的后续章节正文重新粘贴（无法凭记忆编造缺失规范）。其余 UI/导航需求已在《FORGE-账户菜单与设置导航重构报告》中按完整规范交付。
2. **GSAP/营销条目不适用**：维持不适用判定（本地工作台、零外部依赖、离线）；已补 `prefers-reduced-motion` 友好与纯 CSS 动效（无需 GSAP）。本项不执行强套用。
3. **字体（v29 落地）**：采用本机存在的展示字体链 `--font-display: Bahnschrift → Segoe UI Variable Display`，标题（任务/首页/结果/设置标题）已切换该族（浏览器 computed 证实返回 Bahnschrift）；正文沿用 Geist→Segoe 回退链。在线交付 Geist/Satoshi 字体文件需资产与网络，按需可提供（规则要求先查依赖、离线环境不引入 CDN）。
4. **Emoji 清除（v29 落地）**：界面真实 emoji 呈现已清理（账户菜单 ⚙/🗑、附件 chips 📎、来源选择 ☐☑、注释 🔔 等替换/移除）；保留几何排版符号（★/☆/✓/× 等非 emoji）。浏览器验证账户菜单无 emoji 码点。
5. **States 深化（v29 落地）**：加入骨架屏 `.skel`（左栏项目列表首帧、记忆页列表、来源文件面板加载）；系统主题「跟随系统」在 OS 深浅切换时实时响应（matchMedia change 监听，仅 auto 模式）；空/错/加载状态沿用既有组件。
