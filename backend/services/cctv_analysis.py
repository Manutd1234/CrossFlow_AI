"""Bounded, camera-scoped temporal CCTV measurements; no network or image storage.

Image-plane occupancy and displacement are camera proxies. They are neither road
speed nor queue length. A detector score is not a measured accuracy. Camera
thresholds and an independent validation receipt are required for operational use.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from statistics import median
from typing import Any, Mapping

from services.vehicle_counter import inside_roi, polygon_roi

SCHEMA = "crossflow.cctv_evidence.v1"
VEHICLE_CLASSES = {"vehicle", "car", "bus", "truck", "motorcycle", "bicycle"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
MAX_DETECTIONS = 200


def _number(value, label, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid {label}")
    return float(value)


def _flag(value, label):
    if type(value) is not bool:
        raise ValueError(f"Invalid {label}")
    return value


def _identifier(value, label, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _reference(value, label):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid {label}")
    return value


def timestamp(value):
    if isinstance(value, str) and len(value) <= 64:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Invalid captured_at timestamp") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("CCTV timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    corridor_id: str | None = None
    road_segment_id: str | None = None
    enabled: bool = False
    analysis_authorized: bool = False
    authorization_reference: str | None = None
    roi: tuple[tuple[float, float], ...] | None = None
    minimum_confidence: float = .5
    maximum_uncertain_fraction: float = .3
    minimum_samples: int = 5
    minimum_window_seconds: float = 10.
    window_seconds: float = 60.
    maximum_gap_seconds: float = 5.
    maximum_age_seconds: float = 15.
    maximum_future_seconds: float = 2.
    stationary_seconds: float = 15.
    stationary_motion_per_second: float = .004
    match_center_distance: float = .15
    congestion_persistence_seconds: float = 15.
    moderate_occupancy: float = .15
    high_occupancy: float = .4
    high_stationary_fraction: float = .6
    thresholds_validated: bool = False
    threshold_validation_reference: str | None = None
    independent_validation_reference: str | None = None
    validated_detector_id: str | None = None
    counting_line: tuple[tuple[float, float], tuple[float, float]] | None = None

    def __post_init__(self):
        _identifier(self.camera_id, "camera_id")
        _identifier(self.corridor_id, "corridor_id", optional=True)
        _identifier(self.road_segment_id, "road_segment_id", optional=True)
        _identifier(self.validated_detector_id, "validated_detector_id", optional=True)
        for key in ("enabled", "analysis_authorized", "thresholds_validated"):
            _flag(getattr(self, key), key)
        for key in ("authorization_reference", "threshold_validation_reference", "independent_validation_reference"):
            _reference(getattr(self, key), key)
        if self.roi is not None:
            roi = polygon_roi([list(p) for p in self.roi])
            if _area(roi) < .0001:
                raise ValueError("Camera road ROI is too small")
            object.__setattr__(self, "roi", tuple(roi))
        for key, low, high in (
            ("minimum_confidence", 0., 1.), ("maximum_uncertain_fraction", 0., 1.),
            ("minimum_window_seconds", 1., 300.), ("window_seconds", 1., 600.),
            ("maximum_gap_seconds", .1, 60.), ("maximum_age_seconds", 1., 120.),
            ("maximum_future_seconds", 0., 5.), ("stationary_seconds", 1., 300.),
            ("stationary_motion_per_second", .00001, .2), ("match_center_distance", .001, .5),
            ("congestion_persistence_seconds", 1., 300.), ("moderate_occupancy", .001, .99),
            ("high_occupancy", .001, 1.), ("high_stationary_fraction", .01, 1.)):
            _number(getattr(self, key), key, low, high)
        if type(self.minimum_samples) is not int or not 2 <= self.minimum_samples <= 180:
            raise ValueError("Invalid minimum_samples")
        if self.minimum_window_seconds > self.window_seconds or self.congestion_persistence_seconds > self.window_seconds:
            raise ValueError("Camera window is shorter than its measurement requirements")
        if self.moderate_occupancy >= self.high_occupancy:
            raise ValueError("Camera occupancy thresholds must be ordered")
        if self.counting_line is not None:
            line = self.counting_line
            if not isinstance(line, (tuple, list)) or len(line) != 2:
                raise ValueError("Counting line requires two normalized points")
            points = []
            for p in line:
                if not isinstance(p, (tuple, list)) or len(p) != 2:
                    raise ValueError("Invalid counting line")
                points.append(tuple(_number(v, "counting line", 0., 1.) for v in p))
            if math.dist(*points) < .01:
                raise ValueError("Counting line is too short")
            object.__setattr__(self, "counting_line", tuple(points))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]):
        if not isinstance(value, Mapping):
            raise ValueError("Camera configuration must be an object")
        known = cls.__dataclass_fields__
        unknown = set(value) - set(known)
        if unknown:
            raise ValueError("Unknown camera configuration fields: " + ", ".join(sorted(unknown)))
        try:
            return cls(**value)
        except TypeError as error:
            raise ValueError("Invalid camera configuration") from error

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    @property
    def permission_ready(self):
        return self.enabled and self.analysis_authorized and bool(self.authorization_reference)

    @property
    def calibration_ready(self):
        return bool(self.roi and self.thresholds_validated and self.threshold_validation_reference)


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]
    score: float
    class_name: str

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or not isinstance(value.get("class_name"), str) or value["class_name"] not in VEHICLE_CLASSES:
            raise ValueError("Invalid CCTV vehicle class")
        box = value.get("box", value.get("box_normalized_xyxy"))
        if not isinstance(box, (tuple, list)) or len(box) != 4:
            raise ValueError("Invalid CCTV detection box")
        box = tuple(_number(v, "detection box", 0., 1.) for v in box)
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("Invalid CCTV detection extent")
        return cls(box, _number(value.get("score"), "detection confidence", 0., 1.), value["class_name"])

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)


@dataclass(frozen=True)
class FrameObservation:
    captured_at: datetime
    detections: tuple[Detection, ...]
    frame_width: int
    frame_height: int
    detector_id: str
    camera_stable: bool | None = None
    exposure_ok: bool | None = None
    may_be_truncated: bool = False
    timestamp_basis: str = "source_clock"

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError("CCTV frame metadata must be an object")
        detections = value.get("detections")
        if not isinstance(detections, list) or len(detections) > MAX_DETECTIONS:
            raise ValueError("CCTV frame exceeds detection bounds")
        width, height = value.get("frame_width"), value.get("frame_height")
        if any(type(v) is not int or not 2 <= v <= 6000 for v in (width, height)) or width * height > 12_000_000:
            raise ValueError("Invalid CCTV frame dimensions")
        for key in ("camera_stable", "exposure_ok"):
            if value.get(key) is not None:
                _flag(value[key], key)
        basis = value.get("timestamp_basis", "source_clock")
        if basis not in {"source_clock", "stream_observation"}:
            raise ValueError("Invalid camera timestamp basis")
        return cls(timestamp(value.get("captured_at")), tuple(Detection.from_dict(v) for v in detections),
                   width, height, _identifier(value.get("detector_id"), "detector_id"),
                   value.get("camera_stable"), value.get("exposure_ok"), _flag(value.get("may_be_truncated", False), "may_be_truncated"), basis)


def _area(points):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2 if points else 0.


def _clip_rectangle(points, rectangle):
    """Clip a simple polygon to a rectangle using four half planes."""
    result = list(points)
    for axis, boundary, keep_above in ((0, rectangle[0], True), (0, rectangle[2], False),
                                        (1, rectangle[1], True), (1, rectangle[3], False)):
        output = []
        if not result:
            break
        for previous, current in zip(result[-1:] + result[:-1], result):
            p_inside = previous[axis] >= boundary if keep_above else previous[axis] <= boundary
            c_inside = current[axis] >= boundary if keep_above else current[axis] <= boundary
            if p_inside != c_inside:
                proportion = (boundary - previous[axis]) / (current[axis] - previous[axis])
                output.append(tuple(previous[d] + proportion * (current[d] - previous[d]) for d in range(2)))
            if c_inside:
                output.append(current)
        result = output
    return result


def image_occupancy(boxes, roi):
    """Area of the union of normalized boxes intersected with the road ROI.

    Overlapping detections count once. This is image-plane occupancy; perspective
    prevents interpreting it as physical road density or road occupancy in metres.
    """
    if not roi:
        return None
    roi = list(roi)
    area = _area(roi)
    if not boxes or not area:
        return 0.
    boundaries = sorted({b[0] for b in boxes} | {b[2] for b in boxes})
    occupied = 0.
    for left, right in zip(boundaries, boundaries[1:]):
        midpoint = (left + right) / 2
        intervals = sorted((b[1], b[3]) for b in boxes if b[0] <= midpoint <= b[2])
        merged = []
        for low, high in intervals:
            if merged and low <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((low, high))
        for low, high in merged:
            occupied += _area(_clip_rectangle(roi, (left, low, right, high)))
    return min(1., max(0., occupied / area))


def _iou(a, b):
    overlap = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / union if union > 0 else 0.


def _line_side(point, line):
    a, b = line
    return (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])


def _crosses(previous, current, line):
    p, c = _line_side(previous, line), _line_side(current, line)
    if p * c >= 0 or abs(p) < 1e-6 or abs(c) < 1e-6:
        return None
    a, b = line
    denominator = (current[0] - previous[0]) * (b[1] - a[1]) - (current[1] - previous[1]) * (b[0] - a[0])
    if abs(denominator) < 1e-12:
        return None
    # Intersect the finite counting segment, not its infinite supporting line.
    fraction = ((a[0] - previous[0]) * (current[1] - previous[1]) - (a[1] - previous[1]) * (current[0] - previous[0])) / denominator
    return ("positive" if c > p else "negative") if 0 <= fraction <= 1 else None


@dataclass
class _Track:
    identifier: int
    detection: Detection
    last_at: datetime
    observations: int = 1
    stationary_duration: float = 0.
    motion: float | None = None
    last_crossing_at: datetime | None = None
    missed: bool = False
    velocity: tuple[float, float] | None = None
    line_anchor: tuple[float, float] | None = None


class CCTVAnalyzer:
    """One isolated state per fixed camera; call serially under the collector lock."""
    def __init__(self, config: CameraConfig):
        if not isinstance(config, CameraConfig):
            raise ValueError("Expected a registered CameraConfig")
        self.config = config
        self._tracks: dict[int, _Track] = {}
        self._samples = deque(maxlen=180)
        self._tracking_samples = deque(maxlen=180)
        self._crossings = deque(maxlen=1000)
        self._next_id = 1
        self._last_at = None
        self._last_dimensions = None
        self._last_detector = None
        self._last_timestamp_basis = None
        self._last_candidate = None
        self._candidate_since = None

    def _reset_temporal(self):
        self._tracks.clear()
        self._samples.clear()
        self._tracking_samples.clear()
        self._crossings.clear()
        self._last_candidate = self._candidate_since = None

    def ingest(self, frame: FrameObservation | Mapping[str, Any], *, now=None):
        if not isinstance(frame, FrameObservation):
            frame = FrameObservation.from_dict(frame)
        # Dataclasses are internal values; revalidate so direct constructors cannot
        # bypass the same timestamp, scalar and bounds contract as JSON ingestion.
        else:
            frame = FrameObservation.from_dict({**frame.__dict__, "detections": [v.__dict__ for v in frame.detections]})
        now = timestamp(now or datetime.now(timezone.utc))
        age = (now - frame.captured_at).total_seconds()
        if age < -self.config.maximum_future_seconds:
            raise ValueError("CCTV capture timestamp is in the future")
        if self._last_at is not None and frame.captured_at <= self._last_at:
            raise ValueError("CCTV capture timestamps must be strictly increasing")
        if self._last_at is not None and (frame.captured_at - self._last_at).total_seconds() < .05:
            raise ValueError("CCTV analysis accepts at most 20 sampled frames per second")
        result = self._base(frame, age)
        if not self.config.enabled:
            return self._blocked(result, "disabled", ["camera_disabled"])
        if not self.config.permission_ready:
            return self._blocked(result, "permission_pending", ["analysis_authorization_missing"])
        reasons = []
        if age > self.config.maximum_age_seconds:
            reasons.append("stale_frame")
        if age < 0:
            reasons.append("future_capture")
        if frame.camera_stable is not True:
            reasons.append("camera_motion" if frame.camera_stable is False else "camera_stability_unknown")
        if frame.exposure_ok is not True:
            reasons.append("poor_exposure" if frame.exposure_ok is False else "exposure_unknown")
        if frame.may_be_truncated:
            reasons.append("detector_truncated")
        dimensions = (frame.frame_width, frame.frame_height)
        if self._last_dimensions is not None and dimensions != self._last_dimensions:
            reasons.append("camera_dimensions_changed")
        if self._last_detector is not None and frame.detector_id != self._last_detector:
            reasons.append("detector_changed")
        if self._last_timestamp_basis is not None and frame.timestamp_basis != self._last_timestamp_basis:
            reasons.append("timestamp_basis_changed")
        gap = (frame.captured_at - self._last_at).total_seconds() if self._last_at else None
        if gap is not None and gap > self.config.maximum_gap_seconds:
            reasons.append("frame_gap")
        # ROI selection uses box centres, matching detector behaviour. Occupancy
        # clips their union to the ROI. Non-road vehicles cannot inflate counts.
        detections = [d for d in frame.detections if inside_roi(*d.center, list(self.config.roi) if self.config.roi else None)]
        uncertain = [d for d in detections if d.score < self.config.minimum_confidence]
        accepted = [d for d in detections if d.score >= self.config.minimum_confidence]
        uncertain_fraction = len(uncertain) / len(detections) if detections else 0.
        if uncertain_fraction > self.config.maximum_uncertain_fraction:
            reasons.append("uncertain_detections")
        self._last_at = frame.captured_at
        self._last_dimensions = dimensions
        self._last_detector = frame.detector_id
        self._last_timestamp_basis = frame.timestamp_basis
        result.update(vehicle_count=len(accepted), unknown_vehicle_count=len(accepted),
                      image_occupancy_ratio=image_occupancy([d.box for d in accepted], self.config.roi),
                      uncertain_vehicle_count=len(uncertain), uncertain_detection_fraction=uncertain_fraction)
        if reasons:
            self._reset_temporal()
            return self._blocked(result, "quality_blocked", reasons)
        self._update_tracks(accepted, frame.captured_at)
        current = [track for track in self._tracks.values() if track.last_at == frame.captured_at]
        observed = [t for t in current if t.motion is not None]
        stationary = [t for t in observed if t.stationary_duration >= self.config.stationary_seconds]
        moving = [t for t in observed if t.motion > self.config.stationary_motion_per_second]
        unknown = len(accepted) - len(stationary) - len(moving)
        motion = median([t.motion for t in observed]) if observed else None
        stationary_fraction = len(stationary) / len(accepted) if accepted else 0.
        self._samples.append(frame.captured_at)
        self._tracking_samples.append((frame.captured_at, len(accepted), len(observed)))
        while self._tracking_samples and (frame.captured_at - self._tracking_samples[0][0]).total_seconds() > self.config.window_seconds:
            self._tracking_samples.popleft()
        while self._samples and (frame.captured_at - self._samples[0]).total_seconds() > self.config.window_seconds:
            self._samples.popleft()
        while self._crossings and (frame.captured_at - self._crossings[0][0]).total_seconds() > self.config.window_seconds:
            self._crossings.popleft()
        window = (frame.captured_at - self._samples[0]).total_seconds() if self._samples else 0.
        result.update(window_start=_iso(self._samples[0]), window_seconds=window, frame_count=len(self._samples),
                      tracked_vehicle_count=len(current), moving_vehicle_count=len(moving),
                      stationary_vehicle_count=len(stationary), unknown_vehicle_count=unknown,
                      stationary_fraction=stationary_fraction, normalized_motion_proxy=motion,
                      line_crossings={"positive": sum(side == "positive" for _, side in self._crossings),
                                      "negative": sum(side == "negative" for _, side in self._crossings)} if self.config.counting_line else None)
        warmup = len(self._samples) < self.config.minimum_samples or window < self.config.minimum_window_seconds
        # Motion history and stopped classification are separate evidence.
        # A matched slow vehicle has observed displacement even before its
        # stationary-duration threshold is met. New arrivals remain unknown;
        # require current motion plus adequate vehicle-weighted rolling
        # coverage, rather than letting empty frames pad the denominator.
        tracking = {"scope": "rolling_detection_observations_v1",
                    "vehicle_observations": sum(item[1] for item in self._tracking_samples),
                    "motion_observations": sum(item[2] for item in self._tracking_samples),
                    "current_motion_observations": len(observed)}
        track_quality_blocked = bool(accepted) and (not observed or (
            1 - tracking["motion_observations"] / tracking["vehicle_observations"] > self.config.maximum_uncertain_fraction))
        quality_reasons = (["minimum_temporal_window"] if warmup else []) + (["insufficient_track_history"] if track_quality_blocked else [])
        usable = not warmup and not track_quality_blocked
        result["quality"] = {"usable": usable, "reasons": quality_reasons,
                             "age_seconds": age, "frame_count": len(self._samples), "window_seconds": window,
                             "tracking": tracking}
        result["status"] = "warming_up" if warmup else "quality_blocked" if track_quality_blocked else "experimental"
        if not self.config.calibration_ready:
            result["severity_status"] = "uncalibrated"
        elif not usable:
            result["severity_status"] = "warming_up"
            self._last_candidate = self._candidate_since = None
        else:
            occupancy = result["image_occupancy_ratio"]
            if occupancy >= self.config.high_occupancy and stationary_fraction >= self.config.high_stationary_fraction:
                candidate = "high"
            elif occupancy >= self.config.moderate_occupancy:
                candidate = "moderate"
            else:
                candidate = "low"
            if candidate != self._last_candidate:
                self._last_candidate = candidate
                self._candidate_since = frame.captured_at
            result["severity_persistence_seconds"] = (frame.captured_at - self._candidate_since).total_seconds()
            persistent = result["severity_persistence_seconds"] >= self.config.congestion_persistence_seconds
            result["severity_status"] = "persistent" if persistent else "unstable"
            if persistent:
                result["camera_congestion_severity"] = candidate
                slowdown = max(0., 1. - motion / (5 * self.config.stationary_motion_per_second)) if motion is not None else 0.
                result["camera_congestion_score_proxy"] = 100 * min(occupancy / self.config.high_occupancy, 1.) * (
                    .5 + .3 * stationary_fraction + .2 * slowdown)
        result["operational_eligible"] = _operational(result, self.config)
        if result["operational_eligible"]:
            result["experimental"] = False
            result["status"] = "validated"
            result["congestion_score"] = result["camera_congestion_score_proxy"]
        return result

    def _update_tracks(self, detections, captured_at):
        expired = [key for key, t in self._tracks.items() if (captured_at - t.last_at).total_seconds() > self.config.maximum_gap_seconds]
        for key in expired:
            del self._tracks[key]
        candidates = []
        for key, track in self._tracks.items():
            elapsed = (captured_at - track.last_at).total_seconds()
            velocity = track.velocity if not track.missed else None
            shift = tuple(v * elapsed for v in velocity) if velocity is not None else (0., 0.)
            predicted_center = tuple(c + v for c, v in zip(track.detection.center, shift))
            predicted_box = tuple(v + shift[i % 2] for i, v in enumerate(track.detection.box))
            # Extrapolation uses already observed motion, stays within a
            # bounded two-step displacement, and is disabled after a miss.
            bound = self.config.match_center_distance + min(self.config.match_center_distance, math.hypot(*shift))
            for index, detection in enumerate(detections):
                distance = math.dist(track.detection.center, detection.center)
                prediction_error = math.dist(predicted_center, detection.center)
                overlap = _iou(predicted_box, detection.box)
                if distance <= bound and prediction_error <= self.config.match_center_distance and (
                        overlap >= .05 or prediction_error <= self.config.match_center_distance / 2):
                    candidates.append((overlap - prediction_error - .05 * distance, key, index))
        matched_tracks, matched_detections = set(), set()
        for _, key, index in sorted(candidates, reverse=True):
            if key in matched_tracks or index in matched_detections:
                continue
            track, detection = self._tracks[key], detections[index]
            elapsed = (captured_at - track.last_at).total_seconds()
            motion = math.dist(track.detection.center, detection.center) / elapsed
            track.stationary_duration = track.stationary_duration + elapsed if motion <= self.config.stationary_motion_per_second and not track.missed else 0.
            if self.config.counting_line and not track.missed:
                self._count_crossing(track, detection, captured_at)
            track.velocity = tuple((a - b) / elapsed for a, b in zip(detection.center, track.detection.center))
            track.motion, track.detection, track.last_at = motion, detection, captured_at
            track.observations += 1
            track.missed = False
            matched_tracks.add(key)
            matched_detections.add(index)
        for key, track in self._tracks.items():
            if key not in matched_tracks:
                track.missed = True
                track.stationary_duration = 0.
                track.line_anchor = None
        for index, detection in enumerate(detections):
            if index not in matched_detections:
                if len(self._tracks) >= 256:
                    oldest = min(self._tracks, key=lambda key: self._tracks[key].last_at)
                    del self._tracks[oldest]
                self._tracks[self._next_id] = _Track(self._next_id, detection, captured_at)
                self._next_id += 1

    def _count_crossing(self, track, detection, captured_at):
        line = self.config.counting_line
        length = math.dist(*line)
        # A small box-scaled dead band prevents centroid jitter on the line
        # from becoming repeated directional traffic. It does not create any
        # crossing when a vehicle first appears or reappears after occlusion.
        margin = max(.001, min(.005, .05 * min(detection.box[2] - detection.box[0], detection.box[3] - detection.box[1])))
        def side(point):
            distance = _line_side(point, line) / length
            return 0 if abs(distance) <= margin else 1 if distance > 0 else -1
        if track.line_anchor is None and side(track.detection.center):
            track.line_anchor = track.detection.center
        current_side = side(detection.center)
        if current_side and track.line_anchor is not None:
            direction = _crosses(track.line_anchor, detection.center, line)
            if direction and (track.last_crossing_at is None or (captured_at - track.last_crossing_at).total_seconds() >= 2.):
                self._crossings.append((captured_at, direction))
                track.last_crossing_at = captured_at
            track.line_anchor = detection.center
        elif current_side:
            track.line_anchor = detection.center

    def _base(self, frame, age):
        return {"schema": SCHEMA, "camera_id": self.config.camera_id, "corridor_id": self.config.corridor_id,
                "road_segment_id": self.config.road_segment_id, "config_fingerprint": self.config.fingerprint,
                "captured_at": _iso(frame.captured_at), "window_start": _iso(frame.captured_at),
                "timestamp_basis": frame.timestamp_basis,
                "window_seconds": 0., "frame_count": 0, "detector_id": frame.detector_id,
                "status": "experimental", "vehicle_count": None, "tracked_vehicle_count": None,
                "moving_vehicle_count": None, "stationary_vehicle_count": None, "unknown_vehicle_count": None, "uncertain_vehicle_count": None,
                "uncertain_detection_fraction": None, "image_occupancy_ratio": None,
                "normalized_motion_proxy": None, "stationary_fraction": None, "line_crossings": None,
                "camera_congestion_severity": None, "congestion_score": None, "camera_congestion_score_proxy": None, "severity_status": "unavailable", "severity_persistence_seconds": 0.,
                "quality": {"usable": False, "reasons": [], "age_seconds": age, "frame_count": 0, "window_seconds": 0.},
                "operational_eligible": False, "experimental": True, "speed_kmh": None,
                "travel_time_seconds": None, "queue_length_m": None, "images_retained": False,
                "measurement_scope": "Fixed-camera image-plane proxies; detector scores are not measured accuracy",
                "flow_scope": "Optional track line-crossing events; fragmentation and occlusion can change counts"}

    @staticmethod
    def _blocked(result, status, reasons):
        result["status"] = status
        result["quality"]["reasons"] = reasons
        return result


def _tracking_evidence(value, result):
    if not isinstance(value, Mapping) or set(value) != {"scope", "vehicle_observations", "motion_observations", "current_motion_observations"} or value.get("scope") != "rolling_detection_observations_v1":
        raise ValueError("Invalid CCTV tracking evidence")
    count = result.get("vehicle_count")
    if type(count) is not int:
        raise ValueError("Tracking evidence requires current vehicle counts")
    for key in ("vehicle_observations", "motion_observations", "current_motion_observations"):
        if type(value[key]) is not int or not 0 <= value[key] <= result["frame_count"] * MAX_DETECTIONS:
            raise ValueError("Invalid CCTV tracking observation count")
    total, resolved, current = (value[k] for k in ("vehicle_observations", "motion_observations", "current_motion_observations"))
    classified = sum(result.get(key) or 0 for key in ("moving_vehicle_count", "stationary_vehicle_count"))
    if not (total >= count and 0 <= current <= count and current <= resolved <= total and current >= classified):
        raise ValueError("Contradictory CCTV tracking evidence")
    return dict(value)


def _operational(evidence, config):
    return bool(config.permission_ready and config.calibration_ready and config.independent_validation_reference
                and config.validated_detector_id and evidence["detector_id"] == config.validated_detector_id
                and evidence.get("timestamp_basis", "source_clock") == "source_clock"
                and evidence["status"] in {"experimental", "validated"} and evidence["quality"]["usable"] and evidence["severity_status"] == "persistent"
                and evidence["camera_congestion_severity"] in {"low", "moderate", "high"}
                and type(evidence.get("unknown_vehicle_count")) is int
                and (evidence["unknown_vehicle_count"] / evidence["vehicle_count"] if evidence.get("vehicle_count") else 0.) <= config.maximum_uncertain_fraction)


def validate_evidence(value: Mapping[str, Any], config: CameraConfig, *, now=None):
    """Validate a trusted worker's numeric result against its registered config.

    This is a boundary/schema check, not a cryptographic proof of video contents.
    Never accept these records from public clients; the authenticated collector
    and the store's strictly increasing watermark are separate required controls.
    Recomputes operational gates instead of trusting the supplied eligibility bit.
    """
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise ValueError("Unsupported CCTV evidence")
    basis = value.get("timestamp_basis", "source_clock")
    if basis not in {"source_clock", "stream_observation"}:
        raise ValueError("Invalid camera timestamp basis")
    expected = {"camera_id": config.camera_id, "corridor_id": config.corridor_id,
                "road_segment_id": config.road_segment_id, "config_fingerprint": config.fingerprint}
    if any(value.get(k) != v for k, v in expected.items()):
        raise ValueError("CCTV evidence does not match registered camera configuration")
    captured, start = timestamp(value.get("captured_at")), timestamp(value.get("window_start"))
    now = timestamp(now or datetime.now(timezone.utc))
    age = (now - captured).total_seconds()
    if age < -config.maximum_future_seconds or age > config.maximum_age_seconds:
        raise ValueError("CCTV evidence is not fresh")
    window = _number(value.get("window_seconds"), "evidence window", 0., config.window_seconds)
    if abs((captured - start).total_seconds() - window) > .001:
        raise ValueError("CCTV evidence interval does not match timestamps")
    result = {k: value[k] for k in (
        "schema", "camera_id", "corridor_id", "road_segment_id", "config_fingerprint", "captured_at", "window_start",
        "window_seconds", "frame_count", "detector_id", "status", "vehicle_count", "tracked_vehicle_count",
        "moving_vehicle_count", "stationary_vehicle_count", "unknown_vehicle_count", "uncertain_vehicle_count", "uncertain_detection_fraction",
        "image_occupancy_ratio", "normalized_motion_proxy", "stationary_fraction", "line_crossings",
        "camera_congestion_severity", "congestion_score", "camera_congestion_score_proxy", "severity_status", "severity_persistence_seconds", "quality") if k in value}
    required = {"frame_count", "status", "detector_id", "quality", "severity_status", "camera_congestion_severity"}
    result["timestamp_basis"] = basis
    if not required <= result.keys():
        raise ValueError("Missing CCTV evidence fields")
    _identifier(result["detector_id"], "detector_id")
    if type(result["frame_count"]) is not int or not 0 <= result["frame_count"] <= 180:
        raise ValueError("Invalid CCTV frame count")
    if result["status"] not in {"disabled", "permission_pending", "warming_up", "quality_blocked", "experimental", "validated"}:
        raise ValueError("Invalid CCTV status")
    for key in ("vehicle_count", "tracked_vehicle_count", "moving_vehicle_count", "stationary_vehicle_count", "unknown_vehicle_count", "uncertain_vehicle_count"):
        count = result.get(key)
        if count is not None and (type(count) is not int or not 0 <= count <= MAX_DETECTIONS):
            raise ValueError("Invalid CCTV vehicle counts")
    for key in ("uncertain_detection_fraction", "image_occupancy_ratio", "stationary_fraction"):
        if result.get(key) is not None:
            _number(result[key], key, 0., 1.)
    counts = [result.get(k) for k in ("vehicle_count", "moving_vehicle_count", "stationary_vehicle_count", "unknown_vehicle_count")]
    if all(v is not None for v in counts) and sum(counts[1:]) != counts[0]:
        raise ValueError("Contradictory CCTV vehicle counts")
    if result.get("normalized_motion_proxy") is not None:
        _number(result["normalized_motion_proxy"], "normalized_motion_proxy", 0., 100.)
    crossings = result.get("line_crossings")
    if crossings is not None:
        if not config.counting_line or not isinstance(crossings, Mapping) or set(crossings) != {"positive", "negative"} or any(
                type(crossings[k]) is not int or not 0 <= crossings[k] <= 1000 for k in crossings):
            raise ValueError("Invalid CCTV line crossings")
        result["line_crossings"] = dict(crossings)
    quality = result["quality"]
    if not isinstance(quality, Mapping) or type(quality.get("usable")) is not bool or not isinstance(quality.get("reasons"), list):
        raise ValueError("Invalid CCTV quality")
    if len(quality["reasons"]) > 20 or any(not isinstance(r, str) or not re.fullmatch(r"[a-z_]{1,64}", r) for r in quality["reasons"]):
        raise ValueError("Invalid CCTV quality reasons")
    if quality["usable"] and (quality["reasons"] or result["frame_count"] < config.minimum_samples or window < config.minimum_window_seconds or age < 0
                              or result["status"] not in {"experimental", "validated"}):
        raise ValueError("Contradictory CCTV quality")
    tracking = _tracking_evidence(quality["tracking"], result) if "tracking" in quality else None
    if quality["usable"]:
        required_counts = ("vehicle_count", "tracked_vehicle_count", "moving_vehicle_count", "stationary_vehicle_count", "unknown_vehicle_count", "uncertain_vehicle_count")
        if any(result.get(k) is None for k in required_counts) or result.get("stationary_fraction") is None or result.get("uncertain_detection_fraction") is None:
            raise ValueError("Healthy CCTV evidence is missing its measurements")
        if result["tracked_vehicle_count"] != result["vehicle_count"]:
            raise ValueError("CCTV tracked count mismatch")
        expected_stationary = result["stationary_vehicle_count"] / result["vehicle_count"] if result["vehicle_count"] else 0.
        if abs(result["stationary_fraction"] - expected_stationary) > 1e-8:
            raise ValueError("CCTV stationary fraction mismatch")
        if tracking is None:
            unresolved = result["unknown_vehicle_count"] / result["vehicle_count"] if result["vehicle_count"] else 0.
            tracking_missing = False
        else:
            unresolved = 1 - tracking["motion_observations"] / tracking["vehicle_observations"] if result["vehicle_count"] else 0.
            tracking_missing = bool(result["vehicle_count"]) and tracking["current_motion_observations"] == 0
        if tracking_missing or unresolved > config.maximum_uncertain_fraction or result["uncertain_detection_fraction"] > config.maximum_uncertain_fraction:
            raise ValueError("CCTV track or confidence quality is insufficient")
        if result["vehicle_count"] and result.get("normalized_motion_proxy") is None:
            raise ValueError("Healthy CCTV evidence is missing temporal motion")
    result["quality"] = {"usable": quality["usable"], "reasons": list(quality["reasons"]), "age_seconds": age,
                         "frame_count": result["frame_count"], "window_seconds": window}
    if tracking is not None:
        result["quality"]["tracking"] = tracking
    if result["severity_status"] not in {"unavailable", "uncalibrated", "warming_up", "unstable", "persistent"}:
        raise ValueError("Invalid CCTV severity status")
    severity = result["camera_congestion_severity"]
    if severity is not None and severity not in {"low", "moderate", "high"}:
        raise ValueError("Invalid CCTV severity")
    persistence = _number(result.get("severity_persistence_seconds", 0.), "severity persistence", 0., config.window_seconds)
    if severity is not None and (not config.calibration_ready or not quality["usable"] or result["severity_status"] != "persistent"
                                 or persistence < config.congestion_persistence_seconds):
        raise ValueError("Unqualified CCTV severity")
    score = result.get("camera_congestion_score_proxy")
    if score is not None:
        _number(score, "camera congestion proxy", 0., 100.)
        if severity is None:
            raise ValueError("Unqualified CCTV congestion proxy")
        occupancy = result.get("image_occupancy_ratio")
        stationary = result.get("stationary_fraction")
        motion = result.get("normalized_motion_proxy")
        if occupancy is None or stationary is None:
            raise ValueError("CCTV congestion proxy is missing its measurements")
        expected_severity = ("high" if occupancy >= config.high_occupancy and stationary >= config.high_stationary_fraction
                             else "moderate" if occupancy >= config.moderate_occupancy else "low")
        if severity != expected_severity:
            raise ValueError("CCTV severity does not match camera thresholds")
        slowdown = max(0., 1. - motion / (5 * config.stationary_motion_per_second)) if motion is not None else 0.
        expected_score = 100 * min(occupancy / config.high_occupancy, 1.) * (.5 + .3 * stationary + .2 * slowdown)
        if abs(score - expected_score) > 1e-8:
            raise ValueError("CCTV congestion proxy formula mismatch")
    elif severity is not None:
        raise ValueError("CCTV severity is missing its congestion proxy")
    result["operational_eligible"] = _operational(result, config)
    result["congestion_score"] = score if result["operational_eligible"] else None
    result.update(experimental=not result["operational_eligible"], speed_kmh=None, travel_time_seconds=None,
                  queue_length_m=None, images_retained=False,
                  measurement_scope="Fixed-camera image-plane proxies; detector scores are not measured accuracy",
                  flow_scope="Optional track line-crossing events; fragmentation and occlusion can change counts")
    if result["status"] == "validated" and not result["operational_eligible"]:
        result["status"] = "experimental"
    return result
