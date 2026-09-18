"""共享密钥模式表（guardrails / project_edit / codex_loop 三处使用，单一来源）。

所有匹配均大小写不敏感。新增厂商密钥只需在此追加一行。
"""
from __future__ import annotations

import re

# 按厂商排列；长前缀优先放前面防止短前缀正则误吞。
# 每个 pattern 均 ≥16 字符有效载荷，降低误报率。

_PATTERNS_RAW: list[str] = [
    # Anthropic
    r"sk-ant-[A-Za-z0-9_-]{16,}",
    # OpenAI 兼容网关（含自定义 gateway）
    r"sk-[A-Za-z0-9]{16,}",
    # Google
    r"AIza[A-Za-z0-9_-]{20,}",
    # Tavily
    r"tvly-[A-Za-z0-9]{16,}",
    # 项目内部 cpk
    r"cpk-[A-Za-z0-9_-]{16,}",
    # GitHub PAT / OAuth / App token
    r"ghp_[A-Za-z0-9]{20,}",
    r"gho_[A-Za-z0-9]{20,}",
    r"ghs_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    # Slack
    r"xox[baprs]-[A-Za-z0-9-]{16,}",
    # AWS Access Key ID（不是 Secret，仅作风险检测）
    r"AKIA[A-Z0-9]{12,}",
    # Stripe
    r"sk_live_[A-Za-z0-9]{16,}",
    r"rk_live_[A-Za-z0-9]{16,}",
    r"pk_live_[A-Za-z0-9]{16,}",
    # 通用 JWT（非 Base64 格式校验，仅作风险检测）
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
]

# 导出 compiled 版本；三处使用方直接 import KEY_PATTERNS
KEY_PATTERNS: list = [re.compile(p, re.I) for p in _PATTERNS_RAW]


def mask_keys(text: str) -> str:
    """把命中的密钥替换为 ***，保留其余文本。"""
    for pat in KEY_PATTERNS:
        text = pat.sub("***", text)
    return text


def has_key(text: str) -> bool:
    return any(pat.search(text) for pat in KEY_PATTERNS)
