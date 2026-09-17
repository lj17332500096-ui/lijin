# xlsx（电子表格）
## 用途
打开/读取/编辑/修复 .xlsx/.xlsm/.xltx/.csv/.tsv（加列、公式、格式化、图表、清洗脏数据），或新建/互转表格。
## 触发
用户以电子表格为主要输入/输出，或引用某表格文件并要对它做处理时使用。
## 核心做法
- **生成/编辑 + 公式/格式**：`openpyxl`（已预装，勿 `pip install`）。
- **批量进出**：`pandas`（`read_excel`/`to_excel`）。
- **快速查看**：`markitdown file.xlsx`（每表一格，无坐标，别用它规划编辑）。
- **读公式+值**：两次 `load_workbook`（一次 data_only 取计算值，一次取公式）。
- 详见 `assets/references/`。
## 约束
交付物必须是表格文件；若主要交付是 Word/HTML/脚本/数据库管道，即使涉及表格也不要用。
