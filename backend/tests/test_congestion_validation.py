"""Regression tests for bounded inference and leakage-free forecast evaluation."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.congestion_model import CongestionForecaster, SpatialPredictionContext
from services.congestion_validation import backtest_congestion, prediction_invariant_failures
from services.traffic_observations import SpatialTrafficObservation


def observations(*, source="probe_gps", steps=200):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [SpatialTrafficObservation.create(
        corridor_id=f"corridor-{corridor + 1}",
        observed_at=start + timedelta(minutes=15 * step),
        latitude=1.10, longitude=104.0,
        actual_speed_kph=20 + step % 15, free_flow_speed_kph=50,
        source=source, road_class="primary", capacity_vph=1800,
        upstream_event_id=f"sample-{step}-{corridor}",
    ) for step in range(steps) for corridor in range(5)]


class _Candidate:
    def __init__(self):
        self.training = []
        self.contexts = []

    def retrain_from_observations(self, rows, **_kwargs):
        self.training = list(rows)
        return {"calibration": {"holdout_order": "chronological_observed_at"}}

    def predict_spatial_batch(self, contexts):
        self.contexts.extend(contexts)
        return [SimpleNamespace(mean_score=20, p10_score=10, p90_score=30) for _ in contexts]


class BacktestBoundaryTests(unittest.TestCase):
    def test_empty_or_modelled_history_does_not_claim_observed_accuracy(self):
        for rows in ([], observations(source="synthetic")):
            result = backtest_congestion(rows)
            self.assertEqual(result["status"], "insufficient_observations")
            self.assertEqual(result["observed_rows"], 0)
            self.assertEqual(result["horizons"], [])

    def test_training_is_strictly_before_first_origin_and_timestamp_groups_stay_together(self):
        candidate = _Candidate()
        with patch("services.congestion_validation.CongestionForecaster", return_value=candidate):
            result = backtest_congestion(reversed(observations()))
        self.assertEqual(result["status"], "complete")
        first_origin = datetime.fromisoformat(result["training_input_end_exclusive"])
        self.assertTrue(all(row.observed_at < first_origin for row in candidate.training))
        self.assertEqual(result["outer_holdout_rows"], 200)
        self.assertEqual([item["forest"]["samples"] for item in result["horizons"]], [200, 200, 200])
        self.assertFalse(result["model_replaced"])

    def test_targets_cannot_supply_future_speed_or_future_road_metadata_to_predictor(self):
        rows = observations()
        # First outer target's metadata is deliberately changed. The candidate
        # must see the prior available record's metadata, not these values.
        target = rows[800]
        replacement = SpatialTrafficObservation.create(
            corridor_id=target.corridor_id, observed_at=target.observed_at,
            latitude=target.latitude, longitude=target.longitude,
            actual_speed_kph=0, free_flow_speed_kph=110,
            source=target.source, road_class="motorway", capacity_vph=7900,
            upstream_event_id=target.upstream_event_id,
        )
        rows[800] = replacement
        candidate = _Candidate()
        with patch("services.congestion_validation.CongestionForecaster", return_value=candidate):
            backtest_congestion(rows, horizons_minutes=(15,))
        first = candidate.contexts[0]
        self.assertEqual(first.free_flow_speed_kph, 50)
        self.assertEqual(first.road_class, "primary")
        self.assertEqual(first.capacity_vph, 1800)
        self.assertEqual(first.hour_float, target.local_observed_at.hour)

    def test_heldout_target_cannot_leak_into_persistence_or_history_blend(self):
        rows = observations()
        # Every held-out row is stationary, while prior rows are moving. The
        # first forecast's persistence error must stay >0; using its target
        # speed as "current" would spuriously produce zero error.
        for index in range(800, len(rows)):
            row = rows[index]
            rows[index] = SpatialTrafficObservation.create(
                corridor_id=row.corridor_id, observed_at=row.observed_at,
                latitude=row.latitude, longitude=row.longitude,
                actual_speed_kph=0, free_flow_speed_kph=50,
                source=row.source, upstream_event_id=row.upstream_event_id,
            )
        with patch("services.congestion_validation.CongestionForecaster", return_value=_Candidate()):
            result = backtest_congestion(rows, horizons_minutes=(60,))
        self.assertGreater(result["horizons"][0]["persistence"]["mae_points"], 0)
        self.assertGreater(result["horizons"][0]["current_history_blend"]["mae_points"], 0)

    def test_exact_duplicate_replays_do_not_multiply_backtest_samples(self):
        rows = observations()
        with patch("services.congestion_validation.CongestionForecaster", return_value=_Candidate()):
            result = backtest_congestion(rows + rows)
        self.assertEqual(result["input_rows"], 2000)
        self.assertEqual(result["unique_rows"], 1000)
        self.assertEqual(result["outer_holdout_rows"], 200)

    def test_sparse_current_data_returns_explicit_insufficiency(self):
        rows = observations(steps=200)[::5]
        # One sample every day cannot support a <=30-minute current snapshot.
        start = rows[0].observed_at
        spaced = [SpatialTrafficObservation.create(
            corridor_id=row.corridor_id, observed_at=start + timedelta(hours=index * 2),
            latitude=row.latitude, longitude=row.longitude,
            actual_speed_kph=30, free_flow_speed_kph=50, source=row.source,
            upstream_event_id=row.upstream_event_id,
        ) for index, row in enumerate(rows)]
        with patch("services.congestion_validation.CongestionForecaster", return_value=_Candidate()):
            result = backtest_congestion(spaced, horizons_minutes=(15, 30, 60))
        self.assertEqual(result["status"], "insufficient_current_observations")
        self.assertTrue(all(item["forest"]["mae_points"] is None for item in result["horizons"]))

    def test_other_location_in_same_corridor_cannot_be_used_as_current(self):
        rows = observations()
        for index in range(800, len(rows)):
            row = rows[index]
            rows[index] = SpatialTrafficObservation.create(
                corridor_id=row.corridor_id, observed_at=row.observed_at,
                latitude=1.15, longitude=104.1,
                actual_speed_kph=row.actual_speed_kph, free_flow_speed_kph=50,
                source=row.source, upstream_event_id=row.upstream_event_id,
            )
        with patch("services.congestion_validation.CongestionForecaster", return_value=_Candidate()):
            result = backtest_congestion(rows, horizons_minutes=(60,))
        # The first hour at the new location has no prior point there. Later
        # holdout measurements may legitimately become current observations.
        self.assertEqual(result["horizons"][0]["skipped_missing_or_stale_current"], 20)

    def test_bound_checked_before_consuming_infinite_input(self):
        consumed = 0
        def stream():
            nonlocal consumed
            while True:
                consumed += 1
                yield None
        with patch("services.congestion_validation.MAX_BACKTEST_ROWS", 5):
            with self.assertRaisesRegex(ValueError, "limited to 5"):
                backtest_congestion(stream())
        self.assertEqual(consumed, 6)

    def test_invalid_horizons_are_rejected(self):
        for value in ((), (True,), (0,), (181,), (15, 15), (1.5,)):
            with self.assertRaises(ValueError):
                backtest_congestion([], horizons_minutes=value)


class BatchInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = CongestionForecaster()

    def test_mixed_batch_matches_individual_predictions_exactly(self):
        contexts = [SpatialPredictionContext.from_legacy(hour, day, weather, surge, corridor)
                    for hour, day, weather, surge, corridor in (
                        (0, 0, 0, 0, 0), (23.99, 1, 2, 1, 4), (18, 0, 1, 1, 2))]
        contexts.extend((replace(contexts[0], risk_aversion=2.0),
                         replace(contexts[1], minutes_until_ferry_departure=45.0)))
        expected = [self.model.predict_spatial(item) for item in contexts]
        actual = self.model.predict_spatial_batch(contexts)
        self.assertEqual(actual, expected)
        self.assertEqual(prediction_invariant_failures(actual), {})

    def test_empty_batch_and_oversize_stream_are_bounded(self):
        self.assertEqual(self.model.predict_spatial_batch(()), [])
        consumed = 0
        def stream():
            nonlocal consumed
            while True:
                consumed += 1
                yield None
        with patch("models.congestion_model.MAX_SPATIAL_PREDICTION_BATCH", 3):
            with self.assertRaises(ValueError):
                self.model.predict_spatial_batch(stream())
        self.assertEqual(consumed, 4)

    def test_batch_uses_one_tree_snapshot_and_rejects_wrong_inputs(self):
        context = SpatialPredictionContext.from_legacy(8, 0, 0, 0, 0)
        with patch.object(self.model, "_tree_predictions", wraps=self.model._tree_predictions) as trees:
            self.model.predict_spatial_batch((context,) * 20)
        self.assertEqual(trees.call_count, 1)
        with self.assertRaises(TypeError):
            self.model.predict_spatial_batch([context, object()])

    def test_emissions_cache_changes_after_retraining_revision(self):
        from services import simulator
        fake_model = SimpleNamespace(metrics={"last_trained_at": "revision-1"})
        fake_model.predict_spatial = lambda _: SimpleNamespace(mean_score=20.0)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        simulator._cached_score.cache_clear()
        try:
            with patch.object(simulator, "forecaster", fake_model):
                first = simulator.co2_accrued_today(now)
                fake_model.predict_spatial = lambda _: SimpleNamespace(mean_score=80.0)
                still_cached = simulator.co2_accrued_today(now)
                self.assertEqual(first, still_cached)
                fake_model.metrics = {"last_trained_at": "revision-2"}
                updated = simulator.co2_accrued_today(now)
                self.assertGreater(updated["projected_full_day_kg"], first["projected_full_day_kg"])
        finally:
            simulator._cached_score.cache_clear()


if __name__ == "__main__":
    unittest.main()
