# 三项 Runtime 优先修复

## 目标
落实能力查询隔离、多阶段 Runtime 强制编排、Router p95 口径修正，并用针对性测试验收。

## 阶段
- [x] 能力查询走 Runtime 事实块、禁用业务工具并记录 trace
- [x] 多阶段计划按用户表达顺序执行
- [x] 阶段抢跑拦截、进展持久化和不确定结果恢复
- [x] 未完成阶段不得正常完成 Run
- [x] p95 使用 nearest-rank percentile
- [x] 运行相关 Router、Completion Gate、生产收口测试

## 验收
- 命令：.venv/Scripts/python.exe -m pytest tests/test_tool_router.py tests/test_completion_gate.py tests/test_production_closure.py -q
- 结果：137 passed，2 subtests passed
- 限制：使用 fake 模型与 Runtime 集成测试；未访问真实天气/搜索/写入服务。

## 后续：Layer / 恢复 / 证据 / 扩展契约

- [x] 在路由 trace 中明确 Layer 仅做粗粒度判定，Router 拥有具体候选工具
- [x] Broker 重试入口拒绝非幂等 ToolSpec；每次重试生成唯一 invocation id
- [x] MCP 默认不重试；仅支持配置显式声明幂等的远端工具重试
- [x] MCP 网络失败以 outcome-unknown 进入 Runner，副作用及多阶段状态不伪报成功/失败
- [x] 无进展降级答复依真实执行证据区分部分执行与零执行
- [x] 多阶段完成证据或 WAL 收口失败时 fail-closed，不允许后续阶段继续
- [x] Run 终态证据要求与数据库中当前持久状态匹配
- [x] 将 MCP 幂等性透传到 ToolSpec/工具目录
- [x] 定向验收 316 passed，2 subtests passed（审批依赖用例测试进程临时设 APPROVAL=on）
- [ ] Laya route head 未训练；真实 Layer-to-candidate-tool 路由需要先有可校验的模型/基准
