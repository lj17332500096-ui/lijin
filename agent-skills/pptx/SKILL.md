# pptx（PowerPoint 演示文稿）
## 用途
创建/读取/编辑 .pptx/.potx：生成演讲 Deck、提取文本、模板(.potx)、备注与批注、拆分/合并幻灯片。
## 触发
用户提到 deck/幻灯片/presentation 或 .pptx/.potx 文件即使用。
## 核心做法
- **新建**：写 `pptxgenjs` 脚本（依赖已就绪，勿先装）。
- **编辑/从模板构建**：`unzip` -> 改 `ppt/slides/slideN.xml` -> 再 `zip`。
- **读取**：`markitdown deck.pptx`（每页一个块）；可视化网格 `python scripts/thumbnail.py deck.pptx`。
- 脚本路径相对技能目录；其余为普通 Python/node/shell。详见 `assets/references/`。
