"""Shared congestion units and safe degradation of live provider measurements."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from services import live_traffic, simulator  # noqa: E402
from services.congestion_math import speed_ratio_congestion_score  # noqa: E402


FIXTURE_KEY = "fixture-provider-key-never-log"
PROVIDER_URL = "https://api.tomtom.com/traffic/fixture?key=" + FIXTURE_KEY
NOW = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)


def response_payload(*, current=30.0, free_flow=60.0, confidence=0.9):
    return {"flowSegmentData": {
        "currentSpeed": current,
        "freeFlowSpeed": free_flow,
        "confidence": confidence,
    }}


class SharedCongestionMathTests(unittest.TestCase):
    def test_speed_ratio_has_consistent_physical_units_and_cost_floor(self) -> None:
        for ratio, expected in ((1, 0), (0, 95), (0.5, 50), (1.5, 0),
                                (0.001, 95), (0.05, 95)):
            with self.subTest(ratio=ratio):
                self.assertEqual(speed_ratio_congestion_score(ratio), expected)

    def test_invalid_speed_ratios_are_rejected(self) -> None:
        for ratio in (-0.001, float("nan"), float("inf"), -float("inf"), True, False):
            with self.subTest(ratio=ratio):
                with self.assertRaises(ValueError):
                    speed_ratio_congestion_score(ratio)

    def test_live_adapter_uses_the_same_free_flow_half_speed_and_stopped_units(self) -> None:
        for current, free_flow, expected in (
            (60, 60, 0), (0, 60, 95), (30, 60, 50), (90, 60, 0), (32, 60, 46.7),
        ):
            with self.subTest(current=current, free_flow=free_flow):
                self.assertEqual(live_traffic._congestion_from_speed_ratio(current, free_flow), expected)


class TomTomMeasurementBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each case owns its provider cache and uses fixture responses only.
        cache = patch.dict(live_traffic._tomtom_cache, {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        tls = patch.object(live_traffic.tls, "default_context", return_value=object())
        tls.start()
        self.addCleanup(tls.stop)

    def provider(self, payload):
        encoded = json.dumps(payload).encode("utf-8")
        return patch.object(
            live_traffic.urllib.request, "urlopen",
            side_effect=lambda *args, **kwargs: io.BytesIO(encoded),
        )

    def test_valid_confidence_boundaries_and_stopped_speed_remain_observed(self) -> None:
        for confidence, current in ((0.0, 30.0), (1.0, 0.0)):
            with self.subTest(confidence=confidence, current=current):
                live_traffic._tomtom_cache.clear()
                with self.provider(response_payload(current=current, confidence=confidence)), \
                        patch.object(live_traffic.clock, "now", return_value=NOW):
                    segment = live_traffic._tomtom_segment(1.1, 104.0, FIXTURE_KEY)
                self.assertEqual(segment, {
                    "current_speed_kmh": current,
                    "free_flow_speed_kmh": 60.0,
                    "confidence": confidence,
                    "snapshot_generated_at": live_traffic.clock.iso(NOW),
                })
                # Reusing a cached point must retain its measurement time.
                # A new dashboard request must not make the old sample fresh.
                with patch.object(live_traffic.urllib.request, "urlopen") as network:
                    cached = live_traffic._tomtom_segment(1.1, 104.0, FIXTURE_KEY)
                self.assertEqual(cached["snapshot_generated_at"], live_traffic.clock.iso(NOW))
                network.assert_not_called()

    def test_invalid_provider_confidence_falls_back_to_simulated_corridors(self) -> None:
        fallback = [{"id": corridor, "live_congestion_score": 31.0}
                    for corridor in live_traffic._CORRIDOR_POINTS]
        for confidence in (-0.001, 1.001, float("nan"), float("inf"), True, "0.9"):
            with self.subTest(confidence=confidence):
                live_traffic._tomtom_cache.clear()
                with self.provider(response_payload(confidence=confidence)), patch.dict(
                    os.environ, {"TOMTOM_API_KEY": FIXTURE_KEY},
                ), patch.object(simulator, "get_live_corridor_telemetry", return_value=fallback), patch.object(
                    live_traffic, "get_corridor_map_hotspots", return_value=[],
                ), patch.object(live_traffic.clock, "now", return_value=NOW), redirect_stdout(io.StringIO()):
                    result = live_traffic.get_live_traffic()
                self.assertEqual(result["overall_source"], "simulated")
                self.assertEqual(len(result["segments"]), len(fallback))
                for segment in result["segments"]:
                    self.assertEqual(segment["source"], "simulated")
                    self.assertEqual(segment["congestion_index"], 31.0)
                    self.assertIsNone(segment["current_speed_kmh"])
                    self.assertIsNone(segment["provider_confidence"])

    def test_malformed_provider_speeds_are_rejected_before_score_conversion(self) -> None:
        for current, free_flow in ((-1, 60), (float("nan"), 60), (float("inf"), 60),
                                   (True, 60), (30, 0), (30, float("inf"))):
            with self.subTest(current=current, free_flow=free_flow):
                live_traffic._tomtom_cache.clear()
                with self.provider(response_payload(current=current, free_flow=free_flow)), redirect_stdout(io.StringIO()):
                    self.assertIsNone(live_traffic._tomtom_segment(1.1, 104.0, FIXTURE_KEY))

    def test_direct_provider_exception_never_logs_key_bearing_url_or_error_text(self) -> None:
        output = io.StringIO()
        with patch.object(
            live_traffic.urllib.request, "urlopen", side_effect=URLError(PROVIDER_URL),
        ) as request, redirect_stdout(output):
            self.assertIsNone(live_traffic._tomtom_segment(1.1, 104.0, FIXTURE_KEY))
            # A brief failure cache prevents repeated provider calls/logs.
            self.assertIsNone(live_traffic._tomtom_segment(1.1, 104.0, FIXTURE_KEY))
        request.assert_called_once()
        self.assertIn("URLError", output.getvalue())
        self.assertNotIn(FIXTURE_KEY, output.getvalue())
        self.assertNotIn(PROVIDER_URL, output.getvalue())
        self.assertNotIn("https://", output.getvalue())

    def test_unexpected_worker_exception_also_keeps_provider_text_out_of_logs(self) -> None:
        output = io.StringIO()
        with patch.object(
            live_traffic, "_tomtom_segment", side_effect=RuntimeError(PROVIDER_URL),
        ), redirect_stdout(output):
            result = live_traffic._tomtom_segments(FIXTURE_KEY)
        self.assertEqual(result, {corridor: None for corridor in live_traffic._CORRIDOR_POINTS})
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn(FIXTURE_KEY, output.getvalue())
        self.assertNotIn(PROVIDER_URL, output.getvalue())
        self.assertNotIn("https://", output.getvalue())


if __name__ == "__main__":
    unittest.main()
