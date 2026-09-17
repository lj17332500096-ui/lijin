# Readiness Fixture App（仅测试用）

这是一个最小可复现的前端认证/接口项目，只用于 Task Readiness 的 Coding E2E：

- `src/auth.ts`：登录与 token 刷新逻辑
- `src/session.ts`：会话存储（刷新页面会丢失的已知缺陷点）
- `src/api.ts`：HTTP 封装与上传限制常量
- `tests/auth.test.ts`：认证逻辑的 jest 用例

运行方式：

```bash
npm test
npm run build
```

已知待修点（供 E2E 案例使用，不要在生产项目里出现）：

1. 登录后刷新页面会退出：token 只存在内存变量，未持久化到 sessionStorage。
2. 上传大小限制为 5MB（常量 `MAX_UPLOAD_MB`）。
3. 接口错误处理缺少 401 重试。
