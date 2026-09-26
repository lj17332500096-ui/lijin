"""Laya llama bridge 测试：FORGE_LAYA_BACKEND=llama 时走 8099 GPU 推理。

用法：
    FORGE_LAYA_BACKEND=llama FORGE_LAYA_LLM_URL=http://127.0.0.1:8099 \
    PYTHONPATH= .venv/Scripts/python.exe tests/test_laya_llama_bridge.py
"""
import os
import sys

# 确保 8099 在线
from runtime.laya_llama_bridge import laya_llama_bridge_available

def main() -> int:
    url = os.getenv("FORGE_LAYA_LLM_URL", "http://127.0.0.1:8099")
    if not laya_llama_bridge_available():
        print(f"[FAIL] {url} 不可达，无法测试 llama 桥接")
        return 1

    os.environ["FORGE_LAYA_BACKEND"] = "llama"
    from runtime.laya_router import LayaRouter

    r = LayaRouter(preload=True)
    assert r.backend == "llama", f"expected llama backend, got {r.backend}"
    print(f"[OK] backend = {r.backend}")

    # 测试用例：(query, 期望 choice)
    cases = [
        ("帮我算 1234 加 5678", "tool"),
        ("搜索一下新能源汽车的最新消息", "tool"),
        ("帮我写一段 Python 快速排序", "tool"),
        ("什么是量子计算", "text"),
        ("解释一下 Transformer 原理", "text"),
        ("你好", "text"),
    ]
    ok = 0
    for q, expected in cases:
        res = r.predict(
            q,
            questions={
                "needs_tool": {
                    "type": "choice",
                    "instructions": q,
                    "criteria": {
                        "text": "answer directly without tools",
                        "tool": "requires tool call",
                    },
                }
            },
        )
        ans = res["answers"]["needs_tool"]
        choice, conf = ans["choice"], ans["confidence"]
        screen = r.screen(q)
        status = "OK" if choice == expected else "WRONG"
        if choice == expected:
            ok += 1
        print(f"  [{status}] {q[:20]:20s}  choice={choice} (expected {expected})  conf={conf}  screen={screen}")

    print(f"\n[RESULT] {ok}/{len(cases)} correct")
    return 0 if ok == len(cases) else 2


if __name__ == "__main__":
    sys.exit(main())
