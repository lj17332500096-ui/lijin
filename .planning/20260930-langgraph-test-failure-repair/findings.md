# 排查发现

- 全量验收：1464 passed、62 failed、3 skipped。
- 图级冒烟：有界补充和终态短路均通过。
- 相关 task plan / schema 测试：18 passed。
- 复现到测试替身 `_FakeHarness._execute` 不接受新增的 `provider` 关键字；真实 `runtime.execution.execute_turn` 接受该参数。
- 即使移除空值 provider 参数，旧测试替身仍只返回用户答复格式；结果复核器要求独立的 AnswerReview 结构，解析失败必须按 Runtime 安全策略失败关闭。
- 不应为了旧测试返回内容放宽结果复核失败语义；需要确认是测试适配问题，或真正生产调用问题。
- 比较迁移前后的 `runner.py` 发现真实逻辑回归：接入 LangGraph 时遗漏了 deterministic completion gate 的“首次执行 + 一次修复后失败关闭”分支，错误回答因此会耗尽 6 次总尝试预算。
- 恢复 Run 的 API 分析/计划/复核应使用持久化任务目标；SDK 续跑可保持空 continuation message，但空字符串不能替代原任务目标。
- 测试入口 `tests/run_tests.py` 才会设置隔离用的 `APPROVAL=on` 和 UI 标志。通用 `pytest` 受开发 `.env` 中 `APPROVAL=off` 影响，产生了审批测试误报。
- 对生产入口的旧假模型已添加测试适配器：模拟无工具的结构化分析/复核阶段，而真实选定 Agent 调用仍走原测试桩；不会改变生产复核失败关闭策略。
