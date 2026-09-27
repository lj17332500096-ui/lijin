# Skill 按需加载与插件清单

## 目标
在现有 Python Agent 扩展体系中实现：1) Skill 指引按需读取；2) Skill、本地插件工具、MCP 服务器与 MCP 工具统一盘点。保留 Runtime Registry、ApprovalGate、FileScope 与调用记账作为执行真相。

## 阶段
- [complete] 调查 Skill / MCP / Registry / Runner 生命周期
- [complete] 设计兼容性：轻量目录、显式加载工具、统一插件状态快照
- [complete] 实现并接入现有工具路由与 Runtime 权限名册
- [complete] 语法检查与 `git diff --check`
- [complete] 文档和差异回顾
- [not run] 测试套件（当前工作要求未授权运行测试）

## 不变量
- 不把插件安装/任意 Python 代码动态执行开放给模型。
- 插件工具必须经过现有 Runtime 工具包装、审批和审计。
- 新目录/清单只是注册视图，不复制工具执行状态。
- 默认 Skill 清单轻量；只有明确加载后才把完整 Skill 指引放入对话。
- 不改用户已有无关工作区变更。

## 风险 / 错误
| 项目 | 处理 |
|---|---|
| 工作区已有大量未提交更改 | 只修改本任务的产品文件；其余既有差异保留 |
| 首次补丁路径漏掉仓库子目录 | 工具未写入文件；改用完整工作区路径后成功 |
| 测试套件 | 未运行；只做 Python 语法编译与 diff whitespace 检查 |
