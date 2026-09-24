"""Laya 前置判别层（L1 工具选择层）。

职责演进（2026-09-24 P0/P2）：从「二分类快筛」升级为「三判别」——
  - intent：       纯文本 / 需工具 / 算术 / 打招呼（choice）
  - completeness： 自足 / 需 LLM 补全（choice）
  - route：        候选工具名（choice，多路头；未训练前回落 tool_router）

设计纪律（不可违背）：
1. **惰性加载**：模块级不再构造单例（此前 `_SINGLETON = LayaRouter()` 导致
   import 即加载 checkpoint，实测 11.6s）。改为 `laya_router()` 函数按需构造。
2. **高置信度才短路**：confidence >= 阈值才给判定，低于阈值一律 None 回落，
   绝不让 Laya 误判把该用工具的查询判成纯文本。
3. **失败降级**：laya / torch 未装、推理异常、字段不对齐 → 一律 None 回落现有
   tool_router，绝不阻塞主链。
4. **三个 env 开关**：FORGE_LAYA（off 一键关）、FORGE_LAYA_CONF（阈值）、
   FORGE_LAYA_PRELOAD（常驻 checkpoint 避免冷加载）。
5. **不碰 tool_router.py 一行**，不进 C2 熔断统计。

route 判别头（≤8 工具多路）的 checkpoint 尚未训练；未训练前 `classify()` 的
route 字段恒为 None，由 orchestrator 回落现有 select_tool_names。
"""
from __future__ import annotations

import os
import importlib.util
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "laya_available",
    "laya_router_enabled",
    "laya_router_confidence_threshold",
    "laya_fast_screen",
    "laya_screen_result",
    "laya_router",
    "LayaDecision",
    "LayaRouter",
]

# 默认置信度阈值：低于此值的判断一律回落现有 tool_router。
_DEFAULT_CONF = 0.85


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if v == "":
        return default
    return v in ("on", "true", "1")


def _env_conf() -> float:
    try:
        return float(os.getenv("FORGE_LAYA_CONF", str(_DEFAULT_CONF)))
    except ValueError:
        return _DEFAULT_CONF


def laya_available() -> bool:
    """laya 与 torch 是否都可用（缺失即降级，不抛异常）。"""
    return bool(
        importlib.util.find_spec("laya")
        and importlib.util.find_spec("torch")
    )


def laya_router_enabled() -> bool:
    """是否启用 Laya 快筛（须同时满足：laya/torch 可用 + FORGE_LAYA 非 off）。"""
    if not laya_available():
        return False
    return _env_flag("FORGE_LAYA", default=True) and not _env_flag("FORGE_LAYA_OFF", default=False)


def laya_router_confidence_threshold() -> float:
    return _env_conf()


# ---------------------------------------------------------------------------
# 惰性单例（F3 修复）：此前是模块级 `_SINGLETON = LayaRouter()`，import 即构造。
# 现在延迟到真正需要时；关掉开关时永不构造，import 零开销。
# ---------------------------------------------------------------------------
_SINGLETON: Optional["LayaRouter"] = None


def laya_router() -> "LayaRouter":
    """懒加载单例：构造会加载 checkpoint，只在真正需要时才做。"""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = LayaRouter()
    return _SINGLETON


def _reset_singleton_for_test() -> None:
    """仅测试用：重置惰性单例。"""
    global _SINGLETON
    _SINGLETON = None


def laya_screen_result() -> Optional[str]:
    """Laya 当前对某 query 的结论缓存（由 LayaRouter.screen 填充，便于 runner 读取）。

    返回 "direct_text" / "tool_needed" / None。
    """
    if _SINGLETON is None:
        return None
    return _SINGLETON.last_result


@dataclass
class LayaDecision:
    """一次三判别前向的结论。字段为 None 表示该路未命中/未训练/降级。"""

    intent: Optional[str] = None
    intent_conf: float = 0.0
    completeness: Optional[str] = None
    completeness_conf: float = 0.0
    route: Optional[str] = None
    route_conf: float = 0.0

    @classmethod
    def fallback(cls) -> "LayaDecision":
        """全 None：编排器据此回落 LLM 兜底。"""
        return cls()


def laya_fast_screen(query: str) -> Optional[str]:
    """模块级快筛入口（F1 修复：不再用类调用实例方法）。

    返回 "direct_text" / "tool_needed" / None（回落）。
    未启用 / 未安装 / 异常 → None。
    """
    if not laya_router_enabled():
        return None
    try:
        return laya_router().screen(query)
    except Exception:
        return None


class LayaRouter:
    """Laya 判别器包装。只暴露快筛 + 三判别，不碰 tool_router。

    用法（orchestrator 集成点）：
        from runtime import laya_router
        res = laya_router.laya_fast_screen(query)   # -> "direct_text"|"tool_needed"|None
        # 或三判别：
        d = laya_router.laya_router().classify(query)
    """

    def __init__(self, preload: bool = False, device: Optional[str] = None):
        self._router = None
        self.last_result: Optional[str] = None
        self.last_decision: Optional[LayaDecision] = None
        self._preload = preload or _env_flag("FORGE_LAYA_PRELOAD")
        self._device = device
        # F3 修复：只有真正启用时才加载模型（此前不看开关就构造）。
        if self._preload and laya_available():
            self._init_router()

    # -- 懒加载（首次 screen/classify 时构建） --
    def _init_router(self) -> None:
        if not laya_available():
            return
        try:
            from laya import Router  # noqa: F401  (保留原接口，Router 仅作包装)
            from laya.agent import Agent
            # 用 FORGE 微调 checkpoint（若存在）；否则回落英文默认。
            ckpt = os.getenv("FORGE_LAYA_CHECKPOINT", "").strip()
            if ckpt:
                self._agent = Agent(ckpt, device=self._device)
            else:
                self._agent = Agent(device=self._device)
            self._router = self._agent  # system_one / predict 别名
        except Exception:
            # 加载失败：保持 _router=None，调用方回落，绝不抛。
            self._router = None

    def _ensure(self) -> None:
        if self._router is None and laya_available():
            self._init_router()

    def screen(self, query: str) -> Optional[str]:
        """对 query 做「需工具 vs 纯文本」二分类快筛（向后兼容旧接口）。

        返回 "direct_text" / "tool_needed" / None（回落）。
        """
        self._ensure()
        if self._router is None:
            return None
        thr = laya_router_confidence_threshold()
        try:
            questions = {
                "needs_tool": {
                    "type": "choice",
                    "instructions": query,
                    "criteria": {"text": "answer directly without tools",
                                 "tool": "requires tool call"},
                },
            }
            decision = self._router.predict(query, questions=questions)
            text_conf = self._extract_confidence(decision, "text")
            if text_conf is None:
                return None
            if text_conf >= thr:
                self.last_result = "direct_text"
                return "direct_text"
            if (1.0 - text_conf) >= thr:
                self.last_result = "tool_needed"
                return "tool_needed"
            return None
        except Exception:
            return None

    def classify(self, query: str) -> LayaDecision:
        """三判别：intent + completeness + route（route 头未训练前恒 None）。"""
        self._ensure()
        if self._router is None:
            self.last_decision = LayaDecision.fallback()
            return self.last_decision

        try:
            questions = {
                "intent": {
                    "type": "choice",
                    "instructions": query,
                    "criteria": {
                        "greeting": "greeting or small talk",
                        "arith": "pure arithmetic that needs no tool",
                        "tool_needed": "needs a tool to answer",
                        "ambiguous": "unclear intent",
                    },
                },
                "completeness": {
                    "type": "choice",
                    "instructions": query,
                    "criteria": {
                        "self_sufficient": "can act immediately",
                        "need_llm": "needs LLM to fill params/context",
                    },
                },
            }
            res = self._router.predict(query, questions=questions)
            answers = res.get("answers", {}) if isinstance(res, dict) else {}

            intent = self._choice(answers, "intent")
            intent_conf = self._conf(answers, "intent")
            completeness = self._choice(answers, "completeness")
            completeness_conf = self._conf(answers, "completeness")

            self.last_decision = LayaDecision(
                intent=intent,
                intent_conf=intent_conf,
                completeness=completeness,
                completeness_conf=completeness_conf,
                route=None,  # route 头未训练，恒回落
                route_conf=0.0,
            )
            return self.last_decision
        except Exception:
            self.last_decision = LayaDecision.fallback()
            return self.last_decision

    @staticmethod
    def _choice(answers: dict, qid: str) -> Optional[str]:
        block = answers.get(qid)
        if isinstance(block, dict):
            return block.get("choice") or block.get("label")
        return None

    @staticmethod
    def _conf(answers: dict, qid: str) -> float:
        block = answers.get(qid)
        if isinstance(block, dict):
            v = block.get("confidence")
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0

    @staticmethod
    def _extract_confidence(decision: dict, option: str) -> Optional[float]:
        """从 Laya 结果里抠出某选项的校准概率；字段名因版本而异，逐一试。"""
        for key in ("probabilities", "answers", "score", "calibration"):
            block = decision.get(key)
            if isinstance(block, dict):
                v = block.get(option)
                if isinstance(v, (int, float)):
                    return float(v)
        v = decision.get(option)
        if isinstance(v, (int, float)):
            return float(v)
        return None
