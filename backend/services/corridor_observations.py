"""Read-only corridor cards from current, explicitly associated observations.

Planning scenarios and observed traffic are separate contracts. Provider values
describe one configured point; camera proxies describe only exact directed
edges that overlap the corridor's immutable baseline path. Neither measures
the entire corridor's physical delay. This reader never records history, fits
a model, adds a synthetic fallback, or fetches camera video.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import math
from types import MappingProxyType
from typing import Any

from services import clock, cctv_routing, cctv_store, live_traffic, router
from services.operations_analytics import MAX_SNAPSHOT_AGE_SECONDS, _sample
from services.observed_conditions import (
    MIN_PROVIDER_CONFIDENCE, OBSERVED_CONDITION_POLICY, PRESSURE_MULTIPLIER,
    PRESSURE_POLICY, PRESSURE_CONDITION_POLICY, PRESSURE_HEAVY_MIN_SCORE,
    observed_speed_condition, pressure_band_condition, pressure_index,
)
from services.route_learning_store import LearningSnapshot
from services.route_solver import CORRIDORS


def _instant(value: datetime | str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Corridor observations require an explicit timestamp")
    return stamp.astimezone(timezone.utc)


@lru_cache(maxsize=32)
def _baseline_path(graph_revision: str, source: int, target: int):
    """Cache graph topology, never a live score, permission or camera identity."""
    with cctv_routing.use_snapshot(None):
        path = router.astar_detailed(
            source, target, vehicle_type="COMMUTER",
            learning_snapshot=LearningSnapshot(graph_revision, 0, MappingProxyType({})),
        )
    if path is None:
        return frozenset(), None
    edges = frozenset((node, edge.target, edge.road_index)
                      for node, edge in zip(path.nodes, path.edges))
    return edges, round(path.distance_m / 1000., 2)


def _camera_observations(now: datetime):
    """The routing boundary rechecks graph, exact direction, policy and age."""
    try:
        registry = {row["camera_id"]: row for row in cctv_store.load_registry()}
        snapshot = cctv_routing.snapshot(now=now)
    except (cctv_store.CCTVStoreUnavailable, OSError, ValueError):
        return []
    result = []
    for value in snapshot.observations:
        camera = registry.get(value.camera_id)
        if camera is None:
            continue
        maximum_age = camera["analysis_config"].get("maximum_age_seconds")
        if (type(maximum_age) not in (int, float) or not math.isfinite(maximum_age)
                or not 0 < maximum_age <= cctv_routing.MAX_OBSERVATION_AGE_SECONDS):
            continue
        captured = _instant(value.observed_at)
        expiry = captured + timedelta(seconds=maximum_age)
        if not captured <= now < expiry:
            continue
        result.append((value, captured, expiry))
    return result


def _provider_observation(segment: Any, now: datetime):
    if not isinstance(segment, dict) or segment.get("source", "tomtom_live") != "tomtom_live":
        return None
    sample = _sample(segment, now)
    if sample is None or sample["provider_confidence"] < MIN_PROVIDER_CONFIDENCE:
        return None
    captured = _instant(sample["as_of"])
    expiry = captured + timedelta(seconds=MAX_SNAPSHOT_AGE_SECONDS)
    # A received timestamp does not renew source freshness. Future-dated input
    # is withheld even when a looser upstream transport accepts clock skew.
    if not captured <= now < expiry:
        return None
    return sample, captured, expiry


def _empty_observation(now: datetime) -> dict[str, Any]:
    return {
        "status": "unavailable", "source": "unavailable", "scope": "unavailable",
        "checked_at": clock.iso(now), "observed_at": None, "fresh_until": None,
        "congestion_index": None, "delay_mins": None,
        "pressure_index": None, "pressure_multiplier": PRESSURE_MULTIPLIER,
        "pressure_policy": PRESSURE_POLICY,
        "pressure_condition": "UNAVAILABLE", "pressure_condition_policy": PRESSURE_CONDITION_POLICY,
        "pressure_forecast_30m": None, "pressure_forecast_60m": None,
        "condition": "UNAVAILABLE", "condition_policy": "unavailable",
        "current_speed_kmh": None, "free_flow_speed_kmh": None,
        "provider_confidence": None,
        "provider_point_count": 0, "camera_count": 0,
        "camera_congestion_index": None, "camera_fresh_until": None,
        "camera_condition": None, "camera_warning": False,
        "camera_pressure_index": None, "camera_pressure_condition": "UNAVAILABLE",
        "camera_pressure_condition_policy": PRESSURE_CONDITION_POLICY,
        "corridor_wide": False,
        "forecast_30m": None, "forecast_60m": None,
        "forecast_method": "unavailable", "forecast_observed_at": None,
        "forecast_fresh_until": None,
    }


def get_current_corridors(now: datetime | None = None) -> list[dict[str, Any]]:
    """Return current cards without consulting forecasting/training archives.

    A provider point supports an explicitly labelled hold-latest persistence
    outlook. This is a baseline, not a forest prediction or a field-accuracy
    claim. Image occupancy cannot supply that speed-based outlook or delay.
    """
    try:
        segments = live_traffic._tomtom_segments(live_traffic._tomtom_api_key())
    except Exception:
        segments = {}
    if not isinstance(segments, dict):
        segments = {}
    paths = {}
    for corridor in CORRIDORS:
        source = router.LANDMARKS.get(corridor["origin"])
        target = router.LANDMARKS.get(corridor["destination"])
        try:
            paths[corridor["id"]] = _baseline_path(router.GRAPH_REVISION, source, target)
        except (KeyError, ValueError, TypeError):
            paths[corridor["id"]] = frozenset(), None
    # Cold graph construction/provider calls must finish before short-lived
    # camera evidence is checked and its original expiry is published.
    instant = _instant(now or clock.now())
    cameras = _camera_observations(instant)
    results = []
    for corridor in CORRIDORS:
        edges, distance = paths[corridor["id"]]
        matching = [(value, captured, expiry) for value, captured, expiry in cameras
                    if value.edge_key in edges]
        observation = _empty_observation(instant)
        observation["camera_count"] = len({value.camera_id for value, _, _ in matching})
        if matching:
            observation["camera_congestion_index"] = round(max(value.congestion_index for value, _, _ in matching), 1)
            observation["camera_fresh_until"] = clock.iso(min(expiry for _, _, expiry in matching))
            observation["camera_condition"] = (
                "LOCAL_CONGESTION_ESTIMATE" if observation["camera_congestion_index"] >= 40
                else "LOW_PRESSURE_ESTIMATE"
            )
            observation["camera_pressure_index"] = pressure_index(observation["camera_congestion_index"])
            observation["camera_pressure_condition"] = pressure_band_condition(observation["camera_pressure_index"])
        provider = _provider_observation(segments.get(corridor["id"]), instant)
        if provider is not None:
            sample, captured, expiry = provider
            observation.update(
                status="available", source="tomtom_live", scope="representative_provider_point",
                # Secondary camera metadata has its own short expiry; it must
                # neither extend nor prematurely withdraw a provider point.
                observed_at=clock.iso(captured), fresh_until=clock.iso(expiry),
                congestion_index=sample["score"], provider_point_count=1,
                condition=observed_speed_condition(sample["score"]),
                condition_policy=OBSERVED_CONDITION_POLICY,
                current_speed_kmh=sample["current_speed_kmh"],
                free_flow_speed_kmh=sample["free_flow_speed_kmh"],
                provider_confidence=sample["provider_confidence"],
                forecast_30m=sample["score"], forecast_60m=sample["score"],
                forecast_method="persistence", forecast_observed_at=clock.iso(captured),
                forecast_fresh_until=clock.iso(expiry),
            )
        elif matching:
            observation.update(
                status="available", source="cctv_camera", scope="mapped_camera_road_views",
                observed_at=clock.iso(min(captured for _, captured, _ in matching)),
                fresh_until=clock.iso(min(expiry for _, _, expiry in matching)),
                congestion_index=observation["camera_congestion_index"],
                condition="CAMERA_ESTIMATE", condition_policy="mapped_camera_proxy_v1",
            )
        score = observation["congestion_index"]
        # This display metric transforms only the chosen, already-qualified
        # source. Camera views remain estimates and cannot invent forecasts.
        observation["pressure_index"] = pressure_index(score)
        observation["pressure_condition"] = pressure_band_condition(observation["pressure_index"])
        observation["pressure_forecast_30m"] = pressure_index(observation["forecast_30m"])
        observation["pressure_forecast_60m"] = pressure_index(observation["forecast_60m"])
        observation["camera_warning"] = bool(
            provider is not None and observation["camera_pressure_index"] is not None
            and observation["camera_pressure_index"] >= PRESSURE_HEAVY_MIN_SCORE
            and observation["pressure_index"] < PRESSURE_HEAVY_MIN_SCORE
        )
        status = "UNAVAILABLE" if score is None else "CRITICAL" if score >= 70 else "HEAVY" if score >= 40 else "SMOOTH"
        results.append({
            "id": corridor["id"], "name": corridor["name"],
            "origin": corridor["origin"], "destination": corridor["destination"],
            "distance_km": distance, "base_time_mins": corridor["base_time_mins"],
            "live_congestion_score": score, "delay_mins": None, "status": status,
            "risk_level": None if score is None else {"CRITICAL": "HIGH", "HEAVY": "MODERATE", "SMOOTH": "LOW"}[status],
            "forecast_30m": observation["forecast_30m"], "forecast_60m": observation["forecast_60m"],
            "trend": "STABLE" if provider is not None else None,
            "key_checkpoints": list(corridor["key_checkpoints"]),
            "is_weekend": instant.astimezone(clock.BATAM_TZ).weekday() >= 5,
            "observed_traffic": observation,
        })
    return results
