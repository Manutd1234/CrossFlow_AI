"""Real source verification survives a local restart without weakening shared storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from services import ferry_freshness_store, ferry_local_freshness, ferry_schedule


class LocalFerryFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="crossflow-ferry-freshness-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "verification.db"
        self.environment = patch.dict(os.environ, {
            "CROSSFLOW_FERRY_FRESHNESS_DB": str(self.path), "VERCEL": "", "VERCEL_ENV": "",
            "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.remote = patch.object(ferry_freshness_store, "_config", return_value=None)
        self.remote.start()
        self.addCleanup(self.remote.stop)
        ferry_schedule._reset_runtime_verification_for_tests()
        self.addCleanup(ferry_schedule._reset_runtime_verification_for_tests)

    def test_validated_receipt_survives_an_actual_fresh_process(self):
        expected = ferry_local_freshness.save("snapshot-one", "2026-10-24T12:01:00+07:00", "2026-10-24T12:00:00+07:00")
        environment = {**os.environ, "PYTHONPATH": str(BACKEND)}
        read = subprocess.run([
            sys.executable, "-c",
            "import json; from services import ferry_local_freshness; print(json.dumps(ferry_local_freshness.load('snapshot-one')))",
        ], env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(read.stdout), expected)

    def test_receipts_are_snapshot_bound_and_monotonic_across_timezones(self):
        ferry_local_freshness.save("snapshot-one", "2026-10-24T12:01:00+07:00", "2026-10-24T12:00:00+07:00")
        self.assertIsNone(ferry_local_freshness.load("snapshot-two"))
        earlier = ferry_local_freshness.save("snapshot-one", "2026-10-24T04:59:00+00:00", "2026-10-24T04:58:00+00:00")
        self.assertEqual(earlier["last_verified_at"], "2026-10-24T12:00:00+07:00")
        self.assertEqual(earlier["latest_checked_at"], "2026-10-24T12:01:00+07:00")

    def test_failed_check_advances_checked_time_only_after_restart(self):
        verified = "2026-10-24T12:00:00+07:00"
        ferry_schedule.record_refresh_result(verified, verified)
        ferry_schedule._reset_runtime_verification_for_tests(clear_local=False)
        ferry_schedule.record_refresh_result("2026-10-24T12:05:00+07:00")
        ferry_schedule._reset_runtime_verification_for_tests(clear_local=False)
        metadata = ferry_schedule.timetable_metadata()
        self.assertEqual(metadata["last_verified_at"], verified)
        self.assertEqual(metadata["latest_checked_at"], "2026-10-24T12:05:00+07:00")
        self.assertEqual(metadata["freshness_durability"], "local_sqlite")
        self.assertEqual(metadata["snapshot_verified_at"], "2026-08-13T00:30:50+07:00")

    def test_invalid_timestamps_do_not_create_receipts(self):
        for checked, verified in (
            ("2026-10-24T12:00:00", "2026-10-24T12:00:00+07:00"),
            ("2026-10-24T11:59:00+07:00", "2026-10-24T12:00:00+07:00"),
        ):
            with self.subTest(checked=checked):
                self.assertIsNone(ferry_local_freshness.save("snapshot", checked, verified))
                self.assertIsNone(ferry_local_freshness.load("snapshot"))

    def test_required_shared_storage_never_uses_a_local_receipt(self):
        ferry_local_freshness.save("snapshot", "2026-10-24T12:00:00+07:00", "2026-10-24T12:00:00+07:00")
        with patch.dict(os.environ, {"VERCEL_ENV": "production"}), \
                patch.object(ferry_local_freshness, "load") as local_load, \
                patch.object(ferry_local_freshness, "save") as local_save:
            self.assertIsNone(ferry_freshness_store.load("snapshot"))
            self.assertIsNone(ferry_freshness_store.save("snapshot", "2026-10-24T12:00:00+07:00", "2026-10-24T12:00:00+07:00"))
        local_load.assert_not_called()
        local_save.assert_not_called()

    def test_ephemeral_vercel_and_memory_only_modes_do_not_write_local_files(self):
        with patch.dict(os.environ, {"VERCEL": "1"}):
            self.assertIsNone(ferry_local_freshness.save("snapshot", "2026-10-24T12:00:00+07:00", "2026-10-24T12:00:00+07:00"))
        with patch.dict(os.environ, {"CROSSFLOW_FERRY_FRESHNESS_DB": ":memory:"}):
            self.assertIsNone(ferry_local_freshness.save("snapshot", "2026-10-24T12:00:00+07:00", "2026-10-24T12:00:00+07:00"))
        self.assertFalse(self.path.exists())

    def test_corrupt_local_storage_fails_without_breaking_reference_data(self):
        self.path.write_bytes(b"not a database")
        self.assertIsNone(ferry_local_freshness.load("snapshot"))
        self.assertIsNone(ferry_local_freshness.save("snapshot", "2026-10-24T12:00:00+07:00", "2026-10-24T12:00:00+07:00"))
        self.assertFalse(ferry_local_freshness.available())
        self.assertGreater(len(ferry_schedule.timetable_metadata()["sources"]), 0)


if __name__ == "__main__":
    unittest.main()
