"""Current analytics from validated provider samples and published sailings.

This dashboard intentionally has no synthetic operating measurements. Model
scenarios remain available to engineering callers in ``simulator``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from services import clock, ferry_schedule, live_traffic, cctv_routing, cctv_store, router
from services.congestion_math import speed_ratio_congestion_score
from services.route_solver import CORRIDORS


MAX_SNAPSHOT_AGE_SECONDS = 180
MAX_FUTURE_SKEW_SECONDS = 30
BOTTLENECK_THRESHOLD = 60.0


def _coverage(now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """Aggregate camera/graph coverage without publishing camera feeds.

    Camera occupancy is a separate image-space estimate. It must not enter the
    speed-derived average, imply measured km/h, or turn every mapped road into
    a monitored road. Exact-edge eligibility is checked by the routing adapter.
    """
    try:
        monitor = cctv_store.camera_status(now=now)
        configured = monitor.get("summary", {}).get("configured", 0)
        cameras = monitor.get("cameras", [])
    except Exception:
        monitor, configured, cameras = {"status": "unavailable"}, None, []
    fresh = []
    for camera in cameras:
        if not isinstance(camera, dict) or camera.get("status") not in {"live", "estimated"}:
            continue
        score = camera.get("congestion_score")
        maximum_age = camera.get("maximum_age_seconds")
        if (type(score) not in (int, float) or not math.isfinite(score)
                or not 0 <= score <= 100 or type(maximum_age) not in (int, float)
                or not math.isfinite(maximum_age) or not 0 < maximum_age <= 120):
            continue
        try:
            stamp = datetime.fromisoformat(camera["observed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not 0 <= (now - stamp).total_seconds() <= maximum_age:
                continue
        except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
            continue
        fresh.append((camera, stamp))
    times = [stamp for _, stamp in fresh]
    counts = [camera["vehicle_count"] for camera, _ in fresh
              if type(camera.get("vehicle_count")) is int and camera["vehicle_count"] >= 0]
    camera_summary = {
        "status": monitor.get("status", "unavailable"),
        "configured_cameras": configured,
        "fresh_cameras": len(fresh),
        "validated_cameras": sum(camera.get("operational_eligible") is True for camera, _ in fresh),
        "estimated_cameras": sum(camera.get("validation_status") == "estimated" for camera, _ in fresh),
        "mean_congestion_index": round(sum(camera["congestion_score"] for camera, _ in fresh) / len(fresh), 1) if fresh else None,
        "vehicle_detections": sum(counts) if counts else None,
        "latest_observed_at": clock.iso(max(times)) if times else None,
        "oldest_observed_at": clock.iso(min(times)) if times else None,
        "valid_until": clock.iso(min(stamp + timedelta(seconds=camera["maximum_age_seconds"]) for camera, stamp in fresh)) if fresh else None,
        "source": "registered_camera_aggregates",
        "scope": "fresh_registered_camera_views_only",
        "measured_road_speed": False,
        "independently_validated": bool(fresh) and all(camera.get("operational_eligible") is True for camera, _ in fresh),
    }
    try:
        camera_edges = len(cctv_routing.snapshot(now=now).edge_scores)
    except Exception:
        camera_edges = None
    # A worker can advance its watermark between the two reads. Until a
    # consistent public aggregate supplies its expiry, withhold edge coverage.
    if camera_edges and not fresh:
        camera_edges = None
    directed_edges = sum(len(edges) for edges in router.ROAD_ADJ.values())
    coverage = {
        "source": "OpenStreetMap",
        "scope": "bundled_batam_routing_graph",
        "mapped_nodes": len(router.NODES),
        "mapped_source_ways": router.GRAPH_META.get("way_count"),
        "directed_edges": directed_edges,
        "live_camera_edge_count": camera_edges,
        "live_camera_edge_coverage_percent": round(100 * camera_edges / directed_edges, 4) if directed_edges and camera_edges is not None else None,
        "complete_live_coverage": False,
    }
    return camera_summary, coverage


def _sample(segment: Any, now: datetime) -> Optional[dict[str, Any]]:
    if not isinstance(segment, dict):
        return None
    numeric = [segment.get(key) for key in (
        "current_speed_kmh", "free_flow_speed_kmh", "confidence",
    )]
    if any(isinstance(value, bool) or not isinstance(value, (float, int))
           or not math.isfinite(value) for value in numeric):
        return None
    current, free_flow, confidence = numeric
    if not (0 <= current <= 250 and 0 < free_flow <= 250
            and current / free_flow <= 1.5 and 0 <= confidence <= 1):
        return None
    try:
        timestamp = datetime.fromisoformat(segment["snapshot_generated_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        age = (now.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        return None
    if age > MAX_SNAPSHOT_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
        return None
    return {
        "score": round(speed_ratio_congestion_score(current / free_flow), 1),
        "as_of": clock.iso(timestamp),
        "age_seconds": max(0, int(age)),
        "current_speed_kmh": current,
        "free_flow_speed_kmh": free_flow,
        "provider_confidence": confidence,
    }


def get_operations_analytics(now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or clock.now()
    cameras, coverage = _coverage(now)
    try:
        segments = live_traffic._tomtom_segments(live_traffic._tomtom_api_key())
    except Exception:
        segments = {}
    if not isinstance(segments, dict):
        segments = {}
    samples = []
    for corridor in CORRIDORS:
        sample = _sample(segments.get(corridor["id"]), now)
        if sample is not None:
            samples.append({"id": corridor["id"], "name": corridor["name"], **sample})
    observed = bool(samples)
    avg = round(sum(item["score"] for item in samples) / len(samples), 1) if observed else None
    bottlenecks = [item for item in samples if item["score"] > BOTTLENECK_THRESHOLD]
    oldest = max(samples, key=lambda item: item["age_seconds"]) if samples else None
    alerts = [{
        "id": f"flow-{item['id']}", "severity": "CRITICAL",
        "corridor_id": item["id"],
        "title": f"{item['name']} traffic pressure",
        "message": (
            f"Observed representative-point speed {item['current_speed_kmh']:g} km/h "
            f"against {item['free_flow_speed_kmh']:g} km/h free flow; "
            f"derived congestion index {item['score']:g}. This is not a road-wide incident report."
        ),
        "timestamp": item["as_of"], "source": "tomtom_live", "observed": True,
    } for item in bottlenecks]
    try:
        sailings = ferry_schedule.generate_sailings(now, horizon_hours=12)
        departure_count = len(sailings)
    except Exception:
        departure_count = None
    metadata = {
        "generated_at": clock.iso(now),
        "as_of": oldest["as_of"] if oldest else None,
        "input_snapshot_at": oldest["as_of"] if oldest else None,
        "snapshot_age_seconds": oldest["age_seconds"] if oldest else None,
        "traffic_observed": observed,
        "traffic_source": "tomtom_live" if observed else "unavailable",
        "observed_corridor_count": len(samples),
        "expected_corridor_count": len(CORRIDORS),
        "complete_corridor_coverage": len(samples) == len(CORRIDORS),
        "ferry_schedule_source": "published_timetable_snapshot",
        "ferry_schedule_observed": False,
        "ferry_schedule_computed_at": clock.iso(now),
        "limitations": [
            "One measured road point per available corridor; not whole-network coverage.",
            "The congestion index is derived from measured speed/free-flow ratio.",
            "Camera detections are image-view counts, not deduplicated fleet volumes or measured road speeds.",
            "Camera congestion estimates apply only to fresh registered views and explicitly bound graph edges.",
            "No measured emissions reduction is available; recorded traffic history begins with collection.",
            "Ferry counts are published timetable departures, not live sailing status.",
        ],
    }
    return {
        "overall_network_status": (
            "UNAVAILABLE" if avg is None else
            "CONGESTED" if avg > 65 else "MODERATE" if avg > 40 else "OPTIMAL"
        ),
        "average_congestion_index": avg,
        "active_bottlenecks": len(bottlenecks) if observed else None,
        "bottleneck_corridors": [
            {"id": item["id"], "name": item["name"], "score": item["score"]}
            for item in bottlenecks
        ],
        "bottleneck_threshold": BOTTLENECK_THRESHOLD,
        "total_co2_reduced_today_kg": None,
        "co2_by_corridor_kg": {}, "co2_by_vehicle_type": {},
        "hourly_co2_distribution": [], "live_co2_rate_kg_hr": None,
        "api_source": "tomtom_live" if observed else "unavailable",
        "active_ferry_sailings": departure_count,
        "scheduled_ferry_departures_next_12h": departure_count,
        "operations_methodology": {
            "observed": False,
            "source": "Current validated traffic point measurements and source-dated ferry timetable",
            "scopes": {
                "network": "Available measured representative corridor points only",
                "emissions": "Unavailable: no measured fleet/emissions data",
                "ferries": "Published scheduled departures in the next 12 hours; not operational status",
            },
            "assumptions": {},
        },
        "analytics_metadata": metadata,
        "current_traffic_samples": samples,
        "camera_traffic_summary": cameras,
        "road_network_coverage": coverage,
        "alerts": alerts,
    }
