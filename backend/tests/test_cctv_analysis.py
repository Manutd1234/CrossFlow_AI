"""Engineering fixtures for temporal semantics; these are not accuracy evidence."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from services.cctv_analysis import CameraConfig, CCTVAnalyzer, FrameObservation, image_occupancy, timestamp, validate_evidence

START = datetime(2026, 10, 24, tzinfo=timezone.utc)
ROI = ((0., 0.), (1., 0.), (1., 1.), (0., 1.))


def config(**kwargs):
    return CameraConfig(camera_id="fixture-camera", corridor_id="fixture-corridor", enabled=True,
                        analysis_authorized=True, authorization_reference="engineering-fixture-only", roi=ROI,
                        minimum_samples=3, minimum_window_seconds=2, stationary_seconds=3,
                        congestion_persistence_seconds=2, **kwargs)


def frame(second, boxes=None, **kwargs):
    return {"captured_at": (START + timedelta(seconds=second)).isoformat(),
            "detections": [{"box": list(b), "class_name": "car", "score": .9} for b in (boxes if boxes is not None else [(.1, .1, .9, .9)])],
            "frame_width": 640, "frame_height": 480, "detector_id": "fixture-detector",
            "camera_stable": True, "exposure_ok": True, **kwargs}


def ingest(analyzer, second, boxes=None, **kwargs):
    return analyzer.ingest(frame(second, boxes, **kwargs), now=START + timedelta(seconds=second))


def calibrated(**kwargs):
    return config(thresholds_validated=True, threshold_validation_reference="engineering-threshold-fixture", **kwargs)


class CCTVGeometryTests(unittest.TestCase):
    def test_union_occupancy_does_not_double_count_overlapping_boxes(self):
        self.assertAlmostEqual(image_occupancy([(.1, .1, .6, .6), (.4, .4, .9, .9)], ROI), .46)

    def test_occupancy_clips_boxes_to_triangle_roi(self):
        triangle = ((0., 0.), (1., 0.), (0., 1.))
        self.assertAlmostEqual(image_occupancy([(.0, .0, 1., 1.)], triangle), 1.)
        self.assertAlmostEqual(image_occupancy([(.5, .5, 1., 1.)], triangle), 0.)

    def test_concave_roi_and_overlapping_union(self):
        roi = ((0., 0.), (1., 0.), (1., .3), (.3, .3), (.3, 1.), (0., 1.))
        self.assertAlmostEqual(image_occupancy([(0., 0., 1., 1.), (.2, .2, .8, .8)], roi), 1.)
        self.assertAlmostEqual(image_occupancy([(.5, .5, 1., 1.)], roi), 0.)

    def test_empty_and_missing_roi_have_distinct_semantics(self):
        self.assertEqual(image_occupancy([], ROI), 0.)
        self.assertIsNone(image_occupancy([], None))

    def test_timestamp_requires_timezone_and_rejects_nonfinite_coordinates(self):
        with self.assertRaises(ValueError):
            timestamp("2026-10-24T00:00:00")
        for coordinate in (True, float("nan"), -1., 2.):
            with self.subTest(coordinate=coordinate), self.assertRaises(ValueError):
                FrameObservation.from_dict(frame(0, [(coordinate, .1, .9, .9)]))


class CCTVTemporalTests(unittest.TestCase):
    def test_default_configuration_disables_analysis_before_tracking(self):
        analyzer = CCTVAnalyzer(CameraConfig("fixture"))
        result = ingest(analyzer, 0)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["operational_eligible"])
        self.assertIsNone(result["vehicle_count"])
        self.assertEqual(len(analyzer._tracks), 0)

    def test_authorization_requires_reference_and_explicit_flag(self):
        for cfg in (replace(config(), analysis_authorized=False), replace(config(), authorization_reference=None)):
            result = ingest(CCTVAnalyzer(cfg), 0)
            self.assertEqual(result["status"], "permission_pending")
            self.assertIsNone(result["congestion_score"])

    def test_roi_excludes_nonroad_vehicle_and_occupancy_is_clipped(self):
        analyzer = CCTVAnalyzer(replace(config(), roi=((0., 0.), (.5, 0.), (.5, 1.), (0., 1.))))
        result = ingest(analyzer, 0, [(.1, .1, .2, .2), (.7, .1, .9, .3)])
        self.assertEqual(result["vehicle_count"], 1)
        self.assertAlmostEqual(result["image_occupancy_ratio"], .02)

    def test_stationary_is_temporal_and_requires_minimum_duration(self):
        analyzer = CCTVAnalyzer(config())
        result = ingest(analyzer, 0)
        self.assertEqual(result["stationary_vehicle_count"], 0)
        self.assertEqual(result["unknown_vehicle_count"], 1)
        for t in (1, 2):
            result = ingest(analyzer, t)
            self.assertEqual(result["stationary_vehicle_count"], 0)
        result = ingest(analyzer, 3)
        self.assertEqual(result["stationary_vehicle_count"], 1)
        self.assertEqual(result["normalized_motion_proxy"], 0.)
        self.assertIsNone(result["speed_kmh"])
        self.assertIsNone(result["queue_length_m"])

    def test_moving_vehicle_motion_is_image_displacement_only(self):
        analyzer = CCTVAnalyzer(config())
        ingest(analyzer, 0, [(.1, .1, .3, .3)])
        result = ingest(analyzer, 1, [(.12, .1, .32, .3)])
        self.assertEqual(result["moving_vehicle_count"], 1)
        self.assertAlmostEqual(result["normalized_motion_proxy"], .02)
        self.assertIsNone(result["travel_time_seconds"])

    def test_gap_resets_stationary_and_temporal_window(self):
        analyzer = CCTVAnalyzer(config())
        for t in range(5):
            ingest(analyzer, t)
        result = ingest(analyzer, 11)
        self.assertIn("frame_gap", result["quality"]["reasons"])
        result = ingest(analyzer, 12)
        self.assertEqual(result["frame_count"], 1)
        self.assertEqual(result["stationary_vehicle_count"], 0)

    def test_camera_pan_darkness_truncation_and_unknown_quality_reset(self):
        for changed, reason in (({"camera_stable": False}, "camera_motion"), ({"camera_stable": None}, "camera_stability_unknown"),
                                ({"exposure_ok": False}, "poor_exposure"), ({"exposure_ok": None}, "exposure_unknown"),
                                ({"may_be_truncated": True}, "detector_truncated")):
            analyzer = CCTVAnalyzer(config())
            for t in range(4):
                ingest(analyzer, t)
            result = ingest(analyzer, 4, **changed)
            self.assertIn(reason, result["quality"]["reasons"])
            self.assertIsNone(result["congestion_score"])
            self.assertFalse(result["operational_eligible"])
            self.assertEqual(len(analyzer._tracks), 0)

    def test_resolution_and_detector_changes_reset_state(self):
        for changed, reason in (({"frame_width": 1280}, "camera_dimensions_changed"), ({"detector_id": "other-model"}, "detector_changed")):
            analyzer = CCTVAnalyzer(config())
            ingest(analyzer, 0)
            result = ingest(analyzer, 1, **changed)
            self.assertIn(reason, result["quality"]["reasons"])

    def test_out_of_order_duplicate_and_far_future_rejected_before_mutation(self):
        analyzer = CCTVAnalyzer(config())
        ingest(analyzer, 5)
        for t in (4, 5):
            with self.assertRaisesRegex(ValueError, "strictly increasing"):
                ingest(analyzer, t)
        with self.assertRaisesRegex(ValueError, "future"):
            analyzer.ingest(frame(20), now=START + timedelta(seconds=6))
        self.assertEqual(analyzer._last_at, START + timedelta(seconds=5))

    def test_stale_frame_is_blocked_not_empty_road(self):
        analyzer = CCTVAnalyzer(config())
        result = analyzer.ingest(frame(0, []), now=START + timedelta(seconds=20))
        self.assertIn("stale_frame", result["quality"]["reasons"])
        self.assertFalse(result["quality"]["usable"])
        self.assertIsNone(result["congestion_score"])

    def test_uncertain_detection_quality_and_count_partition(self):
        analyzer = CCTVAnalyzer(config())
        low = frame(0)
        low["detections"][0]["score"] = .2
        result = analyzer.ingest(low, now=START)
        self.assertIn("uncertain_detections", result["quality"]["reasons"])
        self.assertEqual(result["uncertain_vehicle_count"], 1)
        self.assertEqual(result["vehicle_count"], 0)
        self.assertFalse(result["operational_eligible"])

    def test_unvalidated_thresholds_allow_measurements_but_no_severity(self):
        analyzer = CCTVAnalyzer(config())
        for t in range(8):
            result = ingest(analyzer, t)
        self.assertTrue(result["quality"]["usable"])
        self.assertEqual(result["severity_status"], "uncalibrated")
        self.assertIsNone(result["camera_congestion_severity"])
        self.assertIsNone(result["congestion_score"])

    def test_calibrated_proxy_needs_persistence_and_never_promotes_current_unvalidated_model(self):
        analyzer = CCTVAnalyzer(calibrated())
        for t in range(5):
            result = ingest(analyzer, t)
        self.assertIsNone(result["camera_congestion_severity"])
        for t in (5, 6):
            result = ingest(analyzer, t)
        self.assertEqual(result["camera_congestion_severity"], "high")
        self.assertGreater(result["camera_congestion_score_proxy"], 0)
        self.assertFalse(result["operational_eligible"])
        self.assertIsNone(result["congestion_score"])

    def test_explicit_independent_camera_detector_binding_required_for_operational_score(self):
        cfg = calibrated(independent_validation_reference="engineering-validation-only", validated_detector_id="fixture-detector")
        analyzer = CCTVAnalyzer(cfg)
        for t in range(7):
            result = ingest(analyzer, t)
        self.assertTrue(result["operational_eligible"])
        self.assertEqual(result["status"], "validated")
        self.assertGreaterEqual(result["congestion_score"], 0.)
        self.assertLessEqual(result["congestion_score"], 100.)
        self.assertEqual(result["congestion_score"], result["camera_congestion_score_proxy"])
        self.assertIsNone(result["speed_kmh"])

    def test_line_crossing_is_finite_and_not_counted_on_initial_appearance(self):
        cfg = config(counting_line=((.5, .2), (.5, .8)), match_center_distance=.25)
        analyzer = CCTVAnalyzer(cfg)
        a = ingest(analyzer, 0, [(.39, .4, .59, .6)])
        self.assertEqual(a["line_crossings"], {"positive": 0, "negative": 0})
        b = ingest(analyzer, 1, [(.41, .4, .61, .6)])
        self.assertEqual(sum(b["line_crossings"].values()), 1)
        other = CCTVAnalyzer(cfg)
        ingest(other, 0, [(.39, .0, .59, .1)])
        outside = ingest(other, 1, [(.41, .0, .61, .1)])
        self.assertEqual(sum(outside["line_crossings"].values()), 0)

    def test_occlusion_does_not_accumulate_stationary_time_or_crossings(self):
        analyzer = CCTVAnalyzer(config())
        for t in range(4):
            ingest(analyzer, t)
        ingest(analyzer, 4, [])
        result = ingest(analyzer, 5)
        self.assertEqual(result["stationary_vehicle_count"], 0)

    def test_matched_slow_motion_is_not_confused_with_unobserved_motion_before_stopped_duration(self):
        cfg = replace(config(), minimum_samples=5, minimum_window_seconds=10, stationary_seconds=15)
        analyzer = CCTVAnalyzer(cfg)
        for t in range(0, 12, 2):
            result = ingest(analyzer, t)
        self.assertTrue(result['quality']['usable'])
        self.assertEqual(result['stationary_vehicle_count'], 0)
        self.assertEqual(result['unknown_vehicle_count'], 1)
        self.assertEqual(result['normalized_motion_proxy'], 0.)
        self.assertFalse(result['operational_eligible'])
        clean = validate_evidence(result, cfg, now=START + timedelta(seconds=10))
        self.assertTrue(clean['quality']['usable'])
        self.assertEqual(clean['unknown_vehicle_count'], 1)
        self.assertIsNone(clean['queue_length_m'])

    def test_new_arrivals_keep_unknown_counts_without_discarding_established_rolling_motion(self):
        cfg = config()
        analyzer = CCTVAnalyzer(cfg)
        for t in range(11):
            left = .05 + .005 * t
            ingest(analyzer, t, [(left, .1, left + .1, .3)])
        result = ingest(analyzer, 11, [(.105, .1, .205, .3), (.6, .1, .7, .3), (.8, .1, .9, .3)])
        self.assertEqual(result['vehicle_count'], 3)
        self.assertEqual(result['moving_vehicle_count'], 1)
        self.assertEqual(result['unknown_vehicle_count'], 2)
        self.assertTrue(result['quality']['usable'])
        self.assertFalse(result['operational_eligible'])
        self.assertTrue(validate_evidence(result, cfg, now=START + timedelta(seconds=11))['quality']['usable'])

    def test_past_motion_cannot_qualify_an_entirely_new_current_vehicle(self):
        analyzer = CCTVAnalyzer(config())
        for t in range(10):
            ingest(analyzer, t, [(.05 + .005 * t, .1, .15 + .005 * t, .3)])
        result = ingest(analyzer, 10, [(.8, .7, .9, .9)])
        self.assertFalse(result['quality']['usable'])
        self.assertIn('insufficient_track_history', result['quality']['reasons'])
        self.assertIsNone(result['normalized_motion_proxy'])

    def test_empty_frames_do_not_pad_tracking_coverage_for_new_vehicles(self):
        analyzer = CCTVAnalyzer(config())
        for t in range(20):
            ingest(analyzer, t, [])
        ingest(analyzer, 20, [(.2, .2, .4, .4)])
        result = ingest(analyzer, 21, [(.21, .2, .41, .4)])
        self.assertFalse(result['quality']['usable'])
        self.assertEqual(result['quality']['tracking']['vehicle_observations'], 2)
        self.assertEqual(result['quality']['tracking']['motion_observations'], 1)

    def test_rolling_partial_motion_does_not_promote_independently_validated_congestion(self):
        cfg = calibrated(independent_validation_reference='engineering-validation-only', validated_detector_id='fixture-detector')
        analyzer = CCTVAnalyzer(cfg)
        for t in range(11):
            ingest(analyzer, t, [(.05, .05, .65, .95)])
        result = ingest(analyzer, 11, [(.05, .05, .65, .95), (.75, .2, .8, .3), (.85, .5, .9, .6)])
        self.assertTrue(result['quality']['usable'])
        self.assertEqual(result['unknown_vehicle_count'], 2)
        self.assertFalse(result['operational_eligible'])
        self.assertIsNone(result['congestion_score'])
        self.assertFalse(validate_evidence(result, cfg, now=START + timedelta(seconds=11))['operational_eligible'])

    def test_motion_prediction_keeps_two_crossing_tracks_and_directional_events_separate(self):
        cfg = config(counting_line=((.5, .2), (.5, .8)), match_center_distance=.25)
        analyzer = CCTVAnalyzer(cfg)
        for t, centers in enumerate(((.3, .7), (.42, .58), (.54, .46))):
            result = ingest(analyzer, t, [(x - .05, .45, x + .05, .55) for x in centers])
        self.assertEqual(result['moving_vehicle_count'], 2)
        self.assertAlmostEqual(analyzer._tracks[1].detection.center[0], .54)
        self.assertAlmostEqual(analyzer._tracks[2].detection.center[0], .46)
        self.assertEqual(result['line_crossings'], {'positive': 1, 'negative': 1})

    def test_counting_line_dead_band_does_not_turn_centroid_jitter_into_flow(self):
        analyzer = CCTVAnalyzer(config(counting_line=((.5, .2), (.5, .8))))
        for t, x in enumerate((.48, .498, .502, .499, .501, .497, .503, .52)):
            result = ingest(analyzer, t, [(x - .1, .4, x + .1, .6)])
            if t < 7:
                self.assertEqual(sum(result['line_crossings'].values()), 0)
        self.assertEqual(sum(result['line_crossings'].values()), 1)

    def test_flow_is_not_inferred_across_occlusion_and_old_events_expire(self):
        cfg = config(counting_line=((.5, .2), (.5, .8)), window_seconds=6)
        analyzer = CCTVAnalyzer(cfg)
        ingest(analyzer, 0, [(.38, .4, .58, .6)])
        ingest(analyzer, 1, [])
        result = ingest(analyzer, 2, [(.44, .4, .64, .6)])
        self.assertEqual(sum(result['line_crossings'].values()), 0)
        result = ingest(analyzer, 3, [(.38, .4, .58, .6)])
        self.assertEqual(sum(result['line_crossings'].values()), 1)
        for t in range(4, 11):
            result = ingest(analyzer, t, [(.38, .4, .58, .6)])
        self.assertEqual(sum(result['line_crossings'].values()), 0)

    def test_queue_state_needs_continuous_stopped_duration_and_clears_when_vehicle_moves(self):
        analyzer = CCTVAnalyzer(config())
        for t, left in enumerate((.1, .12, .14, .14, .14, .14, .17)):
            result = ingest(analyzer, t, [(left, .1, left + .2, .3)])
            if t < 5:
                self.assertEqual(result['stationary_vehicle_count'], 0)
            elif t == 5:
                self.assertEqual(result['stationary_vehicle_count'], 1)
            else:
                self.assertEqual(result['stationary_vehicle_count'], 0)
                self.assertEqual(result['moving_vehicle_count'], 1)
            self.assertIsNone(result['queue_length_m'])

    def test_five_second_playback_check_is_shorter_than_the_congestion_evidence_window(self):
        analyzer = CCTVAnalyzer(replace(config(), minimum_samples=5, minimum_window_seconds=10, stationary_seconds=15))
        for t in range(6):
            result = ingest(analyzer, t)
        self.assertEqual(result['window_seconds'], 5)
        self.assertFalse(result['quality']['usable'])
        self.assertIn('minimum_temporal_window', result['quality']['reasons'])
        self.assertIsNone(result['congestion_score'])

    def test_config_rejects_crossed_roi_unknown_keys_and_bad_thresholds(self):
        for value in ({"camera_id": "x", "unknown": True}, {"camera_id": "x", "enabled": "true"},
                      {"camera_id": "x", "moderate_occupancy": .8, "high_occupancy": .4},
                      {"camera_id": "x", "roi": [[0, 0], [1, 1], [0, 1], [1, 0]]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CameraConfig.from_dict(value)

    def test_per_camera_state_and_fingerprints_are_isolated(self):
        a, b = CCTVAnalyzer(config()), CCTVAnalyzer(replace(config(), camera_id="other"))
        for t in range(4):
            ingest(a, t)
        result = ingest(b, 4)
        self.assertEqual(result["frame_count"], 1)
        self.assertNotEqual(a.config.fingerprint, b.config.fingerprint)


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = calibrated(independent_validation_reference="engineering-validation-only", validated_detector_id="fixture-detector")
        analyzer = CCTVAnalyzer(self.cfg)
        for t in range(7):
            self.result = ingest(analyzer, t)
        self.now = START + timedelta(seconds=6)

    def test_valid_evidence_round_trip_and_private_fields_are_removed(self):
        value = {**self.result, "image_base64": "must-not-leak", "stream_url": "private"}
        output = validate_evidence(value, self.cfg, now=self.now)
        self.assertTrue(output["operational_eligible"])
        self.assertNotIn("image_base64", output)
        self.assertNotIn("stream_url", output)
        self.assertFalse(output["images_retained"])

    def test_forged_eligibility_is_recomputed_from_registered_config(self):
        cfg = replace(self.cfg, independent_validation_reference=None)
        value = {**self.result, "config_fingerprint": cfg.fingerprint, "operational_eligible": True}
        output = validate_evidence(value, cfg, now=self.now)
        self.assertFalse(output["operational_eligible"])
        self.assertIsNone(output["congestion_score"])
        self.assertEqual(output["status"], "experimental")

    def test_foreign_camera_stale_future_and_mismatching_config_rejected(self):
        for value, now in (({**self.result, "camera_id": "other"}, self.now),
                           ({**self.result, "config_fingerprint": "0" * 64}, self.now),
                           (self.result, self.now + timedelta(seconds=20)),
                           (self.result, self.now - timedelta(seconds=10))):
            with self.subTest(now=now), self.assertRaises(ValueError):
                validate_evidence(value, self.cfg, now=now)

    def test_tracking_observation_contradictions_cannot_bypass_quality(self):
        tracking = self.result['quality']['tracking']
        for changes in ({'motion_observations': tracking['vehicle_observations'] + 1},
                        {'current_motion_observations': self.result['vehicle_count'] + 1},
                        {'current_motion_observations': 0}, {'vehicle_observations': -1},
                        {'scope': 'unverified'}, {'extra': 1}):
            bad = deepcopy(self.result)
            bad['quality']['tracking'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_evidence(bad, self.cfg, now=self.now)

    def test_numeric_contradictions_and_proxy_tampering_rejected(self):
        variants = [dict(vehicle_count=True), dict(vehicle_count=201), dict(image_occupancy_ratio=float("nan")),
                    dict(window_seconds=5), dict(moving_vehicle_count=2), dict(stationary_fraction=2),
                    dict(camera_congestion_score_proxy=5), dict(severity_persistence_seconds=0), dict(vehicle_count=None),
                    dict(tracked_vehicle_count=None), dict(normalized_motion_proxy=None), dict(status="quality_blocked"),
                    dict(camera_congestion_severity="low")]
        for fields in variants:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_evidence({**self.result, **fields}, self.cfg, now=self.now)
        bad = deepcopy(self.result)
        bad["quality"]["reasons"] = ["camera_motion"]
        with self.assertRaises(ValueError):
            validate_evidence(bad, self.cfg, now=self.now)


if __name__ == "__main__":
    unittest.main()
