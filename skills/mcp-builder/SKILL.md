# mcp-builder（MCP 服务器构建）
## 用途
构建高质量 MCP（模型上下文协议）服务器：resources / tools / prompts。
## 触发
用户要做一个 MCP 服务器/给 AI 提供工具时使用。
## 核心做法
用 Python SDK 实现 resources（只读数据）、tools（可调用函数）、prompts（模板）；按规范声明 schema 与描述；示例与最佳实践见 `assets/references/`。
