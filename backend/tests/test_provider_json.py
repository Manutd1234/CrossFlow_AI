"""Exercise bounded decoding and actual provider fallback/cache boundaries."""
import io
import json
from pathlib import Path
import sys
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import geocoder, google_maps_router, live_traffic, provider_json


class ProviderJsonTests(unittest.TestCase):
    def test_valid_nested_unicode_response_is_preserved(self):
        raw = json.dumps({"name": "Jalan Pangeran Diponegoro", "rows": [0, .5, None]}, ensure_ascii=False).encode()
        self.assertEqual(provider_json.read_json(io.BytesIO(raw), max_bytes=len(raw)), json.loads(raw))

    def test_oversized_response_uses_only_the_bounded_read(self):
        class Oversized(io.BytesIO):
            def read(self, size=-1):
                self_size.append(size)
                return super().read(size)
        self_size = []
        response = Oversized(b" " * 1000)
        with self.assertRaisesRegex(ValueError, "byte budget"):
            provider_json.read_json(response, max_bytes=100)
        self.assertEqual(self_size, [101])
        self.assertEqual(response.tell(), 101)

    def test_duplicate_fields_nonfinite_values_and_malformed_utf8_are_rejected(self):
        for raw in (b'{"speed":1,"speed":2}', b'{"nested":{"x":1,"x":2}}',
                    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\xff"}', b'{"x":'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                provider_json.read_json(io.BytesIO(raw), max_bytes=1024)

    def test_nominatim_failure_is_retryable_and_does_not_log_the_query(self):
        with patch.object(geocoder, "_RATE_LIMIT_S", 0), patch.object(
            geocoder.urllib.request, "urlopen", side_effect=OSError("sensitive request URL"),
        ), patch("builtins.print") as log:
            self.assertIsNone(geocoder._nominatim_get("https://nominatim.openstreetmap.org/search?q=private"))
        self.assertNotIn("sensitive", str(log.call_args_list))
        self.assertNotIn("private", str(log.call_args_list))

    def test_search_cache_cannot_be_mutated_by_a_previous_caller(self):
        source = [{"lat": "1.29027", "lon": "103.851959", "display_name": "Singapore CBD"}]
        with patch.object(geocoder, "_cache", geocoder._TTLCache()), patch.object(
            geocoder, "_nominatim_get", return_value=source,
        ) as request:
            first = geocoder.geocode("Singapore CBD")
            first[0]["display_name"] = "Mutated caller value"
            second = geocoder.geocode("Singapore CBD")
            self.assertEqual(second[0]["display_name"], "Singapore CBD")
            second.clear()
            self.assertEqual(len(geocoder.geocode("Singapore CBD")), 1)
        request.assert_called_once()

    def test_oversized_geocoder_and_ambiguous_flow_fail_into_existing_fallbacks(self):
        with patch.object(geocoder, "_RATE_LIMIT_S", 0), patch.object(
            geocoder.urllib.request, "urlopen", return_value=io.BytesIO(b" " * (geocoder._MAX_RESPONSE_BYTES + 1)),
        ):
            self.assertIsNone(geocoder._nominatim_get("https://nominatim.openstreetmap.org/search?q=Batam"))
        payload = b'{"flowSegmentData":{"currentSpeed":1,"currentSpeed":50,"freeFlowSpeed":50,"confidence":1}}'
        with patch.object(live_traffic, "_tomtom_cache", {}), patch.object(
            live_traffic.urllib.request, "urlopen", return_value=io.BytesIO(payload),
        ):
            self.assertIsNone(live_traffic._tomtom_segment(1.1, 104, "unit-key"))

    def test_flow_speed_quality_matches_the_observed_archive_and_preserves_stopped_traffic(self):
        for current, free_flow, valid in ((0, 50, True), (251, 50, False), (50, 251, False), (76, 50, False)):
            raw = json.dumps({'flowSegmentData': {'currentSpeed': current, 'freeFlowSpeed': free_flow, 'confidence': .9}}).encode()
            with self.subTest(current=current, free_flow=free_flow), patch.object(live_traffic, '_tomtom_cache', {}), \
                    patch.object(live_traffic.urllib.request, 'urlopen', return_value=io.BytesIO(raw)), patch('builtins.print'):
                value = live_traffic._tomtom_segment(1.1, 104, 'speed-quality-test-key')
            if valid:
                self.assertEqual(value['current_speed_kmh'], 0)
            else:
                self.assertIsNone(value)

    def test_concurrent_same_location_checks_share_one_provider_request_including_failures(self):
        raw = b'{"flowSegmentData":{"currentSpeed":30,"freeFlowSpeed":50,"confidence":0.9}}'
        for fail in (False, True):
            barrier = threading.Barrier(5)
            def request(*_args, **_kwargs):
                # Keep the response pending long enough for all independent
                # consumers to reach the same cold cache concurrently.
                time.sleep(0.04)
                if fail:
                    raise OSError("provider unavailable")
                return io.BytesIO(raw)
            def check():
                barrier.wait(timeout=2)
                return live_traffic._tomtom_segment(1.1, 104, "single-flight-test-key")
            with self.subTest(failure=fail), patch.object(live_traffic, "_tomtom_cache", {}), \
                    patch.object(live_traffic, "_tomtom_request_locks", {}), \
                    patch.object(live_traffic.urllib.request, "urlopen", side_effect=request) as fetch, \
                    patch("builtins.print"), ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(lambda _: check(), range(5)))
                self.assertEqual(fetch.call_count, 1)
                if fail:
                    self.assertEqual(results, [None] * 5)
                else:
                    self.assertTrue(all(row['current_speed_kmh'] == 30 for row in results))
                    self.assertEqual(len({row['snapshot_generated_at'] for row in results}), 1)

    def test_optional_google_adapter_sanitizes_provider_error_messages(self):
        response = io.BytesIO(b'{"status":"REQUEST_DENIED","error_message":"sensitive-key"}')
        with patch.object(google_maps_router, "get_api_key", return_value="unit-key"), patch.object(
            google_maps_router.urllib.request, "urlopen", return_value=response,
        ), patch("builtins.print") as log:
            self.assertIsNone(google_maps_router.get_google_route("Public origin", "Public destination"))
        self.assertNotIn("sensitive-key", str(log.call_args_list))
