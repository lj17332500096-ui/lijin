"""Versioned default token-price estimates with operator overrides.

Set FORGE_MODEL_PRICING to JSON, e.g.
{"model-id":{"input_per_million_usd":2.5,"output_per_million_usd":10}}
Default rates are list-price snapshots for the Agnes gateway models used by this
project. They are estimates (promotions/account terms may differ); overrides win.
"""
from __future__ import annotations

import json
import os

_DEFAULT_RATES = {
    # Snapshot 2026-09-27. USD / million tokens; see docs acceptance notes.
    "agnes-3.0-flash": (0.05, 0.15),
    "agnes-2.5-flash": (0.05, 0.15),
    "agnes-2.5-pro": (0.45, 0.90),
    "agnes-2.5-pro-beta": (0.10, 0.30),
}


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> tuple[float | None, str | None]:
    raw = os.getenv("FORGE_MODEL_PRICING", "").strip()
    if not model:
        return None, None
    try:
        table = json.loads(raw) if raw else {}
        rate = table.get(model) if isinstance(table, dict) else None
        if isinstance(rate, dict):
            input_rate = float(rate["input_per_million_usd"])
            output_rate = float(rate["output_per_million_usd"])
            version = str(rate.get("version") or "operator-config")
        elif os.getenv("FORGE_MODEL_PREF", "gateway").strip().lower() == "local":
            return 0.0, "local-no-token-charge"
        elif (model.lower() in _DEFAULT_RATES
              and any(host in os.getenv("OPENAI_BASE_URL", "").lower()
                      for host in ("agnes-ai.cn", "agnes-ai.com"))):
            input_rate, output_rate = _DEFAULT_RATES[model.lower()]
            version = "agnes-list-price-2026-09-27"
        else:
            return None, None
        if input_rate < 0 or output_rate < 0:
            return None, None
        cost = (max(0, input_tokens) * input_rate + max(0, output_tokens) * output_rate) / 1_000_000
        return cost, version
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None, None
