"""Shared, service-only latest CCTV aggregates for ephemeral API instances.

Only numeric evidence already checked by cctv_store crosses this adapter. It
never fetches a camera or stores media. A database RPC serializes each camera's
immutable capture watermark so two collectors cannot overwrite newer evidence.
"""
from __future__ import annotations

import os
from services import supabase_server

TABLE_PATH = "/rest/v1/crossflow_cctv_latest"
INGEST_PATH = "/rest/v1/rpc/crossflow_ingest_cctv_observation"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class SharedCCTVUnavailable(RuntimeError):
    """Safe shared-store failure with no server credentials or response text."""


class SharedCCTVConflict(ValueError):
    """An immutable camera capture watermark rejected an older/conflicting row."""


def enabled():
    value = os.environ.get("CROSSFLOW_CCTV_STORE", "").strip().lower()
    if value not in {"", "local", "supabase"}:
        raise SharedCCTVUnavailable("Invalid CCTV storage mode")
    # A serverless /tmp file is neither durable nor shared between requests.
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV") in {"preview", "production"}:
        if value == "local":
            raise SharedCCTVUnavailable("Serverless CCTV requires shared storage")
        return True
    return value == "supabase"


def _request(**kwargs):
    config = supabase_server.load_server_config()
    if config is None:
        raise SharedCCTVUnavailable("Shared CCTV storage is not configured")
    try:
        return supabase_server.request_json(config, **kwargs)
    except supabase_server.SupabaseServerError as error:
        raise SharedCCTVUnavailable("Shared CCTV storage is unavailable") from error


def latest(camera_ids):
    if not camera_ids:
        return {}
    # IDs are validated registry values, never user-provided filter fragments.
    if len(camera_ids) > 128 or any(not isinstance(value, str) or not value or len(value) > 128
                                    or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for c in value)
                                    for value in camera_ids):
        raise SharedCCTVUnavailable("Invalid registered camera identifiers")
    rows = _request(method="GET", rest_path=TABLE_PATH,
                    query={"camera_id": "in.(" + ",".join(camera_ids) + ")",
                           "select": "camera_id,captured_at,evidence_json", "limit": "128"},
                    max_response_bytes=MAX_RESPONSE_BYTES)
    if not isinstance(rows, list) or len(rows) > 128:
        raise SharedCCTVUnavailable("Invalid shared CCTV response")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"camera_id", "captured_at", "evidence_json"}
                or row["camera_id"] not in camera_ids or row["camera_id"] in result
                or not isinstance(row["captured_at"], str) or not isinstance(row["evidence_json"], dict)):
            raise SharedCCTVUnavailable("Invalid shared CCTV observation")
        result[row["camera_id"]] = {"captured_at": row["captured_at"], "evidence": row["evidence_json"]}
    return result


def ingest(clean, receipt, *, stored_at):
    row = _request(method="POST", rest_path=INGEST_PATH,
                   payload={"p_camera_id": clean["camera_id"], "p_captured_at": clean["captured_at"],
                            "p_evidence_sha256": receipt, "p_evidence": clean, "p_stored_at": stored_at},
                   max_response_bytes=4096)
    if not isinstance(row, dict) or row.get("status") not in {"accepted", "conflict", "capacity"}:
        raise SharedCCTVUnavailable("Invalid shared CCTV ingest receipt")
    if row["status"] == "conflict":
        raise SharedCCTVConflict("Camera timestamp conflicts with its immutable watermark")
    if row["status"] == "capacity":
        raise SharedCCTVUnavailable("Shared CCTV camera capacity reached")
    if (type(row.get("idempotent")) is not bool or row.get("camera_id") != clean["camera_id"]
            or row.get("observed_at") != clean["captured_at"]):
        raise SharedCCTVUnavailable("Invalid shared CCTV ingest receipt")
    return {"accepted": True, "idempotent": row["idempotent"], "camera_id": clean["camera_id"],
            "observed_at": clean["captured_at"], "operational_eligible": clean["operational_eligible"]}
