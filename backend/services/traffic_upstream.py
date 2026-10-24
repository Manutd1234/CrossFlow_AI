"""Transient traffic measurements from one explicitly trusted public API.

This adapter sends no credentials and retains only validated current segments
in process memory. Upstream zones, scores and model outputs are discarded.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import logging
import math
import os
import queue
import threading
import time
from typing import Any, Mapping, Optional
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from services import tls
from services.congestion_math import speed_ratio_congestion_score


ENV_NAME = "CROSSFLOW_TRAFFIC_UPSTREAM_URL"
TRUSTED_URL = "https://cross-flow-ai.vercel.app/api/live-traffic"
TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 512 * 1024
CACHE_TTL_SECONDS = 120.0
FAILURE_CACHE_TTL_SECONDS = 15.0
MAX_SNAPSHOT_AGE_SECONDS = 180.0
MAX_FUTURE_SKEW_SECONDS = 30.0
_KNOWN_IDS = frozenset(f"corridor-{index}" for index in range(1, 6))
_LOGGER = logging.getLogger(__name__)
_CACHE_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_WORKER_SLOTS = threading.BoundedSemaphore(2)
_cache: Optional[tuple[Any, float, Optional[dict[str, Any]]]] = None


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirectHandler(), HTTPSHandler(context=tls.default_context()))


def configured() -> bool:
    """Enable the fixed public feed locally, preventing a serverless self-relay."""
    return (
        os.environ.get(ENV_NAME, "").strip() == TRUSTED_URL
        and not os.environ.get("VERCEL")
        and not os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        and os.environ.get("VERCEL_ENV", "").strip().casefold() not in {"preview", "production"}
    )


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_numeric_measurement")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("invalid_numeric_measurement")
    return numeric


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("invalid_snapshot_timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_snapshot_timestamp")
    return parsed.astimezone(timezone.utc)


def _age(timestamp: str, now: datetime) -> float:
    age = (now - _timestamp(timestamp)).total_seconds()
    if age > MAX_SNAPSHOT_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("stale_or_future_snapshot")
    return max(0.0, age)


def _validate(
    payload: Any,
    expected_points: Mapping[str, tuple[float, float]],
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("overall_source") != "tomtom_live":
        raise ValueError("not_observed_traffic")
    timestamp = _timestamp(payload.get("generated_at")).isoformat()
    age = _age(timestamp, now)
    rows = payload.get("segments")
    if not isinstance(rows, list) or len(rows) != 5 or set(expected_points) != _KNOWN_IDS:
        raise ValueError("invalid_corridor_coverage")
    segments: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid_segment")
        corridor_id = row.get("corridor_id")
        if not isinstance(corridor_id, str) or corridor_id not in _KNOWN_IDS or corridor_id in segments:
            raise ValueError("invalid_corridor_coverage")
        lat, lng = _finite(row.get("lat")), _finite(row.get("lng"))
        expected_lat, expected_lng = expected_points[corridor_id]
        if not math.isclose(lat, expected_lat, rel_tol=0, abs_tol=0.000001) or not math.isclose(
            lng, expected_lng, rel_tol=0, abs_tol=0.000001,
        ):
            raise ValueError("unexpected_corridor_coordinates")
        current = _finite(row.get("current_speed_kmh"))
        free_flow = _finite(row.get("free_flow_speed_kmh"))
        confidence = _finite(row.get("provider_confidence"))
        if (
            not 0 <= current <= 250 or not 0 < free_flow <= 250
            or current / free_flow > 1.5 or not 0 <= confidence <= 1
        ):
            raise ValueError("invalid_segment_measurement")
        if row.get("source") != "tomtom_live":
            raise ValueError("not_observed_traffic")
        segments[corridor_id] = {
            "current_speed_kmh": current,
            "free_flow_speed_kmh": free_flow,
            "confidence": confidence,
            "congestion_index": round(speed_ratio_congestion_score(current / free_flow), 1),
            "provider_transport": "public_crossflow_vercel_upstream",
            "upstream_source": TRUSTED_URL,
            "snapshot_generated_at": timestamp,
            "snapshot_age_seconds": age,
        }
    return {"generated_at": timestamp, "segments": segments}


def _read_public_snapshot() -> Any:
    """Bound total wall time as well as socket inactivity and response bytes."""
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise RuntimeError("request_capacity_saturated")
    request = Request(TRUSTED_URL, method="GET", headers={
        "Accept": "application/json", "User-Agent": "CrossFlowAI/3.0 public-traffic",
    })
    outcome: queue.Queue = queue.Queue(maxsize=1)
    response_holder: dict[str, Any] = {}
    response_lock = threading.Lock()
    cancelled = threading.Event()
    deadline = time.monotonic() + TIMEOUT_SECONDS

    def read() -> None:
        try:
            try:
                with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
                    with response_lock:
                        response_holder["response"] = response
                    if cancelled.is_set() or int(getattr(response, "status", 200)) != 200:
                        raise ValueError("unavailable_http_response")
                    chunks, total = [], 0
                    while total <= MAX_RESPONSE_BYTES:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("request_timeout")
                        chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise ValueError("response_too_large")
                outcome.put((True, b"".join(chunks)))
            except Exception:
                outcome.put((False, None))
        finally:
            with response_lock:
                response_holder.clear()
            _WORKER_SLOTS.release()

    try:
        threading.Thread(target=read, name="public-traffic-request", daemon=True).start()
    except Exception:
        _WORKER_SLOTS.release()
        raise
    try:
        succeeded, raw = outcome.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        cancelled.set()
        with response_lock:
            response = response_holder.get("response")
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        raise TimeoutError("request_timeout") from None
    if not succeeded:
        raise ValueError("unavailable_public_traffic")
    return json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(
        ValueError("non_finite_json"),
    ))


def get_segments(
    expected_points: Mapping[str, tuple[float, float]],
) -> dict[str, dict[str, Any]]:
    """Return current validated segments, or an empty mapping for simulation."""
    global _cache
    if not configured():
        return {}
    key = tuple(sorted(expected_points.items()))
    now = datetime.now(timezone.utc)
    with _CACHE_LOCK:
        cached = _cache
    if cached is not None and cached[0] == key and time.monotonic() < cached[1]:
        if cached[2] is None:
            return {}
        try:
            age = _age(cached[2]["generated_at"], now)
        except ValueError:
            pass  # A cached response can age out before its ordinary TTL.
        else:
            segments = copy.deepcopy(cached[2]["segments"])
            for segment in segments.values():
                segment["snapshot_age_seconds"] = age
            return segments
    if not _FETCH_LOCK.acquire(blocking=False):
        return {}
    try:
        try:
            snapshot = _validate(_read_public_snapshot(), expected_points, datetime.now(timezone.utc))
        except Exception as error:
            # Never print upstream bodies, response URLs or exception messages.
            _LOGGER.warning("[traffic_upstream] unavailable (%s)", type(error).__name__)
            with _CACHE_LOCK:
                _cache = (key, time.monotonic() + FAILURE_CACHE_TTL_SECONDS, None)
            return {}
        with _CACHE_LOCK:
            _cache = (key, time.monotonic() + CACHE_TTL_SECONDS, snapshot)
        return copy.deepcopy(snapshot["segments"])
    finally:
        _FETCH_LOCK.release()
