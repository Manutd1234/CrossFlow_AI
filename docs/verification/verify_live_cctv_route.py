"""Verify real local CCTV aggregates against the exact Batam routing graph.

No camera rows, registry settings or production databases are written. No HTTP,
authentication, video decoding or training occurs. Output contains aggregate
numbers and engine audit metadata only. Fixture tests are separate evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

APP = Path(__file__).resolve().parents[2]
BACKEND = APP / "backend"
DATA = BACKEND / "data"
CAMERA_ID = "15"
EXPECTED_EDGE = (5253032156, 5394956319, 619)


def learning_readonly_copy():
    """Use actual learned records without router's normal writable DB startup."""
    # This affects only this verification process. The live server is untouched.
    os.environ["CROSSFLOW_ROUTE_LEARNING_DB"] = ":memory:"
    from services import route_learning_store

    store = route_learning_store.DEFAULT_ROUTE_LEARNING_STORE
    source_path = DATA / "route_learning.db"
    if source_path.exists():
        with sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0) as source:
            source.execute("PRAGMA query_only=ON")
            source.backup(store._conn)
        origin = "actual_default_database_readonly_backup_to_memory"
    else:
        origin = "default_database_absent_no_learned_records_available"
    return store, {"source": origin, "production_database_written": False}


def safe_camera(camera):
    keys = ("camera_id", "status", "observed_at", "maximum_age_seconds", "window_seconds",
            "vehicle_count", "moving_vehicles", "stopped_vehicles", "occupancy_ratio",
            "congestion_score", "severity", "validation_status", "timestamp_basis",
            "routing_enabled", "operational_eligible", "detector_id")
    return {key: camera.get(key) for key in keys}


def safe_evidence(record, read_at):
    if record is None:
        return None
    evidence = record["evidence"]
    keys = ("captured_at", "window_start", "window_seconds", "frame_count", "vehicle_count",
            "tracked_vehicle_count", "moving_vehicle_count", "stationary_vehicle_count",
            "unknown_vehicle_count", "uncertain_vehicle_count", "uncertain_detection_fraction",
            "image_occupancy_ratio", "normalized_motion_proxy", "stationary_fraction",
            "line_crossings", "detector_id", "timestamp_basis", "operational_eligible")
    result = {key: evidence.get(key) for key in keys}
    quality = evidence.get("quality") or {}
    result["quality"] = {key: quality.get(key) for key in ("usable", "reasons", "tracking")}
    captured = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
    result["age_seconds_at_read"] = (read_at - captured).total_seconds()
    return result


def route_result(router, cctv_routing, snapshot, learning):
    source, target, _ = EXPECTED_EDGE
    with cctv_routing.use_snapshot(snapshot):
        payload = router.route_between_nodes(source, target, include_alternatives=False)
        path = router.astar_detailed(source, target, learning_snapshot=learning)
        if payload is None or path is None:
            return {"route_found": False}
        profile = router.vehicle_profile("COMMUTER")
        breakdown = router.path_cost_breakdown(path, profile, learning_snapshot=learning)
        return {
            "route_found": True,
            "nodes": path.nodes,
            "directed_edges": [[node, edge.target, edge.road_index] for node, edge in zip(path.nodes, path.edges)],
            "distance_m": path.distance_m,
            "search_cost_s": path.search_cost_s,
            "objective_cost_s": payload["objective_cost_s"],
            "modeled_travel_time_mins": payload["modeled_travel_time_mins"],
            "raw_cost_breakdown_seconds": breakdown,
            "public_cost_breakdown": payload["routing_cost_breakdown"],
            "cctv_audit": payload["routing_model"].get("cctv"),
        }


def verify(wait_seconds, *, historical=False):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(BACKEND))
    # Explicit default paths: do not load credentials or the server's environment.
    os.environ["CROSSFLOW_CCTV_DB"] = str(DATA / "cctv_observations.sqlite")
    os.environ["CROSSFLOW_CCTV_REGISTRY"] = str(DATA / "cctv_cameras.json")
    store, learning_origin = learning_readonly_copy()
    from services import cctv_routing, cctv_store, clock, router

    registry_path = DATA / "cctv_cameras.json"
    registry = cctv_store.load_registry()
    camera_config = next(row for row in registry if row["camera_id"] == CAMERA_ID)
    expected_id = "osm-edge:" + ":".join(map(str, EXPECTED_EDGE))
    if camera_config["analysis_config"]["road_segment_id"] != expected_id:
        raise ValueError("Camera15 binding changed; review the helper's explicit expected endpoints")
    edge = router.road_edge_by_key(*EXPECTED_EDGE)
    if edge is None:
        raise ValueError("Camera15's exact directed edge does not exist in the current graph")
    learning = store.snapshot(router.GRAPH_REVISION)
    learning_origin.update(revision=learning.revision, qualifying_edges=len(learning.entries))
    # Warm the actual graph/profile path before capturing the short-lived snapshot.
    baseline = route_result(router, cctv_routing, None, learning)
    waiting_started = time.monotonic()
    deadline = waiting_started + wait_seconds
    if historical:
        original = cctv_store._read_latest(registry).get(CAMERA_ID)
        if original is None:
            raise ValueError("No actual stored camera15 evidence exists for historical replay")
        instant = datetime.fromisoformat(original["captured_at"].replace("Z", "+00:00"))
        snapshot = cctv_routing.snapshot(now=instant)
    else:
        while True:
            instant = clock.now().astimezone(timezone.utc)
            snapshot = cctv_routing.snapshot(now=instant)
            if any(row.camera_id == CAMERA_ID for row in snapshot.observations) or time.monotonic() >= deadline:
                break
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    waited_seconds = time.monotonic() - waiting_started
    # These are independent read-only reads while the real collector continues.
    # Receipt times can therefore differ; the immutable routing snapshot stays fixed.
    status_instant = clock.now().astimezone(timezone.utc)
    status = cctv_store.camera_status(now=status_instant)
    latest = cctv_store._read_latest(registry).get(CAMERA_ID)
    latest_read_at = clock.now().astimezone(timezone.utc)
    public_camera = next(row for row in status["cameras"] if row["camera_id"] == CAMERA_ID)
    observed_camera = next((row for row in snapshot.observations if row.camera_id == CAMERA_ID), None)
    config = cctv_store.CameraConfig.from_dict(camera_config["analysis_config"])
    config_match = latest is not None and latest["evidence"].get("config_fingerprint") == config.fingerprint
    evaluation_status, _ = cctv_store._assess(camera_config, latest, instant)
    profile = router.vehicle_profile("COMMUTER")
    with cctv_routing.use_snapshot(None):
        clear_components = router._edge_cost_components(EXPECTED_EDGE[0], edge, profile, learning_snapshot=learning)
    with cctv_routing.use_snapshot(snapshot):
        live_components = router._edge_cost_components(EXPECTED_EDGE[0], edge, profile, learning_snapshot=learning)
    live = route_result(router, cctv_routing, snapshot, learning)
    after = route_result(router, cctv_routing, None, learning)
    changed = baseline.get("directed_edges") != live.get("directed_edges")
    penalty = live_components["cctv_search_penalty_s"]
    free_flow = live_components["free_flow_s"]
    checks = {
        "camera15_has_historical_qualified_snapshot_observation" if historical else "camera15_has_current_qualified_snapshot_observation": observed_camera is not None,
        "all_three_routes_found": all(row["route_found"] for row in (baseline, live, after)),
        "baseline_has_no_cctv_audit": baseline.get("cctv_audit") is None,
        "after_context_has_no_cctv_audit": after.get("cctv_audit") is None,
        "baseline_restored_after_context": baseline == after,
        "camera_edge_penalty_within_bound": 0.0 <= penalty <= 2.0 * free_flow,
        "same_path_physical_eta_unchanged": None if changed else baseline.get("modeled_travel_time_mins") == live.get("modeled_travel_time_mins"),
    }
    result = {
        "schema": "crossflow.actual_live_cctv_route_verification.v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_evaluated_at": instant.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "evidence_kind": "historical_replay_of_actual_stored_evidence_at_original_timestamp" if historical else "actual_readonly_local_store_and_real_graph_no_fixture_rows",
        "historical_replay": historical,
        "current_freshness_claimed": not historical and observed_camera is not None,
        "verification_state": ("historical_camera15_accepted_at_original_observed_time" if observed_camera else "historical_camera15_rejected_by_current_gates") if historical else ("camera15_qualified" if observed_camera else "camera15_unavailable_or_stale"),
        "camera_id": CAMERA_ID,
        "graph_revision": router.GRAPH_REVISION,
        "registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        "road_segment_id": expected_id,
        "edge_distance_m": edge.distance_m,
        "route_learning": learning_origin,
        "status_summary": status["summary"],
        "camera_status": safe_camera(public_camera),
        "status_at_snapshot_evaluation_time": {**safe_camera(evaluation_status), "reason": evaluation_status.get("reason")},
        "stored_evidence_matches_current_registry_fingerprint": config_match,
        "status_read_at": status_instant.isoformat(),
        "latest_evidence_read_at": latest_read_at.isoformat(),
        "latest_numeric_evidence": safe_evidence(latest, latest_read_at),
        "status_and_evidence_timestamp_match": latest is not None and public_camera.get("observed_at") == latest["captured_at"],
        "snapshot_observation_count": len(snapshot.observations),
        "snapshot_camera_ids": [row.camera_id for row in snapshot.observations],
        "camera15_snapshot_index": observed_camera.congestion_index if observed_camera else None,
        "camera15_snapshot_age_seconds": observed_camera.age_seconds if observed_camera else None,
        "camera15_snapshot_observed_at": observed_camera.observed_at if observed_camera else None,
        "snapshot_wait_seconds": round(waited_seconds, 3),
        "exact_edge_cost_seconds": {
            "free_flow": free_flow,
            "without_camera_penalty": clear_components["cctv_search_penalty_s"],
            "with_actual_snapshot_penalty": penalty,
            "maximum_camera_penalty": 2.0 * free_flow,
        },
        "baseline_without_cctv": baseline,
        "with_actual_cctv_snapshot": live,
        "after_snapshot_context": after,
        "selected_path_changed": changed,
        "checks": checks,
        "no_authentication_or_http_requests": True,
        "production_database_or_registry_written": False,
        "camera_rows_injected": False,
        "camera_media_read_or_retained": False,
        "limits": [
            "Historical mode replays the original timestamp without changing stored rows; it never establishes current camera freshness." if historical else "Current mode uses the actual current clock; stale evidence stays excluded.",
            "This verifies local engine consumption of real stored numeric evidence, not an authenticated HTTP/frontend route request.",
            "Scores remain provisional image-space estimates; no independently measured road speed or delay is inferred.",
            "An unchanged selected path is valid when the bounded penalty does not make a detour preferable.",
            "The status/latest-evidence reads may advance independently of the frozen routing snapshot while collectors run.",
            "Separate synthetic graph regression tests establish behavior under controlled busy/stale/revoked cases; this artifact contains no fixture camera rows.",
        ],
    }
    store.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    parser.add_argument("--historical", action="store_true", help="Replay actual stored evidence at its original timestamp; never claim current freshness.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0.0 <= args.wait_seconds <= 120.0:
        parser.error("--wait-seconds must be within0..120")
    if args.historical and args.wait_seconds:
        parser.error("Historical replay cannot wait for current observations")
    result = verify(args.wait_seconds, historical=args.historical)
    output = args.output or Path(__file__).with_name("cctv-reviewed-six-route-" + ("historical-replay" if args.historical else "live") + "-2026-10-24.json")
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"file": str(output), "verified_at": result["verified_at"], "verification_state": result["verification_state"],
                      "camera_index": result["camera15_snapshot_index"], "camera_age_seconds": result["camera15_snapshot_age_seconds"],
                      "edge_penalty_seconds": result["exact_edge_cost_seconds"]["with_actual_snapshot_penalty"],
                      "selected_path_changed": result["selected_path_changed"], "checks": result["checks"]}, indent=2))


if __name__ == "__main__":
    main()
