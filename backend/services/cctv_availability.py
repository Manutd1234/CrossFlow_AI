"""Bounded public-HLS metadata checks, independent of traffic authorization.

Only playlist bytes enter RAM. No media segment is requested, decoded or kept.
An advancing sequence is source availability evidence, not proof of physical
capture time, playable video, vehicle accuracy, or permission to run inference.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import math
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_SOURCES = 128
MAX_PLAYLIST_BYTES = 65_536
MAX_LINES = 2_000
WORKERS = 2
POLL_SECONDS = 15.
RETRY_SECONDS = 60.
SOCKET_TIMEOUT_SECONDS = 3.
READ_DEADLINE_SECONDS = 6.
CHECK_EXPIRY_SECONDS = 90.


class SourceCheckError(ValueError):
    def __init__(self, code, *, status="invalid"):
        super().__init__(code)
        self.code, self.status = code, status


def _instant(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Availability timestamps require a timezone")
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _url(value):
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or any(ord(c) < 32 for c in value):
        raise SourceCheckError("public_hls_not_configured", status="unsupported")
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError
        parsed.port  # Reject invalid ports before network access.
    except ValueError:
        raise SourceCheckError("public_hls_not_configured", status="unsupported") from None
    return value


def _playlist_url(value):
    value = _url(value)
    if not urlparse(value).path.lower().endswith(".m3u8"):
        raise SourceCheckError("public_hls_not_configured", status="unsupported")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceCheckError("redirect_not_registered", status="unsupported")


def fetch_playlist(url):
    request = Request(_playlist_url(url), headers={"User-Agent": "CrossFlow-public-HLS-availability/1.0", "Accept-Encoding": "identity"})
    with build_opener(_NoRedirect).open(request, timeout=SOCKET_TIMEOUT_SECONDS) as response:
        if response.headers.get("Content-Encoding", "identity") not in {"identity", ""}:
            raise SourceCheckError("unsupported_envelope")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type.startswith(("image/", "audio/", "video/")) and content_type not in {"audio/mpegurl", "audio/x-mpegurl"}:
            raise SourceCheckError("media_response_not_playlist", status="unsupported")
        read = getattr(response, "read1", None)
        if not callable(read):
            raise SourceCheckError("unsupported_transport", status="unsupported")
        deadline, chunks, size = time.monotonic() + READ_DEADLINE_SECONDS, [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            chunk = read(min(4096, MAX_PLAYLIST_BYTES + 1 - size))
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > MAX_PLAYLIST_BYTES:
                raise SourceCheckError("playlist_too_large")
            chunks.append(chunk)


def parse_metadata(body, source):
    """Validate direct unencrypted HLS; retain only safe scalars plus private hash."""
    _playlist_url(source)
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_PLAYLIST_BYTES:
        raise SourceCheckError("playlist_size_invalid")
    try:
        lines = body.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise SourceCheckError("playlist_encoding_invalid") from None
    if not lines or lines[0].strip() != "#EXTM3U" or len(lines) > MAX_LINES:
        raise SourceCheckError("playlist_invalid")
    sequence, duration, clock, source_end, segments, target, last_url = None, None, None, None, 0, None, None
    origin = urlparse(source)
    for raw in lines[1:]:
        line = raw.strip()
        if line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-MAP", "#EXT-X-BYTERANGE")):
            raise SourceCheckError("direct_ts_playlist_required", status="unsupported")
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise SourceCheckError("encrypted_playlist", status="unsupported")
        try:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                sequence = int(line.split(":", 1)[1])
                if sequence < 0 or sequence > 2**63 - 1:
                    raise ValueError
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                target = float(line.split(":", 1)[1])
                if not math.isfinite(target) or not .1 <= target <= 120:
                    raise ValueError
            elif line == "#EXT-X-DISCONTINUITY":
                clock = None
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                clock = _instant(datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00")))
            elif line.startswith("#EXTINF:"):
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
                if not math.isfinite(duration) or not .1 <= duration <= 30:
                    raise ValueError
            elif line and not line.startswith("#"):
                if duration is None:
                    raise ValueError
                last_url = _url(urljoin(source, line))
                destination = urlparse(last_url)
                if (origin.scheme, origin.hostname, origin.port) != (destination.scheme, destination.hostname, destination.port):
                    raise SourceCheckError("cross_origin_media", status="unsupported")
                segments += 1
                source_end = clock + timedelta(seconds=duration) if clock else None
                clock, duration = source_end, None
        except SourceCheckError:
            raise
        except (ValueError, OverflowError):
            raise SourceCheckError("playlist_metadata_invalid") from None
    ended = any(line.strip() == "#EXT-X-ENDLIST" for line in lines)
    if not ended and (not segments or sequence is None):
        raise SourceCheckError("live_sequence_missing", status="unsupported")
    return {"ended": ended, "sequence": sequence + segments - 1 if sequence is not None and segments else sequence,
            "segment_identity": hashlib.sha256(last_url.encode()).hexdigest() if last_url else None,
            "segments": segments, "target_duration_seconds": target, "source_media_at": source_end}


def _failure(error):
    if isinstance(error, SourceCheckError):
        return error.status, error.code
    if isinstance(error, HTTPError):
        return "unreachable", "http_unavailable"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "unreachable", "source_timeout"
    if isinstance(error, URLError) and isinstance(error.reason, ssl.SSLCertVerificationError):
        return "unreachable", "tls_unavailable"
    return "unreachable", "source_unavailable"


class AvailabilityMonitor:
    def __init__(self, *, fetch=fetch_playlist, monotonic=time.monotonic, utcnow=None):
        self._fetch, self._monotonic = fetch, monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._rows, self._states, self._due = {}, {}, {}
        self._queue, self._active, self._threads = deque(), set(), []

    @staticmethod
    def _configuration(row):
        url = row.get("stream_url")
        age = row.get("analysis_config", {}).get("maximum_age_seconds", 30.)
        if type(age) not in (int, float) or not math.isfinite(age) or not 1 <= age <= 120:
            age = 30.
        return url, float(age)

    def reconcile(self, rows):
        current = {row["camera_id"]: self._configuration(row) for row in list(rows)[:MAX_SOURCES]}
        with self._lock:
            for camera_id, config in current.items():
                if self._rows.get(camera_id) != config:
                    self._states.pop(camera_id, None)
                    self._due[camera_id] = 0.
            self._rows = current
            self._states = {key: value for key, value in self._states.items() if key in current}
            self._due = {key: value for key, value in self._due.items() if key in current}
            self._queue = deque(key for key in self._queue if key in current)
            present = set(self._queue)
            self._queue.extend(key for key in current if key not in present)

    def start(self):
        with self._lifecycle_lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if self._threads:
                return  # A previous bounded stop may still have a request in flight.
            self._stop.clear()
            self._threads = [threading.Thread(target=self._worker, name=f"crossflow-cctv-availability-{slot + 1}", daemon=True)
                             for slot in range(WORKERS)]
            for thread in self._threads:
                thread.start()

    def stop(self):
        with self._lifecycle_lock:
            self._stop.set()
            deadline = time.monotonic() + 10.
            for thread in self._threads:
                thread.join(max(0., deadline - time.monotonic()))
            self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _worker(self):
        while not self._stop.is_set():
            if not self.check_once():
                self._stop.wait(.25)

    def check_once(self):
        """Claim one source fairly; two worker slots bound all network activity."""
        with self._lock:
            claim = None
            if len(self._active) >= WORKERS:
                return False
            for _ in range(len(self._queue)):
                camera_id = self._queue.popleft()
                self._queue.append(camera_id)
                if camera_id not in self._active and self._due.get(camera_id, 0.) <= self._monotonic():
                    claim = camera_id, self._rows[camera_id]
                    self._active.add(camera_id)
                    break
        if claim is None:
            return False
        camera_id, config = claim
        metadata, failure, reachable = None, None, False
        try:
            body = self._fetch(_playlist_url(config[0]))
            reachable = True
            metadata = parse_metadata(body, config[0])
        except Exception as error:
            failure = _failure(error)
        now = _instant(self._utcnow())
        with self._lock:
            self._active.discard(camera_id)
            if self._rows.get(camera_id) != config:
                return True  # Removed/replaced while a request was in flight.
            state = self._states.setdefault(camera_id, {})
            state["checked_at"] = now
            state["reachable"] = reachable
            state["playlist_validated"] = metadata is not None
            if failure is not None:
                state.update(status=failure[0], code=failure[1], failures=min(1000, state.get("failures", 0) + 1))
            else:
                self._observe(state, metadata, now, config[1])
                state["failures"] = 0
            delay = POLL_SECONDS if state["status"] in {"advancing", "awaiting_progress", "stalled", "stale"} else RETRY_SECONDS
            self._due[camera_id] = self._monotonic() + delay
        return True

    @staticmethod
    def _observe(state, metadata, now, maximum_age):
        state.update(segments=metadata["segments"], sequence=metadata["sequence"],
                     target_duration_seconds=metadata["target_duration_seconds"], source_media_at=metadata["source_media_at"])
        if metadata["ended"]:
            state.update(status="ended", code="playlist_ended", anchor=None, last_progress_at=None)
            return
        source_at = metadata["source_media_at"]
        if source_at and not -15 <= (now - source_at).total_seconds() <= maximum_age:
            state.update(status="stale", code="source_clock_stale" if source_at <= now else "source_clock_future", anchor=None)
            return
        current = (metadata["sequence"], metadata["segment_identity"])
        anchor = state.get("anchor")
        state["anchor"] = current
        state.setdefault("first_seen_at", now)
        if anchor is None or current[0] < anchor[0]:
            state.update(status="awaiting_progress", code="awaiting_sequence_advance", first_seen_at=now, last_progress_at=None)
        elif current[0] > anchor[0] and current[1] != anchor[1]:
            state.update(status="advancing", code="playlist_advancing", last_progress_at=now)
        elif current != anchor:
            state.update(status="invalid", code="sequence_identity_conflict", last_progress_at=None)
        else:
            age = (now - (state.get("last_progress_at") or state["first_seen_at"])).total_seconds()
            state.update(status="stalled" if age > maximum_age else "advancing" if state.get("last_progress_at") else "awaiting_progress",
                         code="playlist_stalled" if age > maximum_age else "playlist_advancing" if state.get("last_progress_at") else "awaiting_sequence_advance")

    def snapshot(self, rows, *, now=None):
        instant = _instant(now)
        sources = {}
        with self._lock:
            for row in list(rows)[:MAX_SOURCES]:
                camera_id = row["camera_id"]
                config = self._configuration(row)
                state = self._states.get(camera_id, {}) if self._rows.get(camera_id) == config else {}
                status, code = state.get("status", "unchecked"), state.get("code", "not_checked")
                checked, progress = state.get("checked_at"), state.get("last_progress_at")
                source_at = state.get("source_media_at")
                if checked and (instant - checked).total_seconds() > CHECK_EXPIRY_SECONDS:
                    status, code = "check_expired", "availability_check_expired"
                elif status in {"advancing", "awaiting_progress"} and source_at and not -15 <= (instant - source_at).total_seconds() <= config[1]:
                    status, code = "stale", "source_clock_stale" if source_at <= instant else "source_clock_future"
                elif status == "advancing" and progress and (instant - progress).total_seconds() > config[1]:
                    status, code = "stalled", "playlist_stalled"
                sources[camera_id] = {"status": status, "code": code, "checked_at": _iso(checked),
                    "last_progress_at": _iso(progress), "source_media_at": _iso(state.get("source_media_at")),
                    "playlist_sequence": state.get("sequence"), "playlist_segments": state.get("segments"),
                    "target_duration_seconds": state.get("target_duration_seconds"), "consecutive_failures": state.get("failures", 0),
                    "reachable": state.get("reachable"), "playlist_validated": state.get("playlist_validated"),
                    "metadata_only": True, "playback_verified": False}
            workers = sum(thread.is_alive() for thread in self._threads)
        summary = {}
        for source in sources.values():
            summary[source["status"]] = summary.get(source["status"], 0) + 1
        return {"sources": sources, "summary": summary, "policy": {"scope": "registered_public_hls_playlists",
            "registered_sources": len(sources), "maximum_parallel_checks": WORKERS, "running_check_workers": workers,
            "poll_seconds": POLL_SECONDS, "failed_or_ended_retry_seconds": RETRY_SECONDS, "check_expiry_seconds": CHECK_EXPIRY_SECONDS,
            "maximum_playlist_bytes": MAX_PLAYLIST_BYTES, "media_segments_requested": False,
            "grants_inference_or_routing_eligibility": False}}


MONITOR = AvailabilityMonitor()
