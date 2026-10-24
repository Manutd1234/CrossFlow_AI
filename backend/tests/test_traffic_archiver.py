"""Observed history collection is opt-in, local, bounded and idempotent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from services import historical_store, traffic_archiver  # noqa: E402
from services.analytics_history import get_observed_history  # noqa: E402


NOW = datetime(2026, 10, 23, 9, 45, tzinfo=timezone.utc)


class LiveTrafficArchiverTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"CROSSFLOW_ARCHIVE_LIVE_TRAFFIC": "1"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.store = historical_store.HistoryStore(":memory:", now_provider=lambda: NOW)
        self.addCleanup(self.store.close)
        self.archive = traffic_archiver.LiveTrafficArchiver(self.store)
        self.addCleanup(self.archive.stop)

    def sample(self, *, age=30, **overrides):
        return {
            "current_speed_kmh": 25.0, "free_flow_speed_kmh": 50.0,
            "confidence": 0.95, "snapshot_generated_at": (NOW - timedelta(seconds=age)).isoformat(),
            **overrides,
        }

    def test_opt_in_and_runtime_scope_are_enforced_before_any_io(self):
        for values, reason in (({"CROSSFLOW_ARCHIVE_LIVE_TRAFFIC": "0"}, "disabled"), ({"VERCEL": "1"}, "local_only"), ({"AWS_LAMBDA_FUNCTION_NAME": "api"}, "local_only"), ({"CROSSFLOW_ROUTING_INTELLIGENCE_STORE": "supabase"}, "local_store_required")):
            with self.subTest(values=values), patch.dict(os.environ, values), \
                    patch.object(traffic_archiver.live_traffic, "_tomtom_segments") as fetch:
                self.assertEqual(self.archive.collect_once(now=NOW)["status"], reason)
                self.assertFalse(self.archive.start())
                fetch.assert_not_called()

    def test_actual_samples_preserve_original_time_and_deduplicate_snapshots(self):
        segment = self.sample()
        with patch.object(traffic_archiver.live_traffic, "_tomtom_segments", return_value={"corridor-1": segment}):
            first = self.archive.collect_once(now=NOW)
            second = self.archive.collect_once(now=NOW + timedelta(seconds=1))
        self.assertEqual((first["inserted"], second["inserted"], second["duplicates"]), (1, 0, 1))
        rows = self.store.get_spatial_training_dataset(now=NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].observed_at, NOW - timedelta(seconds=30))
        self.assertTrue(rows[0].observed)
        self.assertEqual(rows[0].source, "tomtom_live")
        self.assertEqual((rows[0].latitude, rows[0].longitude), traffic_archiver.live_traffic._CORRIDOR_POINTS["corridor-1"])
        self.assertEqual(self.archive.status()["inserted_total"], 1)

    def test_collected_samples_reach_observed_history_without_inventing_past_days(self):
        with patch.object(traffic_archiver.live_traffic, "_tomtom_segments", return_value={"corridor-1": self.sample()}):
            self.archive.collect_once(now=NOW)
        with patch.object(historical_store, "get_spatial_training_dataset", wraps=self.store.get_spatial_training_dataset), \
                patch.object(historical_store, "get_storage_metadata", wraps=self.store.storage_metadata):
            history = get_observed_history("corridor-1", 7, NOW)
        self.assertEqual(history["history_metadata"]["sample_count"], 1)
        self.assertEqual(len(history["weekly_trend"]), 1)
        self.assertEqual(sum(row["sample_count"] for row in history["hourly_profile"]), 1)

    def test_synthetic_stale_and_invalid_snapshots_never_enter_archive(self):
        segments = {
            "corridor-1": self.sample(source="simulated"),
            "corridor-2": self.sample(age=181),
            "corridor-3": self.sample(current_speed_kmh=-1),
            "corridor-4": self.sample(snapshot_generated_at="2026-10-23T09:45:00"),
            "unknown": self.sample(),
        }
        with patch.object(traffic_archiver.live_traffic, "_tomtom_segments", return_value=segments):
            result = self.archive.collect_once(now=NOW)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(self.store.get_spatial_training_dataset(now=NOW), [])

    def test_conflicting_provider_snapshot_is_not_overwritten(self):
        with patch.object(traffic_archiver.live_traffic, "_tomtom_segments", return_value={"corridor-1": self.sample()}):
            self.archive.collect_once(now=NOW)
        with patch.object(traffic_archiver.live_traffic, "_tomtom_segments", return_value={"corridor-1": self.sample(current_speed_kmh=40)}):
            result = self.archive.collect_once(now=NOW)
        self.assertEqual(result["status"], "collection_unavailable")
        self.assertEqual(self.store.get_spatial_training_dataset(now=NOW)[0].actual_speed_kph, 25)

    def test_background_worker_collects_once_and_stops_without_waiting_two_minutes(self):
        collected = threading.Event()
        with patch.object(self.archive, "collect_once", side_effect=lambda: collected.set()) as poll:
            self.assertTrue(self.archive.start())
            self.assertTrue(collected.wait(timeout=2))
            self.assertFalse(self.archive.start())
            self.archive.stop()
        self.assertEqual(poll.call_count, 1)
        self.assertFalse(self.archive.status()["active"])
        self.assertIsNotNone(self.archive.status()["collection_started_at"])

    def test_provider_failure_does_not_crash_worker_or_expose_secret_details(self):
        with patch.object(traffic_archiver.live_traffic, "_tomtom_segments", side_effect=RuntimeError("private key")):
            result = self.archive.collect_once(now=NOW)
        self.assertEqual(result["status"], "collection_unavailable")
        self.assertNotIn("private key", str(self.archive.status()))

    def test_lifespan_starts_and_stops_collector_even_on_application_error(self):
        import main as api_main

        async def exercise():
            with self.assertRaises(RuntimeError):
                async with api_main._lifespan(api_main.app):
                    raise RuntimeError("test application shutdown")

        with patch.object(api_main, "_WARM_ROUTING_CACHES", False), \
                patch.object(traffic_archiver.ARCHIVER, "start") as start, \
                patch.object(traffic_archiver.ARCHIVER, "stop") as stop:
            asyncio.run(exercise())
        start.assert_called_once_with()
        stop.assert_called_once_with()

    def test_dashboard_reads_expose_status_without_collecting_or_writing(self):
        import main as api_main
        from fastapi import Response

        with patch.object(traffic_archiver.ARCHIVER, "collect_once") as collect, \
                patch.object(api_main, "get_operations_analytics", return_value={"analytics_metadata": {"traffic_observed": False, "traffic_source": "unavailable"}}), \
                patch.object(api_main, "get_observed_history", return_value={"history_metadata": {}}):
            operations = api_main.get_ops(Response())
            history = api_main.api_historical_congestion("corridor-1", 7)
        collect.assert_not_called()
        self.assertTrue(operations["analytics_metadata"]["archive"]["enabled"])
        self.assertTrue(history["history_metadata"]["archive"]["observed_only"])


if __name__ == "__main__":
    unittest.main()
