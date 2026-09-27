# Findings

- TUI 用 StatusHeader、MessageLog、ToolGroup、Spinner、ModelPicker 和 InputBar 组成纵向界面。
- assistant 流式回答曾在正文后额外添加独立类型行；已改为给原消息补上类型标题。
- 工具组折叠后现在显示总数、成功/失败/运行中数量和总耗时。
- `_render_result` 现在会按 Run outcome 设置顶栏终态，不再将失败状态重置为就绪。
- 固定界面标签、状态、快捷键说明、命令菜单、Inspector、模型/历史/审批/会话/设置弹窗已统一为中文。
- 快捷键、命令、模型和工具标识及运行数据属于技术标识/内容数据，保留原样；模型或外部工具返回的自由文本语言由其内容本身决定。
- 未运行测试或物理终端动态验收。
