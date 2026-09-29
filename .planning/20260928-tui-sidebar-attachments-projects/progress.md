# Progress

## 2026-09-28
- 阅读 `planning-with-files` 技能和仓库 `AGENTS.md`。
- 确认并保留本任务开始前已存在的 Runtime 改动。
- 完成 Textual 工具栏与可折叠左侧会话栏；支持新建、真实切换、`/sessions`、`/switch`、`/new`。
- 完成项目文件夹选择与项目容器绑定；项目在侧栏以文件夹标识。
- 完成系统文件/文件夹选择器、`/attach <path>`、附件暂存/移除及容器隔离绑定。
- Runtime 附件绑定限制到当前 task container；工具文件读取支持经 FileScope 授权的项目数据目录。
- 附件仅在下一条消息使用；每条最多 8 个，最大 25 MB/个、50 MB 合计，并拒绝密钥/证书类文件。
- `python -m py_compile` 覆盖本轮改动 Python 文件并通过；`git diff --check` 通过。
- 未运行测试或启动真实终端 TUI。
