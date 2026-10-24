"""Causal forecasting regressions using explicitly artificial unit fixtures.

These generated observations never enter a history database or substantiate
measured Batam accuracy. No provider, network service or serving model is used.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import causal_forecasting as causal
from services.traffic_observations import (
    ObservationConflictError, SpatialTrafficObservation, estimate_speed_ratio_congestion,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = START + timedelta(days=20)


def fixture_history(steps=9 * 24 * 4 + 1, *, source="probe_gps", constant=False):
    rows = []
    for step in range(steps):
        for index in range(5):
            # Calendar signal plus persistent changes; entirely artificial.
            score = 35.0 if constant else 38 + 19 * np.sin(2 * np.pi * step / 96) + index * 2
            rows.append(SpatialTrafficObservation.create(
                corridor_id=f"corridor-{index + 1}",
                observed_at=START + timedelta(minutes=15 * step),
                latitude=1.10 + index * .01, longitude=104.0 + index * .01,
                actual_speed_kph=50 * (1 - score / 100), free_flow_speed_kph=50,
                source=source, road_class="primary", upstream_event_id=f"unit-only-{step}-{index}",
                validation_now=NOW,
            ))
    return rows


def available(rows):
    return {row.observation_key: row.observed_at for row in rows}


def altered(row, **overrides):
    arguments = {
        "corridor_id": row.corridor_id, "observed_at": row.observed_at,
        "latitude": row.latitude, "longitude": row.longitude,
        "actual_speed_kph": row.actual_speed_kph, "free_flow_speed_kph": row.free_flow_speed_kph,
        "source": row.source, "road_class": row.road_class,
        "upstream_event_id": row.upstream_event_id, "validation_now": NOW,
    }
    arguments.update(overrides)
    return SpatialTrafficObservation.create(**arguments)


class CausalSelectionTests(unittest.TestCase):
    def test_insufficient_archive_does_not_construct_an_estimator(self):
        rows = fixture_history(steps=20)
        with patch.object(causal, "_fit_estimator") as fit:
            report = causal.evaluate_causal_training(rows, now=NOW, availability_times=available(rows))
        fit.assert_not_called()
        self.assertFalse(report["training_performed"])
        self.assertFalse(report["promotion_eligible"])
        self.assertIn("observed_span_below_7_days", report["reasons"])

    def test_provider_training_is_blocked_by_default_before_fit(self):
        rows = fixture_history(steps=10, source="tomtom_live")
        with patch.object(causal, "_fit_estimator") as fit:
            report = causal.evaluate_causal_training(rows, now=NOW, availability_times=available(rows))
        fit.assert_not_called()
        self.assertEqual(report["status"], "provider_training_not_authorized")
        self.assertEqual(report["selection"]["source_counts"], {"tomtom_live": 50})
        self.assertEqual(report["selection"]["selected_rows"], 0)
        allowed = causal.evaluate_causal_training(rows, now=NOW, availability_times=available(rows), allow_provider_training=True)
        self.assertEqual(allowed["selection"]["selected_rows"], 50)

    def test_excludes_modelled_missing_availability_and_future_rows(self):
        rows = fixture_history(steps=3)
        modeled = fixture_history(steps=1, source="synthetic")
        future = altered(rows[0], observed_at=NOW + timedelta(seconds=1), upstream_event_id="unit-only-future")
        times = available(rows[1:])
        report = causal.evaluate_causal_training(rows + rows + modeled + [future], now=NOW, availability_times=times)
        self.assertEqual(report["selection"]["duplicate_rows"], len(rows))
        self.assertEqual(report["selection"]["excluded"], {"missing_availability": 1, "non_observed": 5, "future_observation": 1})
        self.assertEqual(report["selection"]["selected_rows"], len(rows) - 1)

    def test_bound_is_checked_before_record_validation_and_bad_clocks_fail(self):
        consumed = 0
        def endless():
            nonlocal consumed
            while True:
                consumed += 1
                yield object()
        with self.assertRaisesRegex(ValueError, "limited to3|limited to 3"):
            causal.evaluate_causal_training(endless(), max_rows=3, now=NOW)
        self.assertEqual(consumed, 4)
        for value in (True, 0, 100001, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                causal.evaluate_causal_training([], now=NOW, max_rows=value)
        with self.assertRaises(ValueError):
            causal.evaluate_causal_training([], now=NOW.replace(tzinfo=None))
        row = fixture_history(steps=1)[0]
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            causal.evaluate_causal_training([row], now=NOW, availability_times={row.observation_key: START.replace(tzinfo=None)})

    def test_conflicting_identity_and_forged_provenance_fail(self):
        row = fixture_history(steps=1)[0]
        with self.assertRaises(ObservationConflictError):
            causal.evaluate_causal_training([row, altered(row, actual_speed_kph=0)], now=NOW)
        with self.assertRaises(ValueError):
            causal.evaluate_causal_training([replace(row, observed=False)], now=NOW)

    def test_timestamp_groups_and_four_purges(self):
        partitions = causal._partitions(fixture_history())
        names = list(partitions)
        for earlier, later in zip(names, names[1:]):
            self.assertLess(partitions[earlier][-1].observed_at,
                            partitions[later][0].observed_at - timedelta(minutes=60))
        for rows in partitions.values():
            for at in {row.observed_at for row in rows}:
                self.assertEqual(sum(row.observed_at == at for row in rows), 5)


class CausalFeatureTests(unittest.TestCase):
    def setUp(self):
        self.rows = fixture_history(steps=20)
        self.target = self.rows[-5]

    def test_target_speed_reference_class_and_capacity_never_become_features(self):
        original, _ = causal._examples(self.rows, [self.target], 30, available(self.rows))
        replacement = altered(self.target, actual_speed_kph=0, free_flow_speed_kph=110,
                              road_class="motorway", capacity_vph=9000)
        changed = [replacement if row is self.target else row for row in self.rows]
        result, _ = causal._examples(changed, [replacement], 30, available(changed))
        self.assertEqual(original[0].features, result[0].features)
        self.assertNotEqual(original[0].actual, result[0].actual)
        self.assertEqual(result[0].prior_free_flow, 50)

    def test_late_current_or_lag_never_enters_features(self):
        origin = self.target.observed_at - timedelta(minutes=30)
        same = [row for row in self.rows if row.corridor_id == self.target.corridor_id]
        late = next(row for row in same if row.observed_at == origin)
        times = available(self.rows)
        times[late.observation_key] = origin + timedelta(seconds=1)
        before, _ = causal._examples(self.rows, [self.target], 30, times)
        changed_late = altered(late, actual_speed_kph=0)
        changed = [changed_late if row is late else row for row in self.rows]
        after, _ = causal._examples(changed, [self.target], 30, times)
        self.assertEqual(before[0].features, after[0].features)
        self.assertEqual(before[0].features[1], 15)
        self.assertEqual(before[0].features[19], 30)
        self.assertEqual(len(before[0].features), len(causal.FEATURE_NAMES))

    def test_stale_current_gap_is_skipped_and_locations_cannot_cross(self):
        target = altered(self.target, observed_at=self.target.observed_at + timedelta(hours=5), upstream_event_id="unit-only-gap")
        rows = self.rows + [target]
        examples, skipped = causal._examples(rows, [target], 30, available(rows))
        self.assertEqual(examples, [])
        self.assertEqual(skipped, {"missing_or_stale_current": 1})
        moved = altered(self.target, longitude=104.2)
        rows = self.rows[:-5] + [moved]
        examples, _ = causal._examples(rows, [moved], 30, available(rows))
        self.assertEqual(examples, [])

    def test_past_perturbation_changes_forecast_features(self):
        origin = self.target.observed_at - timedelta(minutes=30)
        current = next(row for row in self.rows if row.corridor_id == self.target.corridor_id and row.observed_at == origin)
        changed = [altered(row, actual_speed_kph=0) if row is current else row for row in self.rows]
        before, _ = causal._examples(self.rows, [self.target], 30, available(self.rows))
        after, _ = causal._examples(changed, [self.target], 30, available(changed))
        self.assertNotEqual(before[0].features[0], after[0].features[0])
        self.assertNotEqual(before[0].persistence, after[0].persistence)

    def test_fast_canonical_baseline_matches_existing_validated_estimator(self):
        same = [row for row in self.rows if row.corridor_id == self.target.corridor_id]
        for index in (0, 3, 19):
            expected = estimate_speed_ratio_congestion(same[index], same[:index]).congestion_score
            self.assertEqual(causal._canonical_history_blend(same[index], same[:index]), expected)

    def test_target_label_arriving_at_or_after_freeze_is_excluded(self):
        freeze = self.target.observed_at + timedelta(hours=1)
        times = available(self.rows)
        for arrival in (freeze, freeze + timedelta(seconds=1)):
            times[self.target.observation_key] = arrival
            examples, skipped = causal._examples(self.rows, [self.target], 30, times,
                                                 label_available_before=freeze)
            self.assertEqual(examples, [])
            self.assertEqual(skipped, {"late_label_at_partition_freeze": 1})
        times[self.target.observation_key] = freeze - timedelta(microseconds=1)
        examples, _ = causal._examples(self.rows, [self.target], 30, times,
                                      label_available_before=freeze)
        self.assertEqual(len(examples), 1)


class CausalTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = fixture_history()
        cls.report = causal.evaluate_causal_training(cls.rows, now=NOW, availability_times=available(cls.rows))

    def test_real_estimator_experiment_has_separate_windows_baselines_and_units(self):
        self.assertTrue(self.report["training_performed"])
        self.assertFalse(self.report["model_replaced"])
        self.assertFalse(self.report["artifact_saved"])
        self.assertEqual(len(self.report["horizons"]), 3)
        for horizon in self.report["horizons"]:
            self.assertEqual(horizon["status"], "complete")
            self.assertGreater(horizon["candidate"]["samples"], 30)
            self.assertGreaterEqual(horizon["speed"]["mae_kph"], 0)
            self.assertEqual(horizon["interval"]["nominal_coverage"], .8)
            self.assertFalse(horizon["interval"]["coverage_guaranteed"])
            self.assertEqual(set(horizon["per_corridor"]), set(causal.CORRIDORS))
            self.assertEqual(len(horizon["model"]["model_sha256"]), 64)
        json.dumps(self.report, allow_nan=False)

    def test_holdout_perturbation_cannot_change_models_selection_or_calibration(self):
        start = datetime.fromisoformat(self.report["splits"]["holdout"]["start_at"])
        changed = [altered(row, actual_speed_kph=0) if row.observed_at >= start else row for row in self.rows]
        result = causal.evaluate_causal_training(changed, now=NOW, availability_times=available(changed))
        for original, new in zip(self.report["horizons"], result["horizons"]):
            self.assertEqual(original["model"], new["model"])
            self.assertEqual(original["validation_candidates"], new["validation_candidates"])
            self.assertEqual(original["interval"]["radius_points"], new["interval"]["radius_points"])
        self.assertFalse(result["promotion_eligible"])

    def test_unverified_availability_cannot_promote(self):
        result = causal.evaluate_causal_training(self.rows, now=NOW)
        self.assertTrue(result["training_performed"])
        self.assertFalse(result["promotion_eligible"])
        self.assertIn("availability_unverified_promotion_blocked", result["reasons"])

    def test_zero_error_persistence_cannot_be_called_improved(self):
        rows = fixture_history(constant=True)
        result = causal.evaluate_causal_training(rows, now=NOW, availability_times=available(rows))
        self.assertTrue(result["training_performed"])
        self.assertFalse(result["promotion_eligible"])
        self.assertTrue(any("not_better_than_persistence" in reason for reason in result["reasons"]))

    def test_delayed_training_selection_and_calibration_labels_cannot_change_fit(self):
        partitions = causal._partitions(self.rows)
        delayed = [values[len(values) // 2] for name, values in partitions.items() if name != "holdout"]
        times = available(self.rows)
        for row in delayed:
            times[row.observation_key] = NOW
        original = causal.evaluate_causal_training(self.rows, now=NOW, availability_times=times)
        keys = {row.observation_key for row in delayed}
        changed = [altered(row, actual_speed_kph=0) if row.observation_key in keys else row for row in self.rows]
        result = causal.evaluate_causal_training(changed, now=NOW, availability_times=times)
        self.assertEqual(original["selection"]["identity_list_sha256"], result["selection"]["identity_list_sha256"])
        self.assertNotEqual(original["selection"]["canonical_payload_and_availability_sha256"],
                            result["selection"]["canonical_payload_and_availability_sha256"])
        for first, second in zip(original["horizons"], result["horizons"]):
            self.assertEqual(first["model"], second["model"])
            self.assertEqual(first["validation_candidates"], second["validation_candidates"])
            self.assertEqual(first["interval"]["radius_points"], second["interval"]["radius_points"])
            for name in ("fit", "validation", "calibration"):
                self.assertEqual(first["skipped"][name]["late_label_at_partition_freeze"], 1)


if __name__ == "__main__":
    unittest.main()
