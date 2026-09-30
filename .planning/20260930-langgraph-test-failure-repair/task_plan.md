# LangGraph 回归失败修复

## 目标

定位上一轮自动化验收中的失败根因，修复本次编排迁移引入的兼容或逻辑问题，并重跑相关测试与全量套件。

## 阶段

1. 按失败类型复现并区分生产代码问题与测试替身问题。`complete`
2. 修复确认的根因，同时保留 Runtime 安全与失败关闭语义。`complete`
3. 重跑相关用例及全量自动化测试，记录剩余问题。`complete`

## 结果

- 修复 completion gate 最多一次修复的 Runtime 上限回归。
- 恢复 Run 的 API 分析、规划与复核使用任务持久化目标。
- 让可选 Provider 注入兼容旧版执行适配器。
- 更新 Run 集成测试桩，分别提供结构化分析与复核响应；保持生产复核不可用时失败关闭。
- 正式离线套件：1432 tests，全部通过。
- 未进行真实模型或 MCP 网络对话验收。

## 后续：将两处重试循环收敛到 LangGraph

- `completion repair` 与 `obligation feedback` 由执行节点返回带类型的重试请求，LangGraph 统一执行有界计数、去重及 `retry → plan → execute` 转移。
- 复核后的补充由 `review → supplement → plan → execute` 转移执行，补充次数固定为一次。
- Runtime 回调仍持有工具授权、执行、副作用证据和终态落库；LangGraph 只管理流程状态与转移。
- 增加图级回归用例，覆盖单次修复、重复义务签名、单次复核补充和总执行次数上限。
- 图级用例 4 项通过；正式离线全量套件 1436 项通过（148.336 秒）；`py_compile` 与 `git diff --check` 通过。
- 测试记录：`var/test-reports/langgraph-two-loops-20260930.txt`。未进行真实模型或 MCP 网络对话验收。

## 约束

- 不降低分析/复核失败时的安全要求。
- 不把仅适用于旧测试替身的行为塞入生产逻辑。
- 测试及日志产物写入 var/ 或系统临时目录。
