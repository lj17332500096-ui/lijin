"""Model Router：按任务元数据/渠道选择模型档位。

档位配置（.env，缺省 = 维持现状单模型不克隆）：
- MODEL_DEFAULT / AGENT_MODEL    默认档（推理对话）
- MODEL_CHEAP                    轻量档（快速问答、定时任务等）
- MODEL_REASONING                重推理档（深度任务）

Agent 实例是按 profile 克隆的（model + max_tokens 来自当前 Agent 的 ModelSettings），
克隆基准永远取"运行时正在用的 Agent"（main 里 --no-guardrails 切换的那一份），
避免绕过 CLI 的安全开关。
"""

import os
from typing import Any

from runtime.task import Task


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def default_model() -> str:
    return _env("AGENT_MODEL", "") or _env("MODEL_DEFAULT", "")


def profile_model(profile: str) -> str | None:
    if profile == "cheap":
        return _env("MODEL_CHEAP", "") or None
    if profile == "reasoning":
        return _env("MODEL_REASONING", "") or None
    return None


def route_profile(task: Task) -> str:
    """根据任务元数据与渠道推断档位：reasoning > cheap > default。"""
    meta = getattr(task, "metadata", {}) or {}
    explicit = str(meta.get("model_profile") or "").strip().lower()
    if explicit in ("cheap", "reasoning"):
        return explicit
    channel = str(meta.get("channel") or "chat")
    if meta.get("deep"):
        return "reasoning"
    if channel in ("scheduled", "daemon") and profile_model("cheap"):
        return "cheap"
    return "default"


def max_output_tokens() -> int | None:
    raw = _env("MODEL_MAX_OUTPUT_TOKENS", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


_clone_cache: dict[str, Any] = {}


def agent_for(base_agent: Any, profile: str) -> Any:
    """返回适配档位的 Agent（模型没变/未配置时原样返回，保持行为不变）。"""
    if profile == "default":
        return base_agent
    model = profile_model(profile)
    if not model:
        return base_agent
    current_model = getattr(base_agent, "model", None)
    if not isinstance(current_model, str):
        current_model = getattr(current_model, "name", None) if current_model else None
    if current_model == model:
        return base_agent
    cache_key = f"{id(base_agent)}::{model}::{max_output_tokens()}"
    cached = _clone_cache.get(cache_key)
    if cached is not None:
        return cached
    clone = base_agent.clone(model=model)
    tokens = max_output_tokens()
    if tokens:
        from agents import ModelSettings

        current_settings = getattr(clone, "model_settings", None)
        settings = ModelSettings(max_tokens=tokens)
        if current_settings is not None:
            settings = ModelSettings(
                max_tokens=tokens,
                temperature=current_settings.temperature,
                top_p=current_settings.top_p,
            )
        clone.model_settings = settings
    _clone_cache[cache_key] = clone
    return clone
