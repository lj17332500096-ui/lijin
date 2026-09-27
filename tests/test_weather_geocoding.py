from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import tools


class WeatherGeocodingTests(unittest.TestCase):
    def test_chinese_jinzhou_retries_latin_alias_and_picks_jinzhou_city(self) -> None:
        no_results = Mock()
        no_results.raise_for_status.return_value = None
        no_results.json.return_value = {}

        ambiguous_latin_results = Mock()
        ambiguous_latin_results.raise_for_status.return_value = None
        ambiguous_latin_results.json.return_value = {
            "results": [
                {"name": "Jinzhou", "admin1": "辽宁", "admin2": "大连市",
                 "latitude": 39.1, "longitude": 121.71},
                {"name": "锦州市", "admin1": "辽宁", "admin2": "锦州市",
                 "latitude": 41.10778, "longitude": 121.14167},
            ]
        }

        with patch.object(tools.requests, "get",
                          side_effect=[no_results, ambiguous_latin_results]) as get:
            location = tools._geocode_weather_city("锦州")

        self.assertEqual(location["name"], "锦州市")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["name"], "锦州")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["name"], "Jinzhou")

    def test_unknown_city_does_not_get_a_guessed_alias(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {}
        with patch.object(tools.requests, "get", return_value=response) as get:
            self.assertIsNone(tools._geocode_weather_city("不存在的城"))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["params"]["name"], "不存在的城")


if __name__ == "__main__":
    unittest.main()
