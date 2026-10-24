"""Provider failures must leave explicit place-search retries usable."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import geocoder  # noqa: E402


VALID_PLACE = {
    "lat": "1.29027",
    "lon": "103.851959",
    "display_name": "Singapore CBD",
    "importance": 0.8,
}


class GeocoderResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        cache = patch.object(geocoder, "_cache", geocoder._TTLCache())
        cache.start()
        self.addCleanup(cache.stop)

    def test_transient_search_failure_can_be_retried_immediately(self) -> None:
        for failed_payload in (None, {"error": "temporarily unavailable"}):
            with self.subTest(payload=failed_payload), patch.object(
                geocoder, "_cache", geocoder._TTLCache(),
            ), patch.object(
                geocoder, "_nominatim_get",
                side_effect=[failed_payload, [VALID_PLACE]],
            ) as provider:
                self.assertEqual(geocoder.geocode("Singapore CBD"), [])
                self.assertEqual(
                    geocoder.geocode("Singapore CBD")[0]["display_name"],
                    "Singapore CBD",
                )
                self.assertEqual(provider.call_count, 2)

    def test_successful_empty_search_is_still_cached(self) -> None:
        with patch.object(geocoder, "_nominatim_get", return_value=[]) as provider:
            self.assertEqual(geocoder.geocode("unmatched place"), [])
            self.assertEqual(geocoder.geocode("unmatched place"), [])
        provider.assert_called_once()

    def test_invalid_provider_rows_do_not_break_valid_search_results(self) -> None:
        invalid_rows = [
            None, "invalid", 123, {},
            {"lat": None, "lon": "103.85"},
            {"lat": "nan", "lon": "103.85"},
            {"lat": "1.29", "lon": "inf"},
            {"lat": "91", "lon": "103.85"},
            {"lat": "1.29", "lon": "181"},
        ]
        for importance in (None, "unavailable", math.nan, math.inf):
            with self.subTest(importance=importance), patch.object(
                geocoder, "_cache", geocoder._TTLCache(),
            ), patch.object(
                geocoder, "_nominatim_get",
                return_value=invalid_rows + [{**VALID_PLACE, "importance": importance}],
            ):
                results = geocoder.geocode("Singapore CBD")
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["display_name"], "Singapore CBD")
                self.assertEqual(results[0]["importance"], 0.0)
                self.assertEqual(results[0]["supported_region"], "SINGAPORE")

    def test_reverse_failure_returns_coordinates_but_does_not_hide_recovery(self) -> None:
        with patch.object(
            geocoder, "_nominatim_get",
            side_effect=[None, {"display_name": "Singapore CBD"}],
        ) as provider:
            first = geocoder.reverse_geocode(1.29027, 103.851959)
            self.assertEqual(first["display_name"], "1.29027, 103.85196")
            recovered = geocoder.reverse_geocode(1.29027, 103.851959)
            self.assertEqual(recovered["display_name"], "Singapore CBD")
            self.assertEqual(geocoder.reverse_geocode(1.29027, 103.851959), recovered)
        self.assertEqual(provider.call_count, 2)


if __name__ == "__main__":
    unittest.main()
