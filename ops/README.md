# FORGE Router 指标监控接入

本目录提供 Router 指标接入外部 dashboard（Grafana + Prometheus）的完整配置。

## 架构

```
logs/tool_router.jsonl  ←  C1 日志（每次 router 调用追加一行）
           │
           ▼
scripts/router_metrics_server.py  ←  常驻 HTTP 服务（/metrics 端点）
           │
           ▼
Prometheus scrape（每 60s）
           │
           ▼
Grafana dashboard（ops/grafana/forge-router-dashboard.json）
           │
           ▼
告警规则（ops/prometheus/alert_rules.yml）
```

## 部署步骤

### 1. 启动指标服务

```bash
cd F:/Byong-hermes/Byong-hermes/my_creative_agent
.venv/Scripts/python.exe scripts/router_metrics_server.py --port 9095
```

验证：

```bash
curl http://127.0.0.1:9095/health
curl http://127.0.0.1:9095/metrics | head -20
```

### 2. 配置 Prometheus

把 `ops/prometheus/prometheus.yml` 合进你的 `prometheus.yml` 的 `scrape_configs` 数组，
并把 `ops/prometheus/alert_rules.yml` 加进 `rule_files` 数组：

```yaml
# prometheus.yml
scrape_configs:
  - job_name: forge-router
    static_configs:
      - targets: ['127.0.0.1:9095']
    scrape_interval: 60s

rule_files:
  - "ops/prometheus/alert_rules.yml"
```

重启 Prometheus。

### 3. 导入 Grafana dashboard

- Grafana → Dashboards → Import → 上传 `ops/grafana/forge-router-dashboard.json`
- 选择 Prometheus 数据源
- 看到 5 个 panel：命中率 / 总调用次数 / 平均工具数 / P95 查询长度 / 命中率趋势 + 命中 0 次数

### 4. 告警（可选）

`ops/prometheus/alert_rules.yml` 定义 4 条告警：

| 告警 | 条件 | 持续 | 级别 |
|------|------|------|------|
| ForgeRouterHitZeroRateHigh | `hit_zero_rate > 0.05` | 10m | warning |
| ForgeRouterHitZeroRateCritical | `hit_zero_rate > 0.15` | 5m | critical |
| ForgeRouterMetricsStale | 指标停更 | 15m | warning |
| ForgeRouterAvgToolsHigh | `avg_selected_tools > 14` | 15m | info |

接 Alertmanager 即可收到通知。

## 手动查询（无 Prometheus 时）

```bash
# 24h 窗口报告
.venv/Scripts/python.exe scripts/router_metrics_report.py --window 24h

# 1h 窗口
.venv/Scripts/python.exe scripts/router_metrics_report.py --window 1h
```

退出码：
- 0 = 正常
- 2 = 参数错误
- 3 = 告警（hit_zero_rate > 5%）

## 系统服务化（可选）

**systemd（Linux）**：

```ini
# /etc/systemd/system/forge-router-metrics.service
[Unit]
Description=FORGE Router Metrics Server
After=network.target

[Service]
WorkingDirectory=/path/to/my_creative_agent
ExecStart=/path/to/.venv/bin/python scripts/router_metrics_server.py --port 9095
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Windows 任务计划**（用 nssm 或 schtasks）：

```powershell
schtasks /create /tn "ForgeRouterMetrics" /tr "F:\Byong-hermes\Byong-hermes\my_creative_agent\.venv\Scripts\python.exe F:\Byong-hermes\Byong-hermes\my_creative_agent\scripts\router_metrics_server.py --port 9095" /sc onstart
```

## 指标清单

| 指标 | 类型 | 说明 |
|------|------|------|
| `forge_router_total_calls_total` | counter | 窗口内总调用次数 |
| `forge_router_hit_zero_total` | counter | 命中 0 目标工具次数 |
| `forge_router_hit_zero_rate` | gauge | 命中率（0-1） |
| `forge_router_avg_selected_tools` | gauge | 平均选中工具数 |
| `forge_router_p95_query_len_bytes` | gauge | P95 查询长度 |
| `forge_router_last_observed_timestamp` | gauge | 最近观测时间（Unix ts） |

## 故障排查

| 症状 | 原因 | 修复 |
|------|------|------|
| `/metrics` 返回空 | `logs/tool_router.jsonl` 不存在或为空 | 确认 C1 日志开启（`TOOL_ROUTER_LOG=on`） |
| Prometheus 抓不到 | 端口 9095 被防火墙拦 | 检查 `netstat -ano | findstr 9095`，放行端口 |
| dashboard 无数据 | Grafana 数据源没配 Prometheus | 检查 Grafana → Data sources |
| 告警没触发 | 告警规则没加载 | `curl :9090/api/v1/rules` 看规则是否注册 |
