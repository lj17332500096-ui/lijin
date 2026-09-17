# 此刻 · NOW — 技能库与运行状态

`skills/<技能名>/skill.md` = 人设指令（由 `skills_loader` 读取 `.env` 的 `SKILLS=` 启用项）；
`tools.py`（可选）里的 `@function_tool` 会注册为可调用工具。

## 启用 / 停用 / 重启
- 在 `.env` 的 `SKILLS=技能1,技能2` 增删技能名（仅 `字母/数字/_/-`），改后**重启 webapp** 生效。
- 技能目录保留在 `skills/` 即不删除；停用 = 只是不在 `SKILLS=` 里。

## 当前启用（16 个）—— 均在 agnes 上真实验证过
验证方式：真实模型运行示例提示，确认按各自技能结构输出（✅ = completed 且结构正确；⚠️ = 触发正确，但需确认关键信息或走审批门后再执行）。

| 组 | 技能 | 状态 | 验证要点 |
|---|---|---|---|
| 翻译 | `baoyu-translate` | ✅ | 中英互译（保留结构/术语） |
| 文案 | `content-creator` | ⚠️ | 品牌声音+SEO；缺信息先澄清后创作 |
| 邮件 | `email-drafter` | ⚠️ | 先确认收件人/主题后写草稿 |
| 内部沟通 | `internal-comms` | ✅ | 输出完整内部公告/通知草稿 |
| 文档协同 | `doc-coauthoring` | ✅ | 先大纲(目的/读者/结构)再分块 |
| 每日计划 | `daily-plan` | ⚠️ | 先确认时间/待办后给计划 |
| 会议总结 | `summarize-meeting` | ✅ | 决策/行动项(人·截止)/待确认 |
| 周复盘 | `weekly-review` | ✅ | 成果/学到/卡点/下周 |
| 笔记整理 | `note-organizer` | ✅ | 结构化 Markdown 知识库(优缺点/组件表) |
| 简历定制 | `resume-tailor` | ✅ | JD 关键词/匹配/重写/ATS |
| PDF | `pdf` | ⚠️(审批门) | 正确方案/代码(pypdf/reportlab)；执行前审批 |
| MCP | `mcp-builder` | ⚠️(审批门) | 完整 MCP 服务器代码(mcp SDK)；执行前审批 |
| 前端设计 | `frontend-design` | ⚠️ | 先确认品牌/受众后给配色·字体·版式 |
| 主题 | `theme-factory` | ⚠️ | 先确认内容后给主题(配色+字体) |
| 品牌规范 | `brand-guidelines` | ✅ | 输出色板表 + 可落盘 Word/PPT |
| GIF | `slack-gif-creator` | ✅ | Slack GIF 尺寸/帧率/色数 + 命令 |

> 说明：⚠️ = 技能正确触发；其中"先澄清"来自基础系统指令（设计/创作/计划类关键信息缺失先问），给足信息后即出完整结构；"审批门" = 代码/沙箱执行需你在运行时批准。

## 停用但在 `skills/` 内，需要时再启
`skill-creator`、`skill-assets`、`simplify`、`summarise-session`、`daily-summary`、`gpt-taste`、`redesign-existing-projects`、`dep_doctor`、`gorden-ppt`、`weekly_report`、`discernment-nudge`

### skill-creator（造技能，按需启用）
重型编排（草稿→评估→迭代→落盘），默认**停用**避免占轮次。需要造/改技能时临时启用：
1. 把 `skill-creator` 加入 `.env` 的 `SKILLS=`（例如追加 `,… skill-creator`）。
2. **重启 webapp**。
3. 用后如不常造技能，再把它从 `SKILLS=` 移除并重启。

### read_skill_asset（读取技能 assets）
需读取某技能的 `assets/references|scripts` 时，启用 `skill-assets`（同上加入 `SKILLS=` 并重启）即提供 `read_skill_asset(技能名, 相对路径)`；只读、防路径穿越、单文件 ≤200KB。

## 运行依赖
- `pdf`：`pypdf`（已装）；高级表单/OCR 需 `pymupdf`/`reportlab`（可选 `pip install pymupdf reportlab`）
- `mcp-builder`：`mcp`（已装）
- `slack-gif-creator`：`Pillow`（已装）+ 主机 `ffmpeg`
- 其余：纯提示词，无额外依赖
- Word/Excel/PPT 的创建/读取由**运行时内置原生工具**承担（`save_word_doc/save_excel_workbook/save_ppt_deck/read_office_file/read_spreadsheet`），无需技能依赖。

## 模型与安全
- 当前模型：**agnes-2.5-flash**（远程网关 `https://apihub.agnes-ai.cn/v1`，`FORGE_MODEL_PREF=gateway`）。运行快、`completed` 秒级。
- API Key 前缀 `cpk-` 已纳入脱敏（`guardrails/tools/project_edit/runtime.audit`），不会出现在日志/产物；请勿外泄、勿提交到仓库。

## 归档（已删除，仅记录）
- 旧设计系：`design-taste-frontend(-v1)、brandkit、image-to-code、imagegen-frontend-mobile/web、minimalist-ui、industrial-brutalist-ui、stitch-design-taste、high-end-visual-design、full-output-enforcement`
- 运行依赖缺失/与内置重复：`docx、pptx、xlsx、webapp-testing、canvas-design、algorithmic-art`
