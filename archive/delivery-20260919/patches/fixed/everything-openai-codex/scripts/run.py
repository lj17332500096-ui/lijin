#!/usr/bin/env python3
"""scripts/run.py — 实现层入口（含健康契约别名，防 selftest sys.path 抢占）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as _impl

# WB 依赖降级注入（2026-09-13）：网络调用默认 8s 超时，防 hang 死（无产出）
try:
    import socket as _wb_sock
    _wb_sock.setdefaulttimeout(8)
except Exception:
    pass

main = _impl.main
read_text_safe = getattr(_impl, '_read_text_safe', None) or getattr(_impl, 'read_text_safe', None) or getattr(_impl, '_read_text_safe_enc', None)
dry_run = getattr(_impl, 'dry_run', False)

def _safe_main():
    try:
        return _impl.main()
    except Exception as e:
        print(f"[ERROR] 运行异常: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(_safe_main())
