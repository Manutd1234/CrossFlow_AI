"""Collect a registered, authorized HLS camera without saving video or stills.

Only server-registered authorized cameras run. Source-clock mode requires HLS
program-date-time; explicitly registered provisional mode uses first-observed
playlist advancement and never claims the physical capture time is verified.
Both modes require a fixed view and a monitored static background polygon.
Transient source errors retry in the same worker; images remain in RAM only.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import re
import shutil
from statistics import median, pstdev
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
import socket
import ssl
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from services.cctv_analysis import CameraConfig, CCTVAnalyzer, Detection, inside_roi, timestamp
from services.cctv_store import collector_image_policy, collector_detector_id

MAX_PLAYLIST_BYTES = 256 * 1024
MAX_SEGMENT_BYTES = 12 * 1024 * 1024
MAX_DECODER_BYTES = 32 * 1024 * 1024
MAX_FRAMES = 32
# Same strict lower bound as CCTVAnalyzer.ingest; preserve source timestamps.
MIN_ANALYSIS_INTERVAL_SECONDS = .05


class CaptureBlocked(ValueError):
    """The source cannot satisfy the registered collection contract."""


class CapturePending(CaptureBlocked):
    """Await one observed playlist advance before dating an unclocked segment."""


@dataclass(frozen=True)
class Segment:
    url: str
    captured_at: datetime
    duration: float
    timestamp_basis: str = "source_clock"


class ObservedPlaylistClock:
    """Receiver-side time anchor for a progressing public playlist.

    This establishes when a new segment was first seen, not when the physical
    scene was captured. The resulting indices must remain labelled estimates.
    Repeated or stopped playlists never receive a renewed timestamp.
    """
    def __init__(self, maximum_age_seconds=15.):
        self.maximum_age_seconds = maximum_age_seconds
        self.sequence = self.url = self.segment = self.seen_at = None

    def resolve(self, sequence, url, duration, observed_at):
        if type(sequence) is not int or sequence < 0:
            raise CaptureBlocked("Unclocked HLS requires a media sequence")
        now = timestamp(observed_at)
        if self.sequence is None:
            self.sequence, self.url, self.seen_at = sequence, url, now
            raise CapturePending("Waiting for a new public playlist segment")
        if sequence < self.sequence:
            raise CaptureBlocked("Public playlist sequence moved backwards")
        if sequence == self.sequence:
            if url != self.url:
                raise CaptureBlocked("Public playlist sequence changed its segment identity")
            if (now - self.seen_at).total_seconds() > self.maximum_age_seconds:
                raise CaptureBlocked("Public playlist has stopped advancing")
            if self.segment is None:
                raise CapturePending("Waiting for a new public playlist segment")
            return self.segment
        self.sequence, self.url, self.seen_at = sequence, url, now
        self.segment = Segment(url, now - timedelta(seconds=duration), duration, "stream_observation")
        return self.segment


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CaptureBlocked("Source redirects require an explicitly registered canonical URL")


def _origin(url):
    p = urlparse(url)
    return p.scheme, p.hostname, p.port


def _url(url):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or len(url) > 4096:
        raise CaptureBlocked("Collector requires a credential-free registered HTTPS HLS source")
    return url


def fetch_bytes(url, maximum):
    _url(url)
    request = Request(url, headers={"User-Agent": "CrossFlow-authorized-camera-collector/1.0", "Accept-Encoding": "identity"})
    with build_opener(_NoRedirect).open(request, timeout=10) as response:
        if response.headers.get("Content-Encoding", "identity") not in ("identity", ""):
            raise CaptureBlocked("Compressed source envelopes are unsupported")
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise CaptureBlocked("Source response exceeds collector bounds")
    return data


def parse_live_playlist(body, source_url, *, observation_clock=None, observed_at=None):
    """Use source clock, or explicitly label a receiver-side progress estimate."""
    if not isinstance(body, bytes) or len(body) > MAX_PLAYLIST_BYTES:
        raise CaptureBlocked("Invalid HLS playlist size")
    try:
        lines = body.decode("utf-8-sig").splitlines()
    except UnicodeError as error:
        raise CaptureBlocked("Invalid HLS playlist encoding") from error
    if not lines or lines[0].strip() != "#EXTM3U" or len(lines) > 2000:
        raise CaptureBlocked("Expected a bounded HLS media playlist")
    if any(line.strip() == "#EXT-X-ENDLIST" for line in lines):
        raise CaptureBlocked("Recorded HLS playlists cannot establish current live observations")
    clock, duration, segments, sequence = None, None, [], None
    for raw in lines[1:]:
        line = raw.strip()
        if line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-MAP", "#EXT-X-BYTERANGE")):
            raise CaptureBlocked("Register a direct, unencrypted transport-stream media playlist")
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise CaptureBlocked("Encrypted HLS sources are unsupported")
        if line == "#EXT-X-DISCONTINUITY":
            clock = None
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError as error:
                raise CaptureBlocked("Invalid HLS media sequence") from error
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                clock = timestamp(line.split(":", 1)[1])
            except ValueError as error:
                raise CaptureBlocked("Invalid source capture clock") from error
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError as error:
                raise CaptureBlocked("Invalid HLS segment duration") from error
            if not math.isfinite(duration) or not .1 <= duration <= 30:
                raise CaptureBlocked("HLS segment duration exceeds live collection bounds")
        elif line and not line.startswith("#"):
            if duration is None:
                raise CaptureBlocked("HLS segment has no bounded duration")
            if clock is None and observation_clock is None:
                raise CaptureBlocked("HLS source has no program-date-time capture clock")
            target = _url(urljoin(source_url, line))
            if _origin(target) != _origin(source_url):
                raise CaptureBlocked("Cross-origin HLS segments require separate source registration")
            segments.append(Segment(target, clock, duration))
            if clock is not None:
                clock += timedelta(seconds=duration)
            duration = None
    if not segments:
        raise CaptureBlocked("HLS playlist contains no clocked media segments")
    last = segments[-1]
    if observation_clock is not None:
        return observation_clock.resolve(sequence + len(segments) - 1 if sequence is not None else None,
                                         last.url, last.duration, observed_at)
    return last


def decode_segment(data, segment, interval, *, executable=None, max_dimension=1280):
    """Decode RAM bytes only; ffmpeg cannot fetch nested network resources."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_SEGMENT_BYTES:
        raise CaptureBlocked("Invalid HLS segment size")
    if type(max_dimension) is not int or max_dimension not in {1280, 1920}:
        raise CaptureBlocked("Invalid camera decode resolution bound")
    executable = executable or shutil.which("ffmpeg")
    if not executable:
        raise CaptureBlocked("Local ffmpeg is unavailable")
    height_bound = max_dimension * 9 // 16
    filters = f"setpts=PTS-STARTPTS,select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval})',scale='min({max_dimension},iw)':'min({height_bound},ih)':force_original_aspect_ratio=decrease,showinfo"
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
               "-protocol_whitelist", "pipe", "-max_pixels", "12000000", "-i", "pipe:0",
               "-t", str(min(segment.duration, 30)), "-an", "-sn", "-vf", filters,
               "-frames:v", str(MAX_FRAMES), "-fps_mode", "passthrough", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "7", "pipe:1"]
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureBlocked("Local HLS frame decoding failed") from error
    if result.returncode or len(result.stdout) > MAX_DECODER_BYTES or len(result.stderr) > 512 * 1024:
        raise CaptureBlocked("Decoded video exceeds collector limits or is unsupported")
    offsets = [float(value) for value in re.findall(rb"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([0-9.eE+-]+)", result.stderr)]
    frames, position = [], 0
    while position < len(result.stdout):
        start = result.stdout.find(b"\xff\xd8", position)
        if start < 0:
            break
        end = result.stdout.find(b"\xff\xd9", start + 2)
        if end < 0:
            raise CaptureBlocked("Incomplete decoded JPEG frame")
        frame = result.stdout[start:end + 2]
        if len(frame) > 4 * 1024 * 1024:
            raise CaptureBlocked("Decoded frame exceeds image limits")
        frames.append(frame)
        position = end + 2
    if not frames or len(frames) != len(offsets) or len(frames) > MAX_FRAMES:
        raise CaptureBlocked("Decoded frame timestamps are unavailable")
    if any(not math.isfinite(v) or v < 0 or v > segment.duration + .01 for v in offsets) or offsets != sorted(set(offsets)):
        raise CaptureBlocked("Decoded frame times do not match their clocked segment")
    return [(segment.captured_at + timedelta(seconds=offset), frame) for offset, frame in zip(offsets, frames)]


def count_frame(counter, jpeg, *, roi, collector):
    """Optional in-memory road crop; all returned boxes use the full-frame plane.

    The model sees the cropped decoded pixels before its normal resize. This does
    not add source detail, alter its minimum object-size rules, or certify a
    previously unvalidated camera. Background/exposure checks use the full frame.
    """
    policy = collector_image_policy(collector)
    crop = policy["inference_crop"]
    if crop is None:
        result = dict(counter.count_image(jpeg, roi=roi))
    else:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(jpeg)) as image:
            if (image.format != "JPEG" or min(image.size) < 2 or max(image.size) > 6000
                    or image.width * image.height > 12_000_000):
                raise CaptureBlocked("Decoded frame exceeds image limits")
            full = ImageOps.exif_transpose(image).convert("RGB")
            width, height = full.size
            left, top = math.floor(crop[0] * width), math.floor(crop[1] * height)
            right, bottom = math.ceil(crop[2] * width), math.ceil(crop[3] * height)
            if right - left < 2 or bottom - top < 2:
                raise CaptureBlocked("Inference crop has too few decoded pixels")
            output = io.BytesIO()
            full.crop((left, top, right, bottom)).save(output, "JPEG", quality=policy["crop_jpeg_quality"])
            cropped = output.getvalue()
        if len(cropped) > 4 * 1024 * 1024:
            raise CaptureBlocked("Cropped inference image exceeds detector bounds")
        result = dict(counter.count_image(cropped, roi=None))
        if (result.get("image_width") != right - left or result.get("image_height") != bottom - top
                or not isinstance(result.get("detections"), list) or len(result["detections"]) > 200):
            raise CaptureBlocked("Detector crop coordinates do not match the inference image")
        remapped = []
        for value in result["detections"]:
            detection = Detection.from_dict(value)
            box = [
                (left + detection.box[0] * (right - left)) / width,
                (top + detection.box[1] * (bottom - top)) / height,
                (left + detection.box[2] * (right - left)) / width,
                (top + detection.box[3] * (bottom - top)) / height,
            ]
            if not inside_roi((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, roi):
                continue
            remapped.append({**value, "box": box, "box_normalized_xyxy": box,
                             "box_xyxy": [box[0] * width, box[1] * height, box[2] * width, box[3] * height]})
        result.update(detections=remapped, vehicle_count=len(remapped), image_width=width, image_height=height,
                      image={"width": width, "height": height}, roi_applied=roi is not None)
    result["model_id"] = collector_detector_id(result["model_id"], collector)
    return result


class BackgroundMonitor:
    """Conservative translation/residual check in a fixed textured background ROI.

    A low-texture, occluded, or first frame is unknown. This does not replace
    independent camera validation and cannot prove the physical scene is live.
    """
    WIDTH, HEIGHT = 64, 36

    def __init__(self, region, maximum_shift=2):
        self.region = region
        self.maximum_shift = maximum_shift
        self.reference = None
        self.points = [(x, y) for y in range(4, self.HEIGHT - 4) for x in range(4, self.WIDTH - 4)
                       if inside_roi((x + .5) / self.WIDTH, (y + .5) / self.HEIGHT, region)]
        if len(self.points) < 32:
            raise CaptureBlocked("Static background polygon is too small to monitor")

    def check(self, gray):
        thumbnail = gray.resize((self.WIDTH, self.HEIGHT))
        pixels = list(thumbnail.get_flattened_data() if hasattr(thumbnail, "get_flattened_data") else thumbnail.getdata())
        values = [pixels[y * self.WIDTH + x] for x, y in self.points]
        if pstdev(values) < 8:
            return None
        if self.reference is None:
            self.reference = pixels
            return None
        candidates = []
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                differences = [pixels[(y + dy) * self.WIDTH + x + dx] - self.reference[y * self.WIDTH + x] for x, y in self.points]
                brightness_change = median(differences)
                residual = median(abs(v - brightness_change) for v in differences)
                candidates.append((residual, abs(dx) + abs(dy), dx, dy))
        residual, _, dx, dy = min(candidates)
        return math.hypot(dx, dy) <= self.maximum_shift and residual <= 18


def collection_requirements(row):
    config = CameraConfig.from_dict(row["analysis_config"])
    if not config.permission_ready:
        raise CaptureBlocked("Camera analysis is disabled or permission has not been established")
    stream_url = _url(row.get("stream_url") or "")
    collector = row.get("collector_config") or {}
    try:
        policy = collector_image_policy(collector)
    except ValueError as error:
        raise CaptureBlocked("Invalid camera image transform configuration") from error
    crop = policy["inference_crop"]
    if crop is not None and (not config.roi or any(
        not crop[0] <= x <= crop[2] or not crop[1] <= y <= crop[3] for x, y in config.roi
    )):
        raise CaptureBlocked("Inference crop must enclose the complete registered road ROI")
    if collector.get("fixed_view_confirmed") is not True:
        raise CaptureBlocked("Camera fixed-view registration is missing")
    reference = collector.get("source_clock_validation_reference")
    if collector.get("timestamp_mode", "source_clock") == "stream_observation":
        policy = row.get("estimation_config") or {}
        if not (policy.get("enabled") is True and policy.get("reference")):
            raise CaptureBlocked("Receiver-side timing requires an explicit provisional estimate policy")
    elif not isinstance(reference, str) or not reference.strip() or len(reference) > 1024:
        raise CaptureBlocked("Camera source-clock validation is missing")
    region = collector.get("background_region")
    if not region:
        raise CaptureBlocked("A static background polygon is required for movement monitoring")
    background = CameraConfig("background-check", roi=region).roi
    interval = collector.get("frame_interval_seconds", 2.)
    shift = collector.get("background_max_shift_px", 2.)
    if type(interval) not in (int, float) or not math.isfinite(interval) or not .5 <= interval <= 30:
        raise CaptureBlocked("Invalid camera frame interval")
    if type(shift) not in (int, float) or not math.isfinite(shift) or not 0 <= shift <= 3:
        raise CaptureBlocked("Invalid static background tolerance")
    return config, stream_url, float(interval), BackgroundMonitor(background, float(shift))


EVENT_SCHEMA = "crossflow.cctv_worker_event.v1"
STAGES = {"startup", "registry", "playlist_fetch", "playlist_parse", "segment_fetch", "segment_decode", "frame_decode", "inference", "evidence_store", "shutdown"}


def safe_error(error, stage="startup"):
    """Return an allowlisted code, never an exception string, URL or decoder log."""
    from services.cctv_delivery import CCTVDeliveryError
    if isinstance(error, CCTVDeliveryError):
        return error.code, error.retryable
    if isinstance(error, CapturePending):
        return "waiting_segment", True
    if isinstance(error, HTTPError):
        if error.code == 404 and stage == "segment_fetch":
            return "segment_expired", True
        if error.code in {408, 425, 429, 500, 502, 503, 504}:
            return "source_http_temporary", True
        return "source_http_rejected", False
    if isinstance(error, URLError):
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            return "source_tls_rejected", False
        return ("source_timeout" if isinstance(error.reason, (socket.timeout, TimeoutError)) else "source_network_unavailable"), True
    if isinstance(error, (TimeoutError, socket.timeout, ConnectionError)):
        return "source_timeout", True
    if isinstance(error, CaptureBlocked):
        # Messages here are constants defined by this collector. Only codes leave
        # the process, including if a future exception accidentally embeds a URL.
        message = str(error)
        mapping = {
            "Public playlist has stopped advancing": ("source_stalled", True),
            "HLS source capture clock is stale": ("source_stale", True),
            "Camera registration changed; restart with the current configuration": ("registry_changed", False),
            "Camera analysis is disabled or permission has not been established": ("authorization_missing", False),
            "Public playlist sequence moved backwards": ("source_clock_reversed", False),
            "Public playlist sequence changed its segment identity": ("source_sequence_conflict", False),
            "HLS source clock replayed old footage": ("source_replay", False),
            "HLS source capture clock is in the future": ("source_clock_future", False),
        }
        if message in mapping:
            return mapping[message]
        if stage in {"segment_decode", "frame_decode"}:
            return "segment_decode_failed", True
        return "source_configuration_blocked", False
    if stage == "evidence_store":
        return "evidence_rejected", False
    if stage == "inference":
        return "detector_unavailable", False
    if stage in {"segment_decode", "frame_decode"}:
        return "segment_decode_failed", True
    return "collector_unavailable", False


def _event(sink, camera_id, event, stage, code, **numbers):
    if sink is not None:
        sink({"schema": EVENT_SCHEMA, "camera_id": camera_id, "event": event,
              "stage": stage, "code": code, **numbers})


def collect_camera(row, *, max_seconds=60., model_dir=None, fetch=fetch_bytes, decode=decode_segment,
                   counter=None, ingest=None, utcnow=None, monotonic=time.monotonic, sleep=time.sleep,
                   event_sink=None):
    """Run one registered camera; source retries preserve its model and state."""
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 3600:
        raise CaptureBlocked("Collector duration must be between 0 and 3600 seconds")
    config, source, interval, background = collection_requirements(row)
    utcnow = utcnow or (lambda: datetime.now(timezone.utc))
    if counter is None:
        import torch
        torch.set_num_threads(2)
        if row.get("collector_config", {}).get("detector_backend", "yolo26") == "yolo26":
            from services.yolo26_counter import YOLO26VehicleCounter
            counter = YOLO26VehicleCounter(model_dir=model_dir, allow_experimental=True)
        else:
            from vehicle_counter_worker import counter_for_model_dir
            counter = counter_for_model_dir(model_dir or ROOT / "backend/data/research/detector_training")
    registered_ingest = ingest is None
    if registered_ingest:
        from services.cctv_store import ingest_evidence
        from services import cctv_delivery
        if cctv_delivery.configured():
            # Preserve local analytics while publishing only numeric evidence
            # to the separately deployed API. Failed delivery never becomes a
            # false remote success; the next current sample can recover.
            def ingest(value, *, now):
                result = ingest_evidence(value, now=now)
                cctv_delivery.publish(value)
                return result
        else:
            ingest = ingest_evidence
    analyzer = CCTVAnalyzer(config)
    deadline = monotonic() + max_seconds
    last_segment, last_capture, failures = None, None, 0
    observation_clock = (ObservedPlaylistClock(config.maximum_age_seconds)
                         if row.get("collector_config", {}).get("timestamp_mode") == "stream_observation" else None)
    receipts = {"camera_id": config.camera_id, "segments": 0, "frames": 0, "accepted": 0,
                "source_retries": 0, "sample_interval_skips": 0, "images_retained": False}
    _event(event_sink, config.camera_id, "started", "startup", "ok")
    while monotonic() < deadline:
        stage = "registry"
        try:
            if registered_ingest or getattr(ingest, "__module__", None) == "services.cctv_store":
                from services.cctv_store import get_registered_camera
                current = get_registered_camera(config.camera_id)
                if (current["analysis_config"] != row["analysis_config"] or current.get("stream_url") != row.get("stream_url")
                        or current.get("collector_config") != row.get("collector_config")
                        or current.get("estimation_config") != row.get("estimation_config")):
                    raise CaptureBlocked("Camera registration changed; restart with the current configuration")
            stage = "playlist_fetch"
            body = fetch(source, MAX_PLAYLIST_BYTES)
            stage = "playlist_parse"
            segment = parse_live_playlist(body, source, observation_clock=observation_clock, observed_at=utcnow())
            now = timestamp(utcnow())
            if (now - segment.captured_at - timedelta(seconds=segment.duration)).total_seconds() > config.maximum_age_seconds:
                raise CaptureBlocked("HLS source capture clock is stale")
            if (segment.captured_at - now).total_seconds() > config.maximum_future_seconds:
                raise CaptureBlocked("HLS source capture clock is in the future")
            marker = (segment.url, segment.captured_at)
            if marker == last_segment:
                sleep(min(interval, max(0., deadline - monotonic())))
                continue
            if last_capture is not None and segment.captured_at + timedelta(seconds=segment.duration) <= last_capture:
                raise CaptureBlocked("HLS source clock replayed old footage")
            stage = "segment_fetch"
            data = fetch(segment.url, MAX_SEGMENT_BYTES)
            receipts["segments"] += 1
            last_segment = marker
            stage = "segment_decode"
            decoded = (decode(data, segment, interval, max_dimension=row.get("collector_config", {}).get("decode_max_dimension", 1280))
                       if decode is decode_segment else decode(data, segment, interval))
            accepted_before = receipts["accepted"]
            last_quality = False
            for captured_at, jpeg in decoded:
                now = timestamp(utcnow())
                captured_at = timestamp(captured_at)
                if last_capture is not None and captured_at <= last_capture:
                    continue
                if last_capture is not None and (captured_at - last_capture).total_seconds() < MIN_ANALYSIS_INTERVAL_SECONDS:
                    # Receiver-side segment anchors may overlap at a boundary.
                    # Discard the too-close sample before inference; do not
                    # retime it or pad the analyzer's temporal/tracking history.
                    receipts["sample_interval_skips"] += 1
                    continue
                age = (now - captured_at).total_seconds()
                if age < 0 or age > config.maximum_age_seconds:
                    continue
                if monotonic() >= deadline:
                    break
                stage = "frame_decode"
                from PIL import Image, ImageOps, ImageStat
                with Image.open(io.BytesIO(jpeg)) as image:
                    if image.format != "JPEG" or image.width * image.height > 12_000_000 or max(image.size) > 6000:
                        raise CaptureBlocked("Decoded frame exceeds image limits")
                    gray = ImageOps.exif_transpose(image).convert("L")
                    stable = background.check(gray)
                    stats = ImageStat.Stat(gray.resize((32, 32)))
                    exposure = 15 <= stats.mean[0] <= 245 and stats.stddev[0] >= 4
                stage = "inference"
                detected = count_frame(counter, jpeg, roi=[list(p) for p in config.roi] if config.roi else None,
                                       collector=row.get("collector_config") or {})
                after_detection = timestamp(utcnow())
                if (after_detection - captured_at).total_seconds() > config.maximum_age_seconds:
                    analyzer._reset_temporal()
                    continue
                stage = "evidence_store"
                evidence = analyzer.ingest({"captured_at": captured_at, "detections": detected["detections"],
                    "frame_width": detected["image_width"], "frame_height": detected["image_height"],
                    "detector_id": detected["model_id"], "camera_stable": stable, "exposure_ok": exposure,
                    "timestamp_basis": segment.timestamp_basis,
                    "may_be_truncated": detected.get("may_be_truncated", False)}, now=after_detection)
                last_capture = captured_at
                receipts["frames"] += 1
                result = ingest(evidence, now=timestamp(utcnow()))
                receipts["accepted"] += bool(result.get("accepted"))
                last_quality = evidence["quality"]["usable"]
            failures = 0
            _event(event_sink, config.camera_id, "progress", "evidence_store", "ok",
                   accepted=receipts["accepted"], frames=receipts["frames"], quality_usable=bool(last_quality),
                   segment_accepted=receipts["accepted"] - accepted_before,
                   sample_interval_skips=receipts["sample_interval_skips"])
        except CapturePending:
            _event(event_sink, config.camera_id, "waiting", stage, "waiting_segment")
        except Exception as error:
            code, retryable = safe_error(error, stage)
            if not retryable:
                setattr(error, "_cctv_stage", stage)
                raise
            failures = min(failures + 1, 5)
            receipts["source_retries"] += 1
            if stage in {"segment_decode", "frame_decode"}:
                analyzer._reset_temporal()
            delay = min(10., interval * 2 ** (failures - 1))
            _event(event_sink, config.camera_id, "retrying", stage, code,
                   consecutive_failures=failures, retry_in_seconds=delay)
            sleep(min(delay, max(0., deadline - monotonic())))
            continue
        sleep(min(interval, max(0., deadline - monotonic())))
    _event(event_sink, config.camera_id, "completed", "shutdown", "ok", accepted=receipts["accepted"], frames=receipts["frames"],
           sample_interval_skips=receipts["sample_interval_skips"])
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--max-seconds", type=float, default=60.)
    parser.add_argument("--model-dir", type=Path, default=None)
    args = parser.parse_args()
    protocol_stdout = sys.stdout
    try:
        from services.cctv_store import get_registered_camera
        row = get_registered_camera(args.camera_id)
        # Detector libraries may print initialization notes; stdout remains JSON.
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            result = collect_camera(row, max_seconds=args.max_seconds, model_dir=args.model_dir,
                                    event_sink=lambda event: print(json.dumps(event, allow_nan=False), file=protocol_stdout, flush=True))
        print(json.dumps({"ok": True, "result": result}, allow_nan=False))
        return 0
    except Exception as error:
        # Do not leak registered stream credentials, signed URLs or decoder output.
        stage = getattr(error, "_cctv_stage", "startup")
        code, _ = safe_error(error, stage)
        print(json.dumps({"ok": False, "schema": EVENT_SCHEMA, "camera_id": args.camera_id, "event": "failed",
                          "stage": stage, "code": code, "message": "The registered camera collector stopped; see its safe error code."}, allow_nan=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
