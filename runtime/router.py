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


# ---------- P1-B(3)：reasoning_effort 场景路由 ----------
# 该网关（apihub.agnes-ai.cn）对 reasoning_effort 的语义与 OpenAI 标准错位
# （n=5 去噪：minimal 最浅 / low 最深 / medium / high 次之），coding 想深推理
# 须选 low 而非 high。默认 off，显式 FORGE_REASONING_EFFORT=on 才注入。
#
# ## ⚠️ 本段违反 AGENTS.md 分工宪法，且**不要顺手打开**
#
# 下面是**关键词表做语义判断**（"出现 `bug`/`python` 就当编码任务"），正是
# 分工宪法明令禁止的形态：语义识别/匹配必须交给 LLM，确定性规则只许用于
# 结构与格式校验。
#
# 它默认 off、且 `.env`/`.env.example` 都没有配这个键，生产不受影响。
# **实测的误命中率很高**（2026-10-05）：
#     "介绍一下 Python 的历史"    -> low（应为medium，概念提问）
#     "这个 bug 是什么意思"         -> low（应为 medium，只是问概念）
#     "Debian bug 是什么"          -> low（应为 medium，与编码无关）
# 关键词表无法区分「要写代码」与「问一个含该词的概念问题」。
#
# **正确做法**：真需要场景感知档位时，让 LLM 在规划时输出意图标签
# （与 `docs/architecture/P1_7_TASK_PLAN_BOUNDARY_20261004.md` 里
# task_plan 降级为 hints 的思路一致 —— 降级为提示，而不是在决策层重建规则）。
# 在那之前，保持关闭。
_CODING_SCENE_MARKERS = (
    "def ", "class ", "import ", "#include", "function ", "async ",
    "bug", "修复", "实现", "代码", "代码块", "单元测试", "集成测试",
    "报错", "异常", "调试", "refactor", "重构成", "脚本",
    "test_", "_test", "pytest", "run_tests", "写一个", "实现一个",
    "python", "python3", "javascript", "typescript", "rust", "golang",
)


def reasoning_effort_for_scene(message: str, default: str = "medium") -> str:
    """按任务场景选 reasoning_effort 档位。

    coding 场景（代码/修 bug/测试/实现）→ low（该网关错位语义下=最深推理）；
    其余场景 → default（medium）。纯函数，无副作用，可单测。
    仅当 FORGE_REASONING_EFFORT=on 时被调用（由调用方控制）。
    """
    text = (message or "").lower()
    for marker in _CODING_SCENE_MARKERS:
        if marker in text:
            return "low"
    return default


def reasoning_effort_enabled() -> bool:
    """FORGE_REASONING_EFFORT 开关（默认 off，不影响生产）。"""
    return _env("FORGE_REASONING_EFFORT", "").lower() in ("on", "1", "true")


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
