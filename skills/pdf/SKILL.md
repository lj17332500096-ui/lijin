# pdf（PDF 处理）
## 用途
读取/提取文本表格、合并/拆分、旋转、加水印、新建、填表单、加密/解密、提取图片、OCR 扫描件。
## 触发
用户提到 .pdf 文件或要产出 PDF 时使用。
## 核心做法
用 Python 库（pypdf/PyMuPDF 等）+ 命令行工具组合完成上述操作；高级功能与示例看 `assets/REFERENCE.md`；填表单看 `assets/FORMS.md`。先 `python <脚本> --help` 再调用（大脚本当黑盒）。
