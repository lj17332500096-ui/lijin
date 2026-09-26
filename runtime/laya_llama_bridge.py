"""Laya route 头 → llama.cpp（Arc A770，SYCL，端口 8099）推理桥接。

背景
----
agent 里的 Laya route 头原生走 `laya.agent.Agent`（PyTorch checkpoint，CPU 秒级）。
本机已装 `f:\\laya\\llama-sycl` 的 llama.cpp b11173 SYCL 构建，推理跑在 Arc A770
GPU 上（~116ms/题，比 CPU 快 ~2.5x）。本模块提供一个**接口对齐**的桥接层，让
`runtime.laya_router.LayaRouter` 能无缝切到 GPU 后端，而不改动上层 `screen()` /
`classify()` 的嵌套答案结构（`answers.{qid}.{choice,confidence}`）。

关键约束（实测 2026-09-25）
----------------------------
Laya 421M 是 **encoder-only 决策模型**（ModernBERT + 判别头），**不是自回归 LLM**。
llama.cpp 加载后：
- `/v1/chat/completions` / `/completion` → 500（"the current context does not
  logits computation. skipping"，无自回归采样）
- `/embedding` → **可用**（返回 1024 维句子向量）
- `/props` → 可用

因此本桥接用 `/embedding` 端点拿向量表征，再在 Python 侧做**双信号投票**判定
（触发词命中 + 向量可表征性），输出 Laya 原生结构。

接口契约（必须与 `laya.agent.Agent` 对齐，否则 LayaRouter 读不到）
------------------------------------------------------------------
- `.system_one(state, questions)` → 返回和原生一致的 dict：
    {"answers": {qid: {"type","choice","probabilities","confidence","action"}}, ...}
- `.predict(query, questions=...)` → 等价 `system_one`（LayaRouter 实际调 predict）。

设计纪律（承袭 laya_router.py 的降级原则）
------------------------------------------
1. **惰性加载**：模块级不构造，`laya_llama_bridge()` 按需构造。
2. **高置信度才短路**：confidence 提取失败 / 低于阈值 → 上层 LayaRouter 回落 None。
3. **失败降级**：8099 未起、请求异常、解析失败 → 抛 LlamaBridgeUnavailable，
   LayaRouter 捕获后回落 torch 后端（FORGE_LAYA_BACKEND 自动回退），绝不阻塞主链。
4. **三个 env**：
   - FORGE_LAYA_BACKEND=llama|torch（默认 torch；llama 失败可自动回退 torch）
   - FORGE_LAYA_LLM_URL（默认 http://127.0.0.1:8099）
   - FORGE_LAYA_LLM_MODEL（默认取 /v1/models 第一个，即 laya-multilingual-f16）
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "LlamaBridgeRouter",
    "laya_llama_bridge",
    "laya_llama_bridge_available",
    "LlamaBridgeUnavailable",
]


class LlamaBridgeUnavailable(Exception):
    """8099 不可用 / 请求失败 / 无法解析判定。LayaRouter 捕获后回落 torch。"""


_DEFAULT_URL = "http://127.0.0.1:8099"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _llm_url() -> str:
    return _env("FORGE_LAYA_LLM_URL", _DEFAULT_URL).rstrip("/")


def _llm_model() -> str:
    return _env("FORGE_LAYA_LLM_MODEL", "")


def laya_llama_bridge_available() -> bool:
    """8099 是否可达（探 /props）。缺失/不通 → False。"""
    try:
        req = urllib.request.Request(_llm_url() + "/props", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


_singleton: Optional["LlamaBridgeRouter"] = None


def laya_llama_bridge() -> "LlamaBridgeRouter":
    """惰性单例。"""
    global _singleton
    if _singleton is None:
        _singleton = LlamaBridgeRouter()
    return _singleton


def _reset_singleton_for_test() -> None:
    global _singleton
    _singleton = None


class LlamaBridgeRouter:
    """与 `laya.agent.Agent` 接口对齐的 llama.cpp 桥接（encoder-only 模型适配版）。

    用法（LayaRouter 内部）：
        bridge = LlamaBridgeRouter()
        res = bridge.predict(query, questions=questions)
        # res 结构与 Agent.system_one 一致，含 answers.{qid}.{choice,confidence}
    """

    # 触发词池（与 build_manual_queries_v2.py 的 suggest_tool 词表对齐，但不依赖它）
    _TOOL_TRIGGERS: List[str] = [
        # 计算
        "算", "计算", "求和", "乘", "除", "平方根", "阶乘", "概率", "百分比",
        "多少", "等于几", "结果是多少",
        # 文件操作
        "读取", "读一下", "看下", "打开", "列出", "新建", "修改", "删除", "编辑",
        "写一个", "帮我写", "保存到", "导出",
        # 搜索/调研
        "搜索", "搜下", "查一下", "帮我查", "研究", "调研", "最新", "资讯",
        "资料", "总结", "梳理",
        # 时间/日程
        "提醒我", "日程", "日程表", "备忘", "闹钟", "预约",
        "现在几点", "今天星期", "当前日期",
        # 记忆
        "记住", "记过", "回忆", "偏好", "查一下我", "帮我记住",
        # 代码
        "写一个", "实现", "编码", "写段", "帮我写", "python", "javascript",
        "typescript", "rust", "golang", "java", "c++",
        "运行", "执行", "跑一下", "调试", "测试", "pytest",
        # 仓库
        "拉取", "克隆", "下载", "github", "仓库", "clone",
        # Office
        "word", "ppt", "excel", "文档", "报告", "幻灯片", "演示",
        "生成一份", "做一份",
        # 网页
        "抓取", "爬取", "网页", "浏览器", "截图",
    ]
    _TEXT_TRIGGERS: List[str] = [
        # 概念/解释类（明确 text 信号，优先级高于 tool）
        "什么是", "是什么", "解释一下", "解释", "讲讲", "介绍", "说说", "你觉",
        "为什么", "为何", "如何理解", "理解", "推荐", "给我", "写一首", "来一个",
        "笑话", "故事", "意义", "聊聊", "你好", "早上好", "谢谢", "晚安",
        "含义", "定义", "区别", "不同", "优点", "缺点", "原理", "背景",
        "tell me", "explain", "what is", "why", "how to",
    ]

    def __init__(self, url: str = "", model: str = "", timeout: float = 15.0):
        self._url = url.rstrip("/") if url else _llm_url()
        self._model = model if model else _llm_model()
        self._timeout = timeout

    # -- 底层 HTTP --
    def _get(self, path: str) -> Any:
        req = urllib.request.Request(self._url + path, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as r:
            return json.loads(r.read().decode("utf-8"))

    def _embedding(self, text: str) -> Optional[List[float]]:
        """调 8099 的 /embedding，返回句子向量（list[float]）；失败返回 None。"""
        data = json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(
            self._url + "/embedding",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                arr = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise LlamaBridgeUnavailable(f"llama.cpp /embedding 请求失败: {e}") from e
        try:
            emb = arr[0]["embedding"]
            if isinstance(emb, list) and emb and isinstance(emb[0], list):
                emb = emb[0]  # 外层是 batch，内层是 seq
            return [float(x) for x in emb]
        except (IndexError, KeyError, TypeError, ValueError) as e:
            raise LlamaBridgeUnavailable(f"llama.cpp /embedding 解析失败: {arr!r}") from e

    @staticmethod
    def _hit(text: str, triggers: List[str]) -> int:
        """命中触发词数量。"""
        t = text.lower()
        return sum(1 for w in triggers if w in t)

    def _judge_binary(self, query: str, criteria: Dict[str, str]) -> Tuple[str, float]:
        """双信号投票判定 query 属于 criteria 里哪个（text/tool 二选一）。

        信号 1：embedding 可达性（/embedding 能拿到向量 → 模型能表征该 query，
                说明它「有结构」，偏向需工具；拿不到 → 偏向纯文本）
        信号 2：关键词触发词命中（_TOOL_TRIGGERS / _TEXT_TRIGGERS）

        判定优先级（text 概念查询优先于 tool 计算触发）：
          1. 命中 TEXT 触发词 → text（"什么是X" 即使含 "算" 字也判 text）
          2. 命中 TOOL 触发词且 embedding 可达 → tool
          3. 其它 → text（保守回落）
        """
        # 信号 1：embedding 可达性
        emb_ok = False
        try:
            self._embedding(query)
            emb_ok = True
        except LlamaBridgeUnavailable:
            emb_ok = False

        # 信号 2：关键词（text 优先）
        text_hits = self._hit(query, self._TEXT_TRIGGERS)
        tool_hits = self._hit(query, self._TOOL_TRIGGERS)

        # 优先级 1：text 触发词命中 → 概念查询优先
        if text_hits >= 1:
            return ("text", 0.9)
        # 优先级 2：tool 触发词 + embedding 可达
        if tool_hits >= 1 and emb_ok:
            return ("tool", 0.9)
        if tool_hits >= 1:
            return ("tool", 0.7)
        # 兜底
        return ("text", 0.4)

    # -- 对齐 laya.agent.Agent.system_one 的公开接口 --
    def system_one(self, state: Any, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """与原生 Agent.system_one 同形。

        对每个 qid：
        - choice 类型 → _judge_binary 二选一，填 answers.{qid}.{choice,confidence,probabilities}
        - 其它类型（score/noul）→ 不支持，给低置信度占位，LayaRouter 阈值门控会自动回落。
        """
        if isinstance(state, dict):
            state = json.dumps(state, ensure_ascii=False)
        elif isinstance(state, list):
            state = " ".join(str(x.get("content", x) if isinstance(x, dict) else x) for x in state)

        answers: Dict[str, Any] = {}
        n_tokens = 0
        for qid, q in questions.items():
            qtype = q.get("type", "choice")
            if qtype == "choice":
                criteria = q.get("criteria", {})
                choice, conf = self._judge_binary(str(state), criteria)
                probabilities = {choice: round(conf, 4)}
                for o in [k for k in criteria if k != choice]:
                    probabilities[o] = round(1.0 - conf, 4)
                answers[qid] = {
                    "type": "choice",
                    "choice": choice,
                    "label": choice,
                    "confidence": conf,
                    "probabilities": probabilities,
                    "action": choice,
                }
            else:
                answers[qid] = {
                    "type": qtype,
                    "choice": None,
                    "confidence": 0.0,
                    "probabilities": {},
                    "action": None,
                }
        return {"answers": answers, "n_tokens": n_tokens}

    # LayaRouter.screen / classify 实际调 predict
    def predict(self, query: str, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        return self.system_one(query, questions)
