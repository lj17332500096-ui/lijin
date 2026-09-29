# 文档目录

本目录保存 Agent 项目的长期文档，包括架构说明、设计决策、审计记录、验收报告和运行手册。

## 放在这里

- 架构设计和 ADR 放在 `docs/architecture/`。
- 安全、Runtime 与系统审计报告放在 `docs/audits/`。
- 生产路径、发布和回归验收报告放在 `docs/acceptance/`。
- 部署、恢复、配置和故障排查手册放在 `docs/operations/`。
- 正在使用的评测基准说明；已结束的实验、基线和验收快照放在 `docs/archive/`。

## 不放在这里

- 测试生成的临时日志、原始运行 JSON、截图和模型输出；放在 `var/` 下对应目录，不写仓库根目录。
- 尚未整理的随手草稿；整理成可复用说明后再纳入本目录。
- 运行时配置、数据库、密钥和用户生成文件。

## 维护约定

- 文件名包含主题；需要区分版本或验收批次时使用日期或阶段号。
- 新报告说明对象、日期、证据、结论和未覆盖范围。
- 过期文档标记状态或迁入 `docs/archive/`；正在生成的日志和评测输出分别放到 `var/logs/`、`var/benchmark-runs/`，已结束批次再按归档约定整理到 `archive/`。
- `docs/archive/README.md` 登记历史报告的新旧路径；移动报告时同步更新仓库内引用。
- 新报告按以上主题分类放入对应子目录，不在 `docs/` 根目录继续平铺。
- `docs/archive/2026-09/` 收纳被 2026-09-26/27 Runtime 审计、设计和验收替代的 Phase 0/1 记录，以及已退役网页 UI 的测试快照。

## 2026-09-26 Runtime / Layer 工作

- [Runtime 与 Layer 审计](audits/AGENT_RUNTIME_LAYER_AUDIT_2026-09-26.md)
- [修复问题汇总](audits/AGENT_RUNTIME_REPAIR_ISSUES_SUMMARY_2026-09-26.md)
- [目标架构与工作流](architecture/AGENT_RUNTIME_TARGET_ARCHITECTURE_AND_WORKFLOW_2026-09-26.md)
- [Runtime 入口与 Layer/Router 职责 ADR](architecture/ADR_RUNTIME_ENTRY_AND_LAYER_ROUTER_BOUNDARIES_2026-09-26.md)
- [生产路径验收与成本](acceptance/PRODUCTION_PATH_ACCEPTANCE_AND_COST_2026-09-26.md)
- [Public Activity Runtime 验收](acceptance/PUBLIC_ACTIVITY_RUNTIME_ACCEPTANCE_2026-09-27.md)
