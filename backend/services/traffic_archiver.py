"""Opt-in local background collection of fresh, trusted provider speed samples."""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from services import clock, historical_store, live_traffic, routing_intelligence_store
from services.operations_analytics import _sample
from services.traffic_observations import SpatialTrafficObservation


INTERVAL_SECONDS = 120


def collection_allowed() -> tuple[bool, str]:
    if os.environ.get("CROSSFLOW_ARCHIVE_LIVE_TRAFFIC", "").strip() != "1":
        return False, "disabled"
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return False, "local_only"
    if routing_intelligence_store.shared_store_enabled():
        return False, "local_store_required"
    return True, "enabled"


class LiveTrafficArchiver:
    def __init__(self, store=None):
        self._store = store if store is not None else historical_store
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: dict[str, Any] = {
            "active": False, "collection_started_at": None,
            "last_attempt_at": None, "last_success_at": None,
            "last_observation_at": None, "inserted_total": 0,
            "last_received": 0, "last_inserted": 0, "last_duplicates": 0,
            "last_error_code": None,
        }

    def status(self) -> dict[str, Any]:
        allowed, reason = collection_allowed()
        with self._lock:
            return {
                **self._state,
                "enabled": allowed, "configuration_status": reason,
                "interval_seconds": INTERVAL_SECONDS,
                "storage_scope": "local_sqlite",
                "source": "tomtom_live", "observed_only": True,
                "history_begins_at_collection": True,
                "counter_scope": "current_process",
            }

    def collect_once(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        allowed, reason = collection_allowed()
        if not allowed:
            return {"status": reason, "inserted": 0}
        now = now or clock.now()
        with self._lock:
            self._state["last_attempt_at"] = clock.iso(now)
        try:
            segments = live_traffic._tomtom_segments(live_traffic._tomtom_api_key())
            records = []
            for corridor_id, (lat, lng) in live_traffic._CORRIDOR_POINTS.items():
                segment = segments.get(corridor_id) if isinstance(segments, dict) else None
                if isinstance(segment, dict) and segment.get("source", "tomtom_live") != "tomtom_live":
                    continue
                sample = _sample(segment, now)
                if sample is None:
                    continue
                observed_at = datetime.fromisoformat(sample["as_of"]).astimezone(timezone.utc)
                event_id = hashlib.sha256(f"tomtom_live:{corridor_id}:{observed_at.isoformat()}".encode()).hexdigest()
                records.append(SpatialTrafficObservation.create(
                    corridor_id=corridor_id, observed_at=observed_at,
                    latitude=lat, longitude=lng,
                    actual_speed_kph=sample["current_speed_kmh"],
                    free_flow_speed_kph=sample["free_flow_speed_kmh"],
                    source="tomtom_live", upstream_event_id=f"tomtom-{event_id}",
                    validation_now=now,
                ))
            if not records:
                with self._lock:
                    self._state["last_received"] = 0
                    self._state["last_inserted"] = 0
                    self._state["last_duplicates"] = 0
                    self._state["last_error_code"] = "provider_samples_unavailable"
                return {"status": "provider_samples_unavailable", "inserted": 0}
            ingestion_time = max(now, max(record.observed_at for record in records))
            result = self._store.ingest_spatial_batch(records, now=ingestion_time)
            # Archive weather independently. A weather outage must not discard
            # valid traffic; missing context is explicitly masked by training.
            if os.environ.get('CROSSFLOW_REAL_TRAFFIC_TRAINING') == '1':
                from services import live_weather
                live_weather.get_weather(ingestion_time)
            latest = max(item.observed_at for item in records)
            with self._lock:
                self._state.update({
                    "last_success_at": clock.iso(now),
                    "last_observation_at": clock.iso(latest),
                    "last_received": result.received, "last_inserted": result.inserted,
                    "last_duplicates": result.duplicates, "last_error_code": None,
                })
                self._state["inserted_total"] += result.inserted
            return {"status": "collected", "received": result.received, "inserted": result.inserted, "duplicates": result.duplicates}
        except Exception:
            with self._lock:
                self._state["last_error_code"] = "collection_unavailable"
            return {"status": "collection_unavailable", "inserted": 0}

    def start(self) -> bool:
        allowed, _reason = collection_allowed()
        if not allowed:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._state["collection_started_at"] = clock.iso(clock.now())
            self._state["active"] = True
            self._thread = threading.Thread(target=self._run, name="crossflow-live-traffic-archive", daemon=True)
            self._thread.start()
        return True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.collect_once()
                if os.environ.get('CROSSFLOW_REAL_TRAFFIC_TRAINING') == '1':
                    try:
                        from services import forecast_evaluation, real_traffic_model
                        rows, _available, _archive = real_traffic_model.snapshot()
                        forecast_evaluation.score(rows, clock.now())
                        value = real_traffic_model.forecast(clock.now())
                        forecast_evaluation.record(value, clock.now())
                        with self._lock:
                            self._state['evaluation_status'] = 'prospective_predictions_recorded'
                    except Exception:
                        with self._lock:
                            self._state['evaluation_status'] = 'waiting_for_fresh_inputs_or_model'
                if self._stop.wait(INTERVAL_SECONDS):
                    break
        finally:
            with self._lock:
                self._state["active"] = False

    def stop(self) -> None:
        self._stop.set()
        worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=10)


ARCHIVER = LiveTrafficArchiver()
