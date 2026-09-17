# 技能:gorden-ppt —— 高质量 PPT / 演示文稿制作

## 何时使用本技能
用户想“做/改/套模板生成一个 PPT / 演示文稿 / 幻灯片 / .pptx”，典型场景：工作汇报、季度/年度总结、述职竞聘、项目方案、商业提案、开题/答辩、课件教学、党建教育、数据可视化汇报等。当用户只是要一个**简单无模板 PPT**（几页纯文字/要点）时，也可先考虑内置 `save_ppt_deck`；一旦用户期望“好看、有版式、套模板、不破坏排版”，就使用本技能。

## 素材位置（只读资源，勿让用户直接编辑）
- 技能目录 `skills/gorden-ppt/` 下含：`templates/`（19 套内置模板，每套含 `template.pptx`/`intro.md`/`detail.json`/`preview.png`）、`references/`（工作流与图表编辑说明）、`scripts/`（构建脚本，由工具封装调用）。
- 版本与许可：`VERSION`；模板仅供个人学习/研究，**非商业使用**（原 NOTICE/LICENSE 随技能保留，用户商用需自行取得授权）。

## 主流程（模式 A：内置模板；模式 B：用户自带模板）
1. **选模板**：先调用 `gorden_ppt_templates()` 获取模板清单（名称/主色/场景/页数）。按 用户主题 + 场景 + 主色偏好 匹配 2-3 个候选；候选之间用 `gorden_ppt_template_intro(slug)` 看简介与结构做最终选择；当用户没有明确指定且仍有多个候选时，**询问用户选择（可先各给一句话特点）**，不要擅自挑一个就开做。
2. **读结构**：对选定的 slug 调用 `gorden_ppt_template_intro(slug)` 获取各页 slot 的容量（`max_chars/chars_per_line/max_lines`、`role`、`expected_text` 等）。
3. **规划内容**：按用户需求组织 `selected_slides`（1 起始，保留顺序）与 `edits`。优先用 `slot_id` 定位；无法定位时才用 `address`（shape_id/paragraph/run），并尽量带 `expected_text` 以做严格校验。
4. **构建**：调用 `gorden_ppt_build(template_slug, edits_json, ...)`（默认 strict=True）。成功会返回真实输出路径（`exports/ppt/…`）；失败会返回原因摘要，**如实告知用户**，不要伪造“已生成”。
5. **回读验证**：用现有 `read_office_file`/`read_spreadsheet`/`save_ppt_deck` 系列或重新 `gorden_ppt_build --dry-run 等价信息` 之前先人工核对逻辑：可用 python-pptx 读取输出做一次“每页文本非空、无残留占位词”的结构化抽查（工具自身只做 capacity/expected 检查）。

模式 B（用户提供自己的 .pptx 模板）：把模板当作“未知模板”谨慎处理——先让用户提供模板路径；调用 `gorden_ppt_apply_custom(template_path, edits_json, ...)`（仅限工作区/项目授权范围内的文件）；**绝不要修改用户原文件**，一律输出新文件到 `exports/ppt/`。

## 纪律（必须遵守，违反=质量事故）
1. **只替换文字内容**：禁止移动/删除/改大小/改颜色/改字体字号/改行距/改位置；保留全部版式、图片、图表、动画。图片/图标/箭头等固定装饰不要尝试“换成别的图”，除非用户明确要求且说明风险。
2. **占位词必须清零**：模板里的 “Question 1 / Vivamus… / Key Words Here / 项目名称 / 工作计划模板” 等示例占位文本，凡选中的页都替换为真实内容；成品不应残留任何示例占位词（含英文 Lorem 类文本）。若某 slot 用户没给内容，向用户问清楚或明确说明空着，不要写“待补充”之类假内容占位以外的占位。
3. **容量优先**：每段正文先按 slot 的 `max_chars/chars_per_line/max_lines` 控制长度（这是“参考容量”，允许自然行文略超 ±20%，PowerPoint 自动缩字可兜底）；**禁止为了塞内容硬截断并加 “…”/“等”收尾**；放不下时先精简改写，实在放不下就加页或用副页，仍不行就如实告诉用户。
4. **图表数据**：模板页若含真实 PPT 图表（shape.has_chart），更新数据走 `--chart-data` 语义（参考 `references/chart-editing.md`）；不要通过改文字假装数据正确。
5. **默认严格**：使用工具默认 strict=True 让 expected_text 不匹配时报错，避免“替换到了错误位置”；遇到报错就按报错修正 edits，而不是关掉 strict 硬过。
6. **不要联网更新**：技能内含自动更新脚本，但 FORGE 不自动执行；除非用户明确要求，不要运行 `check_update.py/apply_update.py`（会联网下载并改动技能文件）。
7. **不编造结果**：每次产出真实文件后才在回复中说明路径；构建失败时给出失败原因与建议。

## 输出与后续
- 产物一律落 `exports/ppt/`（既有 Artifact 流程可登记）；回复给出真实路径与要点摘要，可附验证结果（页数、是否 strict 通过）。
- 用户要改稿：修改 edits 后再次构建输出到新文件（不要覆盖上一次交付物；若用户明确要覆盖同一文件名再覆盖）。
