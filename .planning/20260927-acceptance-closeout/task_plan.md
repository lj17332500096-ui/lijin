# 未验收场景修复与复验

## 目标
完成前次动态 Runtime 验收中未实际演练的路径；针对演练暴露的问题进行修复和回归，明确隔离仿真与生产服务的边界。

## 阶段
- [x] 1. 盘点未验收点与安全注入边界
- [x] 2. Provider/MCP 中断、超时、恢复和不确定副作用
- [x] 3. Runtime 数据库满、损坏恢复和状态恢复
- [x] 4. TUI 流式/审批恢复与 MCP 策略回归
- [x] 5. Session 隔离、成本/延迟、轮转和 Provider attempt 回灌
- [x] 6. 更新复验报告，注明未覆盖生产边界

## 结论
所列隔离动态场景完成修复验收。全生产验收仍有明确边界：没有中断 Agnes 生产 Provider、没有物理终端人工交互、没有耗尽操作系统卷；Agents SDK 的干净 EOF 合成完成事件仍需后续协议级校验。

## 变更文件
- `integrations/mcp_bridge.py`
- `main.py`
- `cli/tui/app.py`
- `cli/app.py`
- `runtime/snapshot.py`
- `runtime/task_manager.py`
- `tests/test_productization.py`
- `tests/test_task_runtime.py`
- `tests/test_tui.py`
- `tests/test_observability.py`
- `docs/acceptance/DYNAMIC_RUNTIME_REMEDIATION_2026-09-27.md`
