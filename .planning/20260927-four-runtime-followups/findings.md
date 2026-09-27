# Findings

- 当前 `runtime/laya_router.py` route head 未训练；先考察基于现有分类输出的可审计候选路由，以及固定 Router 回退。
- `TaskManager.delete_container()` 已尝试删除 SDK Session，但 `runtime.context.delete_session_history()` 会吞掉异常，且无可重试队列。
- ToolSpec 有能力/类别字段，MCP 工具可有服务器和 remote idempotent 元数据；需找能力清单拼装入口和工具注册来源，避免把“未配置”说成“可用”。
- 第 10 项上一轮增加 per-model cost 和 gateway latency；未记录完整 Run latency，也没有有界、可查询的 Run P50/P95 聚合。
- `.planning/20260927-full-agent-audit` 和 `three-priority-fixes` 是既有审计/修复计划，不覆盖本轮四项实现。
- `LayaRouter.classify()` 目前只问 intent/completeness 两个问题，显式设置 `route=None`；Runner 的 Layer 只做 direct_text 早退，实际工具候选由 `select_tool_names()` 决定。
- 能力盘点状态有两个实现：`capability_introspection.capability_context_block()` 和 `integrations.extensions.extension_manager()`。MCP configured/connected 已较细；`collect_tool_entries()` 把非 MCP `available` 直接等同 `enabled`，需区分已注册、当前可运行和 Skill 指引已加载。
- `delete_session_history()` 吞掉所有异常，TaskManager 只调用一次且没有持久化待清理记录；需增加可恢复的清理 outbox/重试 API，并确保调用方知道部分失败。
- `FORGE_MODEL_PRICING` 是可选配置。要满足“成本默认可估算”，还需基于用户明确的供应商/model 费率，或提供版本化内置费率配置并允许覆盖；不应猜实际价格。
- Run latency 可用 Runner 开始/收尾的单调时钟包围整个 `run_turn()`，在 terminal/cleanup 写入持续聚合样本；P95 需要口径标注和有界保留，避免从无限历史扫描。
- 当前 `.env` 的非敏感模型配置是 `agnes-3.0-flash`，Base URL host 为 `apihub.agnes-ai.cn`。Agnes 官方价格页截至 2026-09-27 把该模型当前优惠价列为 input/output $0，原价分别 $0.05/$0.15 每百万 tokens；需在版本化 rate catalog 记录 current/list 双口径，并让 operator config 覆盖。
- 当前 Laya checkpoint 是多选项 choice 架构，但现有训练语料对 tool/text 做二分类；不把二分类 checkpoint 错当多工具路由头。新候选路由必须有独立 checkpoint、工具类映射及高置信度重叠校验，无法判定时保持现有 Router 结果。
