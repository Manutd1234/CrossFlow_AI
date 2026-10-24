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

    def test_observed_condition_distinguishes_free_flow_slowing_and_congested_bands(self):
        for speed, score, condition in (
            (50., 0., "FREE_FLOW"), (47.5, 5., "FREE_FLOW"),
            (47.45, 5.1, "SLOWING"), (40.05, 19.9, "SLOWING"),
            (40., 20., "HEAVY"), (30.05, 39.9, "HEAVY"),
            (30., 40., "CRITICAL"), (15., 70., "CRITICAL"),
            # Keep the established bounded congestion-index units: the raw
            # stopped speed is0, while the finite routing index is95.
            (0., 95., "CRITICAL"),
        ):
            with self.subTest(speed=speed):
                self.provider_mock.return_value = {"corridor-1": provider(speed)}
                value = self.current()["observed_traffic"]
                self.assertEqual(value["congestion_index"], score)
                self.assertEqual(value["condition"], condition)
                self.assertEqual(value["condition_policy"], "observed_speed_loss_bands_v2")

    def test_provider_condition_retains_the_validated_speed_and_confidence_for_audit(self):
        self.provider_mock.return_value = {"corridor-1": provider(40., confidence=.87)}
        value = self.current()["observed_traffic"]
        self.assertEqual(value["current_speed_kmh"], 40.)
        self.assertEqual(value["free_flow_speed_kmh"], 50.)
        self.assertEqual(value["provider_confidence"], .87)
        self.assertEqual(value["condition"], "HEAVY")
        self.assertFalse(value["camera_warning"])

    def test_warning_bands_do_not_change_measured_score_or_legacy_model_classification(self):
        self.provider_mock.return_value = {"corridor-1": provider(40.)}
        row = self.current()
        self.assertEqual(row["live_congestion_score"], 20.)
        self.assertEqual(row["status"], "SMOOTH")
        self.assertEqual(row["observed_traffic"]["congestion_index"], 20.)
        self.assertEqual(row["observed_traffic"]["condition"], "HEAVY")

    def test_pressure_and_pressure_outlook_preserve_raw_provider_indices(self):
        for speed, raw, pressure in ((50., 0., 0.), (42.5, 15., 37.5), (40., 20., 50.), (30., 40., 100.), (0., 95., 100.)):
            with self.subTest(raw=raw):
                self.provider_mock.return_value = {"corridor-1": provider(speed)}
                row = self.current()
                value = row["observed_traffic"]
                self.assertEqual(value["pressure_policy"], "congestion_pressure_v1")
                self.assertEqual(value["pressure_condition_policy"], "congestion_pressure_bands_v1")
                self.assertEqual(value["pressure_multiplier"], 2.5)
                self.assertEqual(value["congestion_index"], raw)
                self.assertEqual(value["pressure_index"], pressure)
                self.assertEqual(row["live_congestion_score"], raw)
                self.assertEqual(value["forecast_30m"], raw)
                self.assertEqual(value["pressure_forecast_30m"], pressure)
                self.assertEqual(value["pressure_forecast_60m"], pressure)
        expired = self.current(now=NOW + timedelta(seconds=180))["observed_traffic"]
        self.assertIsNone(expired["pressure_index"])
        self.assertIsNone(expired["pressure_forecast_30m"])
        self.assertEqual(expired["pressure_condition"], "UNAVAILABLE")

    def test_published_pressure_is_amber_from70_and_critical_from80_without_changing_raw_conditions(self):
        for speed, raw, pressure, condition in (
            (44.9, 10.2, 25.5, "SLOWING"),
            (36., 28., 70., "HEAVY"),
            (34., 32., 80., "CRITICAL"),
        ):
            with self.subTest(pressure=pressure):
                self.provider_mock.return_value = {"corridor-1": provider(speed)}
                value = self.current()["observed_traffic"]
                self.assertEqual(value["congestion_index"], raw)
                self.assertEqual(value["pressure_index"], pressure)
                self.assertEqual(value["pressure_condition"], condition)
                self.assertEqual(value["pressure_condition_policy"], "congestion_pressure_bands_v1")
                self.assertEqual(value["condition"], "SLOWING" if raw <20 else "HEAVY")

    def test_low_confidence_provider_cannot_claim_free_flow(self):
        for confidence, available in ((.499, False), (0., False), (.5, True), (1., True)):
            with self.subTest(confidence=confidence):
                self.provider_mock.return_value = {"corridor-1": provider(50., confidence=confidence)}
                value = self.current()["observed_traffic"]
                self.assertEqual(value["condition"], "FREE_FLOW" if available else "UNAVAILABLE")
                self.assertEqual(value["provider_point_count"], int(available))
                if not available:
                    self.assertIsNone(value["congestion_index"])
        self.provider_mock.return_value = {"corridor-1": provider(50., confidence=.49)}
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (camera(score=80.),))
        value = self.current()["observed_traffic"]
        self.assertEqual(value["source"], "cctv_camera")
        self.assertEqual(value["condition"], "CAMERA_ESTIMATE")
        self.assertEqual(value["provider_point_count"], 0)
        self.assertIsNone(value["provider_confidence"])

    def test_missing_provider_and_camera_evidence_is_null_never_clear(self):
        value = self.current()
        current = value["observed_traffic"]
        self.assertEqual(current["status"], "unavailable")
        self.assertEqual(current["source"], "unavailable")
        self.assertEqual(current["condition"], "UNAVAILABLE")
        self.assertEqual(current["condition_policy"], "unavailable")
        for key in ("congestion_index", "delay_mins", "observed_at", "fresh_until", "forecast_30m", "forecast_60m", "camera_fresh_until", "pressure_index", "pressure_forecast_30m", "pressure_forecast_60m"):
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
            provider(confidence=math.nan),
            provider(confidence=True),
            provider(confidence=-.1),
            {"congestion_index": 0.},
        ):
            with self.subTest(item=item):
                self.provider_mock.return_value = {"corridor-1": item}
                value = self.current()["observed_traffic"]
                self.assertIsNone(value["congestion_index"])
                self.assertEqual(value["condition"], "UNAVAILABLE")
                self.assertEqual(value["condition_policy"], "unavailable")
                self.assertIsNone(value["current_speed_kmh"])
                self.assertIsNone(value["free_flow_speed_kmh"])
                self.assertIsNone(value["provider_confidence"])

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
        self.assertEqual(value["condition"], "CAMERA_ESTIMATE")
        self.assertEqual(value["condition_policy"], "mapped_camera_proxy_v1")
        self.assertEqual(value["pressure_index"], 100.)
        self.assertIsNone(value["pressure_forecast_30m"])
        self.assertIsNone(value["pressure_forecast_60m"])
        self.assertEqual(value["camera_condition"], "LOCAL_CONGESTION_ESTIMATE")
        self.assertEqual(value["camera_pressure_index"], 100.)
        self.assertEqual(value["camera_pressure_condition"], "CRITICAL")
        self.assertEqual(value["camera_pressure_condition_policy"], "congestion_pressure_bands_v1")
        self.assertIsNone(value["current_speed_kmh"])
        self.assertIsNone(value["provider_confidence"])
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
        self.assertEqual(value["pressure_index"], 0.)
        self.assertEqual(value["camera_count"], 1)
        self.assertEqual(value["condition"], "FREE_FLOW")
        self.assertEqual(value["camera_condition"], "LOCAL_CONGESTION_ESTIMATE")
        self.assertTrue(value["camera_warning"])
        self.assertEqual(value["fresh_until"], clock.iso(NOW + timedelta(seconds=180)))
        self.assertEqual(value["camera_fresh_until"], clock.iso(NOW + timedelta(seconds=30)))
        self.assertEqual(value["forecast_fresh_until"], clock.iso(NOW + timedelta(seconds=180)))
        refreshed = self.current(now=NOW + timedelta(seconds=31))["observed_traffic"]
        self.assertEqual(refreshed["congestion_index"], 0.)
        self.assertEqual(refreshed["camera_count"], 0)
        self.assertIsNone(refreshed["camera_fresh_until"])
        self.assertIsNone(refreshed["camera_condition"])
        self.assertIsNone(refreshed["camera_pressure_index"])
        self.assertEqual(refreshed["camera_pressure_condition"], "UNAVAILABLE")
        self.assertFalse(refreshed["camera_warning"])

    def test_local_camera_warning_requires_fresh_directed_pressure_distinct_from_provider(self):
        for score, edge, stamp, expected in (
            (40., EDGE, NOW, True),
            (28., EDGE, NOW, True),
            (27.9, EDGE, NOW, False),
            (80., (12, 11, 13), NOW, False),
            (80., EDGE, NOW - timedelta(seconds=31), False),
        ):
            with self.subTest(score=score, edge=edge, stamp=stamp):
                self.provider_mock.return_value = {"corridor-1": provider(40.)}
                self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(
                    NOW.isoformat(), (camera(score=score, edge=edge, stamp=stamp),),
                )
                value = self.current()["observed_traffic"]
                self.assertEqual(value["congestion_index"], 20.)
                self.assertEqual(value["condition"], "HEAVY")
                self.assertEqual(value["camera_warning"], expected)
        self.provider_mock.return_value = {"corridor-1": provider(30.)}
        self.camera_mock.return_value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (camera(score=80.),))
        self.assertFalse(self.current()["observed_traffic"]["camera_warning"])
        self.provider_mock.return_value = {"corridor-1": provider(36.)}
        self.assertFalse(self.current()["observed_traffic"]["camera_warning"])

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
