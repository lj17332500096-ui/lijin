# 三项 Runtime 优先修复发现

- 能力盘点以 Runtime 事实块回答，Agent 工具数为 0；Full Run 测试核对了 routing decision 的 allowed_tools。
- 多阶段计划按意图在原请求中的出现位置排序，保留规则顺序作为同位置的稳定 tie-breaker。
- 当前阶段只接受计划中工具；后续/无关工具会被阻止并记录事件。成功推进阶段；已知失败留在当前阶段；结果不确定则暂停并要求用户核对。
- Run 恢复根据 started/completed/failed 事件恢复进度；无收尾的 started 被视为不确定。
- Completion Gate 附加阶段完成约束，阻止模型在跳过计划步骤时用普通回答标记成功。
- save_note 返回路径通过文件存在性确认，避免有效的文件写入被当成未知结果。
- Router p95 使用 nearest-rank percentile。
- 相关测试共 137 passed，2 subtests passed。
