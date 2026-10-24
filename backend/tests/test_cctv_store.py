"""Engineering tests for camera store gates; fixtures are not traffic accuracy evidence."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services.cctv_analysis import CameraConfig, CCTVAnalyzer
from services import cctv_store as store

START = datetime(2026, 10, 24, tzinfo=timezone.utc)
NOW = START + timedelta(seconds=4)
ROI = ((0., 0.), (1., 0.), (1., 1.), (0., 1.))


def configured(camera_id="camera-1", **changes):
    base = CameraConfig(camera_id=camera_id, corridor_id="corridor-1", road_segment_id="road-1",
                        enabled=True, analysis_authorized=True, authorization_reference="engineering-authorization",
                        roi=ROI, thresholds_validated=True, threshold_validation_reference="engineering-thresholds",
                        independent_validation_reference="engineering-independent-validation", validated_detector_id="fixture-model",
                        minimum_samples=2, minimum_window_seconds=1., stationary_seconds=1.,
                        congestion_persistence_seconds=1., window_seconds=10.)
    return replace(base, **changes)


def row(config):
    return {"camera_id": config.camera_id, "name": "Engineering camera", "road_name": "Fixture road",
            "source_url": "https://example.org/public-camera-page", "stream_url": "https://example.org/private-feed.m3u8?token=secret",
            "analysis_config": asdict(config), "collector_config": {"fixed_view_confirmed": True}}


def evidence(config=None, offset=0, boxes=None):
    config = config or configured()
    analyzer = CCTVAnalyzer(config)
    for second in range(5):
        at = START + timedelta(seconds=second+offset)
        result = analyzer.ingest({"captured_at": at.isoformat(), "detections": [
            {"box": list(box), "class_name": "car", "score": .99}
            for box in (boxes if boxes is not None else [(.1, .1, .9, .9)])],
            "frame_width": 640, "frame_height": 480, "detector_id": "fixture-model",
            "camera_stable": True, "exposure_ok": True}, now=at)
    return result


class CCTVStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = self.root / "registry.json"
        self.database = self.root / "observations.sqlite"
        self.environment = patch.dict(os.environ, {"CROSSFLOW_CCTV_REGISTRY": str(self.registry), "CROSSFLOW_CCTV_DB": str(self.database)})
        self.environment.start()
        self.write_registry([row(configured())])

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def write_registry(self, rows):
        self.registry.write_text(json.dumps({"schema_version": 1, "cameras": rows}, allow_nan=False))

    def test_bundled_registry_has_six_reviewed_public_pilots_and_22_pending_without_creating_db(self):
        with patch.dict(os.environ, {"CROSSFLOW_CCTV_REGISTRY": str(store.DATA_DIR / "cctv_cameras.json")}):
            status = store.camera_status(now=NOW)
            self.assertEqual(status["status"], "awaiting_feeds")
            self.assertEqual(status["summary"]["listed"], 28)
            self.assertEqual(status["summary"]["permission_pending"], 22)
            self.assertEqual(status["summary"]["configured"], 6)
            self.assertEqual(status["summary"]["live"], 0)
            self.assertEqual(status["summary"]["estimated"], 0)
            pilot = next(camera for camera in status["cameras"] if camera["camera_id"] == "15")
            self.assertEqual(pilot["status"], "calibration_pending")
            self.assertFalse(pilot["operational_eligible"])
            configured_ids = {camera["camera_id"] for camera in status["cameras"] if camera["status"] == "calibration_pending"}
            self.assertEqual(configured_ids, {"1", "13", "15", "19", "48", "51"})
            self.assertFalse(self.database.exists())
            for camera in status["cameras"]:
                for field in ("vehicle_count", "moving_vehicles", "stopped_vehicles", "occupancy_ratio", "congestion_score", "queue_length_m"):
                    self.assertIsNone(camera[field])
            serialized = json.dumps(status)
            self.assertNotIn(".m3u8", serialized)
            self.assertNotIn("authorization_reference", serialized)

    def test_status_and_scores_are_readonly_before_first_observation(self):
        self.assertEqual(store.camera_status(now=NOW)["status"], "awaiting_feeds")
        self.assertEqual(store.corridor_scores(now=NOW), {})
        self.assertFalse(self.database.exists())

    def test_validated_engine_record_becomes_live_and_camera_score_does_not_assert_speed(self):
        payload = evidence()
        self.assertTrue(payload["operational_eligible"])
        result = store.ingest_evidence(payload, now=NOW)
        self.assertFalse(result["idempotent"])
        status = store.camera_status(now=NOW)
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["summary"]["live"], 1)
        camera = status["cameras"][0]
        self.assertEqual(camera["vehicle_count"], 1)
        self.assertEqual(camera["stopped_vehicles"], 1)
        self.assertEqual(camera["status"], "live")
        self.assertIsNone(camera["queue_length_m"])
        self.assertIsNone(camera["motion_score"])
        scores = store.corridor_scores(now=NOW)
        self.assertEqual(scores["corridor-1"]["source"], "cctv_camera")
        self.assertEqual(scores["corridor-1"]["congestion_index"], payload["congestion_score"])
        self.assertNotIn("speed_kmh", scores["corridor-1"])
        self.assertNotIn("private-feed", json.dumps(status))
        self.assertNotIn("secret", json.dumps(status))
        self.assertNotIn("engineering-independent-validation", json.dumps(status))

    def test_freshness_is_recomputed_without_mutating_storage(self):
        store.ingest_evidence(evidence(), now=NOW)
        digest_before = self.database.read_bytes()
        status = store.camera_status(now=NOW + timedelta(seconds=16))
        self.assertEqual(status["status"], "degraded")
        self.assertEqual(status["cameras"][0]["status"], "stale")
        self.assertIsNone(status["cameras"][0]["congestion_score"])
        self.assertFalse(status["cameras"][0]["operational_eligible"])
        self.assertEqual(store.corridor_scores(now=NOW + timedelta(seconds=16)), {})
        self.assertEqual(digest_before, self.database.read_bytes())

    def test_registry_revocation_disables_previous_fresh_eligible_evidence_immediately(self):
        store.ingest_evidence(evidence(), now=NOW)
        for change in ({"analysis_authorized": False}, {"authorization_reference": None}, {"enabled": False},
                       {"thresholds_validated": False}, {"independent_validation_reference": None},
                       {"validated_detector_id": "different-model"}, {"corridor_id": "different-corridor"}):
            with self.subTest(change=change):
                self.write_registry([row(configured(**change))])
                status = store.camera_status(now=NOW)
                self.assertNotEqual(status["cameras"][0]["status"], "live")
                self.assertFalse(status["cameras"][0]["operational_eligible"])
                self.assertIsNone(status["cameras"][0]["congestion_score"])
                self.assertEqual(store.corridor_scores(now=NOW), {})

    def test_calibration_pending_allows_only_actual_authorized_counts(self):
        config = configured(thresholds_validated=False, independent_validation_reference=None, validated_detector_id=None)
        self.write_registry([row(config)])
        payload = evidence(config)
        self.assertFalse(payload["operational_eligible"])
        store.ingest_evidence(payload, now=NOW)
        camera = store.camera_status(now=NOW)["cameras"][0]
        self.assertEqual(camera["status"], "calibration_pending")
        self.assertEqual(camera["vehicle_count"], 1)
        self.assertIsNone(camera["congestion_score"])
        self.assertEqual(store.corridor_scores(now=NOW), {})

    def test_exact_replay_is_idempotent_but_immutable_conflict_and_out_of_order_are_rejected(self):
        payload = evidence()
        store.ingest_evidence(payload, now=NOW)
        replay = store.ingest_evidence(payload, now=NOW + timedelta(seconds=1))
        self.assertTrue(replay["idempotent"])
        changed = deepcopy(payload)
        changed["measurement_scope"] = "Different claimed scope"
        with self.assertRaises(store.CCTVConflictError):
            store.ingest_evidence(changed, now=NOW)
        with self.assertRaises(store.CCTVConflictError):
            store.ingest_evidence(evidence(offset=-1), now=NOW)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cctv_observations").fetchone()[0], 1)

    def test_aged_replay_and_future_evidence_cannot_refresh_a_camera(self):
        payload = evidence()
        store.ingest_evidence(payload, now=NOW)
        for instant in (NOW + timedelta(seconds=16), NOW - timedelta(seconds=3)):
            with self.subTest(instant=instant), self.assertRaises(ValueError):
                store.ingest_evidence(payload, now=instant)
        self.assertEqual(store.corridor_scores(now=NOW + timedelta(seconds=16)), {})

    def test_unknown_camera_and_disabled_camera_reject_before_db_creation(self):
        unknown = evidence()
        unknown["camera_id"] = "not-registered"
        with self.assertRaisesRegex(ValueError, "Unknown"):
            store.ingest_evidence(unknown, now=NOW)
        config = configured(analysis_authorized=False)
        self.write_registry([row(config)])
        with self.assertRaises(ValueError):
            store.ingest_evidence(evidence(config), now=NOW)
        self.assertFalse(self.database.exists())

    def test_spoofed_scores_eligibility_physical_values_images_and_unknown_fields_are_rejected(self):
        for changes in ({"congestion_score": 42.}, {"operational_eligible": False}, {"camera_congestion_score_proxy": 99.},
                        {"speed_kmh": 20.}, {"travel_time_seconds": 30.}, {"queue_length_m": 20.},
                        {"images_retained": True}, {"frames": ["raw image"]}, {"camera_id": "unknown"}):
            payload = evidence(); payload.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                store.ingest_evidence(payload, now=NOW)
        self.assertFalse(self.database.exists())

    def test_nonfinite_invalid_or_boolean_counts_cannot_become_empty_or_live_roads(self):
        for key, value in (("vehicle_count", float("nan")), ("vehicle_count", True), ("vehicle_count", -1),
                           ("image_occupancy_ratio", float("inf")), ("congestion_score", float("nan")),
                           ("window_seconds", float("nan")), ("moving_vehicle_count", 100),
                           ("vehicle_count", None)):
            payload = evidence(); payload[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                store.ingest_evidence(payload, now=NOW)
        self.assertFalse(self.database.exists())

    def test_unusable_or_warming_evidence_does_not_publish_false_zero_counts(self):
        config = configured(thresholds_validated=False, independent_validation_reference=None, validated_detector_id=None)
        self.write_registry([row(config)])
        analyzer = CCTVAnalyzer(config)
        frame = {"captured_at": NOW.isoformat(), "detections": [], "frame_width": 640, "frame_height": 480,
                 "detector_id": "fixture-model", "camera_stable": True, "exposure_ok": False}
        payload = analyzer.ingest(frame, now=NOW)
        self.assertFalse(payload["quality"]["usable"])
        store.ingest_evidence(payload, now=NOW)
        camera = store.camera_status(now=NOW)["cameras"][0]
        self.assertIsNone(camera["vehicle_count"])
        self.assertIsNone(camera["congestion_score"])
        self.assertIsNone(camera["motion_score"])
        self.assertFalse(camera["operational_eligible"])

    def test_boolean_zero_score_is_rejected_even_when_numeric_zero_is_valid(self):
        payload = evidence(boxes=[])
        self.assertEqual(payload["congestion_score"], 0.)
        payload["congestion_score"] = False
        with self.assertRaises(ValueError):
            store.ingest_evidence(payload, now=NOW)
        self.assertFalse(self.database.exists())

    def test_bad_registry_degrades_without_database_or_invented_counts(self):
        for content in ('{"schema_version":1,"cameras":NaN}', '{"schema_version":1,"schema_version":1,"cameras":[]}', '{broken'):
            self.registry.write_text(content)
            status = store.camera_status(now=NOW)
            self.assertEqual(status["status"], "unavailable")
            self.assertEqual(status["cameras"], [])
            self.assertEqual(store.corridor_scores(now=NOW), {})
            self.assertFalse(self.database.exists())

    def test_registry_identity_bounds_duplicate_and_public_url_secrecy_are_checked(self):
        base = row(configured())
        bad_rows = [[base, base], [{**base, "camera_id": "different"}],
                    [{**base, "source_url": "https://example.org/page?secret=key"}],
                    [{**base, "source_url": "https://example.org/feed.m3u8"}],
                    [{**base, "name": "bad\nlabel"}], [{**base, "frames": []}],
                    [{**base, "collector_config": {"frame_interval_seconds": -1.}}],
                    [{**base, "collector_config": {"fixed_view_confirmed": "yes"}}],
                    [{**base, "collector_config": {"background_max_shift_px": 4.}}]]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                self.write_registry(rows)
                self.assertEqual(store.camera_status(now=NOW)["status"], "unavailable")
        self.registry.write_bytes(b" " * (store.MAX_REGISTRY_BYTES + 1))
        with self.assertRaises(store.CCTVStoreUnavailable):
            store.load_registry()

    def test_private_local_accessor_and_collector_defaults_do_not_change_engine_fingerprint(self):
        registered = store.get_registered_camera("camera-1")
        self.assertIn("secret", registered["stream_url"])
        self.assertIsNone(registered["collector_config"]["source_clock_validation_reference"])
        self.assertEqual(registered["collector_config"]["background_max_shift_px"], 2.)
        self.assertEqual(CameraConfig.from_dict(registered["analysis_config"]).fingerprint, configured().fingerprint)

    def test_crop_policy_change_invalidates_fresh_old_receipts_and_cannot_inherit_validation(self):
        store.ingest_evidence(evidence(), now=NOW)
        registered = row(configured())
        registered["collector_config"].update(inference_crop=[0., 0., 1., 1.], decode_max_dimension=1920)
        self.write_registry([registered])
        self.assertIsNone(store.camera_status(now=NOW)["cameras"][0]["congestion_score"])
        self.assertEqual(store.corridor_scores(now=NOW), {})
        with self.assertRaisesRegex(ValueError, "image transform policy"):
            store.ingest_evidence(evidence(offset=1), now=NOW + timedelta(seconds=1))

        # An otherwise usable transformed observation must not inherit the
        # independent validation registered for the previous full-frame model.
        payload = evidence(offset=1)
        payload.update(detector_id=store.collector_detector_id("fixture-model", registered["collector_config"]),
                       operational_eligible=False, congestion_score=None)
        store.ingest_evidence(payload, now=NOW + timedelta(seconds=1))
        status = store.camera_status(now=NOW + timedelta(seconds=1))["cameras"][0]
        self.assertFalse(status["operational_eligible"])
        self.assertIsNone(status["congestion_score"])
        self.assertEqual(status["vehicle_count"], 1)

        # Removing a crop also invalidates a receipt bound to the old policy.
        self.write_registry([row(configured())])
        self.assertIsNone(store.camera_status(now=NOW + timedelta(seconds=1))["cameras"][0]["congestion_score"])
        with self.assertRaisesRegex(ValueError, "image transform policy"):
            store.ingest_evidence(payload, now=NOW + timedelta(seconds=1))

    def test_corrupt_or_incomplete_db_is_unavailable_and_never_returned_as_zero_traffic(self):
        self.database.write_bytes(b"not a sqlite database")
        status = store.camera_status(now=NOW)
        self.assertEqual(status["status"], "unavailable")
        self.assertIsNone(status["cameras"][0]["vehicle_count"])
        self.assertEqual(store.corridor_scores(now=NOW), {})
        with self.assertRaises(store.CCTVStoreUnavailable):
            store.ingest_evidence(evidence(), now=NOW)

    def test_corrupt_stored_evidence_does_not_bypass_revalidation(self):
        store.ingest_evidence(evidence(), now=NOW)
        with sqlite3.connect(self.database) as db:
            body = json.loads(db.execute("SELECT evidence_json FROM cctv_observations").fetchone()[0])
            body["congestion_score"] = 1.
            db.execute("UPDATE cctv_observations SET evidence_json=?", (json.dumps(body),))
        self.assertEqual(store.camera_status(now=NOW)["cameras"][0]["status"], "unavailable")
        self.assertEqual(store.corridor_scores(now=NOW), {})

    def test_stored_json_array_degrades_safely_instead_of_crashing_status(self):
        store.ingest_evidence(evidence(), now=NOW)
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE cctv_observations SET evidence_json='[]'")
        status = store.camera_status(now=NOW)
        self.assertEqual(status["status"], "unavailable")
        self.assertIsNone(status["cameras"][0]["vehicle_count"])
        self.assertEqual(store.corridor_scores(now=NOW), {})

    def test_history_bound_preserves_latest_per_camera_and_watermark(self):
        with patch.object(store, "MAX_RECORDS", 3):
            for offset in range(7):
                at = NOW + timedelta(seconds=offset)
                store.ingest_evidence(evidence(offset=offset), now=at)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cctv_observations").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cctv_watermarks").fetchone()[0], 1)
        self.assertTrue(store.camera_status(now=NOW + timedelta(seconds=6))["cameras"][0]["operational_eligible"])
        with self.assertRaises(store.CCTVConflictError):
            store.ingest_evidence(evidence(offset=1), now=NOW + timedelta(seconds=6))

    def test_multiple_fresh_cameras_use_worst_registered_region_with_oldest_contributing_age(self):
        cfg1, cfg2 = configured(), configured("camera-2")
        self.write_registry([row(cfg1), row(cfg2)])
        store.ingest_evidence(evidence(cfg1), now=NOW)
        store.ingest_evidence(evidence(cfg2, offset=1, boxes=[]), now=NOW + timedelta(seconds=1))
        combined = store.corridor_scores(now=NOW + timedelta(seconds=2))["corridor-1"]
        self.assertEqual(combined["camera_ids"], ["camera-1", "camera-2"])
        self.assertEqual(combined["congestion_index"], 100.)
        self.assertEqual(combined["age_seconds"], 2.)
        self.assertEqual(combined["observed_at"], NOW.isoformat().replace("+00:00", "Z"))


if __name__ == "__main__":
    unittest.main()
