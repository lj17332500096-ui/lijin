#!/usr/bin/env python3
"""Router 指标实时查询脚本。

从 logs/tool_router.jsonl 实时聚合 hit_zero_rate / 平均工具数 / P95 查询长度，
输出 JSON 报告，可被外部 dashboard 轮询拉取（cron / systemd timer / 手动）。

用法：
    python scripts/router_metrics_report.py [--window 24h]

退出码：
    0 = 正常
    非 0 = 文件缺失或解析失败
"""
import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "tool_router.jsonl"
_WINDOW_RE = re.compile(r"^(\d+)([smhd])$")

def _parse_window(spec: str) -> timedelta:
    m = _WINDOW_RE.match(spec.strip())
    if not m:
        raise ValueError(f"非法窗口格式 {spec!r}，应为 Ns/Nm/Nh/Nd")
    n, unit = int(m.group(1)), m.group(2)
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return timedelta(seconds=n * factor)

def _read_entries_since(cutoff: datetime):
    """从 jsonl 文件尾部读，取 ts >= cutoff 的条目。"""
    entries = []
    if not _LOG_PATH.exists():
        return entries
    with _LOG_PATH.open("r", encoding="utf-8") as f:
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
                entries.append(rec)
    return entries

def build_report(window: str = "24h") -> dict:
    cutoff = datetime.now() - _parse_window(window)
    entries = _read_entries_since(cutoff)
    total = len(entries)
    if total == 0:
        return {
            "window": window,
            "total": 0,
            "hit_zero_rate": 0.0,
            "avg_selected_count": 0,
            "p95_query_len": 0,
            "message": "窗口内无数据",
        }
    hit_zero = sum(1 for e in entries if e.get("hit_zero"))
    selected_counts = [e.get("selected_count", 0) for e in entries]
    query_lens = sorted(len(e.get("query", "")) for e in entries)
    p95_idx = min(total - 1, int(0.95 * total))
    return {
        "window": window,
        "total": total,
        "hit_zero_count": hit_zero,
        "hit_zero_rate": round(hit_zero / total, 4),
        "avg_selected_count": round(statistics.mean(selected_counts), 2),
        "max_selected_count": max(selected_counts),
        "min_selected_count": min(selected_counts),
        "p95_query_len": query_lens[p95_idx] if query_lens else 0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

def main() -> int:
    p = argparse.ArgumentParser(description="Router 指标查询")
    p.add_argument("--window", default="24h", help="时间窗口，默认 24h（支持 Ns/Nm/Nh/Nd）")
    args = p.parse_args()
    try:
        report = build_report(args.window)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"查询失败: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 告警阈值提示
    if report.get("hit_zero_rate", 0) > 0.05:
        print(f"\n⚠️  告警：hit_zero_rate = {report['hit_zero_rate']:.2%} 超过 5% 阈值", file=sys.stderr)
        return 3
    return 0

if __name__ == "__main__":
    sys.exit(main())
