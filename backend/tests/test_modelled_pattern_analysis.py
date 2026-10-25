"""Descriptive scenario patterns use existing selected samples without fitting."""
from datetime import datetime, timedelta, timezone
import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")
from services import clock, historical_store

NOW = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)  #Saturday16:00WIB.


class ModelledPatternAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.store = historical_store.HistoryStore(":memory:", now_provider=lambda: NOW)
        self.addCleanup(self.store.close)
        with self.store._conn:
            self.store._conn.execute("DELETE FROM observations")

    def insert(self, score, *, source="synthetic", when=NOW, corridor="corridor-1"):
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO observations (ts,corridor,score,source) VALUES (?,?,?,?)",
                (int(when.timestamp()), corridor, score, source),
            )

    def analysis(self, days=7):
        return self.store.get_modelled_analysis("corridor-1", days=days, now=NOW)

    def test_empirical_spread_and_means_are_sample_weighted_with_unequal_daily_counts(self):
        friday = NOW - timedelta(days=1)
        self.insert(10., when=friday)
        self.insert(30., when=friday + timedelta(minutes=10))
        self.insert(90.)
        result = self.analysis()
        self.assertEqual(result["version"], "modelled_pattern_analysis_v1")
        self.assertEqual(result["basis"], "synthetic_scenario_samples")
        self.assertFalse(result["observed"])
        self.assertEqual(result["spread_method"], "empirical_linear_interpolation")
        self.assertEqual(result["spread_interpretation"], "descriptive_scenario_spread_not_calibrated_confidence")
        all_days = result["day_profiles"]["all"]
        self.assertEqual(all_days["weighted_mean_score"], 43.3)
        self.assertEqual(all_days["sample_count"], 3)
        self.assertEqual(all_days["day_count"], 2)
        self.assertEqual(all_days["hourly_profile"][16], {
            "hour": 16, "avg_score": 43.3, "p10_score": 14., "p90_score": 78., "sample_count": 3, "day_count": 2,
        })
        self.assertEqual(result["day_profiles"]["weekday"]["weighted_mean_score"], 20.)
        self.assertEqual(result["day_profiles"]["weekday"]["hourly_profile"][16]["p10_score"], 12.)
        self.assertEqual(result["day_profiles"]["weekday"]["hourly_profile"][16]["p90_score"], 28.)
        self.assertEqual(result["day_profiles"]["weekend"]["weighted_mean_score"], 90.)
        self.assertEqual(result["coverage"], {
            "sample_count": 3, "day_count": 2, "hour_count": 1,
            "first_sample_at": clock.iso(friday), "last_sample_at": clock.iso(NOW), "requested_days": 7,
        })

    def test_weekend_and_hour_buckets_use_batam_civil_time(self):
        friday_utc = datetime(2026, 10, 23, 18, 30, tzinfo=timezone.utc)
        self.insert(35., when=friday_utc)
        result = self.analysis()
        self.assertEqual(result["timezone"], "Asia/Jakarta")
        self.assertEqual(result["day_profiles"]["weekday"]["sample_count"], 0)
        self.assertEqual(result["day_profiles"]["weekend"]["hourly_profile"][1]["sample_count"], 1)
        self.assertEqual(result["day_profiles"]["weekend"]["hourly_profile"][1]["p10_score"], 35.)
        self.assertEqual(result["day_profiles"]["weekend"]["hourly_profile"][1]["p90_score"], 35.)

    def test_exact_window_whitelist_and_score_validation_exclude_unsupported_rows(self):
        cutoff = NOW - timedelta(days=7)
        self.insert(10., when=cutoff)
        self.insert(20., when=cutoff - timedelta(seconds=1))
        self.insert(30., when=NOW + timedelta(seconds=1))
        self.insert(40., corridor="corridor-2")
        for source in ("simulated", "tomtom_live", "verified_traffic_observation", "unknown", "modelled"):
            self.insert(90., source=source)
        for invalid in (-1., 101., float("inf"), "invalid"):
            self.insert(invalid)
        result = self.analysis()
        self.assertEqual(result["coverage"]["sample_count"], 1)
        self.assertEqual(result["coverage"]["first_sample_at"], clock.iso(cutoff))
        self.assertEqual(result["coverage"]["last_sample_at"], clock.iso(cutoff))
        self.assertEqual(result["day_profiles"]["all"]["weighted_mean_score"], 10.)

    def test_empty_buckets_do_not_extrapolate_and_read_never_generates_samples(self):
        self.insert(90., source="tomtom_live")
        before = self.store._conn.total_changes
        with patch.object(self.store, "_migrate_synthetic_seed", side_effect=AssertionError("read generated a seed")), \
                patch.object(self.store, "record", side_effect=AssertionError("read recorded a sample")):
            result = self.analysis()
        self.assertEqual(self.store._conn.total_changes, before)
        for profile in result["day_profiles"].values():
            self.assertEqual(profile["sample_count"], 0)
            self.assertEqual(profile["day_count"], 0)
            self.assertIsNone(profile["weighted_mean_score"])
            self.assertEqual(len(profile["hourly_profile"]), 24)
            self.assertTrue(all(row["sample_count"] == row["day_count"] == 0
                                and row["avg_score"] is row["p10_score"] is row["p90_score"] is None
                                for row in profile["hourly_profile"]))
        self.assertIsNone(result["coverage"]["first_sample_at"])
        self.assertIsNone(result["coverage"]["last_sample_at"])
        self.assertEqual(result["coverage"]["hour_count"], 0)

    def test_analysis_is_added_only_to_explicit_modelled_only_api_responses(self):
        api = importlib.import_module("main")
        self.insert(25.)
        with patch.object(historical_store, "_store", self.store), clock.frozen(NOW):
            modelled = api.api_historical_congestion("corridor-1", 7, modelled_only=True)
            mixed = api.api_historical_congestion("corridor-1", 7, include_modelled=True)
            with patch.object(api, "get_observed_history", return_value={"history_metadata": {"selection_scope": "observed_only"}}):
                observed = api.api_historical_congestion("corridor-1", 7)
        self.assertEqual(modelled["modelled_analysis"]["coverage"]["sample_count"], 1)
        self.assertNotIn("modelled_analysis", mixed)
        self.assertNotIn("modelled_analysis", observed)

    def test_modelled_api_fields_share_the_same_valid_score_selection(self):
        api = importlib.import_module("main")
        self.insert(25.)
        for invalid in (-1., 101., float("inf"), "invalid"):
            self.insert(invalid)
        with patch.object(historical_store, "_store", self.store), clock.frozen(NOW):
            result = api.api_historical_congestion("corridor-1", 7, modelled_only=True)
        self.assertEqual(result["history_metadata"]["sample_count"], 1)
        self.assertEqual(result["modelled_analysis"]["coverage"]["sample_count"], 1)
        self.assertEqual(result["hourly_profile"][16]["sample_count"], 1)
        self.assertEqual(result["hourly_profile"][16]["avg_score"], 25.)

    def test_invalid_windows_are_rejected(self):
        for days in (0, 31, True, 1.5):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.analysis(days)


if __name__ == "__main__":
    unittest.main()
