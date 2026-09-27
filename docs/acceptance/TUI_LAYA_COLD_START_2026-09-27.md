# TUI + Laya GGUF 冷启动验收（2026-09-27）

## 结果

**通过。** 使用项目正式入口在本机交互 PTY 中启动 TUI；验收前先停止端口
8099 上此前由本轮启动的 llama.cpp 服务，确认 TUI 能从冷状态启动 GPU encoder、
加载 Python tokenizer/head，并完成一轮真实 Agent 对话。

## 验收记录

| 步骤 | 观察结果 |
|---|---|
| 启动命令 | `.venv/Scripts/python.exe main.py --tui` |
| TUI 初始状态 | 界面正常显示；输入框在 Laya 预热期间禁用 |
| llama.cpp 自动启动 | TUI 后台启动 `F:/laya/llama-sycl/llama-server.exe`，监听 `127.0.0.1:8099` |
| 模型校验 | `/props` 报告 encoder 为 `F:/laya/laya-multilingual-f16.gguf`，与配置一致 |
| Laya 预热 | TUI 显示 `Laya GGUF 已就绪（llama.cpp + laya-multilingual-f16.gguf）`；输入恢复 |
| 实际交互 | 输入 `What is 2 plus 2? Reply with just the number.`；`calculate` 工具成功执行，最终答复 `4` |
| Runtime 终态 | Run `task_8d35880c` 状态为 `completed`；无错误信息 |
| 正常退出 | 通过 `Ctrl+Q` 退出；TUI 本轮创建的 llama.cpp PID `11960` 退出，端口释放 |

## 退出后的环境恢复

验收期间发现另一个更早启动的 TUI 进程仍存在。为避免影响该会话，在验收进程
按生命周期关闭服务后，重新启动了独立的 SYCL llama.cpp 服务 PID `21832`；
当前 `127.0.0.1:8099/props` 返回 HTTP 200，并报告加载了预期 encoder。

## 范围

本次验证了真实终端 UI 启动、TUI 自动启动/复用模型服务、模型与配置路径匹配、
Laya head/tokenizer 预热、用户输入、工具执行、最终答复、Run 完成和退出清理。
不代表 Laya 模型对任意意图的分类准确率已通过基准验收；置信度门限仍保持
`0.85`，低置信度判断继续交给主 Agent。
