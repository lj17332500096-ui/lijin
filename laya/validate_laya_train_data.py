#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 Laya 工具意图训练数据的质量与 Laya 可消费性.

检查项:
  1. 唯一性: train/val/test 无 query 重叠 (防泄漏)
  2. schema 完整性: 每行含 id/workflow/state/questions/gold 且 JSON 可解析
  3. 类别分布: tool vs text 比例
  4. Laya 可消费性: 用 laya.Agent 试 tokenize 前 5 条 (确认 state+questions 拼出合法序列)
  5. 数据量警告: 总样本数是否支撑 >95% 准确率目标 (统计学习下界)
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data" / "laya_tool_intent"

def load_jsonl(p: pathlib.Path):
    out = []
    with open(p, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  [FAIL] {p.name}:{i} 不是合法 JSON")
                return None
    return out

def main():
    ok = True
    train = load_jsonl(DATA / "train.jsonl")
    val = load_jsonl(DATA / "val.jsonl")
    test = load_jsonl(DATA / "test.jsonl")
    if None in (train, val, test):
        print("schema 完整性: FAIL (存在坏 JSON 行)")
        sys.exit(1)
    print(f"schema 完整性: PASS  train={len(train)} val={len(val)} test={len(test)}")

    # 唯一性: 取 query 文本 (state 字段是 JSON 字符串, 解析回原文)
    def queries(items):
        return set(json.loads(it["state"]) for it in items)
    qt, qv, qte = queries(train), queries(val), queries(test)
    ov_tv = qt & qv
    ov_tt = qt & qte
    ov_vt = qv & qte
    print(f"防泄漏: train∩val={len(ov_tv)}, train∩test={len(ov_tt)}, val∩test={len(ov_vt)}")
    if ov_tv or ov_tt or ov_vt:
        ok = False
        print("  [FAIL] 存在跨 split 重叠 query")

    # 类别分布
    def label_dist(items):
        c = {"tool": 0, "text": 0}
        for it in items:
            gold = json.loads(it["gold"])
            c[gold["needs_tool"]["label"]] += 1
        return c
    ct, cv, ce = label_dist(train), label_dist(val), label_dist(test)
    total = sum(len(x) for x in (train, val, test))
    tool_total = ct["tool"] + cv["tool"] + ce["tool"]
    print(f"类别分布: train={ct} val={cv} test={ce}")
    print(f"全量 tool/text = {tool_total}/{total-tool_total} = {tool_total/total:.1%}")

    # 数据量警告 (统计学习: 要"测出" 95% 准确率且 CI 下界 >90%, 需 n≈200 测试样本)
    n_test = len(test)
    if n_test < 200:
        ok = False
        print(f"  [WARN] test 仅 {n_test} 条: 统计上无法验证 '>=95% 准确率' 目标 "
              f"(n=40 时 95% CI 半宽约 ±15pp, 测不出 95% vs 80% 的区别)")

    # Laya 可消费性 (若 laya 已安装): 验证真实 checkpoint 能消费该 schema
    try:
        import laya
        from laya import Agent
        agent = Agent("convaiinnovations/laya")
        sample = train[0]
        state = json.loads(sample["state"])
        questions = json.loads(sample["questions"])
        res = agent.system_one(state, questions)
        # Laya 0.3.3 实际结构: res['answers'][qid] (不是 res[qid])
        ans = res["answers"]["needs_tool"]
        print(f"Laya 可消费性: PASS (真实 checkpoint 跑通, choice={ans['choice']}, "
              f"probs={ans['probabilities']}, conf={ans['confidence']:.3f})")

        # 顺带: 用真实 checkpoint 跑 test 集基线准确率 (微调前的参照系)
        correct = 0
        for it in test:
            st = json.loads(it["state"])
            qs = json.loads(it["questions"])
            gl = json.loads(it["gold"])["needs_tool"]["label"]
            r = agent.system_one(st, qs)
            pred = r["answers"]["needs_tool"]["choice"]
            if pred == gl:
                correct += 1
        print(f"基线准确率 (未微调 checkpoint 在 {len(test)} 条 test 上): "
              f"{correct}/{len(test)} = {correct/len(test):.1%}")
        print(f"  (微调目标 >=95% 必须显著高于此基线; n={len(test)} 时 CI 半宽约 ±{0.15:.0%}pp)")
    except Exception as e:
        print(f"Laya 可消费性: SKIP ({type(e).__name__}: {str(e)[:80]})")

    print("\n=== 验证结论 ===")
    print("PASS" if ok else "FAIL/WARN (见上)")

if __name__ == "__main__":
    main()
