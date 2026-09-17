# docx（Word 文档处理）
## 用途
创建 / 读取 / 编辑 Word 文档（.docx/.dotx）：目录、标题、页码、图片、批注、查找替换、合并文档。
## 触发
用户提到 Word 文档、.docx/.dotx，或要产出 报告/备忘录/信函/模板 等 Word 交付物时使用。
## 核心做法
- **新建**：写一个 Node 脚本用 `docx`（已预装，勿先 `npm install`），`require("docx")` 直接构建并导出。
- **编辑已有**：`unzip x.docx` -> 修改 `word/document.xml` -> 再 `zip` 回（docx-js 不能打开已有文件）。
- **读取内容**：`pandoc -t markdown file.docx` 转纯净文本/Markdown。
- 详细示例/注意事项见资产 `assets/references/*.md`，脚本在 `assets/scripts/`。
## 约束
不要用于 PDF/电子表格/Google Docs/与文档生成无关的编码任务。
