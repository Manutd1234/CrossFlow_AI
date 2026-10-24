"""Provisional live-CCTV boundary fixtures; not measured model accuracy or live media."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cctv_capture as capture
from services.cctv_analysis import CameraConfig, CCTVAnalyzer, validate_evidence
from services import cctv_store as store
from services import cctv_runtime

START = datetime(2026, 10, 24, tzinfo=timezone.utc)
NOW = START + timedelta(seconds=4)
ROI = ((0., 0.), (1., 0.), (1., 1.), (0., 1.))
SOURCE = "https://camera.example/public/stream.m3u8"


def config(*, validated=False):
    return CameraConfig(camera_id="pilot-fixture", corridor_id="fixture-corridor",
                        road_segment_id="osm-edge:1:2:619", enabled=True, analysis_authorized=True,
                        authorization_reference="Explicit engineering fixture pilot; no source agency license claim",
                        roi=ROI, minimum_samples=2, minimum_window_seconds=1., stationary_seconds=1.,
                        congestion_persistence_seconds=1., window_seconds=10., thresholds_validated=validated,
                        threshold_validation_reference="fixture-thresholds" if validated else None,
                        independent_validation_reference="fixture-independent-validation" if validated else None,
                        validated_detector_id="yolo26-fixture" if validated else None)


def registered(cfg=None, *, allow_routing=True):
    cfg = cfg or config()
    return {"camera_id": cfg.camera_id, "name": "Engineering public road pilot", "road_name": "Fixture road view",
            "source_url": "https://camera.example/public-camera", "stream_url": SOURCE,
            "analysis_config": asdict(cfg),
            "collector_config": {"timestamp_mode": "stream_observation", "detector_backend": "yolo26",
                                 "frame_interval_seconds": 2., "fixed_view_confirmed": True,
                                 "background_region": ROI, "source_clock_validation_reference": None},
            "estimation_config": {"enabled": True, "allow_routing": allow_routing,
                                  "graph_revision": "fixture-graph-revision", "reference": "Explicit provisional engineering policy"}}


def evidence(cfg=None, *, basis="stream_observation", offset=0, boxes=None):
    analyzer = CCTVAnalyzer(cfg or config())
    for second in range(5):
        at = START + timedelta(seconds=second+offset)
        result = analyzer.ingest({"captured_at": at.isoformat(), "detections": [
            {"box": list(b), "class_name": "car", "score": .99}
            for b in (boxes if boxes is not None else [(.1, .1, .9, .9)])],
            "frame_width": 640, "frame_height": 480, "detector_id": "yolo26-fixture",
            "camera_stable": True, "exposure_ok": True, "timestamp_basis": basis}, now=at)
    return result


def playlist(sequence, segment="latest.ts", *, duration=8):
    return (f"#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n"
            f"#EXTINF:{duration},\n{segment}\n").encode()


class ObservedPlaylistClockTests(unittest.TestCase):
    def test_initial_snapshot_waits_for_an_advancement_instead_of_redating_existing_footage(self):
        clock = capture.ObservedPlaylistClock(15.)
        with self.assertRaises(capture.CapturePending):
            capture.parse_live_playlist(playlist(100), SOURCE, observation_clock=clock, observed_at=START)
        with self.assertRaises(capture.CapturePending):
            capture.parse_live_playlist(playlist(100), SOURCE, observation_clock=clock,
                                        observed_at=START + timedelta(seconds=2))

    def test_first_advance_is_explicit_receiver_observation_not_source_capture_clock(self):
        clock = capture.ObservedPlaylistClock(15.)
        with self.assertRaises(capture.CapturePending):
            capture.parse_live_playlist(playlist(100), SOURCE, observation_clock=clock, observed_at=START)
        seen = START + timedelta(seconds=8)
        segment = capture.parse_live_playlist(playlist(101), SOURCE, observation_clock=clock, observed_at=seen)
        self.assertEqual(segment.timestamp_basis, "stream_observation")
        self.assertEqual(segment.captured_at, seen - timedelta(seconds=segment.duration))
        self.assertEqual(segment.url, "https://camera.example/public/latest.ts")

    def test_receiver_mode_cannot_promote_newly_appearing_program_date_time(self):
        clock = capture.ObservedPlaylistClock(15.)
        def dated(sequence):
            return playlist(sequence).replace(b"#EXTINF:", b"#EXT-X-PROGRAM-DATE-TIME:2026-10-24T00:00:00Z\n#EXTINF:")
        with self.assertRaises(capture.CapturePending):
            capture.parse_live_playlist(dated(100), SOURCE, observation_clock=clock, observed_at=START)
        segment = capture.parse_live_playlist(dated(101), SOURCE, observation_clock=clock,
                                             observed_at=START + timedelta(seconds=8))
        self.assertEqual(segment.timestamp_basis, "stream_observation")

    def test_repeated_segment_never_receives_a_new_clock_and_stopped_playlist_expires(self):
        clock = capture.ObservedPlaylistClock(15.)
        with self.assertRaises(capture.CapturePending):
            clock.resolve(100, SOURCE, 8., START)
        segment = clock.resolve(101, SOURCE, 8., START + timedelta(seconds=8))
        repeat = clock.resolve(101, SOURCE, 8., START + timedelta(seconds=20))
        self.assertEqual(segment, repeat)
        self.assertEqual(repeat.captured_at, START)
        with self.assertRaisesRegex(capture.CaptureBlocked, "stopped"):
            clock.resolve(101, SOURCE, 8., START + timedelta(seconds=24))

    def test_backwards_sequence_or_changed_identity_cannot_replay_as_a_new_sample(self):
        clock = capture.ObservedPlaylistClock(15.)
        with self.assertRaises(capture.CapturePending):
            clock.resolve(100, SOURCE, 8., START)
        clock.resolve(101, SOURCE, 8., START + timedelta(seconds=8))
        for sequence, url in ((100, SOURCE), (101, "https://camera.example/different.ts")):
            with self.subTest(sequence=sequence, url=url), self.assertRaises(capture.CaptureBlocked):
                clock.resolve(sequence, url, 8., START + timedelta(seconds=9))

    def test_end_markers_and_invalid_sequence_stay_blocked_in_receiver_mode(self):
        for body in (playlist(100) + b"#EXT-X-ENDLIST\n", playlist(-1), playlist("bad")):
            with self.subTest(body=body), self.assertRaises(capture.CaptureBlocked):
                capture.parse_live_playlist(body, SOURCE, observation_clock=capture.ObservedPlaylistClock(), observed_at=START)


class ProvisionalCameraEstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.registry, self.database = self.directory / "registry.json", self.directory / "observations.sqlite"
        self.environment = patch.dict(os.environ, {"CROSSFLOW_CCTV_REGISTRY": str(self.registry), "CROSSFLOW_CCTV_DB": str(self.database)})
        self.environment.start()
        self.write(registered())

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def write(self, row):
        self.registry.write_text(json.dumps({"schema_version": 1, "cameras": [row]}))

    def test_receiver_timing_never_becomes_independently_validated_even_with_all_receipts(self):
        cfg = config(validated=True)
        payload = evidence(cfg)
        self.assertTrue(payload["quality"]["usable"])
        self.assertEqual(payload["severity_status"], "persistent")
        self.assertFalse(payload["operational_eligible"])
        self.assertTrue(payload["experimental"])
        self.assertIsNone(payload["congestion_score"])
        checked = validate_evidence(payload, cfg, now=NOW)
        self.assertEqual(checked["timestamp_basis"], "stream_observation")
        self.assertFalse(checked["operational_eligible"])
        self.assertIsNone(checked["congestion_score"])

    def test_source_clock_control_can_qualify_same_fixture_while_receiver_clock_cannot(self):
        cfg = config(validated=True)
        self.assertTrue(evidence(cfg, basis="source_clock")["operational_eligible"])
        self.assertFalse(evidence(cfg, basis="stream_observation")["operational_eligible"])

    def test_authorized_healthy_provisional_counts_and_score_are_labelled_estimated(self):
        result = store.ingest_evidence(evidence(), now=NOW)
        self.assertFalse(result["operational_eligible"])
        monitor = store.camera_status(now=NOW)
        camera = monitor["cameras"][0]
        self.assertEqual(camera["status"], "estimated")
        self.assertEqual(camera["validation_status"], "estimated")
        self.assertEqual(monitor["summary"]["estimated"], 1)
        self.assertEqual(monitor["summary"]["live"], 0)
        self.assertEqual(camera["timestamp_basis"], "stream_observation")
        self.assertEqual(camera["vehicle_count"], 1)
        self.assertGreater(camera["congestion_score"], 0.)
        self.assertFalse(camera["operational_eligible"])
        self.assertIsNone(camera["motion_score"])
        self.assertIsNone(camera["queue_length_m"])
        self.assertNotIn(".m3u8", json.dumps(monitor))
        self.assertNotIn("fixture-independent", json.dumps(monitor))

    def test_provisional_source_does_not_populate_validated_corridor_scores(self):
        store.ingest_evidence(evidence(), now=NOW)
        self.assertEqual(store.corridor_scores(now=NOW), {})
        exact = store.road_segment_scores(now=NOW)
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]["road_segment_id"], "osm-edge:1:2:619")
        self.assertEqual(exact[0]["graph_revision"], "fixture-graph-revision")
        self.assertEqual(exact[0]["validation_status"], "estimated")
        self.assertTrue(exact[0]["estimated_allowed"])
        self.assertFalse(exact[0]["operational_eligible"])

    def test_routing_can_be_revoked_without_inventing_new_measurements(self):
        store.ingest_evidence(evidence(), now=NOW)
        self.write(registered(allow_routing=False))
        self.assertEqual(store.road_segment_scores(now=NOW), [])
        self.assertEqual(store.camera_status(now=NOW)["cameras"][0]["status"], "estimated")

    def test_revoking_estimation_or_authorization_removes_fresh_provisional_scores(self):
        store.ingest_evidence(evidence(), now=NOW)
        for change in ("estimate_disabled", "estimate_reference_removed", "authorization_revoked"):
            reg = registered()
            if change == "estimate_disabled":
                reg["estimation_config"].update(enabled=False, allow_routing=False)
            elif change == "estimate_reference_removed":
                reg["estimation_config"]["reference"] = None
            else:
                reg["analysis_config"]["analysis_authorized"] = False
            self.write(reg)
            with self.subTest(change=change):
                camera = store.camera_status(now=NOW)["cameras"][0]
                self.assertIsNone(camera["congestion_score"])
                self.assertFalse(camera["operational_eligible"])
                self.assertFalse(camera["routing_enabled"])
                self.assertEqual(store.road_segment_scores(now=NOW), [])

    def test_stale_estimates_expire_in_status_and_exact_road_scores(self):
        store.ingest_evidence(evidence(), now=NOW)
        later = NOW + timedelta(seconds=16)
        camera = store.camera_status(now=later)["cameras"][0]
        self.assertEqual(camera["status"], "stale")
        self.assertIsNone(camera["vehicle_count"])
        self.assertIsNone(camera["congestion_score"])
        self.assertEqual(store.road_segment_scores(now=later), [])
        self.assertEqual(store.corridor_scores(now=later), {})

    def test_wrong_detector_is_not_silently_described_as_yolo26(self):
        payload = evidence()
        payload["detector_id"] = "other-experimental-model"
        store.ingest_evidence(payload, now=NOW)
        camera = store.camera_status(now=NOW)["cameras"][0]
        self.assertEqual(camera["vehicle_count"], 1)
        self.assertNotEqual(camera["status"], "estimated")
        self.assertIsNone(camera["congestion_score"])
        self.assertEqual(store.road_segment_scores(now=NOW), [])

    def test_receiver_score_cannot_be_forged_into_the_operational_field(self):
        for change in ({"operational_eligible": True}, {"congestion_score": 100.}):
            payload = evidence(); payload.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                store.ingest_evidence(payload, now=NOW)
        self.assertFalse(self.database.exists())

    def test_store_rejects_source_clock_claim_for_receiver_timing_registration(self):
        cfg = config(validated=True)
        self.write(registered(cfg))
        payload = evidence(cfg, basis="source_clock")
        self.assertTrue(payload["operational_eligible"])
        with self.assertRaises(ValueError):
            store.ingest_evidence(payload, now=NOW)
        self.assertFalse(self.database.exists())

    def test_revoking_receiver_estimation_policy_stops_collection_before_network(self):
        original = store.get_registered_camera("pilot-fixture")
        changed = deepcopy(original)
        changed["estimation_config"].update(enabled=False, allow_routing=False)
        fetch, decode = Mock(), Mock()
        with patch.object(store, "get_registered_camera", return_value=changed):
            with self.assertRaisesRegex(capture.CaptureBlocked, "registration changed"):
                capture.collect_camera(original, max_seconds=1., fetch=fetch, decode=decode, counter=object(),
                                       ingest=store.ingest_evidence, utcnow=lambda: NOW)
        fetch.assert_not_called()
        decode.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_unknown_timestamp_basis_is_invalid_in_frames_and_numeric_evidence(self):
        for basis in ("downloaded_now", "", None, False):
            with self.subTest(basis=basis), self.assertRaises(ValueError):
                evidence(basis=basis)


class CCTVSupervisorTests(unittest.TestCase):
    def test_explicit_disable_or_serverless_avoids_runtime_and_registry_access(self):
        for environment in ({"CROSSFLOW_CCTV_ENABLED": "0", "VERCEL": ""},
                            {"CROSSFLOW_CCTV_ENABLED": "1", "VERCEL": "1"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment), patch.object(store, "load_registry") as load:
                runtime = cctv_runtime.CCTVRuntime()
                runtime.start()
                load.assert_not_called()
                self.assertEqual(runtime._threads, [])

    def test_runtime_bounds_parallel_slots_but_schedules_all_registered_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "fixture-python"
            python.write_text("Engineering path fixture; never executed")
            rows = [registered() for _ in range(4)]
            for index, item in enumerate(rows):
                item["camera_id"] = "fixture-" + str(index)
            rows[3]["analysis_config"]["enabled"] = False
            with patch.dict(os.environ, {"CROSSFLOW_CCTV_ENABLED": "1", "VERCEL": "", "CROSSFLOW_CCTV_PYTHON": str(python), "CROSSFLOW_CCTV_MAX_CONCURRENT": "2"}), patch.object(store, "load_registry", return_value=rows), patch.object(cctv_runtime.os, "cpu_count", return_value=8), patch.object(cctv_runtime.cctv_availability.MONITOR, "start"), patch.object(cctv_runtime.threading, "Thread") as thread, patch.object(cctv_runtime.subprocess, "Popen") as process:
                runtime = cctv_runtime.CCTVRuntime()
                runtime.start()
                runtime.start()
                self.assertEqual(thread.call_count, 3)
                self.assertEqual(len(runtime._threads), 3)
                self.assertEqual(thread.call_args_list[0].kwargs["target"], runtime._watch_registry)
                self.assertEqual(thread.call_args_list[1].kwargs["args"], (python,))
                self.assertEqual(thread.call_args_list[2].kwargs["args"], (python,))
                self.assertEqual(runtime._schedule.camera_ids, ("fixture-0", "fixture-1", "fixture-2"))
                process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
