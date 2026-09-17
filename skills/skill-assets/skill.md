# skill-assets（技能资源读取）

## 用途
让你能在需要时读取任意已启用技能目录下的资源文件（assets/references/scripts/templates/fonts 等）的**文本内容**，用于拿到深层示例、完整步骤或脚本说明。

## 触发
当某技能的 `skill.md` 提到「详见 assets/references/…」、而你要用到具体内容时，调用 `read_skill_asset`。

## 用法
- `read_skill_asset(slug, relpath)`：`slug`=技能名（如 `docx`），`relpath`=相对该技能目录的路径（如 `references/REFERENCE.md`）。
- 只读；限制在该技能目录内，防路径穿越；单文件 ≤200KB，返回前 8000 字符。
- 示例：`read_skill_asset("docx", "references/REFERENCE.md")`；`read_skill_asset("webapp-testing", "scripts/with_server.py")`。

## 约束
仅用于读取文本资源；不要尝试读取技能目录之外的文件（工具会拒绝）。
