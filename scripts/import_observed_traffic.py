#!/usr/bin/env python3
"""Validate a licensed Batam measurement CSV; apply only to an explicit local DB.

CSV columns: observed_at,corridor_id,latitude,longitude,actual_speed_kph,
free_flow_speed_kph,source. Optional: upstream_event_id,road_class,capacity_vph,
terminal_distance_km. lat/lng are accepted aliases for latitude/longitude.

The JSON manifest requires dataset_name,source_url,license,reference,source.
Its source must match every CSV row. Providing a manifest is an operator
assertion, not independent verification of the dataset or its licence.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Keep dry runs free of historical_store's database initialization/seed I/O.
from services.traffic_observations import SOURCE_POLICIES, SpatialTrafficObservation

MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ROWS = 100_000
BATCH_SIZE = 2_000
REQUIRED = frozenset({"observed_at", "corridor_id", "latitude", "longitude", "actual_speed_kph", "free_flow_speed_kph", "source"})
OPTIONAL = frozenset({"upstream_event_id", "road_class", "capacity_vph", "terminal_distance_km"})
MANIFEST_FIELDS = frozenset({"dataset_name", "source_url", "license", "reference", "source"})
SUPPORTED_CORRIDORS = frozenset(f"corridor-{index}" for index in range(1, 6))


class ImportRejected(ValueError):
    def __init__(self, code: str, *, row: int | None = None):
        super().__init__(code)
        self.code, self.row = code, row


@dataclass(frozen=True)
class StagedImport:
    records: tuple[SpatialTrafficObservation, ...]
    manifest: dict[str, str]
    csv_sha256: str
    manifest_sha256: str
    input_rows: int

    @property
    def import_id(self) -> str:
        return hashlib.sha256(f"{self.csv_sha256}:{self.manifest_sha256}".encode()).hexdigest()


def _read_bounded(path: Path, limit: int, kind: str) -> bytes:
    try:
        if not path.is_file():
            raise ImportRejected(f"{kind}_file_required")
        with path.open("rb") as source:
            body = source.read(limit + 1)
    except OSError:
        raise ImportRejected(f"{kind}_unreadable") from None
    if len(body) > limit:
        raise ImportRejected(f"{kind}_size_limit_exceeded")
    return body


def _retention_days() -> int:
    try:
        value = int(os.environ.get("CROSSFLOW_SPATIAL_HISTORY_RETENTION_DAYS", "") or 5 * 365)
    except ValueError:
        raise ImportRejected("invalid_retention_configuration") from None
    if not 365 <= value <= 20 * 365:
        raise ImportRejected("invalid_retention_configuration")
    return value


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_manifest_field")
        result[key] = value
    return result


def stage_import(csv_path: Path, manifest_path: Path, *, now: datetime | None = None) -> StagedImport:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ImportRejected("reference_time_requires_timezone")
    cutoff = now - timedelta(days=_retention_days())
    manifest_bytes = _read_bounded(manifest_path, MAX_MANIFEST_BYTES, "manifest")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"), object_pairs_hook=_unique_json_object)
        if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
            raise ValueError()
        if any(not isinstance(value, str) or not value.strip() or len(value) > 4096 for value in manifest.values()):
            raise ValueError()
        manifest = {key: value.strip() for key, value in manifest.items()}
        if any(ord(character) < 32 or ord(character) == 127 for character in manifest["source_url"]):
            raise ValueError()
        parsed = urlparse(manifest["source_url"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        source_policy = SOURCE_POLICIES.get(manifest["source"])
        if source_policy is None or not source_policy.observed or source_policy.review_required:
            raise ValueError()
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
        raise ImportRejected("invalid_provenance_manifest") from None
    raw = _read_bounded(csv_path, MAX_CSV_BYTES, "csv")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ImportRejected("csv_requires_utf8") from None
    rows = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(rows)
        aliases = {"lat": "latitude", "lng": "longitude"}
        header = [aliases.get(value.strip(), value.strip()) for value in header]
        if len(header) != len(set(header)) or not REQUIRED <= set(header) or set(header) - REQUIRED - OPTIONAL:
            raise ImportRejected("invalid_csv_columns")
        count = 0
        unique: dict[str, SpatialTrafficObservation] = {}
        for values in rows:
            if not values:
                continue
            count += 1
            if count > MAX_ROWS:
                raise ImportRejected("csv_row_limit_exceeded")
            line = rows.line_num
            if len(values) != len(header) or any(len(value) > 8192 for value in values):
                raise ImportRejected("invalid_csv_row", row=line)
            record = {key: value.strip() for key, value in zip(header, values)}
            try:
                if record["source"] != manifest["source"] or record["corridor_id"] not in SUPPORTED_CORRIDORS:
                    raise ValueError()
                item = SpatialTrafficObservation.create(
                    observed_at=datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00")),
                    corridor_id=record["corridor_id"],
                    latitude=float(record["latitude"]), longitude=float(record["longitude"]),
                    actual_speed_kph=float(record["actual_speed_kph"]),
                    free_flow_speed_kph=float(record["free_flow_speed_kph"]),
                    source=record["source"], validation_now=now,
                    upstream_event_id=record.get("upstream_event_id") or None,
                    road_class=record.get("road_class") or "unclassified",
                    capacity_vph=float(record["capacity_vph"]) if record.get("capacity_vph") else None,
                    terminal_distance_km=float(record["terminal_distance_km"]) if record.get("terminal_distance_km") else None,
                )
                if item.observed_at < cutoff:
                    raise ValueError()
                previous = unique.get(item.observation_key)
                if previous is not None and previous.immutable_payload() != item.immutable_payload():
                    raise ValueError()
                unique[item.observation_key] = item
            except (ValueError, TypeError, OverflowError):
                raise ImportRejected("invalid_observed_row", row=line) from None
    except (StopIteration, csv.Error):
        raise ImportRejected("invalid_csv_structure") from None
    if not count:
        raise ImportRejected("csv_has_no_observations")
    return StagedImport(
        records=tuple(unique.values()), manifest=manifest,
        csv_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_sha256=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        input_rows=count,
    )


def _check_local_apply(database: Path | None) -> Path:
    if database is None or str(database) == ":memory:" or "://" in str(database) or str(database).startswith("file:"):
        raise ImportRejected("explicit_local_database_required")
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("CROSSFLOW_ROUTING_INTELLIGENCE_STORE", "local").strip().casefold() != "local":
        raise ImportRejected("apply_requires_local_runtime_and_store")
    return database.expanduser().resolve()


def _check_existing_conflicts(store, records):
    by_key = {item.observation_key: item for item in records}
    keys = tuple(by_key)
    for offset in range(0, len(keys), 500):
        chunk = keys[offset:offset + 500]
        fields = "observation_key, observed_at_us, corridor, latitude, longitude, actual_speed_kph, free_flow_speed_kph, source, provenance, confidence, observed, reviewed, road_class, capacity_vph, terminal_distance_km, local_timezone_offset_minutes, upstream_event_id"
        rows = store._conn.execute(f"SELECT {fields} FROM spatial_observations WHERE observation_key IN ({','.join('?' for _ in chunk)})", chunk).fetchall()
        if any(store._stored_spatial_immutable(row) != by_key[row[0]].immutable_payload() for row in rows):
            raise ImportRejected("existing_observation_conflict")


def apply_import(staged: StagedImport, database: Path, *, now: datetime | None = None) -> dict[str, Any]:
    database = _check_local_apply(database)
    now = now or datetime.now(timezone.utc)
    # Importing the store initializes its module-level connection. Direct that
    # first connection to the explicitly selected file, never another app DB.
    already_loaded = "services.historical_store" in sys.modules
    previous = os.environ.get("CROSSFLOW_HISTORY_DB")
    os.environ["CROSSFLOW_HISTORY_DB"] = str(database)
    store = None
    module = None
    inserted = processed = 0
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            from services import historical_store as module
            store = module.HistoryStore(str(database), now_provider=lambda: now)
        if store.storage_metadata()["fallback_to_memory"]:
            raise ImportRejected("local_database_unavailable")
        _check_existing_conflicts(store, staged.records)
        db = store._conn
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS observed_csv_imports (import_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL, csv_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, input_rows INTEGER NOT NULL, unique_rows INTEGER NOT NULL, processed_rows INTEGER NOT NULL DEFAULT 0, inserted_rows INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS observed_csv_import_rows (import_id TEXT NOT NULL, observation_key TEXT NOT NULL, PRIMARY KEY(import_id, observation_key))")
            db.execute("INSERT INTO observed_csv_imports VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'applying', ?) ON CONFLICT(import_id) DO UPDATE SET status='applying', processed_rows=0, inserted_rows=0, updated_at=excluded.updated_at", (staged.import_id, json.dumps(staged.manifest, sort_keys=True), staged.csv_sha256, staged.manifest_sha256, staged.input_rows, len(staged.records), now.isoformat()))
        for offset in range(0, len(staged.records), BATCH_SIZE):
            batch = staged.records[offset:offset + BATCH_SIZE]
            result = store.ingest_spatial_batch(batch, now=now)
            inserted += result.inserted
            processed += len(batch)
            with db:
                db.executemany("INSERT OR IGNORE INTO observed_csv_import_rows VALUES (?, ?)", [(staged.import_id, item.observation_key) for item in batch])
                db.execute("UPDATE observed_csv_imports SET processed_rows=?, inserted_rows=?, updated_at=? WHERE import_id=?", (processed, inserted, now.isoformat(), staged.import_id))
        with db:
            db.execute("UPDATE observed_csv_imports SET status='applied' WHERE import_id=?", (staged.import_id,))
        return {"status": "applied", "import_id": staged.import_id, "input_rows": staged.input_rows, "unique_rows": len(staged.records), "inserted_rows": inserted, "existing_rows": len(staged.records) - inserted}
    except ImportRejected:
        raise
    except Exception:
        # Committed batches are idempotent on retry. Do not claim an atomic
        # all-file rollback when a later database/storage operation fails.
        if store is not None:
            try:
                with store._conn:
                    store._conn.execute("UPDATE observed_csv_imports SET status='failed', processed_rows=?, inserted_rows=? WHERE import_id=?", (processed, inserted, staged.import_id))
            except Exception:
                pass
        return {"status": "apply_failed", "code": "local_import_write_failed", "processed_rows": processed, "committed_inserted_rows": inserted, "retry_is_idempotent": True}
    finally:
        if store is not None:
            store.close()
        if module is not None and not already_loaded:
            module.close()
        if previous is None:
            os.environ.pop("CROSSFLOW_HISTORY_DB", None)
        else:
            os.environ["CROSSFLOW_HISTORY_DB"] = previous


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write only after whole-file validation; default is a dry run.")
    parser.add_argument("--database", type=Path, help="Explicit local SQLite file required with --apply.")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            _check_local_apply(args.database)
        now = datetime.now(timezone.utc)
        staged = stage_import(args.csv, args.manifest, now=now)
        result = apply_import(staged, args.database, now=now) if args.apply else {
            "status": "dry_run", "import_id": staged.import_id,
            "input_rows": staged.input_rows, "unique_rows": len(staged.records),
            "writes_performed": False, "manifest_externally_verified": False,
        }
    except ImportRejected as error:
        result = {"status": "rejected", "code": error.code}
        if error.row is not None:
            result["row"] = error.row
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"dry_run", "applied"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
