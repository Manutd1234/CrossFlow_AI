"""Current corridor cards must use source evidence, not planning model output."""
from datetime import datetime, timedelta, timezone
import importlib
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from services import clock, corridor_observations as service, cctv_routing

NOW = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
EDGE = (11, 12, 13)


def provider(score_speed=25., *, stamp=NOW, **overrides):
    return {"current_speed_kmh": score_speed, "free_flow_speed_kmh": 50.,
            "confidence": .95, "snapshot_generated_at": stamp.isoformat(), **overrides}


def camera(*, edge=EDGE, stamp=NOW, score=80., camera_id="reviewed-camera"):
    return cctv_routing.CameraEdgeObservation(camera_id, edge, score, stamp.isoformat(),
                                             (NOW - stamp).total_seconds(), "estimated", "stream_observation")


class CorridorObservationsTests(unittest.TestCase):
    def setUp(self):
        self.provider = patch.object(service.live_traffic, "_tomtom_segments", return_value={})
        self.provider_mock = self.provider.start()
        self.registry = patch.object(service.cctv_store, "load_registry", return_value=[{
            "camera_id": "reviewed-camera", "analysis_config": {"maximum_age_seconds": 30.},
        }])
        self.registry.start()
        self.cameras = patch.object(service.cctv_routing, "snapshot",
                                    return_value=cctv_routing.CameraRoutingSnapshot(NOW.isoformat()))
        self.camera_mock = self.cameras.start()
        self.paths = patch.object(service, "_baseline_path", return_value=(frozenset({EDGE}), 12.34))
        self.path_mock = self.paths.start()
        self.addCleanup(patch.stopall)

    def current(self, **kwargs):
        return service.get_current_corridors(now=kwargs.get("now", NOW))[0]

    def test_provider_values_change_from_actual_speed_and_genuine_zero_is_preserved(self):
        self.provider_mock.return_value = {"corridor-1": provider(50.)}
        clear = self.current()["observed_traffic"]
        self.assertEqual(clear["congestion_index"], 0.)
        self.assertEqual(clear["status"], "available")
        self.assertEqual(clear["source"], "tomtom_live")
        self.assertEqual(clear["scope"], "representative_provider_point")
        self.assertEqual(clear["provider_point_count"], 1)
        self.provider_mock.return_value = {"corridor-1": provider(10.)}
        busy = self.current()["observed_traffic"]
        self.assertGreater(busy["congestion_index"], clear["congestion_index"])
        self.assertFalse(busy["corridor_wide"])
        self.assertIsNone(busy["delay_mins"])

    def test_missing_provider_and_camera_evidence_is_null_never_clear(self):
        value = self.current()
        current = value["observed_traffic"]
        self.assertEqual(current["status"], "unavailable")
        self.assertEqual(current["source"], "unavailable")
        for key in ("congestion_index", "delay_mins", "observed_at", "fresh_until", "forecast_30m", "forecast_60m", "camera_fresh_until"):
            self.assertIsNone(current[key], key)
        self.assertIsNone(value["live_congestion_score"])
        self.assertEqual(value["status"], "UNAVAILABLE")

    def test_old_future_malformed_and_simulated_inputs_do_not_become_live(self):
        for item in (
            provider(stamp=NOW - timedelta(seconds=181)),
            provider(stamp=NOW + timedelta(seconds=1)),
            provider(current_speed_kmh=math.nan),
            provider(current_speed_kmh=True),
            provider(free_flow_speed_kmh=0.),
            provider(source="simulated"),
            {"congestion_index": 0.},
        ):
            with self.subTest(item=item):
                self.provider_mock.return_value = {"corridor-1": item}
                self.assertIsNone(self.current()["observed_traffic"]["congestion_index"])

    def test_source_expiry_is_not_renewed_when_the_card_is_refreshed(self):
        captured = NOW - timedelta(seconds=100)
        self.provider_mock.return_value = {"corridor-1": provider(stamp=captured)}
        first = self.current()["observed_traffic"]
        second = self.current(now=NOW + timedelta(seconds=30))["observed_traffic"]
        self.assertEqual(first["observed_at"], clock.iso(captured))
        self.assertEqual(first["fresh_until"], second["fresh_until"])
        self.assertIsNone(self.current(now=NOW + timedelta(seconds=81))["observed_traffic"]["congestion_index"])

    def test_provider_outlook_is_explicit_persistence_tied_to_the_source_point(self):
        self.provider_mock.return_value = {"corridor-1": provider(20.)}
        value = self.current()["observed_traffic"]
        self.assertEqual(value["forecast_method"], "persistence")
        self.assertEqual(value["forecast_30m"], value["congestion_index"])
        self.assertEqual(value["forecast_60m"], value["congestion_index"])
        self.assertEqual(value["forecast_observed_at"], value["observed_at"])
        self.assertEqual(value["forecast_fresh_until"], value["fresh_until"])

    def test_camera_fallback_requires_exact_directed_path_overlap_and_has_no_speed_outlook(self):
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (camera(),))
        value = self.current()["observed_traffic"]
        self.assertEqual(value["source"], "cctv_camera")
        self.assertEqual(value["scope"], "mapped_camera_road_views")
        self.assertEqual(value["congestion_index"], 80.)
        self.assertEqual(value["camera_count"], 1)
        self.assertEqual(value["provider_point_count"], 0)
        self.assertIsNone(value["delay_mins"])
        self.assertIsNone(value["forecast_30m"])
        self.assertEqual(value["forecast_method"], "unavailable")
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (camera(edge=(12, 11, 13)),))
        self.assertIsNone(self.current()["observed_traffic"]["congestion_index"])

    def test_stale_camera_is_not_mapped_to_an_entire_corridor(self):
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (
            camera(stamp=NOW - timedelta(seconds=31)),
        ))
        value = self.current()["observed_traffic"]
        self.assertEqual(value["camera_count"], 0)
        self.assertIsNone(value["congestion_index"])

    def test_camera_does_not_spill_to_other_corridors_without_directed_edge_overlap(self):
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (camera(),))
        origin = service.router.LANDMARKS["mukakuning"]
        destination = service.router.LANDMARKS["batam_centre"]
        self.path_mock.side_effect = lambda revision, source, target: (
            frozenset({EDGE}) if (source, target) == (origin, destination) else frozenset(), 12.34,
        )
        rows = service.get_current_corridors(now=NOW)
        self.assertEqual(rows[0]["observed_traffic"]["camera_count"], 1)
        for row in rows[1:]:
            self.assertEqual(row["observed_traffic"]["camera_count"], 0)
            self.assertIsNone(row["observed_traffic"]["congestion_index"])

    def test_removed_camera_policy_cannot_leave_a_cached_live_index(self):
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (camera(),))
        self.assertEqual(self.current()["observed_traffic"]["camera_count"], 1)
        with patch.object(service.cctv_store, "load_registry", return_value=[]):
            value = self.current()["observed_traffic"]
        self.assertEqual(value["camera_count"], 0)
        self.assertIsNone(value["congestion_index"])

    def test_camera_and_speed_indices_are_not_blended(self):
        self.provider_mock.return_value = {"corridor-1": provider(50.)}
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (camera(score=80.),))
        value = self.current()["observed_traffic"]
        self.assertEqual(value["source"], "tomtom_live")
        self.assertEqual(value["congestion_index"], 0.)
        self.assertEqual(value["camera_congestion_index"], 80.)
        self.assertEqual(value["camera_count"], 1)
        self.assertEqual(value["fresh_until"], clock.iso(NOW + timedelta(seconds=180)))
        self.assertEqual(value["camera_fresh_until"], clock.iso(NOW + timedelta(seconds=30)))
        self.assertEqual(value["forecast_fresh_until"], clock.iso(NOW + timedelta(seconds=180)))
        refreshed = self.current(now=NOW + timedelta(seconds=31))["observed_traffic"]
        self.assertEqual(refreshed["congestion_index"], 0.)
        self.assertEqual(refreshed["camera_count"], 0)
        self.assertIsNone(refreshed["camera_fresh_until"])

    def test_api_read_never_uses_synthetic_model_or_writes_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "CROSSFLOW_HISTORY_DB": str(Path(directory) / "history.db"),
            "CROSSFLOW_ROUTE_DB": str(Path(directory) / "routes.db"),
            "CROSSFLOW_ROUTE_LEARNING_DB": str(Path(directory) / "learning.db"),
        }):
            api = importlib.import_module("main")
            rows = service.get_current_corridors(now=NOW)
            with patch.object(api, "get_current_corridors", return_value=rows), \
                    patch.object(api, "get_live_corridor_telemetry", side_effect=AssertionError("legacy model called")), \
                    patch.object(api.historical_store, "record", side_effect=AssertionError("read mutated history")), \
                    clock.frozen(NOW):
                response = api.get_corridors()
        self.assertEqual(response["data_source"], "unavailable")
        self.assertEqual(response["corridors"][0]["observed_traffic"]["status"], "unavailable")
        self.assertNotIn("Historical & Telemetry Model", response["provenance"]["traffic"])


if __name__ == "__main__":
    unittest.main()
