"""Snapshot/integrity boundaries; fixtures are not measured traffic evidence."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("ml_runner", ROOT / "scripts/train_ml_engine.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from services.training_experiments import training_experiment_status
from services.traffic_observations import SpatialTrafficObservation

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


class SnapshotTests(unittest.TestCase):
    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.db"
            rows, available, info = runner.archive_snapshot(path, now=NOW)
            self.assertEqual((rows, available), ([], {}))
            self.assertEqual(info["status"], "not_present")
            self.assertFalse(path.exists())

    def test_snapshot_preserves_ingestion_and_never_modifies_database(self):
        row = SpatialTrafficObservation.create(
            corridor_id="corridor-1", observed_at=NOW-timedelta(hours=1), latitude=1.1,
            longitude=104, actual_speed_kph=20, free_flow_speed_kph=50, source="probe_gps",
            upstream_event_id="unit-only-snapshot", validation_now=NOW,
        )
        fields = dict(row.__dataclass_fields__)
        data = {key: getattr(row, key) for key in fields if key not in ("observed_at", "corridor_id")}
        data.update(corridor=row.corridor_id, observed_at_us=row.timestamp_epoch_us,
                    ingested_at_us=row.timestamp_epoch_us+120_000_000)
        data["observed"], data["reviewed"] = int(data["observed"]), int(data["reviewed"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE spatial_observations ("+",".join(data)+")")
            db.execute("INSERT INTO spatial_observations VALUES ("+",".join("?" for _ in data)+")", tuple(data.values()))
            db.commit(); db.close()
            before = path.read_bytes()
            rows, available, info = runner.archive_snapshot(path, now=NOW)
            self.assertEqual(rows, [row])
            self.assertEqual(available[row.observation_key], row.observed_at+timedelta(minutes=2))
            self.assertEqual(info["loaded_rows"], 1)
            self.assertFalse(info["raw_rows_exported"])
            self.assertEqual(path.read_bytes(), before)
            for limit in (True, 0, 100001):
                with self.assertRaises(ValueError):
                    runner.archive_snapshot(path, now=NOW, limit=limit)


class SummaryTests(unittest.TestCase):
    def test_not_run_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            self.assertEqual(training_experiment_status(path)["status"], "not_run")
            raw = json.dumps(dict(schema_version=1, status="completed", model_activated=False,
                                  citywide_Batam_accuracy_validated=False, experiments=[])).encode()
            (path/"status.json").write_bytes(raw)
            runner.write_json(path/"manifest.json", dict(schema_version=1, model_activated=False,
                files=[dict(path="status.json", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())]))
            self.assertEqual(training_experiment_status(path)["status"], "completed")
            (path/"status.json").write_bytes(raw+b" ")
            with self.assertRaises(ValueError):
                training_experiment_status(path)

    def test_reuse_rejects_an_activation_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            runner.write_json(path/"report.json", dict(production_eligible=True))
            with self.assertRaises(ValueError):
                runner.run_experiment("unused.py", path, reuse=True)


if __name__ == "__main__":
    unittest.main()
