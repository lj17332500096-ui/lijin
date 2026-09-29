# Findings

- 仓库已有 Textual 8.x TUI：`cli/tui/app.py`、`cli/tui/panels.py`。
- `Ctrl+S` / `/sessions` 原先只是一个弹窗；旧选择回调只显示“已选”，不切换。现已改为 `ConversationSidebar`，从 `SessionStore` 列举容器并恢复消息。
- `ChatApp.switch_to(container_id)` 原先只改变 SDK session name。现在缓存真实容器 ID，TUI `run_turn` 显式传 `task_container_id`，保证会话历史/项目容器一致。
- 后端已有 WorkLocation、Project Source、Message Attachment 数据契约；Runner 已支持本轮附件路径注入。现在 TUI 会复制用户选择文件到受容器 FileScope 限制的数据目录，并把附件 ID 送入 Runtime 在新 user message 创建时绑定。
- 项目通过新的 `proj-<uuid>` session id 创建，所选目录成为 WorkLocation，继承严格 FileScope。
- 文件范围同时允许读取 Runtime 明确授权的项目数据目录，支持 `FORGE_DATA_DIR` 配置在工作区之外；用户选中的密钥/证书文件被拒绝，避免绕过工具的保护文件检查。
- 工作区原有三个 Runtime 文件的未提交改动已保留。
