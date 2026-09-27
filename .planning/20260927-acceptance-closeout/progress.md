# Progress

## 2026-09-27
- 已定位并修复 MCP 事件循环跨轮次/退出清理问题；真实隔离 stdio server 被杀停后返回明确失败，同 loop 清理及重新连接成功。
- 通过实际 OpenAI SDK + loopback HTTP 动态演练 Provider 503→恢复和首 token 后 idle timeout；attempt 明确记录且中断后无重放。
- 用 TaskManager SQLite 页面上限演练 full disk：写事务失败无部分事件、数据库完整性正常，故障恢复后 stale Run 转 failed。
- 修复 snapshot restore 对“当前库损坏不可恢复”的短路缺陷；新增当前库损坏恢复测试。
- Textual Pilot 完整走通审批暂停、Y 批准、同一 Run 恢复和完成答复。
- Provider attempt 持久日志跨内存丢失回灌、Run cost/latency/trace 关联、ack 和 JSONL 轮转动态通过。
- 修复 Run 延迟低于一秒时被整秒时间戳变成 0 的缺陷；新增 monotonic latency 回归测试。
- 更新 observability 测试以匹配敏感 payload 不落 trace、只记录长度的隐私实现。
- 综合回归：249 passed；py_compile 通过，git diff --check 无空白错误；pytest 留一条既有 `cache_dir` 未知配置警告。
- 保留未覆盖边界：生产 Agnes 网关断网演练、物理终端操作、操作系统真实磁盘耗尽、Agents SDK 干净 EOF 被合成为正常结束；详见补充报告。
- MCP 生命周期清理也接入单次定时任务与 daemon 退出；清理异常会通过 module logger 告警，配置/skip 状态在 close 时重置。
- 收尾 MCP stdio fixture 再跑通过：杀停后 MCP 错误可见，连接状态清零，重新连接两次 read-only ping 成功，清理错误均为 0。
- `py_compile`、修正后的 Observability 定向回归通过；最终 `git diff --check` 无空白问题（Git 仅输出工作区 LF→CRLF 标准化提示）。
