"""Leakage and missingness contracts for the offline research benchmark."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/research_forecasting.py"
SPEC = importlib.util.spec_from_file_location("crossflow_research_forecasting", SCRIPT)
forecast = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = forecast
SPEC.loader.exec_module(forecast)


def grid(values: list[float]) -> object:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return forecast.Grid([start + timedelta(hours=i) for i in range(len(values))], ["9"],
                         np.asarray(values, dtype=float)[:, None], len(values), 0, 0)


class ResearchForecastingTests(unittest.TestCase):
    def test_loader_distinguishes_source_zero_null_and_absent_key(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.csv"
            source.write_text("arc_id,timestamp_end_utc,volume_vehicles_hour\n"
                              "9,2026-08-01T00:00:00Z,0\n"
                              "9,2026-08-01T01:00:00Z,\n"
                              "9,2026-08-01T03:00:00Z,8\n")
            loaded = forecast.load_grid(source)
            self.assertEqual(loaded.values[0, 0], 0)
            self.assertTrue(np.isnan(loaded.values[1, 0]))
            self.assertTrue(np.isnan(loaded.values[2, 0]))
            self.assertEqual(loaded.source_null_targets, 1)
            self.assertEqual(loaded.absent_source_keys, 1)

    def test_current_and_future_values_never_affect_current_features(self):
        original = grid(list(range(250)))
        changed = grid(list(range(250)))
        changed.values[190:] = 100000
        first, _, _ = forecast.causal_features(original)
        second, _, _ = forecast.causal_features(changed)
        np.testing.assert_allclose(first[:191], second[:191], equal_nan=True)
        self.assertFalse(np.array_equal(first[191:], second[191:], equal_nan=True))

    def test_gap_is_not_replaced_by_previous_available_observation(self):
        values = grid([1, 2, np.nan, 4, 5])
        features, names, lag_columns = forecast.causal_features(values)
        self.assertTrue(np.isnan(features[3, lag_columns[1]]))
        self.assertEqual(features[3, lag_columns[2]], 2)
        self.assertEqual(features[3, names.index("q_prior_3h_mean")], 1.5)
        self.assertAlmostEqual(features[3, names.index("q_prior_3h_missing_fraction")], 1 / 3)

    def test_fit_profile_ignores_held_out_target_values(self):
        first = grid([1, 3, 5, 7, 9, 10000])
        second = grid([1, 3, 5, 7, 9, 999999])
        mask = np.array([True] * 5 + [False])
        self.assertEqual(forecast.fit_profile(first, mask), forecast.fit_profile(second, mask))
        self.assertEqual(forecast.fit_profile(first, mask)["global_median"], 5)

    def test_missing_target_is_not_made_zero(self):
        start = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)
        data = forecast.Grid([start + timedelta(hours=i) for i in range(600)], ["9"],
                             np.full((600, 1), 3.), 600, 0, 0)
        data.values[500, 0] = np.nan
        masks = forecast.split_masks(data)
        self.assertFalse(any(mask[500] for mask in masks.values()))
        self.assertTrue(np.isnan(data.values[500, 0]))

    def test_splits_are_strictly_chronological_and_disjoint(self):
        data = grid([3.] * (61 * 24))
        masks = forecast.split_masks(data)
        train, validation, test = [np.flatnonzero(masks[name]) for name in ("train", "validation", "test")]
        self.assertLess(train[-1], validation[0])
        self.assertLess(validation[-1], test[0])
        self.assertFalse(np.any(masks["train"] & masks["test"]))

    def test_histogram_boosting_has_no_random_early_stopping_split(self):
        for name, estimator in forecast.make_candidates(28).items():
            if name.startswith("hist_gradient"):
                self.assertFalse(estimator.early_stopping)


if __name__ == "__main__":
    unittest.main()
