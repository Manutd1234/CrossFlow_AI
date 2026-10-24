"""Local CCTV supervision with bounded, allowlisted diagnostic metadata.

Collectors keep temporal state across temporary source failures. Diagnostics
retain no footage, stream URLs, exception strings, decoder logs or credentials.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from services import cctv_store, cctv_availability

ROOT = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger(__name__)
MAX_EVENT_BYTES = 4096
REGISTRY_REFRESH_SECONDS = 5.
MODEL_AND_IO_GRACE_SECONDS = 60.
STAGES = {"startup", "registry", "playlist_fetch", "playlist_parse", "segment_fetch", "segment_decode", "frame_decode", "inference", "evidence_store", "shutdown"}
CODES = {"ok", "waiting_segment", "segment_expired", "source_http_temporary", "source_http_rejected", "source_tls_rejected",
         "source_timeout", "source_network_unavailable", "source_stalled", "source_stale", "registry_changed",
         "authorization_missing", "source_clock_reversed", "source_sequence_conflict", "source_replay", "source_clock_future",
         "source_configuration_blocked", "evidence_rejected", "detector_unavailable", "segment_decode_failed",
         "collector_unavailable", "worker_protocol_invalid", "process_exit", "runtime_unavailable",
         "sampling_slice_complete", "collector_deadline"}
CODES.update({"delivery_unavailable", "delivery_rejected", "delivery_configuration"})
EVENTS = {"started", "progress", "waiting", "retrying", "completed", "failed"}


def _worker_environment():
    # The detector needs runtime paths and its local store, not API credentials.
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "CROSSFLOW_CCTV_REGISTRY", "CROSSFLOW_CCTV_DB",
               "CROSSFLOW_CCTV_INGEST_URL", "CROSSFLOW_CCTV_INGEST_TOKEN"}
    return {**{key: value for key, value in os.environ.items() if key in allowed},
            "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"}


def _invalid_constant(_value):
    raise ValueError("Non-finite worker metadata")


def parse_worker_event(line, camera_id):
    """Extract finite numeric/status metadata; discard every unapproved field."""
    if not isinstance(line, bytes) or not 0 < len(line) <= MAX_EVENT_BYTES:
        return None
    try:
        value = json.loads(line, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    # The final successful receipt intentionally has no camera event envelope.
    if value.get("ok") is True and isinstance(value.get("result"), dict):
        value = {"schema": "crossflow.cctv_worker_event.v1", "camera_id": camera_id,
                 "event": "completed", "stage": "shutdown", "code": "ok", **{
                     k: v for k, v in value["result"].items() if k in {"accepted", "frames", "sample_interval_skips"}}}
    if (value.get("schema") != "crossflow.cctv_worker_event.v1" or value.get("camera_id") != camera_id
            or any(not isinstance(value.get(k), str) for k in ("event", "stage", "code"))
            or value.get("event") not in EVENTS or value.get("stage") not in STAGES or value.get("code") not in CODES):
        return None
    safe = {key: value[key] for key in ("camera_id", "event", "stage", "code")}
    for key in ("accepted", "frames", "segment_accepted", "consecutive_failures", "sample_interval_skips"):
        if key in value:
            if type(value[key]) is not int or not 0 <= value[key] <= 1_000_000:
                return None
            safe[key] = value[key]
    if "quality_usable" in value:
        if type(value["quality_usable"]) is not bool:
            return None
        safe["quality_usable"] = value["quality_usable"]
    if "retry_in_seconds" in value:
        delay = value["retry_in_seconds"]
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 60:
            return None
        safe["retry_in_seconds"] = float(delay)
    return safe


def _bounded_setting(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _same_collection_configuration(left, right):
    if left is None or right is None:
        return False
    return all(left.get(key) == right.get(key) for key in (
        "analysis_config", "collector_config", "estimation_config", "stream_url",
    ))


class CameraSamplingSchedule:
    """Fair queue with independent source backoff, not a camera-count cutoff.

    The caller holds the runtime lock. One failed source never occupies a slot
    while backing off, and no camera can be acquired by two slots at once.
    """
    def __init__(self):
        self.camera_ids = ()
        self.pending = deque()
        self.active = set()
        self.retry_after = {}
        self.failures = {}
        self.capacity = 2

    def reconcile(self, camera_ids):
        self.camera_ids = tuple(dict.fromkeys(camera_ids))[:cctv_store.MAX_CAMERAS]
        allowed = set(self.camera_ids)
        self.pending = deque(camera_id for camera_id in self.pending if camera_id in allowed)
        present = set(self.pending) | self.active
        self.pending.extend(camera_id for camera_id in self.camera_ids if camera_id not in present)
        self.retry_after = {key: value for key, value in self.retry_after.items() if key in allowed}
        self.failures = {key: value for key, value in self.failures.items() if key in allowed}

    def acquire(self, now):
        if len(self.active) >= self.capacity:
            return None
        for _ in range(len(self.pending)):
            camera_id = self.pending.popleft()
            if camera_id not in self.active and self.retry_after.get(camera_id, 0.) <= now:
                self.active.add(camera_id)
                return camera_id
            self.pending.append(camera_id)
        return None

    def complete(self, camera_id, now, *, failed=False):
        self.active.discard(camera_id)
        if camera_id not in self.camera_ids:
            return
        failures = min(self.failures.get(camera_id, 0) + 1, 5) if failed else 0
        self.failures[camera_id] = failures
        self.retry_after[camera_id] = now + min(60., 2. * 2 ** failures) if failed else now
        if camera_id not in self.pending:
            self.pending.append(camera_id)


class CCTVRuntime:
    def __init__(self):
        self._stop = threading.Event()
        self._threads = []
        self._processes = {}
        self._lock = threading.Lock()
        self._diagnostics = {}
        self._schedule = CameraSamplingSchedule()
        self._rows = {}
        self._registry_available = False
        self._availability_rows = []
        self._max_concurrent = 2
        self._slice_seconds = 60

    def start(self):
        if self._threads or os.environ.get("VERCEL") or os.environ.get("CROSSFLOW_CCTV_ENABLED", "1") != "1":
            return
        python = Path(os.environ.get("CROSSFLOW_CCTV_PYTHON") or ROOT / ".venv-yolo26/bin/python")
        hardware_limit = max(1, min(8, (os.cpu_count() or 2) // 2))
        self._max_concurrent = _bounded_setting("CROSSFLOW_CCTV_MAX_CONCURRENT", min(2, hardware_limit), 1, hardware_limit)
        self._schedule.capacity = self._max_concurrent
        self._slice_seconds = _bounded_setting("CROSSFLOW_CCTV_SLICE_SECONDS", 60, 30, 600)
        self._refresh_registry()
        self._stop.clear()
        registry_thread = threading.Thread(target=self._watch_registry, name="crossflow-cctv-registry", daemon=True)
        self._threads.append(registry_thread)
        registry_thread.start()
        cctv_availability.MONITOR.start()
        if not python.is_file():
            LOGGER.info("[cctv] metadata checks active; optional YOLO26 runtime is not installed")
            return
        for slot in range(self._max_concurrent):
            thread = threading.Thread(target=self._sampling_slot, args=(python,),
                                      name=f"crossflow-cctv-slot-{slot + 1}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _refresh_registry(self):
        try:
            rows = cctv_store.load_registry()
            enabled = {row["camera_id"]: row for row in rows
                       if row["analysis_config"]["enabled"] and row["analysis_config"]["analysis_authorized"]}
            available = True
        except cctv_store.CCTVStoreUnavailable:
            rows, enabled, available = [], {}, False
        # Availability checks include disabled/unreviewed registered public
        # sources; they cannot grant inference, calibration or routing rights.
        cctv_availability.MONITOR.reconcile(rows)
        with self._lock:
            changed = [key for key, value in enabled.items()
                       if not _same_collection_configuration(self._rows.get(key), value)]
            self._rows = enabled
            self._registry_available = available
            self._availability_rows = rows
            self._schedule.reconcile(enabled)
            for key in changed:
                self._schedule.retry_after.pop(key, None)
                self._schedule.failures.pop(key, None)
            retained = set(enabled) | set(self._processes)
            self._diagnostics = {key: value for key, value in self._diagnostics.items() if key in retained}

    def _watch_registry(self):
        while not self._stop.wait(REGISTRY_REFRESH_SECONDS):
            self._refresh_registry()

    def _sampling_duration(self, row, *, rotating):
        if not rotating:
            return 3600
        config = row["analysis_config"]
        interval = row.get("collector_config", {}).get("frame_interval_seconds", 2.)
        # Five seconds of playback proves liveness, not a temporal traffic
        # measurement. Allow warmup, stationary tracking and persistence first.
        temporal = max(config.get("minimum_window_seconds", 10.),
                       config.get("stationary_seconds", 15.),
                       interval * config.get("minimum_samples", 5))
        return min(3600, max(self._slice_seconds, math.ceil(temporal + config.get("congestion_persistence_seconds", 15.) + 10)))

    def _sampling_slot(self, python):
        while not self._stop.is_set():
            with self._lock:
                camera_id = self._schedule.acquire(time.monotonic())
                row = self._rows.get(camera_id)
                duration = self._sampling_duration(row, rotating=len(self._rows) > self._max_concurrent) if row else None
            if row is None:
                self._stop.wait(.25)
                continue
            outcome = self._collect_once(python, camera_id, duration, row)
            with self._lock:
                self._schedule.complete(camera_id, time.monotonic(), failed=outcome == "failed")

    def _record(self, event):
        event = {**event, "received_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            state = self._diagnostics.setdefault(event["camera_id"], {"recent_events": deque(maxlen=20)})
            state["last_event"] = event
            state["recent_events"].append(event)
            if event["event"] == "progress":
                state["last_progress_at"] = event["received_at"]
            if event["event"] in {"retrying", "failed"}:
                state["last_failure"] = event
        if event["event"] in {"retrying", "failed"}:
            LOGGER.warning("[cctv] camera %s %s at %s: %s", event["camera_id"], event["event"], event["stage"], event["code"])

    def diagnostics(self):
        """Internal operator diagnostics; no endpoint or frontend wiring implied."""
        with self._lock:
            count = len(self._rows)
            rotating = count > self._max_concurrent
            durations = [self._sampling_duration(row, rotating=True) for row in self._rows.values()]
            return {"scheduler": {
                    "policy": "bounded_fair_rotation",
                    "registry_available": self._registry_available,
                    "eligible_cameras": count,
                    "max_concurrent_collectors": self._max_concurrent,
                    "running_collectors": len(self._processes),
                    "queued_cameras": len(self._schedule.pending),
                    "mode": "rotating" if rotating else "continuous",
                    "minimum_slice_seconds": self._slice_seconds,
                    "nominal_cycle_seconds": math.ceil(count / self._max_concurrent) * max(durations) if rotating else None,
                    "cycle_excludes_startup_source_delays_and_backoff": True,
                    "freshness_limits_unchanged": True,
                }, "availability": cctv_availability.MONITOR.snapshot(self._availability_rows),
                "collectors": [{"camera_id": camera_id,
                    "running": camera_id in self._processes,
                    "last_event": dict(state["last_event"]),
                    "last_progress_at": state.get("last_progress_at"),
                    "last_failure": dict(state["last_failure"]) if state.get("last_failure") else None,
                    "recent_events": [dict(e) for e in state["recent_events"]]}
                   for camera_id, state in sorted(self._diagnostics.items())]}

    def _read_events(self, process, camera_id):
        pipe = process.stdout
        if pipe is None:
            return
        try:
            while True:
                line = pipe.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT_BYTES:
                    while line and not line.endswith(b"\n"):
                        line = pipe.readline(MAX_EVENT_BYTES + 1)
                    continue
                event = parse_worker_event(line, camera_id)
                if event is not None:
                    self._record(event)
        except (OSError, ValueError):
            pass

    def _collect_once(self, python, camera_id, duration=3600, row=None):
        process = reader = None
        outcome = "failed"
        try:
            if self._stop.is_set():
                return "cancelled"
            started = time.monotonic()
            process = subprocess.Popen(
                [str(python), str(ROOT / "scripts/cctv_capture.py"), "--camera-id", camera_id, "--max-seconds", str(duration)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=_worker_environment(),
            )
            with self._lock:
                self._processes[camera_id] = process
            reader = threading.Thread(target=self._read_events, args=(process, camera_id), daemon=True)
            reader.start()
            while process.poll() is None:
                with self._lock:
                    current = self._rows.get(camera_id)
                    rotating = len(self._rows) > self._max_concurrent
                elapsed = time.monotonic() - started
                if self._stop.is_set() or (row is not None and not _same_collection_configuration(current, row)):
                    outcome = "cancelled"
                    break
                if rotating and row is not None and elapsed >= self._sampling_duration(row, rotating=True) + MODEL_AND_IO_GRACE_SECONDS:
                    outcome = "rotated"
                    break
                if elapsed >= duration + MODEL_AND_IO_GRACE_SECONDS:
                    outcome = "timed_out"
                    self._record({"camera_id": camera_id, "event": "failed", "stage": "shutdown", "code": "collector_deadline"})
                    break
                self._stop.wait(.5)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.)
                except subprocess.TimeoutExpired:
                    process.kill()
            code = process.wait(timeout=5.)
            reader.join(timeout=1.)
            if outcome == "rotated":
                self._record({"camera_id": camera_id, "event": "completed", "stage": "shutdown", "code": "sampling_slice_complete"})
            elif outcome != "cancelled":
                outcome = "completed" if code == 0 and outcome != "timed_out" else "failed"
                if code and not self._stop.is_set():
                    with self._lock:
                        last = self._diagnostics.get(camera_id, {}).get("last_event", {})
                    if last.get("event") != "failed":
                        self._record({"camera_id": camera_id, "event": "failed", "stage": "shutdown", "code": "process_exit"})
        except (OSError, subprocess.SubprocessError):
            self._record({"camera_id": camera_id, "event": "failed", "stage": "startup", "code": "runtime_unavailable"})
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5.)
                except (OSError, subprocess.SubprocessError):
                    # Do not release a process slot while an unreaped child is
                    # still alive. The daemon slot remains occupied until it exits.
                    self._record({"camera_id": camera_id, "event": "failed", "stage": "shutdown", "code": "collector_deadline"})
                    while process.poll() is None:
                        time.sleep(.5)
            with self._lock:
                self._processes.pop(camera_id, None)
            if process is not None and process.stdout is not None:
                process.stdout.close()
        return outcome

    def stop(self):
        self._stop.set()
        cctv_availability.MONITOR.stop()
        with self._lock:
            processes = tuple(self._processes.values())
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=6.)
        self._threads = [thread for thread in self._threads if thread.is_alive()]


RUNTIME = CCTVRuntime()
