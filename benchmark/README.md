# Benchmark 目录

本目录保存 Agent 行为评测、基准用例、结果判定和可复现的评测驱动。它用于判断行为质量与回归，不是生产运行时的一部分。

## 放在这里

- 评测用例、预期行为、观测结构和评分逻辑。
- 可重复运行的评测驱动、分析器与统计工具。
- 按主题组织的新实验及其 README、方案、脚本和用例，放入 `benchmark/<topic>/`。
- 有版本号、用途说明和来源的稳定基准数据。

## 不放在这里

- 生产工具和 Runtime 业务逻辑；放在 `runtime/` 或对应模块。
- 每次执行生成的大批原始输出、日志和临时报告；写到 `var/benchmark-runs/<topic>/<run-id>/`，不要写入仓库根目录或实验源码目录。
- 密钥、生产数据和未经脱敏的用户内容。

## 运行与产物

入口为 `python -m benchmark`。支持的子命令及参数以 `python -m benchmark --help` 和代码中的 CLI 为准。50 case Runtime benchmark 默认把 raw run 写到 `var/benchmark-runs/eval/`；建议用 `--out var/benchmark-runs/eval/<run-id>` 区分多次运行。运行报告应显式指定 `var/benchmark-runs/<topic>/<run-id>/` 下的输出路径，并注明基准版本、运行配置和 Provider。只有复核通过、可复算的基线结果才纳入 `benchmark/<topic>/results/`。
