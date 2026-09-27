# Findings

- 生产入口：AgentRuntime.run_turn -> route_agent -> execute_turn -> Agents SDK Runner -> wrapped tools -> CompletionGate -> terminal state。
- 主链 Laya 仅对部分 query 调 screen 二分类；高置信 direct_text 会清空工具，其它回退关键词 Tool Router。
- classify 三判别、orchestrator.decide、LoopGate 在 runtime.pipeline 显式路径；pipeline 未由 run_turn 调用，默认关闭。
- Tool Router 实际将工具子集注入 Agent，但关键词集合遗漏 Memory retrieval / 多能力案例；Planner 再在子集中选择工具。
- 多个执行 gates 各有责任：intent/readiness/budget/approval/verification/completion；目标设计不能复制第二套工具执行/终止链。
- 当前路由日志不可将 Layer 决策与实际 invocation 按 run/step 关联；Layer metrics 与 per-class benchmark 缺失。
- 方案采用 advisory Layer + 确定性 Capability Policy + Registry capability mapping + 现有 Runner loop；基准验证后逐步灰度强约束。
