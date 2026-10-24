"""CCTV must change a route decision without claiming a measured speed."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import cctv_routing, cctv_store, router, route_solver


NOW = datetime(2026, 10, 24, 8, tzinfo=timezone.utc)


@contextmanager
def graph(*, private_detour=False, only_one_path=False):
    nodes = {1: (1., 104.), 2: (1., 104.001), 3: (1.001, 104.001), 4: (1., 104.002)}
    def edge(target, distance, index, **kw):
        return router.RoadEdge(target, distance, index, highway="primary", name="Test road", **kw)
    adjacency = {
        1: [edge(2, 120., 10)] + ([] if only_one_path else [
            edge(3, 160., 20, access="private" if private_detour else None),
        ]),
        2: [edge(4, 120., 10)], 3: [edge(4, 160., 20)], 4: [],
    }
    router._main_routing_core.cache_clear()
    router._route_between_nodes_cached.cache_clear()
    with patch.multiple(
        router, NODES=nodes, ROAD_ADJ=adjacency, NODE_META={},
        ADJ={node: [(e.target, e.distance_m) for e in edges] for node, edges in adjacency.items()},
    ), patch.object(router, "_alt_index_for_profile", return_value=None):
        yield
    router._main_routing_core.cache_clear()
    router._route_between_nodes_cached.cache_clear()


def observation(**overrides):
    return {
        "camera_id": "test-camera", "road_segment_id": "osm-edge:1:2:10",
        "graph_revision": router.GRAPH_REVISION, "congestion_index": 100.,
        "observed_at": NOW.isoformat(), "maximum_age_seconds": 15.,
        "operational_eligible": False, "estimated_allowed": True,
        "validation_status": "estimated", "timestamp_basis": "stream_observation",
        **overrides,
    }


def load_snapshot(rows):
    with patch.object(cctv_store, "road_segment_scores", return_value=rows, create=True):
        return cctv_routing.snapshot(now=NOW)


def test_fresh_busy_camera_changes_path_and_does_not_poison_clear_cache():
    with graph():
        clear = router.route_between_nodes(1, 4, include_alternatives=False)
        value = load_snapshot([observation()])
        with cctv_routing.use_snapshot(value):
            busy = router.route_between_nodes(1, 4, include_alternatives=False)
        after = router.route_between_nodes(1, 4, include_alternatives=False)
    assert clear["distance_km"] == .24
    assert busy["distance_km"] == .32
    assert after["geometry"] == clear["geometry"]
    audit = busy["routing_model"]["cctv"]
    assert audit["observations"][0]["on_selected_path"] is False
    assert audit["observations"][0]["validation_status"] == "estimated"
    assert audit["measured_speed_or_eta"] is False
    assert "cctv" not in after["routing_model"]


def test_camera_cost_is_separate_from_eta_speed_and_emissions_inputs():
    with graph(only_one_path=True):
        clear = router.route_between_nodes(1, 4, include_alternatives=False)
        with cctv_routing.use_snapshot(load_snapshot([observation()])):
            busy = router.route_between_nodes(1, 4, include_alternatives=False)
    assert busy["geometry"] == clear["geometry"]
    assert busy["modeled_travel_time_mins"] == clear["modeled_travel_time_mins"]
    assert busy["routing_cost_breakdown"]["congestion_delay_mins"] == clear["routing_cost_breakdown"]["congestion_delay_mins"]
    assert busy["routing_cost_breakdown"]["cctv_search_penalty_mins"] > 0
    assert busy["objective_cost_s"] > clear["objective_cost_s"]
    assert "speed_kmh" not in busy["routing_model"]["cctv"]


@pytest.mark.parametrize("overrides", [
    {"observed_at": (NOW - timedelta(seconds=16)).isoformat()},
    {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"observed_at": "2026-10-24T08:00:00"},
    {"graph_revision": "wrong graph"},
    {"estimated_allowed": False},
    {"road_segment_id": "corridor-1"},
    {"road_segment_id": "osm-edge:2:1:10"},
    {"road_segment_id": "osm-edge:1:2:99"},
    {"validation_status": "validated", "operational_eligible": False},
    {"timestamp_basis": "unknown"},
    {"congestion_index": float("nan")},
    {"congestion_index": True},
    {"congestion_index": 101},
    {"maximum_age_seconds": 10_000},
])
def test_invalid_stale_unmapped_revoked_evidence_does_not_influence_routes(overrides):
    with graph():
        assert not load_snapshot([observation(**overrides)]).observations


def test_camera_does_not_unlock_private_detour_or_change_shortest_objective():
    with graph(private_detour=True):
        with cctv_routing.use_snapshot(load_snapshot([observation()])):
            allowed = router.route_between_nodes(1, 4, include_alternatives=False)
        assert allowed["distance_km"] == .24
    with graph():
        with cctv_routing.use_snapshot(load_snapshot([observation()])):
            shortest = router.route_between_nodes(1, 4, include_alternatives=False, route_preference="SHORTEST")
        assert shortest["distance_km"] == .24


def test_exact_directed_edge_scope_and_maximum_not_duplicate_sum():
    with graph():
        value = load_snapshot([observation(), observation(camera_id="second-view")])
        with cctv_routing.use_snapshot(value):
            assert cctv_routing.edge_penalty_s(1, 2, 10, 10., 1.) == 20.
            assert cctv_routing.edge_penalty_s(2, 1, 10, 10., 1.) == 0.
            assert cctv_routing.edge_penalty_s(1, 2, 20, 10., 1.) == 0.
            assert cctv_routing.edge_penalty_s(2, 4, 10, 10., 1.) == 0.
            assert cctv_routing.edge_penalty_s(1, 2, 10, 10., 100.) == 20.


def test_future_departure_and_failed_camera_store_preserve_regular_route():
    with graph(), patch.object(cctv_store, "road_segment_scores", create=True) as read:
        read.return_value = [observation()]
        future = cctv_routing.route_call(
            router.route_between_nodes, 1, 4, include_alternatives=False,
            effective_at=NOW + timedelta(minutes=30), observed_at=NOW,
        )
        read.assert_not_called()
        assert "cctv" not in future["routing_model"]
        read.side_effect = cctv_store.CCTVStoreUnavailable("fixture failure")
        fallback = cctv_routing.route_call(
            router.route_between_nodes, 1, 4, include_alternatives=False,
            effective_at=NOW, observed_at=NOW,
        )
        assert fallback["geometry"] == future["geometry"]


def test_corridor_geometry_passes_departure_and_camera_snapshot_to_actual_router():
    with graph(), patch.dict(router.LANDMARKS, {"test-origin": 1, "test-destination": 4}), patch.object(
        cctv_store, "road_segment_scores", return_value=[observation()], create=True,
    ):
        route = route_solver._geometry(
            {"origin": "test-origin", "destination": "test-destination"},
            effective_at=NOW, observed_at=NOW,
        )
    assert route["distance_km"] == .32
    assert route["routing_model"]["cctv"]["observations"][0]["camera_id"] == "test-camera"


def test_snapshot_context_does_not_leak_after_exception():
    value = cctv_routing.CameraRoutingSnapshot(NOW.isoformat(), (
        cctv_routing.CameraEdgeObservation("fixture", (1, 2, 10), 100., NOW.isoformat(), 0., "estimated", "source_clock"),
    ))
    with pytest.raises(RuntimeError), cctv_routing.use_snapshot(value):
        assert cctv_routing.active_snapshot() is value
        raise RuntimeError("fixture")
    assert cctv_routing.active_snapshot() is None


def test_depart_now_accepts_transport_latency_but_rejects_historical_departure():
    departure, arrival = route_solver._validate_schedule_request(
        NOW - timedelta(milliseconds=300), None, NOW,
    )
    assert departure == NOW and arrival is None
    with pytest.raises(ValueError, match="departure_at must be in the future"):
        route_solver._validate_schedule_request(NOW - timedelta(seconds=31), None, NOW)
    future = NOW + timedelta(minutes=30)
    assert route_solver._validate_schedule_request(future, None, NOW)[0] == future
