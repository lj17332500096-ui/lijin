"""Versioned default token-price estimates with operator overrides.

Set FORGE_MODEL_PRICING to JSON, e.g.
{"model-id":{"input_per_million_usd":2.5,"output_per_million_usd":10}}
Rates are operator supplied estimates. They are not provider invoice data; if a
model rate is unknown, the cost remains unknown instead of being guessed.
"""
from __future__ import annotations

import json
import math
import os

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
            version = str(rate.get("version") or "").strip()
            if not version:
                return None, None
        else:
            return None, None
        if (not math.isfinite(input_rate) or not math.isfinite(output_rate)
                or input_rate < 0 or output_rate < 0):
            return None, None
        cost = (max(0, input_tokens) * input_rate + max(0, output_tokens) * output_rate) / 1_000_000
        return cost, version
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None, None
