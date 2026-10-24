"""Local registered-camera aggregate store; never stores images or inferred speeds.

The local registry is operator configuration, not a public API response. Readers
open SQLite in read-only mode and never create a database. Operational gates are
recomputed against the current registry on every read, so revocation takes effect
without rewriting earlier observations. Trusted collectors are authenticated by
its caller; schema validation is not proof that a camera measurement is accurate.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import urlparse

from services.cctv_analysis import CameraConfig, timestamp, validate_evidence
from services import cctv_availability, cctv_shared_store

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
MAX_REGISTRY_BYTES = 1_048_576
MAX_CAMERAS = 128
MAX_EVIDENCE_BYTES = 32_768
MAX_RECORDS = 10_000
MAX_WATERMARKS = 4_096
MAX_DATABASE_BYTES = 134_217_728
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
ROW_FIELDS = {"camera_id", "name", "road_name", "source_url", "stream_url", "analysis_config",
              "collector_config", "stream_access_status", "estimation_config"}
EVIDENCE_FIELDS = {
    "schema", "camera_id", "corridor_id", "road_segment_id", "config_fingerprint", "captured_at",
    "window_start", "window_seconds", "frame_count", "detector_id", "status", "vehicle_count",
    "tracked_vehicle_count", "moving_vehicle_count", "stationary_vehicle_count", "unknown_vehicle_count",
    "uncertain_vehicle_count", "uncertain_detection_fraction", "image_occupancy_ratio", "normalized_motion_proxy",
    "stationary_fraction", "line_crossings", "camera_congestion_severity", "congestion_score",
    "camera_congestion_score_proxy", "severity_status", "severity_persistence_seconds", "quality",
    "operational_eligible", "experimental", "speed_kmh", "travel_time_seconds", "queue_length_m",
    "images_retained", "measurement_scope", "flow_scope",
    "timestamp_basis",
}


class CCTVStoreUnavailable(RuntimeError):
    """Local registry or aggregate storage could not be read safely."""


class CCTVConflictError(ValueError):
    """A camera timestamp was replayed with different contents or out of order."""


def _now(value=None):
    return timestamp(value if value is not None else datetime.now(timezone.utc))


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _constant(_value):
    raise ValueError("Non-finite JSON value")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _json(body):
    return json.loads(body, parse_constant=_constant, object_pairs_hook=_pairs)


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("CCTV data must contain finite JSON values") from error


def _text(value, *, optional=False, maximum=160):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid registered camera text")
    return value


def _public_page(value):
    _text(value, maximum=2048)
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or re.search(r"\.(?:m3u8|mp4|mjpeg|ts|jpg|jpeg|png|webp)$", parsed.path, re.I)):
        raise ValueError("Camera source must be a public HTTPS page without credentials")
    return value


def _collector_config(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"frame_interval_seconds", "fixed_view_confirmed", "background_region",
                                                       "source_clock_validation_reference", "background_max_shift_px",
                                                       "timestamp_mode", "detector_backend", "inference_crop", "decode_max_dimension"}:
        raise ValueError("Invalid camera collector configuration")
    interval = value.get("frame_interval_seconds", 2.)
    if type(interval) not in (int, float) or not math.isfinite(interval) or not .5 <= interval <= 30.:
        raise ValueError("Invalid camera collection interval")
    fixed = value.get("fixed_view_confirmed", False)
    if type(fixed) is not bool:
        raise ValueError("Invalid fixed-camera confirmation")
    region = value.get("background_region")
    if region is not None:
        # Match the engine's normalized polygon validation, without requiring it
        # to be the road region used for occupancy and vehicle tracks.
        CameraConfig(camera_id="background-check", roi=region)
    clock_reference = _text(value.get("source_clock_validation_reference"), optional=True, maximum=1024)
    max_shift = value.get("background_max_shift_px", 2.)
    if type(max_shift) not in (int, float) or not math.isfinite(max_shift) or not .1 <= max_shift <= 3.:
        raise ValueError("Invalid camera background motion threshold")
    timestamp_mode = value.get("timestamp_mode", "source_clock")
    detector = value.get("detector_backend", "yolo26")
    if timestamp_mode not in {"source_clock", "stream_observation"} or detector not in {"yolo26", "legacy_detector"}:
        raise ValueError("Invalid camera timing or detector policy")
    image_policy = collector_image_policy(value)
    return {"frame_interval_seconds": float(interval), "fixed_view_confirmed": fixed, "background_region": region,
            "source_clock_validation_reference": clock_reference, "background_max_shift_px": float(max_shift),
            "timestamp_mode": timestamp_mode, "detector_backend": detector,
            "inference_crop": image_policy["inference_crop"], "decode_max_dimension": image_policy["decode_max_dimension"]}


def collector_image_policy(collector):
    """Canonical, bounded local image transform; defaults preserve old inference."""
    collector = {} if collector is None else collector
    if not isinstance(collector, dict):
        raise ValueError("Invalid camera image transform configuration")
    crop = collector.get("inference_crop")
    if crop is not None:
        if (not isinstance(crop, (list, tuple)) or len(crop) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in crop)
                or crop[2] <= crop[0] or crop[3] <= crop[1]):
            raise ValueError("Invalid normalized camera inference crop")
        crop = [float(value) + 0. for value in crop]
    maximum = collector.get("decode_max_dimension", 1280)
    if type(maximum) is not int or maximum not in {1280, 1920}:
        raise ValueError("Invalid camera decode resolution bound")
    return {"schema": "crossflow.cctv_image_policy.v1", "inference_crop": crop,
            "decode_max_dimension": maximum, "crop_rounding": "outward_to_full_frame_pixels",
            "crop_jpeg_quality": 95, "output_boxes": "full_frame_normalized_xyxy"}


def collector_image_policy_suffix(collector):
    policy = collector_image_policy(collector)
    if policy["inference_crop"] is None and policy["decode_max_dimension"] == 1280:
        return None
    return "-view" + hashlib.sha256(_encoded(policy).encode()).hexdigest()[:16]


def collector_detector_id(model_id, collector):
    """Bind changed crop/decode semantics without changing the counter globally."""
    if not isinstance(model_id, str) or not IDENTIFIER.fullmatch(model_id):
        raise ValueError("Invalid camera detector identifier")
    suffix = collector_image_policy_suffix(collector)
    if suffix is None:
        return model_id
    base = model_id if len(model_id + suffix) <= 128 else model_id[:88] + "-b" + hashlib.sha256(model_id.encode()).hexdigest()[:16]
    return base + suffix


def _matches_collector_image_policy(model_id, collector):
    suffix = collector_image_policy_suffix(collector)
    return (isinstance(model_id, str) and model_id.endswith(suffix)) if suffix else (
        isinstance(model_id, str) and re.search(r"-view[0-9a-f]{16}$", model_id) is None
    )


def _estimation_config(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"enabled", "allow_routing", "graph_revision", "reference"}:
        raise ValueError("Invalid camera estimate policy")
    enabled, routing = value.get("enabled", False), value.get("allow_routing", False)
    if type(enabled) is not bool or type(routing) is not bool or routing and not enabled:
        raise ValueError("Invalid camera estimate flags")
    return {"enabled": enabled, "allow_routing": routing,
            "graph_revision": _text(value.get("graph_revision"), optional=True, maximum=128),
            "reference": _text(value.get("reference"), optional=True, maximum=1024)}


def load_registry() -> list[dict[str, Any]]:
    """Trusted local collector accessor. Contains private stream configuration.

    Do not serialize this return value into an HTTP response. camera_status()
    builds its response from an explicit public field allowlist instead.
    """
    path = Path(os.environ.get("CROSSFLOW_CCTV_REGISTRY") or DATA_DIR / "cctv_cameras.json")
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_REGISTRY_BYTES + 1)
        if len(body) > MAX_REGISTRY_BYTES:
            raise ValueError("Camera registry exceeds bounds")
        value = _json(body)
        if (not isinstance(value, dict) or set(value) - {"schema_version", "source_audit_reference", "cameras"}
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or not isinstance(value.get("cameras"), list) or len(value["cameras"]) > MAX_CAMERAS):
            raise ValueError("Invalid camera registry")
        rows, seen = [], set()
        for row in value["cameras"]:
            if not isinstance(row, dict) or set(row) - ROW_FIELDS:
                raise ValueError("Invalid camera registry row")
            camera_id = row.get("camera_id")
            if not isinstance(camera_id, str) or not IDENTIFIER.fullmatch(camera_id) or camera_id in seen:
                raise ValueError("Invalid or duplicate camera identifier")
            seen.add(camera_id)
            config = CameraConfig.from_dict(row.get("analysis_config"))
            if config.camera_id != camera_id:
                raise ValueError("Camera configuration identity mismatch")
            stream = row.get("stream_url")
            if stream is not None:
                _text(stream, maximum=4096)
                parsed = urlparse(stream)
                if parsed.scheme not in {"https", "rtsp", "rtsps"} or not parsed.hostname or parsed.fragment:
                    raise ValueError("Invalid local camera stream configuration")
            status = row.get("stream_access_status")
            if status is not None:
                _text(status, maximum=128)
            rows.append({
                "camera_id": camera_id, "name": _text(row.get("name")),
                "road_name": _text(row.get("road_name"), optional=True),
                "source_url": _public_page(row.get("source_url")), "stream_url": stream,
                "analysis_config": asdict(config), "collector_config": _collector_config(row.get("collector_config")),
                "estimation_config": _estimation_config(row.get("estimation_config")),
                "stream_access_status": status,
            })
        return rows
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise CCTVStoreUnavailable("CCTV camera configuration is unavailable") from error


def get_registered_camera(camera_id: str) -> dict[str, Any]:
    """Return trusted local configuration for one explicitly registered camera."""
    for row in load_registry():
        if row["camera_id"] == camera_id:
            return row
    raise ValueError("Unknown registered CCTV camera")


def _db_path():
    configured = os.environ.get("CROSSFLOW_CCTV_DB")
    path = Path(configured) if configured else DATA_DIR / "cctv_observations.sqlite"
    if str(path) == ":memory:":
        raise CCTVStoreUnavailable("CCTV aggregate storage requires a persistent local path")
    return path


def _check_database_bound(path):
    # SQLite journals are temporary transaction files, never imagery. Bounding the
    # database before opening also avoids large unexpected local input files.
    if path.exists() and (not path.is_file() or path.stat().st_size > MAX_DATABASE_BYTES):
        raise CCTVStoreUnavailable("CCTV aggregate storage exceeds supported bounds")


def _read_latest(rows):
    try:
        if cctv_shared_store.enabled():
            return cctv_shared_store.latest([row["camera_id"] for row in rows])
    except cctv_shared_store.SharedCCTVUnavailable as error:
        raise CCTVStoreUnavailable("Shared CCTV observations are unavailable") from error
    path = _db_path()
    try:
        _check_database_bound(path)
        if not path.exists():
            return {}
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.) as db:
            db.execute("PRAGMA query_only=ON")
            if not rows:
                return {}
            ids = [row["camera_id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            records = db.execute(
                "SELECT w.camera_id,w.captured_at,CASE WHEN length(o.evidence_json)<=? THEN o.evidence_json ELSE NULL END FROM cctv_watermarks w "
                "LEFT JOIN cctv_observations o ON o.observation_id=w.observation_id "
                f"WHERE w.camera_id IN ({placeholders}) LIMIT ?", (MAX_EVIDENCE_BYTES, *ids, MAX_CAMERAS),
            ).fetchall()
        latest = {}
        for camera_id, captured_at, body in records:
            if not isinstance(body, str) or len(body.encode()) > MAX_EVIDENCE_BYTES:
                raise ValueError("Stored CCTV evidence exceeds bounds")
            parsed = _json(body)
            if not isinstance(parsed, dict):
                raise ValueError("Stored CCTV evidence must be an object")
            latest[camera_id] = {"captured_at": captured_at, "evidence": parsed}
        return latest
    except (OSError, sqlite3.Error, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise CCTVStoreUnavailable("CCTV aggregate observations are unavailable") from error


def _checked_evidence(payload, config, now):
    if not isinstance(payload, dict) or set(payload) - EVIDENCE_FIELDS:
        raise ValueError("Unsupported CCTV evidence fields")
    body = _encoded(payload)
    if len(body.encode()) > MAX_EVIDENCE_BYTES:
        raise ValueError("CCTV evidence exceeds bounds")
    for key in ("speed_kmh", "travel_time_seconds", "queue_length_m"):
        if payload.get(key) is not None:
            raise ValueError("CCTV image proxies cannot assert physical speed, travel time or queue length")
    if payload.get("images_retained", False) is not False:
        raise ValueError("CCTV aggregate ingestion cannot retain images")
    clean = validate_evidence(payload, config, now=now)
    # The supplied operational score/eligibility are never authoritative. Reject
    # contradictory claims rather than silently accepting a forged promotion.
    if "operational_eligible" in payload and payload["operational_eligible"] is not clean["operational_eligible"]:
        raise ValueError("CCTV operational eligibility does not match validated gates")
    if (payload.get("congestion_score") is not None and
            (type(payload["congestion_score"]) not in (int, float) or not math.isfinite(payload["congestion_score"]))):
        raise ValueError("CCTV congestion score must be a finite number or null")
    if "congestion_score" in payload and payload["congestion_score"] != clean["congestion_score"]:
        raise ValueError("CCTV congestion score does not match validated measurements")
    # Drop free-text scope and add only fixed, locally known descriptions. All
    # retained observation values are numeric aggregates and bounded provenance.
    clean["captured_at"] = _iso(timestamp(clean["captured_at"]))
    clean["window_start"] = _iso(timestamp(clean["window_start"]))
    return clean, hashlib.sha256(body.encode()).hexdigest()


def ingest_evidence(payload, *, now=None) -> dict[str, Any]:
    """Validate an authenticated collector result and atomically advance its watermark."""
    instant = _now(now)
    if not isinstance(payload, dict):
        raise ValueError("CCTV evidence must be an object")
    row = get_registered_camera(payload.get("camera_id"))
    config = CameraConfig.from_dict(row["analysis_config"])
    clean, receipt = _checked_evidence(payload, config, instant)
    if clean["timestamp_basis"] != row["collector_config"]["timestamp_mode"]:
        raise ValueError("Evidence clock does not match the registered timestamp policy")
    if not _matches_collector_image_policy(clean["detector_id"], row["collector_config"]):
        raise ValueError("Evidence detector does not match the registered image transform policy")
    if not config.permission_ready:
        raise ValueError("Registered camera analysis is disabled or not authorized")
    try:
        if cctv_shared_store.enabled():
            return cctv_shared_store.ingest(clean, receipt, stored_at=_iso(instant))
    except cctv_shared_store.SharedCCTVConflict as error:
        raise CCTVConflictError("CCTV evidence timestamp conflicts with its immutable camera watermark") from error
    except cctv_shared_store.SharedCCTVUnavailable as error:
        raise CCTVStoreUnavailable("Shared CCTV storage could not accept the observation") from error
    captured = timestamp(clean["captured_at"])
    path = _db_path()
    try:
        _check_database_bound(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=2.) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS cctv_observations ("
                       "observation_id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT NOT NULL, "
                       "captured_at TEXT NOT NULL, captured_epoch REAL NOT NULL, "
                       "evidence_sha256 TEXT NOT NULL, config_fingerprint TEXT NOT NULL, "
                       "stored_at TEXT NOT NULL, evidence_json TEXT NOT NULL CHECK(length(evidence_json)<=32768))")
            db.execute("CREATE TABLE IF NOT EXISTS cctv_watermarks ("
                       "camera_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, captured_epoch REAL NOT NULL, "
                       "evidence_sha256 TEXT NOT NULL, observation_id INTEGER NOT NULL "
                       "REFERENCES cctv_observations(observation_id))")
            previous = db.execute("SELECT captured_epoch,evidence_sha256 FROM cctv_watermarks WHERE camera_id=?",
                                  (config.camera_id,)).fetchone()
            if previous is not None and captured.timestamp() <= previous[0]:
                if captured.timestamp() == previous[0] and receipt == previous[1]:
                    return {"accepted": True, "idempotent": True, "camera_id": config.camera_id,
                            "observed_at": clean["captured_at"], "operational_eligible": clean["operational_eligible"]}
                raise CCTVConflictError("CCTV evidence timestamp conflicts with its immutable camera watermark")
            if previous is None and db.execute("SELECT COUNT(*) FROM cctv_watermarks").fetchone()[0] >= MAX_WATERMARKS:
                raise CCTVStoreUnavailable("CCTV camera watermark capacity reached")
            stored_body = _encoded(clean)
            cursor = db.execute("INSERT INTO cctv_observations(camera_id,captured_at,captured_epoch,evidence_sha256,"
                                "config_fingerprint,stored_at,evidence_json) VALUES (?,?,?,?,?,?,?)",
                                (config.camera_id, clean["captured_at"], captured.timestamp(), receipt, config.fingerprint,
                                 _iso(instant), stored_body))
            db.execute("INSERT INTO cctv_watermarks(camera_id,captured_at,captured_epoch,evidence_sha256,observation_id) "
                       "VALUES (?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET captured_at=excluded.captured_at,"
                       "captured_epoch=excluded.captured_epoch,evidence_sha256=excluded.evidence_sha256,"
                       "observation_id=excluded.observation_id",
                       (config.camera_id, clean["captured_at"], captured.timestamp(), receipt, cursor.lastrowid))
            surplus = max(0, db.execute("SELECT COUNT(*) FROM cctv_observations").fetchone()[0] - MAX_RECORDS)
            if surplus:
                db.execute("DELETE FROM cctv_observations WHERE observation_id IN (SELECT observation_id FROM "
                           "cctv_observations WHERE observation_id NOT IN (SELECT observation_id FROM cctv_watermarks) "
                           "ORDER BY captured_epoch,observation_id LIMIT ?)", (surplus,))
        return {"accepted": True, "idempotent": False, "camera_id": config.camera_id,
                "observed_at": clean["captured_at"], "operational_eligible": clean["operational_eligible"]}
    except (OSError, sqlite3.Error) as error:
        raise CCTVStoreUnavailable("CCTV aggregate storage could not accept the observation") from error


def _empty_camera(row):
    return {"camera_id": row["camera_id"], "name": row["name"], "road_name": row["road_name"],
            "source_url": row["source_url"], "status": "unavailable", "reason": "No camera observations are available",
            "observed_at": None, "window_seconds": None, "vehicle_count": None, "moving_vehicles": None,
            "stopped_vehicles": None, "occupancy_ratio": None, "motion_score": None, "queue_length_m": None,
            "congestion_score": None, "severity": None, "operational_eligible": False,
            "validation_status": "unavailable", "timestamp_basis": None, "detector_id": None,
            "routing_enabled": False, "maximum_age_seconds": row["analysis_config"]["maximum_age_seconds"]}


def _estimate(clean, config, row):
    """Explicit provisional image-space index; never assert independent validation."""
    policy = row.get("estimation_config") or {}
    if not (policy.get("enabled") is True and policy.get("reference") and config.roi
            and clean["quality"]["usable"] and clean["window_seconds"] >= max(config.minimum_window_seconds, config.stationary_seconds)
            and clean["detector_id"].startswith("yolo26")):
        return None
    occupancy, stationary, motion = (clean.get(key) for key in ("image_occupancy_ratio", "stationary_fraction", "normalized_motion_proxy"))
    if occupancy is None or stationary is None:
        return None
    slowdown = max(0., 1. - motion / (5 * config.stationary_motion_per_second)) if motion is not None else 0.
    score = 100 * min(occupancy / config.high_occupancy, 1.) * (.5 + .3 * stationary + .2 * slowdown)
    severity = "high" if occupancy >= config.high_occupancy and stationary >= config.high_stationary_fraction else "moderate" if occupancy >= config.moderate_occupancy else "low"
    return score, severity


def _assess(row, record, instant):
    config = CameraConfig.from_dict(row["analysis_config"])
    public = _empty_camera(row)
    if not config.analysis_authorized or not config.authorization_reference:
        public.update(status="permission_pending", reason="Automatic camera analysis authorization is not established")
        return public, None
    if not config.enabled:
        public.update(status="disabled", reason="Camera analysis is disabled")
        return public, None
    calibration_ready = (config.calibration_ready and config.independent_validation_reference and config.validated_detector_id)
    if not calibration_ready:
        public.update(status="calibration_pending", reason="Road region, thresholds and independent camera validation are required")
    if record is None:
        return public, None
    try:
        observed = timestamp(record["captured_at"])
        if timestamp(record["evidence"].get("captured_at")) != observed:
            raise ValueError("Stored CCTV timestamp does not match its camera watermark")
        public["observed_at"] = _iso(observed)
        age = (instant - observed).total_seconds()
        if age > config.maximum_age_seconds:
            public.update(status="stale", reason="Camera observations are older than the configured freshness limit")
            return public, None
        # Registry edits invalidate old evidence even if its original detector
        # declared it eligible; _checked_evidence recomputes all current gates.
        clean, _ = _checked_evidence(record["evidence"], config, instant)
        if clean["timestamp_basis"] != row["collector_config"]["timestamp_mode"]:
            raise ValueError("Stored evidence clock no longer matches registered policy")
        if not _matches_collector_image_policy(clean["detector_id"], row["collector_config"]):
            raise ValueError("Stored evidence no longer matches registered image transform policy")
    except (ValueError, KeyError, TypeError):
        public.update(status="unavailable", reason="Latest evidence does not match current camera configuration or quality requirements")
        return public, None
    if not clean["quality"]["usable"]:
        public.update(status="unavailable", reason="Camera temporal or image-quality evidence is not yet usable")
        return public, None
    public.update(window_seconds=clean["window_seconds"], vehicle_count=clean.get("vehicle_count"),
                  moving_vehicles=clean.get("moving_vehicle_count"), stopped_vehicles=clean.get("stationary_vehicle_count"),
                  occupancy_ratio=clean.get("image_occupancy_ratio"), timestamp_basis=clean.get("timestamp_basis", "source_clock"),
                  detector_id=clean["detector_id"])
    # Image-plane displacement has units of normalized distance per second; it
    # is not a calibrated 0..1 motion percentage and has no public score here.
    if clean["operational_eligible"] and clean["quality"]["usable"] and clean.get("congestion_score") is not None:
        public.update(status="live", reason="Fresh camera evidence passed the registered independent validation gates",
                      congestion_score=clean["congestion_score"], severity=clean.get("camera_congestion_severity"),
                      operational_eligible=True)
        public["validation_status"] = "validated"
        return public, clean
    estimate = _estimate(clean, config, row)
    if estimate is not None:
        score, severity = estimate
        policy = row["estimation_config"]
        public.update(status="estimated", reason="Live YOLO26 road-view estimate; independent camera accuracy and thresholds are unvalidated",
                      congestion_score=score, severity=severity, validation_status="estimated",
                      routing_enabled=bool(policy.get("allow_routing") and policy.get("graph_revision") and config.road_segment_id))
        return public, clean
    if not calibration_ready:
        return public, None
    public.update(status="unavailable", reason="Camera evidence has not passed its temporal or image-quality requirements")
    return public, None


def camera_status(*, now=None) -> dict[str, Any]:
    """Safe public camera health. No private stream URL or validation receipt escapes."""
    instant = _now(now)
    summary = dict.fromkeys(("listed", "configured", "live", "estimated", "stale", "unavailable", "permission_pending", "calibration_pending"), 0)
    try:
        rows = load_registry()
    except CCTVStoreUnavailable:
        return {"status": "unavailable", "message": "Camera configuration is unavailable", "updated_at": _iso(instant),
                "summary": summary, "cameras": []}
    summary["listed"] = len(rows)
    summary["configured"] = sum(CameraConfig.from_dict(row["analysis_config"]).permission_ready for row in rows)
    storage_error = False
    try:
        latest = _read_latest(rows)
    except CCTVStoreUnavailable:
        latest, storage_error = {}, True
    cameras = []
    availability = cctv_availability.MONITOR.snapshot(rows, now=instant)
    for row in rows:
        public, _ = _assess(row, latest.get(row["camera_id"]), instant)
        public["source_availability"] = availability["sources"][row["camera_id"]]
        if storage_error and public["status"] not in {"permission_pending", "disabled"}:
            public.update(status="unavailable", reason="Camera aggregate storage is unavailable")
        cameras.append(public)
        if public["status"] in summary:
            summary[public["status"]] += 1
    if storage_error:
        status, message = "unavailable", "Camera aggregate storage is unavailable"
    elif summary["live"]:
        status = "active" if summary["live"] == summary["configured"] else "degraded"
        message = f"{summary['live']} camera sources have fresh independently validated congestion evidence"
    elif summary["estimated"]:
        status, message = "active", f"{summary['estimated']} camera sources have fresh YOLO26 congestion estimates; independent validation is pending"
    elif latest:
        status, message = "degraded", "Camera observations are present but none currently qualify for operational congestion"
    else:
        status, message = "awaiting_feeds", "Awaiting authorized and calibrated camera feeds; no live camera congestion is available"
    return {"status": status, "message": message, "updated_at": _iso(instant), "summary": summary, "cameras": cameras,
            "availability_summary": availability["summary"], "availability_policy": availability["policy"]}


def corridor_scores(*, now=None) -> dict[str, dict[str, Any]]:
    """Maximum validated camera score per explicitly mapped corridor, never km/h.

    This reports the worst observed camera region among the available registered
    cameras, not complete corridor coverage. Oldest contributing time governs age.
    """
    instant = _now(now)
    try:
        rows = load_registry()
        latest = _read_latest(rows)
    except CCTVStoreUnavailable:
        return {}
    grouped = {}
    for row in rows:
        public, evidence = _assess(row, latest.get(row["camera_id"]), instant)
        corridor = row["analysis_config"]["corridor_id"]
        if not corridor or not public["operational_eligible"] or evidence is None:
            continue
        score = evidence.get("congestion_score")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0. <= score <= 100.:
            continue
        captured = timestamp(evidence["captured_at"])
        group = grouped.setdefault(corridor, {"scores": [], "camera_ids": [], "times": []})
        group["scores"].append(float(score)); group["camera_ids"].append(row["camera_id"]); group["times"].append(captured)
    return {corridor: {"congestion_index": max(group["scores"]), "source": "cctv_camera",
                       "camera_ids": sorted(group["camera_ids"]), "observed_at": _iso(min(group["times"])),
                       "age_seconds": (instant - min(group["times"])).total_seconds(), "operational_eligible": True}
            for corridor, group in grouped.items()}


def road_segment_scores(*, now=None) -> list[dict[str, Any]]:
    """Exact road-view estimates for routing; callers must verify graph/edge identity."""
    instant = _now(now)
    try:
        rows = load_registry()
        latest = _read_latest(rows)
    except CCTVStoreUnavailable:
        return []
    result = []
    for row in rows:
        public, evidence = _assess(row, latest.get(row["camera_id"]), instant)
        config = CameraConfig.from_dict(row["analysis_config"])
        policy = row.get("estimation_config") or {}
        estimated_allowed = public["status"] == "estimated" and policy.get("allow_routing") is True
        if not (config.road_segment_id and evidence and public["congestion_score"] is not None
                and (public["operational_eligible"] or estimated_allowed)):
            continue
        result.append({"road_segment_id": config.road_segment_id, "camera_id": config.camera_id,
                       "congestion_index": public["congestion_score"], "observed_at": public["observed_at"],
                       "age_seconds": (instant - timestamp(public["observed_at"])).total_seconds(),
                       "maximum_age_seconds": config.maximum_age_seconds,
                       "graph_revision": policy.get("graph_revision"), "source": "cctv_camera",
                       "operational_eligible": public["operational_eligible"], "estimated_allowed": estimated_allowed,
                       "validation_status": public["validation_status"], "timestamp_basis": public["timestamp_basis"]})
    return result
