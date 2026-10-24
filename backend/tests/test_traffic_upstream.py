"""Strict public traffic relay, transient caching and truthful fallback tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from services import live_traffic, traffic_upstream  # noqa: E402


NOW = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def payload():
    return {
        "generated_at": NOW.isoformat(), "overall_source": "tomtom_live",
        "segments": [{
            "corridor_id": corridor_id, "lat": point[0], "lng": point[1],
            "current_speed_kmh": 50.0, "free_flow_speed_kmh": 50.0,
            "provider_confidence": 0.95, "source": "tomtom_live",
            "congestion_index": 5.0,
        } for corridor_id, point in live_traffic._CORRIDOR_POINTS.items()],
        "zones": [{"zone_id": "untrusted-upstream-zone", "congestion_index": 100}],
    }


class FakeResponse(io.BytesIO):
    status = 200


class TrafficUpstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {traffic_upstream.ENV_NAME: traffic_upstream.TRUSTED_URL}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        traffic_upstream._cache = None

    def get(self):
        with patch.object(traffic_upstream, "datetime", FrozenDateTime):
            return traffic_upstream.get_segments(live_traffic._CORRIDOR_POINTS)

    def test_only_exact_reviewed_public_url_can_trigger_a_request(self) -> None:
        for url in (
            "", "http://cross-flow-ai.vercel.app/api/live-traffic",
            "https://other.vercel.app/api/live-traffic",
            "https://cross-flow-ai.vercel.app.evil.example/api/live-traffic",
            "https://cross-flow-ai.vercel.app/api/live-traffic?token=secret",
            "https://cross-flow-ai.vercel.app/api/live-traffic#fragment",
            "https://user@cross-flow-ai.vercel.app/api/live-traffic",
            "https://cross-flow-ai.vercel.app:443/api/live-traffic",
            "https://cross-flow-ai.vercel.app/api/live-traffic/",
        ):
            with self.subTest(url=url), patch.dict(os.environ, {traffic_upstream.ENV_NAME: url}), patch.object(
                traffic_upstream, "_read_public_snapshot", side_effect=AssertionError("must not fetch"),
            ):
                self.assertFalse(traffic_upstream.configured())
                self.assertEqual(self.get(), {})
        for runtime in (
            {"VERCEL": "1"}, {"AWS_LAMBDA_FUNCTION_NAME": "api"},
            {"VERCEL_ENV": "production"}, {"VERCEL_ENV": "preview"},
        ):
            with self.subTest(runtime=runtime), patch.dict(os.environ, runtime), patch.object(
                traffic_upstream, "_read_public_snapshot", side_effect=AssertionError("must not self-relay"),
            ):
                self.assertFalse(traffic_upstream.configured())
                self.assertEqual(self.get(), {})

    def test_valid_wib_snapshot_normalizes_time_and_recomputes_old_score_floor(self) -> None:
        data = payload()
        data["generated_at"] = NOW.astimezone(timezone(timedelta(hours=7))).isoformat()
        data["segments"][1]["current_speed_kmh"] = 0
        with patch.object(traffic_upstream, "_read_public_snapshot", return_value=data):
            result = self.get()
        self.assertEqual(set(result), set(live_traffic._CORRIDOR_POINTS))
        self.assertEqual(result["corridor-1"]["congestion_index"], 0)
        self.assertEqual(result["corridor-2"]["congestion_index"], 95)
        self.assertEqual(result["corridor-1"]["snapshot_generated_at"], NOW.isoformat())
        self.assertEqual(result["corridor-1"]["upstream_source"], traffic_upstream.TRUSTED_URL)
        self.assertNotIn("untrusted-upstream-zone", json.dumps(result))

    def test_stale_future_and_naive_timestamps_are_rejected(self) -> None:
        for timestamp in (
            (NOW - timedelta(seconds=181)).isoformat(),
            (NOW + timedelta(seconds=31)).isoformat(),
            NOW.replace(tzinfo=None).isoformat(), "not-a-time", None,
        ):
            data = payload()
            data["generated_at"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                traffic_upstream._validate(data, live_traffic._CORRIDOR_POINTS, NOW)

    def test_permitted_clock_skew_and_freshness_boundary_remain_bounded(self) -> None:
        for delta, expected_age in ((-180, 180), (30, 0)):
            data = payload()
            data["generated_at"] = (NOW + timedelta(seconds=delta)).isoformat()
            result = traffic_upstream._validate(data, live_traffic._CORRIDOR_POINTS, NOW)
            self.assertEqual(result["segments"]["corridor-1"]["snapshot_age_seconds"], expected_age)

    def test_wrong_duplicate_missing_and_simulated_corridors_reject_whole_snapshot(self) -> None:
        invalid = []
        data = payload(); data["segments"].pop(); invalid.append(data)
        data = payload(); data["segments"][1] = data["segments"][0]; invalid.append(data)
        data = payload(); data["segments"][0]["corridor_id"] = "corridor-99"; invalid.append(data)
        data = payload(); data["segments"][0]["source"] = "simulated"; invalid.append(data)
        data = payload(); data["overall_source"] = "simulated"; invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                traffic_upstream._validate(data, live_traffic._CORRIDOR_POINTS, NOW)

    def test_spoofed_coordinates_and_invalid_measurements_are_rejected(self) -> None:
        for field, value in (
            ("lat", 1.20), ("lng", 103.7), ("lat", float("nan")),
            ("current_speed_kmh", -1), ("current_speed_kmh", True),
            ("current_speed_kmh", "50"), ("free_flow_speed_kmh", 0),
            ("free_flow_speed_kmh", float("inf")),
            ("current_speed_kmh", 250.1), ("free_flow_speed_kmh", 250.1),
            ("current_speed_kmh", 75.1),
            ("provider_confidence", -0.1), ("provider_confidence", 1.1),
            ("provider_confidence", None),
        ):
            data = payload()
            data["segments"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                traffic_upstream._validate(data, live_traffic._CORRIDOR_POINTS, NOW)
        for current, free_flow in ((250, 250), (75, 50), (0, 250)):
            data = payload()
            data["segments"][0].update(current_speed_kmh=current, free_flow_speed_kmh=free_flow)
            result = traffic_upstream._validate(data, live_traffic._CORRIDOR_POINTS, NOW)
            self.assertEqual(result["segments"]["corridor-1"]["current_speed_kmh"], current)

    def test_success_cache_is_copy_isolated_for_120_seconds(self) -> None:
        with patch.object(traffic_upstream, "_read_public_snapshot", return_value=payload()) as read:
            first = self.get()
            first["corridor-1"]["confidence"] = 0
            second = self.get()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(second["corridor-1"]["confidence"], 0.95)
        self.assertGreater(traffic_upstream._cache[1] - time.monotonic(), 119)
        self.assertLessEqual(traffic_upstream._cache[1] - time.monotonic(), 120)

    def test_freshness_is_rechecked_even_inside_success_cache_ttl(self) -> None:
        with patch.object(traffic_upstream, "_read_public_snapshot", return_value=payload()):
            self.get()
        key, expiry, snapshot = traffic_upstream._cache
        snapshot["generated_at"] = (NOW - timedelta(seconds=181)).isoformat()
        traffic_upstream._cache = key, expiry, snapshot
        with patch.object(traffic_upstream, "_read_public_snapshot", return_value=payload()) as read:
            result = self.get()
        read.assert_called_once()
        self.assertEqual(result["corridor-1"]["snapshot_age_seconds"], 0)

    def test_failure_cache_is_short_and_never_logs_exception_message(self) -> None:
        with patch.object(
            traffic_upstream, "_read_public_snapshot", side_effect=RuntimeError("private-token-in-error"),
        ) as read, self.assertLogs(traffic_upstream._LOGGER, level="WARNING") as log:
            self.assertEqual(self.get(), {})
            self.assertEqual(self.get(), {})
        self.assertEqual(read.call_count, 1)
        self.assertNotIn("private-token-in-error", " ".join(log.output))
        self.assertGreater(traffic_upstream._cache[1] - time.monotonic(), 14)
        self.assertLessEqual(traffic_upstream._cache[1] - time.monotonic(), 15)

    def test_transport_is_fixed_get_without_credentials_and_refuses_redirects(self) -> None:
        raw = json.dumps(payload()).encode()
        with patch.object(traffic_upstream.OPENER, "open", return_value=FakeResponse(raw)) as opened:
            self.assertEqual(traffic_upstream._read_public_snapshot()["overall_source"], "tomtom_live")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, traffic_upstream.TRUSTED_URL)
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Apikey", request.headers)
        self.assertLessEqual(opened.call_args.kwargs["timeout"], 3)
        self.assertIsNone(traffic_upstream.NoRedirectHandler().redirect_request(None, None, 302, "", {}, "https://evil"))

    def test_oversized_html_nonfinite_and_non_200_bodies_fail_closed(self) -> None:
        cases = [FakeResponse(b"x" * 101), FakeResponse(b"<html>login</html>"), FakeResponse(b'{"value":NaN}')]
        failed_http = FakeResponse(b"{}")
        failed_http.status = 302
        cases.append(failed_http)
        for response in cases:
            with self.subTest(response=response), patch.object(
                traffic_upstream, "MAX_RESPONSE_BYTES", 100,
            ), patch.object(traffic_upstream.OPENER, "open", return_value=response), self.assertRaises(ValueError):
                traffic_upstream._read_public_snapshot()

    def test_absolute_timeout_interrupts_a_slow_response(self) -> None:
        class SlowResponse(FakeResponse):
            def __init__(self):
                super().__init__(b"{}")
                self.closed_event = threading.Event()

            def read(self, size=-1):
                self.closed_event.wait(1)
                return b""

            def close(self):
                self.closed_event.set()
                super().close()

        response = SlowResponse()
        started = time.monotonic()
        with patch.object(traffic_upstream, "TIMEOUT_SECONDS", 0.1), patch.object(
            traffic_upstream.OPENER, "open", return_value=response,
        ), self.assertRaises(TimeoutError):
            traffic_upstream._read_public_snapshot()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(response.closed_event.is_set())

    def test_upstream_reaches_dashboard_with_local_scores_and_explicit_provenance(self) -> None:
        with patch.object(live_traffic, "_tomtom_api_key", return_value=""), patch.object(
            traffic_upstream, "_read_public_snapshot", return_value=payload(),
        ), patch.object(traffic_upstream, "datetime", FrozenDateTime):
            result = live_traffic.get_live_traffic()
        self.assertEqual(result["overall_source"], "tomtom_live")
        self.assertFalse(result["tomtom_key_configured"])
        self.assertTrue(result["traffic_upstream_configured"])
        self.assertTrue(all(item["congestion_index"] == 0 for item in result["segments"]))
        self.assertTrue(all(item["snapshot_age_seconds"] == 0 for item in result["segments"]))
        self.assertTrue(all(item["provider_transport"] == "public_crossflow_vercel_upstream" for item in result["segments"]))
        self.assertFalse(any(item["zone_id"] == "untrusted-upstream-zone" for item in result["zones"]))
        self.assertTrue(all(item["observed"] is False for item in result["zones"]))

    def test_upstream_failure_stays_simulated_and_does_not_reuse_old_measurements(self) -> None:
        fallback = [{"id": corridor_id, "live_congestion_score": 31.0}
                    for corridor_id in live_traffic._CORRIDOR_POINTS]
        with patch.object(live_traffic, "_tomtom_api_key", return_value=""), patch.object(
            traffic_upstream, "_read_public_snapshot", side_effect=TimeoutError,
        ), patch("services.simulator.get_live_corridor_telemetry", return_value=fallback):
            result = live_traffic.get_live_traffic()
        self.assertEqual(result["overall_source"], "simulated")
        self.assertTrue(all(item["source"] == "simulated" for item in result["segments"]))
        self.assertTrue(all(item["upstream_source"] is None for item in result["segments"]))

    def test_direct_tomtom_key_has_priority_and_never_contacts_upstream(self) -> None:
        segment = {"current_speed_kmh": 50, "free_flow_speed_kmh": 50, "confidence": 0.9}
        with patch.object(live_traffic, "_tomtom_segment", return_value=segment), patch.object(
            traffic_upstream, "get_segments", side_effect=AssertionError("must not use relay"),
        ):
            result = live_traffic._tomtom_segments("direct-key")
        self.assertEqual(len(result), 5)


if __name__ == "__main__":
    unittest.main()
