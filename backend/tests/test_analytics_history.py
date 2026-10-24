"""Only canonical measurements may populate operational history charts."""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from services import analytics_history, historical_store, routing_intelligence_store
from services.traffic_observations import SpatialTrafficObservation


NOW = datetime(2026, 10, 23, 1, 30, tzinfo=timezone.utc)


def observation(*, when=NOW, speed=30, source="probe_gps", corridor="corridor-1"):
    return SpatialTrafficObservation.create(
        corridor_id=corridor, observed_at=when,
        latitude=1.13, longitude=104.05,
        actual_speed_kph=speed, free_flow_speed_kph=60,
        source=source, validation_now=when,
    )


class ObservedAnalyticsHistoryTests(unittest.TestCase):
    def setUp(self):
        mode = patch.object(routing_intelligence_store, "shared_store_enabled", return_value=False)
        mode.start()
        self.addCleanup(mode.stop)

    def profile(self, records, days=7):
        with patch.object(historical_store, "get_spatial_training_dataset", return_value=records) as read:
            result = analytics_history.get_observed_history("corridor-1", days, NOW)
        read.assert_called_once_with(limit=analytics_history.MAX_HISTORY_SNAPSHOT_ROWS, now=NOW)
        return result

    def test_legacy_synthetic_seed_never_becomes_observed_history(self):
        store = historical_store.HistoryStore(":memory:", now_provider=lambda: NOW)
        self.addCleanup(store.close)
        store.record("corridor-1", 95, source="synthetic_profile_seed_v1", now=NOW)
        with patch.object(historical_store, "_store", store):
            result = analytics_history.get_observed_history("corridor-1", 7, NOW)
        self.assertEqual(len(result["hourly_profile"]), 24)
        self.assertTrue(all(row["avg_score"] is None and row["sample_count"] == 0 for row in result["hourly_profile"]))
        self.assertEqual(result["weekly_trend"], [])
        metadata = result["history_metadata"]
        self.assertEqual(metadata["selection_scope"], "observed_only")
        self.assertEqual(metadata["sample_count"], 0)
        self.assertFalse(metadata["observed"])
        self.assertFalse(metadata["contains_observed_samples"])
        self.assertIsNone(metadata["latest_sample_at"])
        self.assertEqual(metadata["freshness"], "empty")

    def test_measured_speeds_aggregate_in_batam_hour_and_day_without_modelled_rows(self):
        result = self.profile([
            observation(speed=0),
            observation(speed=60, when=NOW - timedelta(minutes=10), source="loop_sensor"),
            observation(speed=10, source="synthetic"),
            observation(speed=10, source="simulated"),
            observation(speed=10, source="modelled"),
        ])
        bucket = result["hourly_profile"][8]
        self.assertEqual(bucket, {"hour": 8, "avg_score": 47.5, "sample_count": 2})
        self.assertEqual(result["weekly_trend"], [{"date": "2026-10-23", "avg_score": 47.5, "sample_count": 2}])
        metadata = result["history_metadata"]
        self.assertEqual(metadata["source_counts"], {"loop_sensor": 1, "probe_gps": 1})
        self.assertTrue(metadata["observed"])
        self.assertEqual(metadata["sample_count"], 2)
        self.assertEqual(metadata["latest_sample_age_seconds"], 0)

    def test_today_uses_civil_midnight_and_excludes_other_corridors_and_future(self):
        midnight = NOW.replace(hour=17, minute=0) - timedelta(days=1)  # WIB midnight
        result = self.profile([
            observation(when=midnight),
            observation(when=midnight - timedelta(microseconds=1)),
            observation(when=NOW + timedelta(seconds=1)),
            observation(corridor="corridor-2"),
        ], days=1)
        self.assertEqual(result["history_metadata"]["sample_count"], 1)
        self.assertEqual(result["hourly_profile"][0]["sample_count"], 1)
        self.assertEqual(len(result["weekly_trend"]), 1)

    def test_identical_replayed_rows_are_counted_once(self):
        row = observation()
        self.assertEqual(self.profile([row, row])["history_metadata"]["sample_count"], 1)

    def test_forged_source_or_observed_flag_cannot_be_displayed_as_measurement(self):
        for row in (
            replace(observation(source="synthetic"), observed=True),
            replace(observation(), source="untrusted-provider"),
            replace(observation(), actual_speed_kph=float("nan")),
        ):
            with self.subTest(row=row), self.assertRaises(analytics_history.AnalyticsHistoryUnavailable):
                self.profile([row])

    def test_shared_mode_uses_one_shared_snapshot_and_never_local_seed_fallback(self):
        shared = Mock()
        shared.get_spatial_training_dataset.return_value = [observation(source="tomtom_live")]
        with patch.object(routing_intelligence_store, "shared_store_enabled", return_value=True), \
                patch.object(routing_intelligence_store, "configured", return_value=True), \
                patch.object(historical_store, "get_spatial_training_dataset") as local:
            result = analytics_history.get_observed_history("corridor-1", 7, NOW, shared_store=shared)
        shared.get_spatial_training_dataset.assert_called_once_with(limit=analytics_history.MAX_HISTORY_SNAPSHOT_ROWS)
        local.assert_not_called()
        self.assertTrue(result["history_metadata"]["storage"]["shared_across_instances"])
        self.assertEqual(result["history_metadata"]["source_counts"], {"tomtom_live": 1})

    def test_store_outage_is_sanitized_and_never_claims_empty_history(self):
        with patch.object(historical_store, "get_spatial_training_dataset", side_effect=RuntimeError("privateSQL key=secret")):
            with self.assertRaises(analytics_history.AnalyticsHistoryUnavailable) as caught:
                analytics_history.get_observed_history("corridor-1", 7, NOW)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("SQL", str(caught.exception))

    def test_bounded_snapshot_rejects_adapter_overflow_without_consuming_unbounded_iterator(self):
        consumed = []
        def rows():
            for i in range(20):
                consumed.append(i)
                yield observation()
        with patch.object(analytics_history, "MAX_HISTORY_SNAPSHOT_ROWS", 3):
            with self.assertRaises(analytics_history.AnalyticsHistoryUnavailable):
                self.profile(rows())
        self.assertEqual(len(consumed), 4)

    def test_bad_request_parameters_do_not_touch_storage(self):
        with patch.object(historical_store, "get_spatial_training_dataset") as read:
            for corridor, days, now in (
                ("invalid", 7, NOW), ("corridor-1", True, NOW),
                ("corridor-1", 0, NOW), ("corridor-1", 31, NOW),
                ("corridor-1", 7, NOW.replace(tzinfo=None)),
            ):
                with self.subTest(corridor=corridor, days=days, now=now), self.assertRaises(ValueError):
                    analytics_history.get_observed_history(corridor, days, now)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
