"""Observed candidate source/chronology/promotion and safe artifact regressions.

Measurements generated here are labelled unit-test fixtures, never imported
into the application's archive or used as evidence of real Batam accuracy.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.congestion_model import CongestionForecaster, SpatialPredictionContext
from services import observed_training as training
from services.observed_model_artifact import (
    NumericRandomForest, NumericRegressionTree, load_observed_model, restore_observed_model, save_observed_model,
)
from services.traffic_observations import ObservationConflictError, SpatialTrafficObservation

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = START + timedelta(days=20)


def fixture_history(steps=8 * 24 * 4 + 1, interval_minutes=15, source="probe_gps"):
    rows = []
    for step in range(steps):
        hour = step * interval_minutes / 60
        for corridor in range(5):
            score = 30 + 18 * np.sin(2 * np.pi * hour / 24) + corridor * 3
            rows.append(SpatialTrafficObservation.create(
                corridor_id=f"corridor-{corridor + 1}", observed_at=START + timedelta(minutes=step * interval_minutes),
                latitude=1.10 + corridor * .01, longitude=104.0 + corridor * .01,
                actual_speed_kph=50 * (1 - score / 100), free_flow_speed_kph=50,
                source=source, road_class="primary", capacity_vph=1800,
                terminal_distance_km=1.0, upstream_event_id=f"unit-only-{step}-{corridor}",
                validation_now=NOW,
            ))
    return rows


class ObservedSelectionTests(unittest.TestCase):
    def test_short_measured_archive_is_insufficient_without_fitting_or_fallback(self):
        with patch.object(training, "CongestionForecaster") as construct:
            report = training.evaluate_observed_training(fixture_history(steps=15, interval_minutes=2), now=NOW)
        construct.assert_not_called()
        self.assertEqual(report["status"], "insufficient_observations")
        self.assertEqual(report["selection"]["eligible_rows"], 75)
        self.assertEqual(report["horizons"], [])
        self.assertIn("observed_span_below_7_days", report["activation"]["reasons"])
        self.assertFalse(report["model_replaced"])

    def test_modelled_and_duplicate_inputs_cannot_multiply_observed_evidence(self):
        observed = fixture_history(steps=10)
        synthetic = fixture_history(steps=20, source="synthetic")
        report = training.evaluate_observed_training(observed + observed + synthetic, now=NOW)
        selection = report["selection"]
        self.assertEqual(selection["input_rows"], 200)
        self.assertEqual(selection["duplicate_rows"], 50)
        self.assertEqual(selection["excluded_non_observed_rows"], 100)
        self.assertEqual(selection["eligible_rows"], 50)
        self.assertEqual(selection["source_counts"], {"probe_gps": 50})

    def test_sampling_preserves_an_original_measurement_and_timestamp(self):
        rows = fixture_history(steps=8, interval_minutes=2)
        selected, metadata = training._select(reversed(rows), max_rows=100, now=NOW, policy=training.DEFAULT_POLICY)
        self.assertEqual(len(selected), 5)
        self.assertTrue(all(row.observed_at == START for row in selected))
        self.assertTrue(all(row in rows for row in selected))
        self.assertEqual(metadata["sampling_policy"], "first_original_per_location_15min_utc_bucket")

    def test_conflicting_id_and_spoofed_observed_flag_fail_closed(self):
        row = fixture_history(steps=1)[0]
        changed = SpatialTrafficObservation.create(
            corridor_id=row.corridor_id, observed_at=row.observed_at, latitude=row.latitude, longitude=row.longitude,
            actual_speed_kph=0, free_flow_speed_kph=50, source=row.source, upstream_event_id=row.upstream_event_id,
            validation_now=NOW,
        )
        with self.assertRaises(ObservationConflictError):
            training.evaluate_observed_training([row, changed], now=NOW)
        with self.assertRaises(ValueError):
            training.evaluate_observed_training([replace(row, observed=False)], now=NOW)

    def test_input_bounds_stop_stream_before_canonical_validation(self):
        consumed = 0
        def endless():
            nonlocal consumed
            while True:
                consumed += 1
                yield object()
        with self.assertRaisesRegex(ValueError, "limited to 3"):
            training.evaluate_observed_training(endless(), max_rows=3, now=NOW)
        self.assertEqual(consumed, 4)
        for value in (True, 0, 100001, 3.0):
            with self.subTest(max_rows=value), self.assertRaises(ValueError):
                training.evaluate_observed_training([], max_rows=value, now=NOW)
        with self.assertRaises(ValueError):
            training.evaluate_observed_training([], now=NOW.replace(tzinfo=None))

    def test_ingestion_clock_skew_allowance_does_not_authorize_future_training(self):
        row = fixture_history(steps=1)[0]
        report = training.evaluate_observed_training([row], now=row.observed_at - timedelta(seconds=1))
        self.assertEqual(report["selection"]["eligible_rows"], 0)
        self.assertEqual(report["selection"]["excluded_future_rows"], 1)

    def test_raw_retrain_excludes_synthetic_by_default_and_groups_ties(self):
        model = CongestionForecaster(train_baseline=False)
        report = model.retrain_from_observations(fixture_history(steps=30, source="synthetic"))
        self.assertFalse(model.is_trained)
        self.assertIn("observed", report["last_retrain_skipped_reason"])
        same_time = fixture_history(steps=25)
        same_time = [SpatialTrafficObservation.create(
            corridor_id=row.corridor_id, observed_at=START,
            latitude=row.latitude, longitude=row.longitude,
            actual_speed_kph=row.actual_speed_kph, free_flow_speed_kph=50,
            source=row.source, upstream_event_id=row.upstream_event_id,
            validation_now=NOW,
        ) for row in same_time]
        report = model.retrain_from_observations(same_time)
        self.assertFalse(model.is_trained)
        self.assertEqual(report["last_retrain_skipped_reason"], "insufficient_distinct_chronological_partitions")

    def test_timestamp_groups_and_purge_separate_all_partitions(self):
        rows = fixture_history()
        fit, calibration, holdout = training._partitions(rows, training.DEFAULT_POLICY)
        self.assertLess(fit[-1].observed_at, calibration[0].observed_at - timedelta(minutes=60))
        self.assertLess(calibration[-1].observed_at, holdout[0].observed_at - timedelta(minutes=60))
        for values in (fit, calibration, holdout):
            self.assertTrue(all(sum(row.observed_at == when for row in values) == 5 for when in {row.observed_at for row in values}))

    def test_future_target_metadata_never_enters_forecast_features(self):
        rows = fixture_history()
        _, _, holdout = training._partitions(rows, training.DEFAULT_POLICY)
        target = holdout[0]
        replacement = SpatialTrafficObservation.create(
            corridor_id=target.corridor_id, observed_at=target.observed_at,
            latitude=target.latitude, longitude=target.longitude,
            actual_speed_kph=0, free_flow_speed_kph=110,
            source=target.source, road_class="motorway", capacity_vph=7900,
            upstream_event_id=target.upstream_event_id, validation_now=NOW,
        )
        index = rows.index(target)
        rows[index] = replacement
        actual, _, _, contexts, _, _ = training._examples(rows, [replacement], 15, training.DEFAULT_POLICY)
        self.assertEqual(actual, [95.0])
        self.assertEqual(contexts[0].free_flow_speed_kph, 50)
        self.assertEqual(contexts[0].road_class, "primary")
        self.assertEqual(contexts[0].capacity_vph, 1800)
        self.assertEqual(contexts[0].hour_float, target.local_observed_at.hour + target.local_observed_at.minute / 60)


class ObservedEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = fixture_history()
        cls.report, cls.candidate = training._build_candidate(cls.rows, now=NOW, max_rows=50000)

    def test_actual_candidate_is_observed_only_and_read_only(self):
        self.assertIn(self.report["status"], ("evaluated", "eligible"))
        self.assertFalse(self.report["model_replaced"])
        self.assertFalse(self.report["running_model_evaluated"])
        self.assertEqual(self.candidate.metrics["training_data_source"], "validated_spatial_observed")
        self.assertEqual(set(self.candidate.metrics["training_source_counts"]), {"probe_gps"})
        self.assertEqual(self.candidate.metrics["observed_fit_rows"], self.report["chronology"]["fit"]["rows"])
        self.assertEqual([row["horizon_minutes"] for row in self.report["horizons"]], [15, 30, 60])
        self.assertTrue(all(row["serving_model"]["samples"] > 0 for row in self.report["horizons"]))
        self.assertFalse(self.report["interval_calibration"]["coverage_guaranteed"])
        self.assertGreater(self.report["interval_calibration"]["calibration_forecast_pairs"], self.report["chronology"]["calibration"]["rows"])
        self.assertEqual(self.candidate.metrics["evaluation_horizon_minutes"], 30)
        self.assertEqual(self.candidate.metrics["holdout_first_observed_at"], self.report["chronology"]["holdout"]["start_at"])
        self.assertEqual(self.candidate.metrics["calibration_first_observed_at"], self.report["chronology"]["calibration"]["start_at"])

    def test_model_metrics_count_only_matched_30minute_test_targets(self):
        start = datetime.fromisoformat(self.report["chronology"]["holdout"]["start_at"])
        changed = []
        for row in self.rows:
            if row.observed_at >= start:
                corridor = int(row.corridor_id[-1]) - 1
                row = SpatialTrafficObservation.create(
                    corridor_id=row.corridor_id, observed_at=row.observed_at,
                    latitude=1.17 + corridor * .005, longitude=104.1 + corridor * .005,
                    actual_speed_kph=row.actual_speed_kph, free_flow_speed_kph=row.free_flow_speed_kph,
                    source="loop_sensor" if row.observed_at < start + timedelta(minutes=30) else "probe_gps",
                    road_class=row.road_class, capacity_vph=row.capacity_vph,
                    terminal_distance_km=row.terminal_distance_km, upstream_event_id=row.upstream_event_id,
                    validation_now=NOW,
                )
            changed.append(row)
        report, candidate = training._build_candidate(changed, now=NOW, max_rows=50000)
        matched = next(row["serving_model"]["samples"] for row in report["horizons"] if row["horizon_minutes"] == 30)
        self.assertEqual(matched, report["chronology"]["holdout"]["rows"] - 10)
        self.assertEqual(candidate.metrics["holdout_source_counts"], {"probe_gps": matched})
        self.assertEqual(candidate.metrics["observed_evaluation_rows"], matched)
        self.assertEqual(datetime.fromisoformat(candidate.metrics["holdout_first_observed_at"]), start + timedelta(minutes=30))
        self.assertEqual(candidate.metrics["observed_training_rows"],
                         report["chronology"]["fit"]["rows"] + report["chronology"]["calibration"]["rows"])
        self.assertEqual(candidate.metrics["total_samples"], candidate.metrics["observed_training_rows"] + matched)

    def test_holdout_label_perturbation_cannot_change_fit_or_calibration(self):
        first_holdout = datetime.fromisoformat(self.report["chronology"]["holdout"]["start_at"])
        changed = []
        for row in self.rows:
            if row.observed_at >= first_holdout:
                row = SpatialTrafficObservation.create(
                    corridor_id=row.corridor_id, observed_at=row.observed_at,
                    latitude=row.latitude, longitude=row.longitude, actual_speed_kph=0,
                    free_flow_speed_kph=row.free_flow_speed_kph, source=row.source,
                    road_class=row.road_class, capacity_vph=row.capacity_vph,
                    terminal_distance_km=row.terminal_distance_km, upstream_event_id=row.upstream_event_id,
                    validation_now=NOW,
                )
            changed.append(row)
        report, candidate = training._build_candidate(changed, now=NOW, max_rows=50000)
        self.assertEqual(report["interval_calibration"], self.report["interval_calibration"])
        self.assertEqual(candidate.metrics["fit_source_counts"], self.candidate.metrics["fit_source_counts"])
        contexts = [SpatialPredictionContext.from_legacy(8, 0, 0, 0, 0)]
        self.assertEqual(candidate.predict_spatial_batch(contexts), self.candidate.predict_spatial_batch(contexts))
        self.assertFalse(report["activation"]["eligible"])

    def test_zero_error_persistence_cannot_be_called_improved(self):
        results = deepcopy(self.report["horizons"])
        for result in results:
            result["persistence"]["mae_points"] = 0.0
        with patch.object(training, "_horizon_results", return_value=results):
            report, _ = training._build_candidate(self.rows, now=NOW, max_rows=50000)
        self.assertFalse(report["activation"]["eligible"])
        self.assertTrue(any("not_better_than_persistence" in reason for reason in report["activation"]["reasons"]))

    def test_mutation_requires_eligibility_and_preserves_previous_revision(self):
        serving = CongestionForecaster(train_baseline=False)
        before_model, before_metrics = serving.model, serving.metrics
        report = training.retrain_observed_model(serving, self.rows[:50], now=NOW)
        self.assertFalse(report["model_replaced"])
        self.assertIs(serving.model, before_model)
        self.assertIs(serving.metrics, before_metrics)

    def test_successful_explicit_activation_is_one_revision(self):
        report = deepcopy(self.report)
        report["activation"] = {"eligible": True, "reasons": [], "policy": {}}
        candidate = SimpleNamespace(model=self.candidate.model, metrics=deepcopy(self.candidate.metrics), _interval_padding_points=2.0)
        serving = CongestionForecaster(train_baseline=False)
        with patch.object(training, "_build_candidate", return_value=(report, candidate)):
            result = training.retrain_observed_model(serving, [], now=NOW)
        self.assertTrue(result["model_replaced"])
        self.assertEqual(result["status"], "activated")
        self.assertIs(serving.model, candidate.model)
        self.assertIs(serving.metrics, candidate.metrics)
        self.assertEqual(serving._interval_padding_points, 2.0)
        self.assertEqual(serving.metrics["artifact"]["durability"], "process_memory")
        self.assertFalse(result["artifact"]["persisted"])

    def test_persistence_failure_blocks_live_activation(self):
        report = deepcopy(self.report)
        report["activation"] = {"eligible": True, "reasons": [], "policy": {}}
        serving = CongestionForecaster(train_baseline=False)
        original = serving.model
        with patch.object(training, "_build_candidate", return_value=(report, self.candidate)), patch("services.observed_model_artifact.save_observed_model", side_effect=OSError("sensitive/path")):
            result = training.retrain_observed_model(serving, [], now=NOW, artifact_path="not-written")
        self.assertIs(serving.model, original)
        self.assertFalse(result["model_replaced"])
        self.assertIn("candidate_artifact_persistence_failed", result["activation"]["reasons"])
        self.assertNotIn("sensitive", json.dumps(result))

    def test_parallel_mutations_are_rejected_without_queueing_fits(self):
        serving = CongestionForecaster(train_baseline=False)
        serving._retrain_lock.acquire()
        try:
            with self.assertRaises(training.ObservedTrainingBusy):
                training.retrain_observed_model(serving, [], now=NOW)
        finally:
            serving._retrain_lock.release()

    def test_real_candidate_gate_persistence_activation_and_restart_roundtrip(self):
        serving = CongestionForecaster(train_baseline=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eligible-numeric-model.json"
            result = training.retrain_observed_model(serving, self.rows, now=NOW, artifact_path=path)
            self.assertTrue(result["activation"]["eligible"])
            self.assertTrue(result["model_replaced"])
            self.assertEqual(result["status"], "activated")
            self.assertTrue(result["artifact"]["persisted"])
            self.assertEqual(result["artifact"]["scope"], "single_host")
            self.assertEqual(serving.metrics["observed_evaluation_horizon_minutes"], 30)
            restored = CongestionForecaster(train_baseline=False)
            restore_observed_model(restored, path)
            contexts = [SpatialPredictionContext.from_legacy(18, 0, 0, 0, index) for index in range(5)]
            self.assertEqual(restored.predict_spatial_batch(contexts), serving.predict_spatial_batch(contexts))
            self.assertEqual(restored.metrics["observed_training"]["status"], "activated")
            self.assertTrue(restored.metrics["observed_training"]["model_replaced"])
            self.assertEqual(restored.metrics["artifact"]["durability"], "persistent_local_file")


class NumericArtifactTests(unittest.TestCase):
    def test_numeric_importances_cannot_overflow_or_claim_unbounded_percentages(self):
        tree = {"left": [-1], "right": [-1], "feature": [-2], "threshold": [-2], "value": [20]}
        for weights in ([1e308] * 26, [.1] * 26, [True] + [0] * 25, [-.1] + [0] * 25):
            with self.subTest(weights=weights[:2]), self.assertRaises(ValueError):
                NumericRandomForest([tree], weights)
        constant = NumericRandomForest([tree], [0] * 26)
        self.assertEqual(constant.predict(np.zeros((1, 26))).tolist(), [20])
        normal = NumericRandomForest([tree], [1] + [0] * 25)
        self.assertEqual(float(normal.feature_importances_.sum()), 1)

    @classmethod
    def setUpClass(cls):
        cls.candidate = CongestionForecaster(train_baseline=False)
        cls.candidate.retrain_from_observations(fixture_history(steps=40))
        cls.candidate._interval_padding_points = 1.25
        cls.report = {"activation": {"eligible": True}, "fixture_only": True}

    def test_json_round_trip_reproduces_predictions_and_requires_explicit_restore(self):
        contexts = [SpatialPredictionContext.from_legacy(hour, day, weather, surge, corridor)
                    for hour, day, weather, surge, corridor in ((0, 0, 0, 0, 0), (18, 1, 2, 1, 4), (9.25, 0, 1, 0, 2))]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            metadata = save_observed_model(self.candidate, path, report=self.report)
            self.assertEqual(metadata["format"], "crossflow_observed_rf_numeric_v1")
            self.assertTrue(path.read_text().startswith('{"payload"'))
            loaded = load_observed_model(path)
            self.assertEqual(loaded.predict_spatial_batch(contexts), self.candidate.predict_spatial_batch(contexts))
            serving = CongestionForecaster(train_baseline=False)
            self.assertFalse(serving.is_trained)
            restore_observed_model(serving, path)
            self.assertTrue(serving.is_trained)
            self.assertEqual(serving.predict_spatial_batch(contexts), self.candidate.predict_spatial_batch(contexts))
            self.assertTrue(serving.metrics["artifact"]["restored"])

    def test_checksum_corruption_and_non_json_content_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            save_observed_model(self.candidate, path, report=self.report)
            value = json.loads(path.read_text())
            value["payload"]["interval_padding_points"] = 9
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_observed_model(path)
            path.write_bytes(b'cos\nsystem\n(S"touch never-execute"\ntR.')
            with self.assertRaises(ValueError):
                load_observed_model(path)

    def test_noneligible_or_synthetic_model_is_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "never.json"
            with self.assertRaises(ValueError):
                save_observed_model(self.candidate, path, report={"activation": {"eligible": False}})
            self.assertFalse(path.exists())

    def test_numeric_tree_rejects_cycles_disconnected_nodes_invalid_features_and_nan(self):
        valid = {"left": [1, -1, -1], "right": [2, -1, -1], "feature": [0, -2, -2], "threshold": [.5, -2, -2], "value": [20, 10, 30]}
        for field, index, value in (("left", 0, 0), ("right", 0, 1), ("feature", 0, 26), ("feature", 1, 1), ("threshold", 0, float('nan')), ("value", 1, 101), ("left", 0, True)):
            broken = deepcopy(valid); broken[field][index] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                NumericRegressionTree(broken)
        tree = NumericRegressionTree(valid)
        inputs = np.zeros((2, 26)); inputs[1, 0] = 1
        self.assertEqual(tree.predict(inputs).tolist(), [10, 30])


if __name__ == "__main__":
    unittest.main()
