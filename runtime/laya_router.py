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
from typing import Any, Dict, Optional

__all__ = [
    "laya_available",
    "laya_router_enabled",
    "laya_router_confidence_threshold",
    "laya_fast_screen",
    "laya_screen_result",
    "laya_router",
    "LayaDecision",
    "LayaRouter",
    "laya_router_backend",
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


def laya_router_backend() -> str:
    """当前 Laya 推理后端：'torch'（默认，进程内 laya.agent.Agent）或
    'llama'（HTTP 调 llama.cpp 8099，Arc A770 GPU）。

    env: FORGE_LAYA_BACKEND ∈ {torch, llama}，默认 torch。
    llama 后端不可用（8099 未起）时 LayaRouter 自动回落 torch（绝不阻塞）。
    """
    return os.getenv("FORGE_LAYA_BACKEND", "torch").strip().lower() or "torch"


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
        self._backend: Optional[str] = None
        # F3 修复：只有真正启用时才加载模型（此前不看开关就构造）。
        if self._preload and laya_available():
            self._init_router()

    # -- 懒加载（首次 screen/classify 时构建） --
    def _init_router(self) -> None:
        if self._router is not None:
            return
        backend = laya_router_backend()
        # llama 后端优先（若指定且可用），失败回落 torch
        if backend == "llama":
            try:
                from runtime.laya_llama_bridge import LlamaBridgeRouter, laya_llama_bridge_available
                if laya_llama_bridge_available():
                    self._agent = LlamaBridgeRouter()
                    self._router = self._agent
                    self._backend = "llama"
                    return
                # 8099 不可用 → 回落 torch（不抛）
            except Exception:
                pass
        # torch 后端（默认 / llama 回落）
        if not laya_available():
            return
        try:
            from laya import Router  # noqa: F401  (保留原接口，Router 仅作包装)
            from laya.agent import Agent
            # checkpoint 优先级：
            # 1. FORGE_LAYA_CHECKPOINT 显式指定（可调未微调 english 或微调后产物）
            # 2. 未指定 → 默认指向 5000 数据 XPU 修复后 + 算术口径翻转后微调 checkpoint
            #    （真实判别头 98.0%，FP=0）。这是 P5 主攻路线当前最优产物。
            ckpt = os.getenv("FORGE_LAYA_CHECKPOINT", "").strip()
            if not ckpt:
                from pathlib import Path
                arith_dir = Path(__file__).resolve().parent.parent / "data" / "laya_forge" / "forge_finetuned_5000_xpu_fixed_arith"
                if arith_dir.exists():
                    ckpt = str(arith_dir)
                else:
                    legacy_dir = Path(__file__).resolve().parent.parent / "data" / "laya_forge" / "forge_finetuned_5000_xpu_fixed"
                    ckpt = str(legacy_dir) if legacy_dir.exists() else ""
            if ckpt:
                self._agent = Agent(ckpt, device=self._device)
            else:
                self._agent = Agent(device=self._device)
            self._router = self._agent  # system_one / predict 别名
            self._backend = "torch"
        except Exception:
            # 加载失败：保持 _router=None，调用方回落，绝不抛。
            self._router = None

    def _ensure(self) -> None:
        if self._router is None and (laya_available() or laya_router_backend() == "llama"):
            self._init_router()

    @property
    def backend(self) -> Optional[str]:
        """当前生效后端：'llama' / 'torch' / None（未加载）。"""
        return self._backend

    # -- 转发底层后端的 system_one / predict（LlamaBridgeRouter / laya Agent 都有）--
    def system_one(self, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
        """转发到底层后端。未加载 → 空 answers。"""
        self._ensure()
        if self._router is None:
            return {"answers": {}, "n_tokens": 0}
        return self._router.system_one(state, questions=questions)

    def predict(self, query: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        """转发到底层后端的 predict。"""
        self._ensure()
        if self._router is None:
            return {"answers": {}, "n_tokens": 0}
        return self._router.predict(query, questions=questions)

    def screen(self, query: str) -> Optional[str]:
        """对 query 做「需工具 vs 纯文本」二分类快筛（向后兼容旧接口）。

        返回 "direct_text" / "tool_needed" / None（回落）。

        读取真实 Agent 的嵌套返回结构：
        - 选中项：answers.needs_tool.choice（"text"/"tool"）
        - 选中项置信度：answers.needs_tool.confidence（该 choice 的概率）
        逻辑：choice=text 且 conf≥阈值 → direct_text；choice=tool 且 conf≥阈值 →
        tool_needed；否则回落 None。confidence 是「选中项」概率（非 text 概率），
        旧实现误把 confidence 当 text 概率用，已修正。
        兼容 mock 的顶层 probabilities 结构（_extract_confidence 仍保留）。
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

            # 优先读真实 Agent 的嵌套结构：answers.needs_tool.{choice, confidence}
            ans = (decision.get("answers") or {}).get("needs_tool") if isinstance(decision, dict) else None
            choice, conf = self._extract_choice_conf(ans)
            if choice is not None and conf is not None:
                if choice == "text" and conf >= thr:
                    self.last_result = "direct_text"
                    return "direct_text"
                if choice == "tool" and conf >= thr:
                    self.last_result = "tool_needed"
                    return "tool_needed"
                return None
            # 回落到旧逻辑（mock 的顶层 probabilities 结构）
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

    @staticmethod
    def _extract_choice_conf(ans: Optional[dict]) -> tuple[Optional[str], Optional[float]]:
        """从 answers.needs_tool 块读 (choice, 选中项 confidence)。

        真实 Agent 返回：
            answers.needs_tool = {"type","choice","probabilities"{text,tool},
                                  "confidence", "action"}
        confidence 是「选中项」的概率（choice=text 时 confidence≈text 概率）。
        读不到则返回 (None, None)，调用方回落。
        """
        if not isinstance(ans, dict):
            return None, None
        choice = ans.get("choice") or ans.get("label")
        conf = ans.get("confidence")
        if choice not in ("text", "tool"):
            # 没有明确 choice，尝试从 probabilities 推断（兜底）
            probs = ans.get("probabilities")
            if isinstance(probs, dict):
                tv = probs.get("text")
                ov = probs.get("tool")
                if isinstance(tv, (int, float)) and isinstance(ov, (int, float)):
                    choice = "text" if tv >= ov else "tool"
                    conf = max(tv, ov)
        if choice not in ("text", "tool") or not isinstance(conf, (int, float)):
            return None, None
        return choice, float(conf)

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
