"""Real HTTP camera ingestion -> operator A* -> saved driver dispatch.

The road graph, detections and Supabase responses are engineering fixtures.
The analyzer, evidence validation, identity resolver, FastAPI serialization,
route search, SQLite persistence and credentialless driver lookup are real.
These checks prove the integration contract, not camera/model accuracy.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import uvicorn

from backend.tests.test_vehicle_count_preview_api import api
from backend.tests.test_cctv_live_estimates import config, evidence, registered, NOW
from auth import identity
from auth.tests.test_auth import ADMIN_ID, DRIVER_ID, FakeSupabase, make_token
from services import cctv_routing, clock, route_solver, route_store, router


class CameraRoutingHTTPTests(unittest.TestCase):
    """Exact-edge live costs survive the same boundary used by both UIs."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="crossflow-camera-route-http-"))
        root = Path(folder)
        self.registry = root / "cameras.json"
        self.config = replace(config(), corridor_id=None, road_segment_id="osm-edge:1:2:10")
        self.camera_row = registered(self.config)
        self.camera_row["estimation_config"]["graph_revision"] = router.GRAPH_REVISION
        self.write_registry()
        self.instant = NOW
        self.stack.enter_context(patch.dict(os.environ, {
            "CROSSFLOW_CCTV_REGISTRY": str(self.registry),
            "CROSSFLOW_CCTV_DB": str(root / "camera.sqlite"),
            "CROSSFLOW_CCTV_STORE": "local", "VERCEL": "", "VERCEL_ENV": "",
            "CROSSFLOW_CCTV_ENABLED": "0", "CROSSFLOW_ARCHIVE_LIVE_TRAFFIC": "0",
            "CROSSFLOW_REAL_TRAFFIC_TRAINING": "0",
            "CROSSFLOW_ROUTE_LEARNING_DB": str(root / "learning.sqlite"),
            "CROSSFLOW_FERRY_FRESHNESS_DB": str(root / "ferry.sqlite"),
            "CROSSFLOW_ROUTING_INTELLIGENCE_STORE": "local",
            "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS": "0",
            "CROSSFLOW_ADMIN_TOKEN": "engineering-camera-machine",
            "CROSSFLOW_AUTH_MODE": "supabase",
            "SUPABASE_URL": "https://engineering-fixture.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_engineering_fixture",
            "SUPABASE_SECRET_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": "",
            "TOMTOM_API_KEY": "", "CROSSFLOW_TRAFFIC_UPSTREAM_URL": "",
        }))
        self.stack.enter_context(patch.object(clock, "now", side_effect=lambda: self.instant))
        store = route_store.RouteStore(str(root / "routes.sqlite"))
        self.stack.callback(store.close)
        self.stack.enter_context(patch.object(api, "_ROUTE_STORE", store))
        self.admin_token, self.driver_token = make_token(subject=ADMIN_ID), make_token(subject=DRIVER_ID)
        admin = FakeSupabase(user={"id": ADMIN_ID}, profile_rows=[
            {"id": ADMIN_ID, "role": "admin", "display_name": "Operator fixture"},
        ])
        driver = FakeSupabase()
        self.identity_calls = []
        def authentication_transport(configuration, **kwargs):
            self.identity_calls.append(kwargs)
            target = admin if kwargs.get("access_token") == self.admin_token else driver
            return target(configuration, **kwargs)
        identity.clear_cache()
        self.stack.callback(identity.clear_cache)
        self.stack.enter_context(patch.object(identity.transport, "request_json", side_effect=authentication_transport))

        # A 240 m preferred carriageway, a 320 m detour and their reverse
        # directions. A camera maps only 1->2, never the reverse or full road.
        nodes = {1: (1.13, 104.04), 2: (1.13, 104.041),
                 3: (1.131, 104.041), 4: (1.13, 104.042)}
        def edge(target, length, index):
            return router.RoadEdge(target, length, index, highway="primary", name="Engineering road")
        adjacency = {
            1: [edge(2, 120., 10), edge(3, 160., 20)],
            2: [edge(1, 120., 10), edge(4, 120., 10)],
            3: [edge(1, 160., 20), edge(4, 160., 20)],
            4: [edge(2, 120., 10), edge(3, 160., 20)],
        }
        self.clear_graph_caches()
        self.stack.callback(self.clear_graph_caches)
        self.stack.enter_context(patch.multiple(router, NODES=nodes, ROAD_ADJ=adjacency, NODE_META={},
            ADJ={node: [(item.target, item.distance_m) for item in edges] for node, edges in adjacency.items()},
            LANDMARKS={"camera-origin": 1, "camera-destination": 4}))
        self.stack.enter_context(patch.object(router, "_alt_index_for_profile", return_value=None))
        corridor = {"id": "camera-http-corridor", "name": "Engineering camera route",
                    "origin": "camera-origin", "destination": "camera-destination",
                    "destination_port": None, "base_time_mins": 1., "corridor_idx": 0, "key_checkpoints": []}
        self.stack.enter_context(patch.object(route_solver, "CORRIDORS", [corridor]))
        self.stack.enter_context(patch.object(route_solver, "LOCATION_BY_ID", {
            "camera-origin": {"name": "Fixture origin", "lat": nodes[1][0], "lng": nodes[1][1]},
            "camera-destination": {"name": "Fixture destination", "lat": nodes[4][0], "lng": nodes[4][1]},
        }))
        self.stack.enter_context(patch.object(route_solver.live_traffic, "get_congestion_zones", return_value=[]))
        self.stack.enter_context(patch.object(route_solver, "_probabilistic_corridor_prediction", return_value=(
            {"current_score": 0., "predicted_30min": 0., "predicted_60min": 0.,
             "estimated_delay_mins": 0., "status": "FREE_FLOW", "risk_level": "LOW", "trend": "STABLE"},
            router.EdgeCongestionEstimate(source="engineering_fixture_free_flow"),
        )))
        # Use real sockets/FastAPI handlers while keeping background collection
        # off. No production app or local persistent database is mutated.
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.socket.listen(20)
        self.server = uvicorn.Server(uvicorn.Config(api.app, lifespan="off", log_level="error"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        self.stack.callback(self.stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.fail("The isolated HTTP fixture did not start")
            time.sleep(.01)

    @staticmethod
    def clear_graph_caches():
        router._main_routing_core.cache_clear()
        router._route_between_nodes_cached.cache_clear()

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.socket.close()
        self.assertFalse(self.thread.is_alive(), "HTTP fixture leaked its server thread")

    def write_registry(self):
        self.registry.write_text(json.dumps({"schema_version": 1, "cameras": [self.camera_row]}))

    def call(self, path, *, payload=None, token=None, machine=False):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if machine:
            headers["x-crossflow-admin-token"] = "engineering-camera-machine"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode() if payload is not None else None, headers=headers,
            method="POST" if payload is not None else "GET")
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read()), dict(response.headers)

    def plan(self, *, token=None, **changes):
        request = {"corridor_id": "camera-http-corridor", "vehicle_type": "COMMUTER",
                   "departure_at": self.instant.isoformat(), "weather": 0, "route_preference": "FASTEST", **changes}
        status, result, _ = self.call("/api/optimize-route", payload=request, token=token or self.admin_token)
        self.assertEqual(status, 200, result)
        return result

    def test_live_mapped_view_changes_operator_route_and_exact_driver_dispatch_then_withdraws(self):
        request = {"corridor_id": "camera-http-corridor", "vehicle_type": "COMMUTER",
                   "departure_at": self.instant.isoformat()}
        # Actual auth resolver is active: the camera machine credential and a
        # signed-in driver never acquire planning authority.
        self.assertEqual(self.call("/api/optimize-route", payload=request)[0], 401)
        self.assertEqual(self.call("/api/optimize-route", payload=request, token=self.driver_token)[0], 403)
        self.assertEqual(self.call("/api/optimize-route", payload=request, machine=True)[0], 401)
        clear = self.plan()
        self.assertEqual(clear["corridor"]["distance_km"], .24)
        self.assertNotIn("cctv", clear["routing_model"])
        status, accepted, _ = self.call("/api/internal/cctv-observations",
            payload=evidence(self.config), machine=True)
        self.assertEqual(status, 200, accepted)
        self.assertFalse(accepted["cctv_observation"]["operational_eligible"])

        busy = self.plan()
        self.assertEqual(busy["corridor"]["distance_km"], .32)
        self.assertNotEqual(busy["route_geometry"], clear["route_geometry"])
        self.assertNotEqual(busy["route_code"], clear["route_code"])
        audit = busy["routing_model"]["cctv"]
        self.assertFalse(audit["measured_speed_or_eta"])
        self.assertEqual(audit["scope"], "explicitly_mapped_directed_camera_edges_only")
        self.assertEqual(audit["observations"][0]["validation_status"], "estimated")
        self.assertFalse(audit["observations"][0]["on_selected_path"])
        self.assertTrue(busy["dispatch_available"])
        status, driver, headers = self.call("/api/routes/" + busy["route_code"])
        self.assertEqual(status, 200, driver)
        self.assertIn("no-store", headers["cache-control"])
        for name in ("route_geometry", "navigation", "routing_model", "objective_cost_s", "route_code"):
            self.assertEqual(driver[name], busy[name], name)

        # Camera occupancy cannot masquerade as speed, and opposite/unrelated
        # exact directed edges retain zero camera cost.
        with cctv_routing.use_snapshot(cctv_routing.snapshot(now=self.instant)):
            self.assertGreater(cctv_routing.edge_penalty_s(1, 2, 10, 10., 1.), 0)
            for edge in ((2, 1, 10), (2, 4, 10), (1, 3, 20)):
                self.assertEqual(cctv_routing.edge_penalty_s(*edge, 10., 1.), 0.)
        future = self.plan(departure_at=(self.instant + timedelta(minutes=30)).isoformat())
        self.assertEqual(future["route_geometry"], clear["route_geometry"])
        self.assertNotIn("cctv", future["routing_model"])

        # Withdrawal happens on the next request, without clearing caches or
        # rewriting the driver's previously issued snapshot.
        self.instant = NOW + timedelta(seconds=self.config.maximum_age_seconds + 1)
        expired = self.plan()
        self.assertEqual(expired["route_geometry"], clear["route_geometry"])
        self.assertNotIn("cctv", expired["routing_model"])
        status, still_assigned, _ = self.call("/api/routes/" + busy["route_code"])
        self.assertEqual(status, 200)
        self.assertEqual(still_assigned["route_geometry"], busy["route_geometry"])
        self.instant = NOW
        self.camera_row["estimation_config"]["allow_routing"] = False
        self.write_registry()
        revoked = self.plan()
        self.assertEqual(revoked["route_geometry"], clear["route_geometry"])
        self.assertNotIn("cctv", revoked["routing_model"])
        self.assertTrue(any(call["path"] == "/auth/v1/user" for call in self.identity_calls))


if __name__ == "__main__":
    unittest.main()
