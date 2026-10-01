from __future__ import annotations

import unittest
from unittest import mock

from runtime.pricing import estimate_cost


class PricingTests(unittest.TestCase):
    def test_unconfigured_gateway_price_is_unknown(self) -> None:
        with mock.patch.dict("os.environ", {"FORGE_MODEL_PRICING": "", "FORGE_MODEL_PREF": "gateway"}, clear=False):
            self.assertEqual(estimate_cost("agnes-3.0-flash", 1000, 1000), (None, None))

    def test_unconfigured_local_model_is_not_assumed_free(self) -> None:
        with mock.patch.dict("os.environ", {"FORGE_MODEL_PRICING": "", "FORGE_MODEL_PREF": "local"}, clear=False):
            self.assertEqual(estimate_cost("llama-local", 1000, 1000), (None, None))

    def test_configured_versioned_price_is_estimate(self) -> None:
        env = {"FORGE_MODEL_PRICING": '{"model-x":{"input_per_million_usd":2,"output_per_million_usd":4,"version":"rate-2026-10"}}'}
        with mock.patch.dict("os.environ", env, clear=False):
            cost, version = estimate_cost("model-x", 1_000_000, 500_000)
        self.assertEqual(cost, 4.0)
        self.assertEqual(version, "rate-2026-10")

    def test_missing_rate_version_or_nonfinite_rate_stays_unknown(self) -> None:
        with mock.patch.dict("os.environ", {"FORGE_MODEL_PRICING": '{"model-x":{"input_per_million_usd":1,"output_per_million_usd":1}}'}, clear=False):
            self.assertEqual(estimate_cost("model-x", 100, 100), (None, None))
        with mock.patch.dict("os.environ", {"FORGE_MODEL_PRICING": '{"model-x":{"input_per_million_usd":NaN,"output_per_million_usd":1,"version":"bad"}}'}, clear=False):
            self.assertEqual(estimate_cost("model-x", 100, 100), (None, None))


if __name__ == "__main__":
    unittest.main()
