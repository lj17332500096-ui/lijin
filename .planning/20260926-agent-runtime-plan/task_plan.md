# Plan: Agent Runtime 目标架构方案

## Goal
基于 2026-09-26 Runtime/Layer 审计和当前代码真实结构，产出可执行的 Agent 底层目标架构、工作流、职责协议、验证门槛与分阶段迁移路线图。本轮只设计，不改运行时代码。

## Phases
- [x] 阅读 planning skill、审计报告、真实调用图、责任矩阵、配置真相与既有 Layer 设计
- [x] 基于源码事实明确目标组件边界与单一生产执行链
- [x] 编写结构化 LayerDecision/Capability Policy/Tool routing/Runner execution/terminalization 工作流
- [x] 补充 fallback、trace、benchmark、状态机、验收与迁移依赖
- [x] 输出设计文档并说明交付范围

## Constraints
- 不改 Runtime 代码，不把实验 pipeline 写成生产能力
- 保留 TaskManager/RunContext/Runner/ApprovalGate/ExecutionEvidence/CompletionGate 为既有执行事实源
- Layer 分类不能代替用户授权、参数验证、审批或完成判定
