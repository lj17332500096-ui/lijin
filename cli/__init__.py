"""CLI 消息平台；Textual TUI 复用其会话和 Runtime 调用层。

模块划分
--------
    cli.app          主循环（读入 → run_turn → 过程渲染 → 最终答复/诊断）
    cli.render       过程事件渲染（工具活动、终稿流式分片、控制事件）
    cli.commands     斜杠命令注册表与实现
    cli.store        会话/消息/产物视图（读 agent.db）
    cli.diagnostics  异常 → 诊断码 + 人话 + 下一步
    cli.theme        终端样式（无第三方依赖，不支持颜色时自动降级）

本包不导入 webapp / llama_bridge（前端 UI 层已冻结，边界由 tests/test_ui_isolation.py 强制）。
入口：`python main.py`，或用 `python main.py --tui` 启动 Textual 界面。
"""

__all__ = ["app", "render", "commands", "store", "diagnostics", "theme"]
