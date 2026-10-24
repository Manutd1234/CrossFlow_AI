"""Fresh CCTV image proxies used only as bounded, exact-edge search costs.

Counts and image occupancy are not road speeds. This module deliberately keeps
the camera penalty out of physical travel-time and emissions calculations.
Camera-to-edge bindings are trusted local registry configuration, never client
coordinates, inferred whole corridors, or a radius around a camera landmark.
"""

from __future__ import annotations

import contextvars
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from services import clock, cctv_store


MAX_CAMERA_EDGE_METRES = 750.0
MAX_DEPARTURE_OFFSET_SECONDS = 120.0
MAX_OBSERVATION_AGE_SECONDS = 120.0
MAX_CAMERA_BINDINGS = 256
_EDGE_ID = re.compile(r"osm-edge:([1-9][0-9]{0,18}):([1-9][0-9]{0,18}):([0-9]{1,10})\Z")


@dataclass(frozen=True)
class CameraEdgeObservation:
    camera_id: str
    edge_key: tuple[int, int, int]
    congestion_index: float
    observed_at: str
    age_seconds: float
    validation_status: str
    timestamp_basis: str


@dataclass(frozen=True)
class CameraRoutingSnapshot:
    observed_at: str
    observations: tuple[CameraEdgeObservation, ...] = ()
    edge_scores: Mapping[tuple[int, int, int], float] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        scores: dict[tuple[int, int, int], float] = {}
        for row in self.observations:
            scores[row.edge_key] = max(scores.get(row.edge_key, 0.0), row.congestion_index)
        object.__setattr__(self, "edge_scores", MappingProxyType(scores))


_ACTIVE: contextvars.ContextVar[CameraRoutingSnapshot | None] = contextvars.ContextVar(
    "crossflow_cctv_routing_snapshot", default=None,
)


def _instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("CCTV routing timestamps must have a timezone")
    return value.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def snapshot(*, now: datetime | None = None) -> CameraRoutingSnapshot:
    """Load a copy of currently permitted observations bound to this graph.

    Both the aggregate store and this boundary recheck freshness. Registry
    revisions, revoked permission, missing mappings and stale feeds therefore
    stop influencing the very next route without clearing a global route cache.
    """
    from services import router  # graph initialization stays outside this module

    instant = _instant(now or clock.now())
    rows = cctv_store.road_segment_scores(now=instant)
    observations = []
    for row in rows[:MAX_CAMERA_BINDINGS] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("graph_revision") != router.GRAPH_REVISION:
            continue
        if not (
            row.get("operational_eligible") is True
            or row.get("estimated_allowed") is True
        ):
            continue
        status = row.get("validation_status")
        if status not in {"estimated", "validated"}:
            continue
        if status == "validated" and row.get("operational_eligible") is not True:
            continue
        camera_id = row.get("camera_id")
        match = _EDGE_ID.fullmatch(str(row.get("road_segment_id", "")))
        score = _number(row.get("congestion_index"))
        maximum_age = _number(row.get("maximum_age_seconds", 15.0))
        if (
            not isinstance(camera_id, str) or not camera_id or len(camera_id) > 128
            or match is None or score is None or not 0 <= score <= 100
            or maximum_age is None or not 0 < maximum_age <= MAX_OBSERVATION_AGE_SECONDS
            or row.get("timestamp_basis") not in {"source_clock", "stream_observation"}
        ):
            continue
        edge_key = tuple(int(part) for part in match.groups())
        edge = router.road_edge_by_key(*edge_key)
        if edge is None or not 0 < edge.distance_m <= MAX_CAMERA_EDGE_METRES:
            continue
        try:
            captured = _instant(datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            continue
        age = (instant - captured).total_seconds()
        if not 0 <= age <= maximum_age:
            continue
        observations.append(CameraEdgeObservation(
            camera_id, edge_key, score, captured.isoformat(), age,
            status, row["timestamp_basis"],
        ))
    return CameraRoutingSnapshot(instant.isoformat(), tuple(observations))


@contextmanager
def use_snapshot(value: CameraRoutingSnapshot | None) -> Iterator[None]:
    """Keep one immutable camera context across A*, alternatives and auditing."""
    token = _ACTIVE.set(value)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_snapshot() -> CameraRoutingSnapshot | None:
    value = _ACTIVE.get()
    return value if value is not None and value.observations else None


def edge_penalty_s(
    source: int, target: int, road_index: int, free_flow_s: float,
    congestion_sensitivity: float,
) -> float:
    """A planning cost, at most twice the edge's existing free-flow seconds.

    Multiple camera views of the same edge use their maximum, not their sum.
    The nonnegative addition preserves the A* lower bound and never closes a
    road. Exact directed identities prevent a view affecting opposing traffic.
    """
    value = active_snapshot()
    if value is None:
        return 0.0
    score = value.edge_scores.get((source, target, road_index), 0.0)
    return min(
        2.0 * free_flow_s,
        2.0 * free_flow_s * max(0.0, congestion_sensitivity) * (score / 100.0) ** 2,
    )


def audit(selected_edge_keys: set[tuple[int, int, int]]) -> dict[str, Any] | None:
    value = active_snapshot()
    if value is None:
        return None
    return {
        "source": "cctv_camera",
        "snapshot_at": value.observed_at,
        "scope": "explicitly_mapped_directed_camera_edges_only",
        "effect": "bounded_search_cost_only",
        "measured_speed_or_eta": False,
        "maximum_penalty": "2 times existing edge free-flow seconds before objective weighting",
        "observations": [
            {
                "camera_id": row.camera_id,
                "road_segment_id": "osm-edge:" + ":".join(map(str, row.edge_key)),
                "congestion_index": row.congestion_index,
                "observed_at": row.observed_at,
                "age_seconds": round(row.age_seconds, 3),
                "validation_status": row.validation_status,
                "timestamp_basis": row.timestamp_basis,
                "on_selected_path": row.edge_key in selected_edge_keys,
            }
            for row in value.observations
        ],
        "limitations": (
            "Image congestion is an estimate over the camera's mapped view. "
            "The penalty expresses a routing preference, not measured delay; "
            "unobserved roads retain the existing planning model."
        ),
    }


def route_call(
    calculate: Callable[..., Any], *args: Any,
    effective_at: datetime | None = None, observed_at: datetime | None = None,
    **kwargs: Any,
) -> Any:
    """Apply live evidence to immediate departures, never a future forecast."""
    instant = _instant(observed_at or clock.now())
    value = None
    if effective_at is not None and abs((_instant(effective_at) - instant).total_seconds()) <= MAX_DEPARTURE_OFFSET_SECONDS:
        try:
            value = snapshot(now=instant)
        except (cctv_store.CCTVStoreUnavailable, OSError, ValueError):
            # A camera subsystem failure must not remove an otherwise valid
            # motor-road route or fabricate a zero-congestion observation.
            value = None
    with use_snapshot(value):
        return calculate(*args, **kwargs)
