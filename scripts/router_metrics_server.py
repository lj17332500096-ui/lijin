#!/usr/bin/env python3
"""Router 指标 Prometheus 暴露服务。

基于 http.server 的轻量 HTTP 服务（无 Flask/FastAPI 依赖），暴露：
  - GET /metrics      Prometheus 文本格式指标
  - GET /health       健康检查（返回 200 + 最近一条数据时间戳）

用法：
    python scripts/router_metrics_server.py [--port 9095]

指标清单：
  forge_router_total_calls_total         总调用次数（counter）
  forge_router_hit_zero_total            命中 0 目标工具次数（counter）
  forge_router_hit_zero_rate             命中率（gauge，0-1）
  forge_router_avg_selected_tools        平均选中工具数（gauge）
  forge_router_p95_query_len_bytes       P95 查询长度（gauge）
  forge_router_last_observed_timestamp   最近观测时间（gauge，Unix ts）

Prometheus 抓取配置（prometheus.yml 示例）：
  scrape_configs:
    - job_name: forge-router
      static_configs:
        - targets: ['<host>:9095']
      scrape_interval: 60s
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runtime_paths import LOG_DIR

_LOG_PATH = LOG_DIR / "tool_router.jsonl"
_WINDOW_SECONDS = 24 * 3600  # 默认聚合窗口 24h


def _read_entries(cutoff: datetime) -> List[dict]:
    out = []
    paths = [_LOG_PATH.with_name(_LOG_PATH.name + f".{i}") for i in range(1, 4)] + [_LOG_PATH]
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    t = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if t >= cutoff:
                    out.append(rec)
    return out


def _read_run_latency() -> dict:
    """Read bounded Run samples without opening TaskManager or mutating the DB."""
    try:
        import sqlite3
        from runtime.task_manager import DEFAULT_DB_PATH
        if not DEFAULT_DB_PATH.exists():
            raise FileNotFoundError(DEFAULT_DB_PATH)
        con = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True, timeout=3)
        try:
            rows = con.execute("SELECT latency_ms FROM run_metrics ORDER BY sample_at DESC LIMIT 20000").fetchall()
        finally:
            con.close()
        values = sorted(max(0, int(row[0])) for row in rows)
        return {"count": len(values),
                "p50_ms": values[max(0, math.ceil(.50 * len(values)) - 1)] if values else None,
                "p95_ms": values[max(0, math.ceil(.95 * len(values)) - 1)] if values else None}
    except Exception:
        return {"count": 0, "p50_ms": None, "p95_ms": None}


def compute_metrics() -> dict:
    cutoff = datetime.now() - timedelta(seconds=_WINDOW_SECONDS)
    entries = _read_entries(cutoff)
    total = len(entries)
    hit_zero = sum(1 for e in entries if e.get("hit_zero"))
    selected = [e.get("selected_count", 0) for e in entries]
    qlens = sorted(len(str(e.get("query", "")).encode("utf-8")) for e in entries)
    p95 = qlens[max(0, math.ceil(0.95 * total) - 1)] if qlens else 0
    last_ts = max((e.get("ts") for e in entries), default=None)
    run_latency = _read_run_latency()
    return {
        "total": total,
        "hit_zero": hit_zero,
        "hit_zero_rate": (hit_zero / total) if total else 0.0,
        "avg_selected": (sum(selected) / total) if total else 0.0,
        "p95_query_len": p95,
        "last_observed": last_ts,
        "run_latency": run_latency,
    }


def render_prometheus() -> str:
    m = compute_metrics()
    last_ts_float = (
        datetime.fromisoformat(m["last_observed"]).timestamp()
        if m["last_observed"]
        else 0
    )
    lines = [
        "# HELP forge_router_total_calls_total Total router invocations in the window",
        "# TYPE forge_router_total_calls_total counter",
        f"forge_router_total_calls_total {m['total']}",
        "",
        "# HELP forge_router_hit_zero_total Router invocations that selected 0 target tools",
        "# TYPE forge_router_hit_zero_total counter",
        f"forge_router_hit_zero_total {m['hit_zero']}",
        "",
        "# HELP forge_router_hit_zero_rate Fraction of invocations with zero target tools (0-1)",
        "# TYPE forge_router_hit_zero_rate gauge",
        f"forge_router_hit_zero_rate {m['hit_zero_rate']:.6f}",
        "",
        "# HELP forge_router_avg_selected_tools Average number of tools selected per invocation",
        "# TYPE forge_router_avg_selected_tools gauge",
        f"forge_router_avg_selected_tools {m['avg_selected']:.4f}",
        "",
        "# HELP forge_router_p95_query_len_bytes P95 query length in bytes",
        "# TYPE forge_router_p95_query_len_bytes gauge",
        f"forge_router_p95_query_len_bytes {m['p95_query_len']}",
        "",
        "# HELP forge_router_last_observed_timestamp Unix timestamp of the most recent router log entry",
        "# TYPE forge_router_last_observed_timestamp gauge",
        f"forge_router_last_observed_timestamp {last_ts_float:.0f}",
        "",
        "# HELP forge_run_latency_p50_ms End-to-end Run latency p50 from durable run_metrics samples",
        "# TYPE forge_run_latency_p50_ms gauge",
        f"forge_run_latency_p50_ms {m['run_latency']['p50_ms'] if m['run_latency']['p50_ms'] is not None else 'NaN'}",
        "",
        "# HELP forge_run_latency_p95_ms End-to-end Run latency p95 from durable run_metrics samples",
        "# TYPE forge_run_latency_p95_ms gauge",
        f"forge_run_latency_p95_ms {m['run_latency']['p95_ms'] if m['run_latency']['p95_ms'] is not None else 'NaN'}",
        "",
        "# HELP forge_run_latency_samples Number of retained end-to-end Run samples",
        "# TYPE forge_run_latency_samples gauge",
        f"forge_run_latency_samples {m['run_latency']['count']}",
        "",
    ]
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            self._send(200, render_prometheus())
        elif self.path == "/health":
            m = compute_metrics()
            body = json.dumps(
                {"status": "ok", "last_observed": m["last_observed"], "window_seconds": _WINDOW_SECONDS},
                ensure_ascii=False,
            )
            self._send(200, body, content_type="application/json")
        else:
            self._send(404, "not found\n")

    def log_message(self, fmt: str, *args) -> None:
        # 静默默认日志，避免刷屏
        pass


def main() -> int:
    p = argparse.ArgumentParser(description="Router 指标 Prometheus 服务")
    p.add_argument("--port", type=int, default=9095, help="监听端口，默认 9095")
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[router-metrics] 监听 http://127.0.0.1:{args.port}  (GET /metrics, /health)")
    print(f"[router-metrics] 数据源: {_LOG_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[router-metrics] 退出")
    finally:
        server.server_close()
    return 0


def start_metrics_thread(port: int = 9095) -> bool:
    """把指标服务以守护线程方式嵌入主进程，避免单独起进程。

    主进程（如 webapp.py）启动时调用：
        start_metrics_thread(9095)
    返回 True 表示线程已起来；False 表示端口被占或启动失败（不阻塞主流程）。

    设计要点：
    - 复用 ThreadingHTTPServer（每个请求独立线程），不污染主事件循环
    - daemon 线程：主进程退出时自动回收，不阻塞 shutdown
    - 端口被占 / 其它异常：捕获并记日志，不 raise（C4 降级：指标不可用不阻塞主业务）
    """
    import threading
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[router-metrics] 端口 {port} 启动失败（{e}），跳过指标线程（不阻塞主流程）", file=sys.stderr)
        return False
    t = threading.Thread(
        target=server.serve_forever,
        name=f"router-metrics-{port}",
        daemon=True,  # 主进程退出时自动回收
    )
    t.start()
    print(f"[router-metrics] 线程已起 http://127.0.0.1:{port}  (GET /metrics, /health)")
    print(f"[router-metrics] 数据源: {_LOG_PATH}")
    return True


if __name__ == "__main__":
    sys.exit(main())
