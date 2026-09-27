# Plan: Layer benchmark and Run trace

## Goal
建立可复算的 Layer 分类 benchmark，并用 TaskManager 既有 task_events 将决策与工具候选、实际 invocation 和终态串联；复用 RunContext/TaskManager，不引入第二套执行事实源。

## Phases
- [x] 核查 TaskManager event、RunContext、route_agent 与 tool invocation 现有接口
- [x] 建立版本化、带预期标签的 Layer benchmark 数据集和可运行报告脚本
- [x] 在生产 route_agent 记录 Layer outcome / skip / fallback 与最终候选工具及 schema fingerprint
- [x] 将 decision_id 绑定 RunContext，并传递到每个实际/被阻止工具 invocation 与终态 event
- [x] 记录用法、指标定义与现有限制

## Rollout gate (still open)

- [ ] Expand the 31-case starter dataset with paraphrases and multi-turn cases, and obtain a genuinely independent label review before using it for release thresholds.
- [ ] Verify schema hashes against captured provider request payloads before claiming exact request parity.

## Constraints
- 不新增第二套执行/状态存储；trace 写既有 task_events
- 原始用户文本与工具参数不能未经脱敏落入新事件
- benchmark 固定数据/label/model/config 指纹，区分 abstain 与分类错误
- 不因 instrumentation 改变路由或工具授权行为
- 不运行 tests；完成代码与数据后只做静态检查/脚本帮助信息检查
