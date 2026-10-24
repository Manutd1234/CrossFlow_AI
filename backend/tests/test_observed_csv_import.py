"""Historical imports must be explicit, measured, auditable and repeatable."""

from __future__ import annotations

import contextlib
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[2]
for path in (PROJECT, PROJECT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from scripts import import_observed_traffic as importer
from services import historical_store

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FIELDS = ["observed_at", "corridor_id", "latitude", "longitude", "actual_speed_kph", "free_flow_speed_kph", "source", "upstream_event_id"]


def row(index=1, **overrides):
    return {
        "observed_at": (NOW - timedelta(days=1, minutes=index)).isoformat(),
        "corridor_id": "corridor-1", "latitude": "1.13", "longitude": "104.05",
        "actual_speed_kph": "0", "free_flow_speed_kph": "60",
        "source": "probe_gps", "upstream_event_id": f"fixture-{index}",
        **overrides,
    }


class HistoricalCsvImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.csv = self.directory / "observations.csv"
        self.manifest = self.directory / "manifest.json"
        self.database = self.directory / "history.db"
        environment = patch.dict(os.environ, {"CROSSFLOW_ROUTING_INTELLIGENCE_STORE": "local", "CROSSFLOW_HISTORY_DB": ":memory:"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.write_manifest()
        self.write_rows([row()])

    def write_manifest(self, **overrides):
        manifest = {
            "dataset_name": "Disposable test measurements", "source_url": "https://example.invalid/dataset",
            "license": "Test fixture only", "reference": "Fixture revision1", "source": "probe_gps",
            **overrides,
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def write_rows(self, rows):
        with self.csv.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def stage(self):
        return importer.stage_import(self.csv, self.manifest, now=NOW)

    def test_dry_run_cli_does_not_initialize_any_database(self):
        env = {**os.environ, "CROSSFLOW_HISTORY_DB": str(self.database)}
        result = subprocess.run([sys.executable, str(PROJECT / "scripts/import_observed_traffic.py"), str(self.csv), "--manifest", str(self.manifest)], capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "dry_run")
        self.assertFalse(summary["writes_performed"])
        self.assertFalse(summary["manifest_externally_verified"])
        self.assertFalse(self.database.exists())
        self.assertNotIn("fixture-1", result.stdout)

    def test_every_row_is_staged_before_database_initialization(self):
        self.write_rows([row(), row(2, observed_at="private-malformed-value")])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = importer.main([str(self.csv), "--manifest", str(self.manifest), "--apply", "--database", str(self.database)])
        self.assertEqual(code, 1)
        self.assertFalse(self.database.exists())
        self.assertEqual(json.loads(output.getvalue())["row"], 3)
        self.assertNotIn("private-malformed-value", output.getvalue())

    def test_no_geographical_relabelling_or_nonphysical_measurements(self):
        for changes in (
            {"latitude": "1.35", "longitude": "103.8"},
            {"actual_speed_kph": "nan"}, {"actual_speed_kph": "-1"},
            {"actual_speed_kph": "91", "free_flow_speed_kph": "60"},
            {"free_flow_speed_kph": "0"}, {"corridor_id": "singapore"},
        ):
            with self.subTest(changes=changes):
                self.write_rows([row(**changes)])
                with self.assertRaises(importer.ImportRejected):
                    self.stage()

    def test_naive_future_and_out_of_retention_timestamps_are_rejected(self):
        for timestamp in (
            NOW.replace(tzinfo=None).isoformat(),
            (NOW + timedelta(minutes=6)).isoformat(),
            (NOW - timedelta(days=5 * 365 + 1)).isoformat(),
        ):
            with self.subTest(timestamp=timestamp):
                self.write_rows([row(observed_at=timestamp)])
                with self.assertRaises(importer.ImportRejected):
                    self.stage()

    def test_synthetic_unknown_and_mismatched_source_labels_are_refused(self):
        for source in ("synthetic", "modelled", "simulated", "invented", "reviewed_community_observation"):
            with self.subTest(source=source):
                self.write_manifest(source=source)
                self.write_rows([row(source=source)])
                with self.assertRaises(importer.ImportRejected):
                    self.stage()
        self.write_manifest()
        self.write_rows([row(source="loop_sensor")])
        with self.assertRaises(importer.ImportRejected):
            self.stage()

    def test_manifest_requires_real_reference_fields_and_no_url_credentials(self):
        for changes in ({"license": ""}, {"reference": ""}, {"source_url": "https://name:secret@example.invalid/data"}, {"source_url": "http://example.invalid/data"}):
            with self.subTest(changes=changes):
                self.write_manifest(**changes)
                with self.assertRaises(importer.ImportRejected):
                    self.stage()

    def test_conflicting_event_replays_fail_whole_file_validation(self):
        self.write_rows([row(), row(actual_speed_kph="30")])
        with self.assertRaises(importer.ImportRejected):
            self.stage()
        self.assertFalse(self.database.exists())

    def test_malformed_manifest_is_rejected_without_tracebacks_or_row_values(self):
        valid = self.manifest.read_text(encoding="utf-8")
        invalid = (
            valid[:-1] + ', "source":"probe_gps"}',
            '{"private-field":' + '[' * 1500 + '0' + ']' * 1500 + '}',
            valid.replace("example.invalid", "example\\n.invalid"),
        )
        for manifest in invalid:
            with self.subTest(manifest_length=len(manifest)):
                self.manifest.write_text(manifest, encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = importer.main([str(self.csv), "--manifest", str(self.manifest)])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output.getvalue())["code"], "invalid_provenance_manifest")
                self.assertNotIn("private-field", output.getvalue())
                self.assertFalse(self.database.exists())

    def test_identical_replays_are_deduplicated_without_changing_timestamps(self):
        item = row(observed_at=(NOW - timedelta(days=1)).astimezone(timezone(timedelta(hours=8))).isoformat())
        self.write_rows([item, item])
        staged = self.stage()
        self.assertEqual(staged.input_rows, 2)
        self.assertEqual(len(staged.records), 1)
        self.assertEqual(staged.records[0].observed_at, NOW - timedelta(days=1))
        self.assertEqual(staged.records[0].upstream_event_id, item["upstream_event_id"])
        self.assertEqual(staged.records[0].actual_speed_kph, 0)

    def test_file_row_and_utf8_limits_are_enforced(self):
        with patch.object(importer, "MAX_CSV_BYTES", 10), self.assertRaises(importer.ImportRejected):
            self.stage()
        self.write_rows([row(), row(2)])
        with patch.object(importer, "MAX_ROWS", 1), self.assertRaises(importer.ImportRejected):
            self.stage()
        self.csv.write_bytes(b"\xffinvalid")
        with self.assertRaises(importer.ImportRejected):
            self.stage()

    def test_apply_preserves_rows_and_manifest_associations_and_is_idempotent(self):
        self.write_rows([row(), row(2, actual_speed_kph="30")])
        staged = self.stage()
        original = historical_store.HistoryStore.ingest_spatial_batch
        with patch.object(importer, "BATCH_SIZE", 1), patch.object(historical_store.HistoryStore, "ingest_spatial_batch", autospec=True, side_effect=original) as ingest:
            first = importer.apply_import(staged, self.database, now=NOW)
        self.assertEqual(first["inserted_rows"], 2)
        self.assertEqual(ingest.call_count, 2)
        self.assertTrue(all(len(call.args[1]) == 1 for call in ingest.call_args_list))
        again = importer.apply_import(staged, self.database, now=NOW)
        self.assertEqual(again["inserted_rows"], 0)
        self.assertEqual(again["existing_rows"], 2)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM spatial_observations WHERE observed=1").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observed_csv_import_rows").fetchone()[0], 2)
            manifest, status = db.execute("SELECT manifest_json,status FROM observed_csv_imports").fetchone()
        self.assertEqual(json.loads(manifest), staged.manifest)
        self.assertEqual(status, "applied")

    def test_existing_conflict_in_later_batch_prevents_all_observation_writes(self):
        self.write_rows([row(2)])
        original = self.stage().records[0]
        store = historical_store.HistoryStore(str(self.database), now_provider=lambda: NOW)
        store.ingest_spatial_batch([original], now=NOW)
        store.close()
        self.write_rows([row(1), row(2, actual_speed_kph="40")])
        with patch.object(importer, "BATCH_SIZE", 1), self.assertRaises(importer.ImportRejected):
            importer.apply_import(self.stage(), self.database, now=NOW)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM spatial_observations").fetchone()[0], 1)

    def test_database_failure_reports_committed_batches_and_remains_resumable(self):
        self.write_rows([row(), row(2)])
        staged = self.stage()
        original = historical_store.HistoryStore.ingest_spatial_batch
        calls = []

        def ingest(store, batch, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError("private database error")
            return original(store, batch, **kwargs)

        with patch.object(importer, "BATCH_SIZE", 1), patch.object(historical_store.HistoryStore, "ingest_spatial_batch", autospec=True, side_effect=ingest):
            failed = importer.apply_import(staged, self.database, now=NOW)
        self.assertEqual(failed["status"], "apply_failed")
        self.assertEqual(failed["committed_inserted_rows"], 1)
        self.assertNotIn("private", json.dumps(failed))
        retried = importer.apply_import(staged, self.database, now=NOW)
        self.assertEqual(retried["status"], "applied")
        self.assertEqual(retried["inserted_rows"], 1)

    def test_apply_is_never_implicit_or_allowed_in_shared_serverless_mode(self):
        with self.assertRaises(importer.ImportRejected):
            importer._check_local_apply(None)
        for env in ({"VERCEL": "1"}, {"AWS_LAMBDA_FUNCTION_NAME": "function"}, {"CROSSFLOW_ROUTING_INTELLIGENCE_STORE": "supabase"}):
            with self.subTest(env=env), patch.dict(os.environ, env), self.assertRaises(importer.ImportRejected):
                importer.apply_import(self.stage(), self.database, now=NOW)
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
