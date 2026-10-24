"""Explicit scenario history never averages measured or unknown sources."""
from datetime import datetime, timedelta, timezone
import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")
from services import clock, historical_store

NOW = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)


class ModelledHistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = historical_store.HistoryStore(":memory:", now_provider=lambda: NOW)
        self.addCleanup(self.store.close)
        self.store._conn.execute("DELETE FROM observations")
        self.store._conn.commit()

    def insert(self, score, source, *, when=NOW, corridor="corridor-1"):
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO observations (ts, corridor, score, source) VALUES (?, ?, ?, ?)",
                (int(when.timestamp()), corridor, score, source),
            )

    def test_modelled_whitelist_excludes_measurements_and_unfamiliar_source_labels(self):
        self.insert(10, "synthetic")
        self.insert(30, "simulated")
        for source in ("tomtom_live", "verified_traffic_observation", "probe_gps", "modelled", "unknown"):
            self.insert(90, source)
        rows = self.store.get_hourly_profile("corridor-1", now=NOW, modelled_only=True)
        self.assertEqual(rows[16], {"hour": 16, "avg_score": 10., "sample_count": 1})
        daily = self.store.get_weekly_trend("corridor-1", now=NOW, modelled_only=True)
        self.assertEqual(daily, [{"date": "2026-10-24", "avg_score": 10., "sample_count": 1}])
        metadata = self.store.get_history_metadata("corridor-1", now=NOW, modelled_only=True)
        self.assertEqual(metadata["source_counts"], {"synthetic": 1})
        self.assertFalse(metadata["observed"])
        self.assertFalse(metadata["contains_observed_samples"])

    def test_modelled_window_has_matching_hourly_daily_and_metadata_counts(self):
        self.insert(10, "synthetic", when=NOW - timedelta(days=7))
        self.insert(30, "synthetic", when=NOW - timedelta(days=7, seconds=1))
        self.insert(80, "synthetic", when=NOW + timedelta(seconds=1))
        self.insert(90, "synthetic", corridor="corridor-2")
        hourly = self.store.get_hourly_profile("corridor-1", days=7, now=NOW, modelled_only=True)
        daily = self.store.get_weekly_trend("corridor-1", days=7, now=NOW, modelled_only=True)
        metadata = self.store.get_history_metadata("corridor-1", days=7, now=NOW, modelled_only=True)
        self.assertEqual(sum(row["sample_count"] for row in hourly), 1)
        self.assertEqual(sum(row["sample_count"] for row in daily), 1)
        self.assertEqual(metadata["source_counts"], {"synthetic": 1})

    def test_measurement_timestamp_does_not_refresh_scenario_metadata(self):
        self.insert(20, "synthetic", when=NOW - timedelta(hours=2))
        self.insert(95, "tomtom_live")
        metadata = self.store.get_history_metadata("corridor-1", now=NOW, modelled_only=True)
        self.assertEqual(metadata["latest_sample_at"], clock.iso(NOW - timedelta(hours=2)))
        self.assertEqual(metadata["latest_sample_age_seconds"], 7200)

    def test_missing_scenario_buckets_stay_empty_not_zero(self):
        self.insert(90, "tomtom_live")
        rows = self.store.get_hourly_profile("corridor-1", now=NOW, modelled_only=True)
        self.assertTrue(all(row["avg_score"] is None and row["sample_count"] == 0 for row in rows))
        self.assertEqual(self.store.get_weekly_trend("corridor-1", now=NOW, modelled_only=True), [])
        self.assertEqual(self.store.get_history_metadata("corridor-1", now=NOW, modelled_only=True)["freshness"], "empty")

    def test_legacy_mixed_selection_is_preserved_without_new_flag(self):
        for score, source in ((10, "synthetic"), (20, "simulated"), (90, "tomtom_live")):
            self.insert(score, source)
        row = self.store.get_hourly_profile("corridor-1", now=NOW)[16]
        self.assertEqual(row, {"hour": 16, "avg_score": 40., "sample_count": 3})
        self.assertTrue(self.store.get_history_metadata("corridor-1", now=NOW)["contains_observed_samples"])

    def test_cold_runtime_initializes_a_current_versioned_scenario_window(self):
        cold = historical_store.HistoryStore(":memory:", now_provider=lambda: NOW)
        self.addCleanup(cold.close)
        rows = cold.get_hourly_profile("corridor-1", days=7, now=NOW, modelled_only=True)
        metadata = cold.get_history_metadata("corridor-1", days=7, now=NOW, modelled_only=True)
        self.assertTrue(all(row["avg_score"] is not None and row["sample_count"] > 0 for row in rows))
        self.assertEqual(metadata["synthetic_seed"]["version"], historical_store.SYNTHETIC_SEED_VERSION)
        self.assertEqual(metadata["synthetic_seed"]["generated_for_date"], NOW.astimezone(clock.BATAM_TZ).date().isoformat())
        self.assertEqual(set(metadata["source_counts"]), {"synthetic"})
        self.assertFalse(metadata["contains_observed_samples"])

    def test_api_explicit_modelled_flag_excludes_observed_rows_and_never_records_samples(self):
        api = importlib.import_module("main")
        self.insert(12, "synthetic")
        self.insert(88, "tomtom_live")
        with patch.object(historical_store, "_store", self.store), clock.frozen(NOW), \
                patch.object(api, "get_observed_history", side_effect=AssertionError("measured path called")), \
                patch.object(historical_store, "record", side_effect=AssertionError("scenario read wrote history")):
            for include_modelled in (False, True):
                with self.subTest(include_modelled=include_modelled):
                    response = api.api_historical_congestion(
                        "corridor-1", 7, include_modelled=include_modelled, modelled_only=True,
                    )
                    self.assertEqual(response["data_source"], "simulated")
                    self.assertEqual(response["hourly_profile"][16]["avg_score"], 12.)
                    self.assertEqual(response["history_metadata"]["selection_scope"], "modelled_only")
                    self.assertEqual(response["history_metadata"]["sample_count"], 1)
                    self.assertFalse(response["history_metadata"]["contains_observed_samples"])
                    self.assertIn("not observed", response["provenance"]["traffic"])


if __name__ == "__main__":
    unittest.main()
