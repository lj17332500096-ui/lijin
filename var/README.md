# Runtime 运行数据

本目录保存可变运行状态和生成文件。除本说明外，目录内容均由 `.gitignore` 忽略。

| 路径 | 内容 |
| --- | --- |
| `state/` | Agent、会话和记忆 SQLite 数据库 |
| `logs/` | Router、安全闸、定时任务、Provider 尝试和编辑备份日志 |
| `traces/` | OTel 格式的本地 JSONL trace |
| `cache/` | 可重建缓存，例如 RAG 索引 |
| `artifacts/` | 运行时产物，例如沙箱快照 |
| `benchmark-runs/` | 实验和基准的原始运行结果，按主题与 run-id 分目录 |
| `test-reports/` | 评估和测试报告 |
| `tmp/` | 需要保留在本机的临时文件；自动清理的短期临时文件仍使用系统临时目录 |
| `legacy_runtime_2026-09/` | 路径切换时保留的旧位置副本，可用于人工回滚或核对 |

可通过 `FORGE_RUNTIME_DIR` 把整棵目录放到其他位置，相对路径按项目根目录解析。`FORGE_AGENT_DB` 和 `FORGE_SESSIONS_DB` 可以单独覆盖数据库路径。

如果对应的 `var/state/` 数据库还不存在，程序会继续使用旧版项目根目录数据库，避免自动创建空库后隐藏已有状态。已迁移数据库的原件保存在 `legacy_runtime_2026-09/`。迁移前先停止 Agent、Web、daemon 和 metrics 进程，再执行预览：

```powershell
.\.venv\Scripts\python scripts/migrate_runtime_data.py
```

确认预览后执行复制迁移：

```powershell
.\.venv\Scripts\python scripts/migrate_runtime_data.py --apply --i-stopped-agent
```

迁移脚本使用 SQLite backup API 复制数据库，不覆盖已有目标。当前项目已完成首次复制并将根目录旧文件移入 `legacy_runtime_2026-09/`；再次运行脚本只会跳过已存在目标。
