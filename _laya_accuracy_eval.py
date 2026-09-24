"""Laya 在 FORGE 工具意图上的真实准确率评估（一次性脚本，非测试）。

构造 16 条有 ground truth 标注的 query（tool 8 + text 8），跑 Laya 真实二分类，
统计：准确率、误判详情、延迟分布、confidence 分布。
用于判定 Laya 是否值得作为 FORGE L1 工具路由增强组件保留。

ground truth 标注原则：
- tool：查询含明确"需调用外部工具"意图（读写文件/搜索/运行/生成文档/记忆操作）
- text：纯文本/概念/问候/改写，无需任何工具
"""
from __future__ import annotations
import time
from laya import Router

# 16 条 ground truth 标注 query（tool=需工具，text=纯文本可答）
QUERIES = [
    # ---- tool 意图（8 条）----
    ("tool", "帮我写一个快速排序算法并保存到文件"),
    ("tool", "读一下项目里的 README 告诉我怎么启动"),
    ("tool", "运行一下 tests/test_runner.py 看看有没有报错"),
    ("tool", "查一下今天的北京天气"),
    ("tool", "帮我搜一下最新的 agnes 模型更新"),
    ("tool", "生成一份 PPT 汇报文档"),
    ("tool", "帮我把这段代码修复下，修复完跑测试"),
    ("tool", "记住我的偏好是赛博朋克风格"),
    # ---- text 意图（8 条）----
    ("text", "What is a black hole?"),
    ("text", "hello"),
    ("text", "什么是 RAG 技术？"),
    ("text", "帮我润色这段措辞让它更正式一些"),
    ("text", "给我讲个笑话"),
    ("text", "2024 年有哪些热门的编程语言？"),
    ("text", "把这句话翻译成英文：很高兴见到你"),
    ("text", "谢谢，再见"),
]

QUESTIONS = {
    "needs_tool": {
        "type": "choice",
        "instructions": (
            "判断这条用户消息是否需要调用外部工具才能完成"
            "（如读写文件、搜索、运行代码、生成文档、记忆操作）。"
            "若纯文本/概念问答/问候/改写可直接回答，则选 text。"
        ),
        "criteria": {"tool": "需要工具", "text": "纯文本可答"},
    }
}


def run():
    print("=== 加载 Laya Router（已本地缓存，~秒级）===")
    t0 = time.time()
    r = Router(preload=True)
    print(f"  加载耗时 {time.time() - t0:.1f}s\n")

    total = len(QUERIES)
    correct = 0
    misclass: list[str] = []
    latencies: list[float] = []
    confs: list[float] = []

    print(f"{'#':<2}{'GT':<5}{'判定':<6}{'正确?':<4}{'延迟':>8}{'conf':>8}  query")
    print("-" * 78)
    for i, (gt, q) in enumerate(QUERIES, 1):
        t0 = time.time()
        res = r.predict({"message": q}, questions=QUESTIONS)
        ms = round((time.time() - t0) * 1000, 1)
        latencies.append(ms)
        ans = res.get("answers", {}).get("needs_tool", {})
        verdict = ans.get("choice", "?")
        conf = float(ans.get("confidence", 0.0))
        confs.append(conf)
        ok = (verdict == gt)
        correct += int(ok)
        mark = "OK" if ok else "X"
        if not ok:
            misclass.append(f"  误判 #{i}: GT={gt} 判定={verdict}  query={q!r}  conf={conf:.4f}")
        print(f"{i:<2}{gt:<5}{verdict:<6}{mark:<4}{ms:>6}ms{conf:>7.4f}  {q}")

    print("\n=== 汇总 ===")
    print(f"  准确率: {correct}/{total} = {correct / total * 100:.1f}%")
    print(f"  误判数: {len(misclass)}")
    for m in misclass:
        print(m)
    import statistics as st
    print(f"\n  延迟:  最小={min(latencies):.0f}ms  中位={st.median(latencies):.0f}ms  "
          f"最大={max(latencies):.0f}ms  均值={st.mean(latencies):.0f}ms")
    print(f"  confidence: 最小={min(confs):.4f}  中位={st.median(confs):.4f}  "
          f"最大={max(confs):.4f}")
    print("\n  （temperature=1.0 下 confidence 被洗得很低；若调 temperature=0.1 "
          "可锐化概率、提升 confidence，但可能过度自信。可另测一组。）")


if __name__ == "__main__":
    run()
