# 进度记录

## 2026-09-30

- 运行全量 pytest：1464 通过，62 失败，3 跳过。
- 复现 `_FakeHarness._execute() got an unexpected keyword argument 'provider'`，导致分析/复核不可用。
- 确认生产 `execute_turn` 支持 `provider` 参数；错误来自测试替身签名与新分析/复核阶段的调用契约不一致。
- LangGraph 独立流程冒烟与 18 项相关单元测试通过。
- 改用项目正式测试入口 `tests/run_tests.py`：1432 项中 41 项失败。把失败分组后发现工作流辅助模型调用被旧测试桩计入主模型调用；审批配置错误因正式 runner 会开启测试审批而排除。
- 修复 Runner 注入执行器对较旧可调用对象的可选关键字兼容。
- 恢复 deterministic completion gate 的单次 completion repair 上限，防止迁移后错误答复消耗完整的六次预算。
- 对恢复 Run 使用持久化任务目标进行分析/计划/复核；续跑消息仍为空以保留 SDK 会话语义。
- 增加 workflow 专用测试桩适配器，让遗留集成测试为分析与复核返回结构化输出，同时把调用计数限制在实际执行阶段。
- 修正验证类测试桩，确保同一错误工具调用不会因一次 Runtime repair 被假模型重复执行。
- 运行迁移相关 20 个测试模块：264 passed。
- 运行正式全量离线回归 `tests/run_tests.py`：第一次发现 41 项由遗漏的 completion repair、旧测试桩和恢复消息上下文引起；修复后第二次结果为 1432 tests，`OK`，用时约 127 秒。
- 真实模型/MCP 动态对话未执行；本轮自动验收只走离线桩。
- 完成 `py_compile` 与 diff whitespace 检查，未生成工作区外副作用。
