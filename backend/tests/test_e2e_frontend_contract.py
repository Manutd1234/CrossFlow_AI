"""End-to-end tests over real HTTP against a live server process.

Every other suite calls handlers in-process, which cannot catch the failures
that actually reach the browser: a route registered at the wrong path, a
serialized field the TypeScript client does not expect, an auth gate that only
behaves correctly when FastAPI resolves the headers itself, or a payload the
frontend cannot parse. This boots uvicorn exactly as the dev server does and
speaks to it the way the frontend does.

The assertions mirror ``frontend/src/types.ts`` and the calls in
``frontend/src/services/api.ts``. If the API stops satisfying them, the
dashboard breaks even though the unit suites stay green.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_DIR / "backend"
SERVER_BOOT_TIMEOUT_S = 180
ROUTE_READ_TOKEN = "e2e-route-read-token"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _isolated_server_environment(directory: Path, extra_env: dict[str, str]) -> dict[str, str]:
    """Keep subprocess HTTP tests away from local collectors and app ledgers."""
    registry = directory / "camera-registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "cameras": []}))
    return {
        **os.environ,
        "CROSSFLOW_ROUTE_READ_TOKEN": ROUTE_READ_TOKEN,
        "CROSSFLOW_AUTH_MODE": "disabled",
        "CROSSFLOW_WARM_ROUTING_CACHES": "0",
        "CROSSFLOW_ROUTE_DB": ":memory:",
        "CROSSFLOW_ROUTE_LEARNING_DB": ":memory:",
        "CROSSFLOW_HISTORY_DB": ":memory:",
        "CROSSFLOW_CCTV_REGISTRY": str(registry),
        "CROSSFLOW_CCTV_DB": str(directory / "cctv-observations.sqlite"),
        "CROSSFLOW_FORECAST_EVALUATION_DB": str(directory / "forecast-evaluation.sqlite"),
        "CROSSFLOW_FERRY_FRESHNESS_DB": str(directory / "ferry-freshness.sqlite"),
        "CROSSFLOW_REAL_MODEL_PATH": str(directory / "unavailable-real-model.json"),
        "CROSSFLOW_OBSERVED_MODEL_PATH": "",
        "CROSSFLOW_ROUTING_INTELLIGENCE_STORE": "local",
        "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS": "0",
        "SUPABASE_URL": "",
        "SUPABASE_SECRET_KEY": "",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        "TOMTOM_API_KEY": "",
        "CROSSFLOW_TRAFFIC_UPSTREAM_URL": "",
        "CROSSFLOW_ENABLE_GOOGLE_BENCHMARK": "false",
        # Explicit class-owned seeded stores/auth configurations still win.
        **extra_env,
        # This HTTP fixture never opts into external collection or fitting,
        # even when its parent shell or a future subclass enables those jobs.
        "CROSSFLOW_CCTV_ENABLED": "0",
        "CROSSFLOW_REAL_TRAFFIC_TRAINING": "0",
        "CROSSFLOW_ARCHIVE_LIVE_TRAFFIC": "0",
    }


class LiveServerTestCase(unittest.TestCase):
    """Boots one server for the whole class; startup dominates the runtime."""

    server: subprocess.Popen
    base_url: str
    #: Per-subclass server environment, so one suite can boot the deployed
    #: Supabase-auth configuration while another boots the open dev default.
    extra_env: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        port = free_port()
        cls.base_url = f"http://127.0.0.1:{port}"
        cls.runtime_directory = tempfile.TemporaryDirectory(prefix="crossflow-http-runtime-")
        cls.addClassCleanup(cls.runtime_directory.cleanup)
        environment = _isolated_server_environment(Path(cls.runtime_directory.name), cls.extra_env)
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(BACKEND_DIR),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        cls.addClassCleanup(cls._stop_server)
        cls._await_ready()

    @classmethod
    def _stop_server(cls) -> None:
        cls.server.terminate()
        try:
            cls.server.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            cls.server.kill()
            cls.server.wait(timeout=5)
        if cls.server.stdout:
            cls.server.stdout.close()

    @classmethod
    def _await_ready(cls) -> None:
        """Poll until the app answers; a cold graph load takes many seconds."""
        deadline = time.monotonic() + SERVER_BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                output = (cls.server.stdout.read() or b"").decode("utf-8", "replace")
                raise AssertionError(f"server exited during boot:\n{output}")
            try:
                with urllib.request.urlopen(f"{cls.base_url}/api/corridors", timeout=10):
                    return
            except (urllib.error.URLError, socket.timeout, ConnectionError):
                time.sleep(0.5)
        raise AssertionError("server did not become ready in time")

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: int = 120,
    ) -> tuple[int, Any]:
        """Return (status, decoded body), treating HTTP errors as results."""
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=payload, method=method,
        )
        request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            raw = error.read() or b"null"
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw.decode("utf-8", "replace")


class LiveServerFixtureIsolationTests(unittest.TestCase):
    def test_inherited_background_jobs_and_app_ledgers_cannot_leak_into_http_tests(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "CROSSFLOW_CCTV_ENABLED": "1", "CROSSFLOW_REAL_TRAFFIC_TRAINING": "1",
            "CROSSFLOW_ARCHIVE_LIVE_TRAFFIC": "1", "CROSSFLOW_CCTV_DB": "/app/cctv.sqlite",
            "CROSSFLOW_FORECAST_EVALUATION_DB": "/app/forecasts.sqlite",
        }):
            directory = Path(folder)
            environment = _isolated_server_environment(directory, {})
            for name in ("CROSSFLOW_CCTV_ENABLED", "CROSSFLOW_REAL_TRAFFIC_TRAINING", "CROSSFLOW_ARCHIVE_LIVE_TRAFFIC"):
                self.assertEqual(environment[name], "0")
            for name in ("CROSSFLOW_CCTV_DB", "CROSSFLOW_FORECAST_EVALUATION_DB", "CROSSFLOW_FERRY_FRESHNESS_DB"):
                self.assertEqual(Path(environment[name]).parent, directory)
                self.assertFalse(Path(environment[name]).exists())
            self.assertEqual(json.loads(Path(environment["CROSSFLOW_CCTV_REGISTRY"]).read_text())["cameras"], [])

    def test_explicit_seeded_fixture_overrides_survive_without_starting_background_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            seeded = str(Path(folder) / "seeded.sqlite")
            overrides = {"CROSSFLOW_ROUTE_DB": seeded, "CROSSFLOW_FORECAST_EVALUATION_DB": seeded,
                         "CROSSFLOW_AUTH_MODE": "supabase", "CROSSFLOW_CCTV_ENABLED": "1"}
            environment = _isolated_server_environment(Path(folder), overrides)
            self.assertEqual(environment["CROSSFLOW_ROUTE_DB"], seeded)
            self.assertEqual(environment["CROSSFLOW_FORECAST_EVALUATION_DB"], seeded)
            self.assertEqual(environment["CROSSFLOW_AUTH_MODE"], "supabase")
            self.assertEqual(environment["CROSSFLOW_CCTV_ENABLED"], "0")


class FrontendRouteContractTests(LiveServerTestCase):
    """The planner's own request/response cycle, over the wire."""

    def plan_route(self) -> dict:
        status, body = self.call("/api/optimize-route", method="POST", body={
            "corridor_id": "corridor-1",
            "vehicle_type": "CARGO_TRUCK",
            "weather": 0,
            "hour": 8,
            "route_preference": "BALANCED",
        })
        self.assertEqual(status, 200, body)
        return body

    def test_current_analytics_and_history_do_not_invent_absent_observations(self) -> None:
        status, summary = self.call("/api/operations")
        self.assertEqual(status, 200, summary)
        self.assertIsNone(summary["average_congestion_index"])
        self.assertIsNone(summary["total_co2_reduced_today_kg"])
        self.assertEqual(summary["analytics_metadata"]["observed_corridor_count"], 0)
        self.assertEqual(summary["alerts"], [])
        status, history = self.call("/api/historical-congestion?corridor_id=corridor-1&days=7")
        self.assertEqual(status, 200, history)
        self.assertEqual(history["history_metadata"]["selection_scope"], "observed_only")
        self.assertEqual(history["history_metadata"]["sample_count"], 0)
        self.assertEqual(len(history["hourly_profile"]), 24)
        self.assertTrue(all(item["avg_score"] is None and item["sample_count"] == 0 for item in history["hourly_profile"]))
        self.assertEqual(history["weekly_trend"], [])

    def test_model_simulation_returns_computed_forecasts_and_private_validation_errors(self) -> None:
        status, body = self.call("/api/analytics/simulation", method="POST", body={
            "weather": 2, "corridor_ids": ["corridor-1"],
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data_source"], "simulation")
        self.assertFalse(body["observed"])
        scenario = body["simulation"]
        self.assertEqual(scenario["prediction_count"], 27)
        self.assertEqual(len(scenario["hourly_forecast"]), 24)
        self.assertEqual(len(scenario["weather_comparison"]), 3)
        self.assertEqual(scenario["corridors"][0]["corridor_id"], "corridor-1")
        request = urllib.request.Request(
            self.base_url + "/api/analytics/simulation",
            data=json.dumps({"horizon_hours": 1_000_000}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=15)
        self.assertEqual(caught.exception.code, 422)
        self.assertIn("no-store", caught.exception.headers.get("Cache-Control", ""))
        caught.exception.close()

    def test_planner_response_carries_every_field_the_ui_renders(self) -> None:
        body = self.plan_route()
        # Each of these is read directly by RouteOptimizer's summary panel.
        for field in (
            "route_id", "route_code", "corridor", "vehicle_type",
            "estimated_travel_time_mins", "total_eta_mins", "co2_emissions_kg",
            "congestion_prediction", "optimal_departure", "route_geometry",
            "route_data_source",
        ):
            self.assertIn(field, body, f"{field} missing from planner response")

        self.assertRegex(body["route_code"], r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$")
        self.assertRegex(body["route_id"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(body["route_geometry"], list)
        self.assertGreater(len(body["route_geometry"]), 1)
        corridor = body["corridor"]
        for field in ("id", "name", "distance_km", "base_time_mins"):
            self.assertIn(field, corridor)

    def test_balanced_objective_survives_the_round_trip(self) -> None:
        """The picker was removed, so the client always sends BALANCED."""
        body = self.plan_route()
        self.assertEqual(body.get("route_preference", "BALANCED"), "BALANCED")

    def test_planned_route_is_retrievable_by_its_dispatch_code(self) -> None:
        planned = self.plan_route()
        status, stored = self.call(f"/api/routes/{planned['route_code']}")
        self.assertEqual(status, 200, stored)
        self.assertEqual(stored["route_code"], planned["route_code"])
        self.assertEqual(stored["route_id"], planned["route_id"])

    def test_retrieval_needs_no_credential(self) -> None:
        """The driver client is a guest; the code alone is the capability."""
        planned = self.plan_route()
        status, _ = self.call(f"/api/routes/{planned['route_code']}")
        self.assertEqual(status, 200)

    def test_retrieval_is_not_cached_by_the_browser(self) -> None:
        planned = self.plan_route()
        request = urllib.request.Request(
            f"{self.base_url}/api/routes/{planned['route_code']}",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_unknown_code_is_a_clean_404_the_client_can_message(self) -> None:
        status, body = self.call("/api/routes/2222222")
        self.assertEqual(status, 404)
        self.assertIsInstance(body.get("detail"), str)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{self.base_url}/api/routes/2222222", timeout=10)
        self.assertIn("no-store", caught.exception.headers.get("Cache-Control", ""))
        caught.exception.close()

    def test_malformed_code_is_rejected_before_any_lookup(self) -> None:
        # Lowercase and ambiguous characters are outside the code alphabet.
        for bad_code in ("abcdefg", "0000000", "TOOLONGCODE"):
            status, _ = self.call(f"/api/routes/{bad_code}")
            self.assertEqual(status, 422, f"{bad_code} should fail validation")

    def test_identical_requests_resolve_to_the_same_route_identity(self) -> None:
        """Content addressing is what lets history dedupe a replanned journey."""
        self.assertEqual(self.plan_route()["route_id"], self.plan_route()["route_id"])

    def test_free_point_planner_serializes_a_scheduled_road_journey(self) -> None:
        departure = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(
            timezone(timedelta(hours=7)),
        ).isoformat()
        status, body = self.call("/api/optimize-free-route", method="POST", body={
            "origin_lat": 1.1465, "origin_lng": 104.0125,
            "destination_lat": 1.1318, "destination_lng": 104.0554,
            "vehicle_type": "COMMUTER",
            "departure_at": departure,
            "weather": 1,
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["route_type"], "ROAD_ROUTE")
        self.assertEqual(body["scheduling"]["mode"], "DEPART_AT")
        self.assertTrue(body["planned_departure"].endswith("+07:00"))
        self.assertGreater(body["total_eta_mins"], 0)
        self.assertGreater(len(body["route_geometry"]), 1)
        status, stored = self.call(f"/api/routes/{body['route_code']}")
        self.assertEqual(status, 200, stored)
        self.assertEqual(stored["scheduling"], body["scheduling"])

    def test_multi_stop_planner_serializes_all_legs_and_dwell(self) -> None:
        departure = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(
            timezone(timedelta(hours=7)),
        ).isoformat()
        status, body = self.call("/api/optimize-multi-stop-route", method="POST", body={
            "stops": [
                {"lat": 1.1480, "lng": 104.0060, "name": "Batu Ampar"},
                {"lat": 1.1465, "lng": 104.0125, "name": "Nagoya Hill", "dwell_mins": 15},
                {"lat": 1.1318, "lng": 104.0554, "name": "Batam Centre"},
            ],
            "vehicle_type": "COMMUTER",
            "departure_at": departure,
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["route_type"], "MULTI_STOP_ROUTE")
        self.assertEqual(len(body["stops"]), 3)
        self.assertEqual(len(body["legs"]), 2)
        self.assertEqual(body["stops"][1]["dwell_mins"], 15)
        self.assertEqual(body["scheduling"]["mode"], "DEPART_AT")
        self.assertGreater(body["total_eta_mins"], 15)

    def test_route_validation_reaches_the_browser_as_actionable_errors(self) -> None:
        cases = [
            ("/api/optimize-route", {"corridor_id": "unknown", "vehicle_type": "COMMUTER"}, 400),
            ("/api/optimize-route", {"corridor_id": "corridor-1", "vehicle_type": "PLANE"}, 422),
            ("/api/optimize-route", {
                "corridor_id": "corridor-1", "vehicle_type": "COMMUTER",
                "departure_at": "2026-10-23T08:00:00",
            }, 422),
            ("/api/optimize-free-route", {
                "origin_lat": 91, "origin_lng": 104,
                "destination_lat": 1.13, "destination_lng": 104.05,
            }, 422),
            ("/api/optimize-multi-stop-route", {"stops": []}, 422),
        ]
        for path, request, expected in cases:
            with self.subTest(path=path, request=request):
                status, body = self.call(path, method="POST", body=request)
                self.assertEqual(status, expected, body)
                self.assertIn("detail", body)


class FrontendHistoryContractTests(LiveServerTestCase):
    """History is the one route-facing endpoint that requires an account."""

    def test_history_refuses_service_when_auth_is_disabled(self) -> None:
        status, body = self.call("/api/routes")
        self.assertEqual(status, 503, body)
        self.assertIn("authentication", body["detail"].lower())

    def test_history_never_returns_runs_to_an_anonymous_caller(self) -> None:
        status, body = self.call("/api/routes?limit=5")
        self.assertNotEqual(status, 200)
        self.assertNotIn("runs", body if isinstance(body, dict) else {})

    def test_history_limit_is_validated_at_the_boundary(self) -> None:
        status, _ = self.call("/api/routes?limit=99999")
        self.assertEqual(status, 422)


class FrontendCorridorContractTests(LiveServerTestCase):
    """The views that render before anyone plans anything."""

    extra_env = {"CROSSFLOW_ADMIN_TOKEN": "e2e-diagnostics-only"}

    def test_read_only_backtest_reports_insufficient_observed_history_over_http(self) -> None:
        status, before = self.call("/api/model-status")
        self.assertEqual(status, 200)
        request = urllib.request.Request(
            f"{self.base_url}/api/routing-intelligence/congestion-backtest",
            data=b"{}", headers={
                "Content-Type": "application/json",
                "X-CrossFlow-Admin-Token": "e2e-diagnostics-only",
            }, method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            body = json.loads(response.read())
        self.assertEqual(body["backtest"]["status"], "insufficient_observations")
        self.assertEqual(body["backtest"]["observed_rows"], 0)
        self.assertFalse(body["backtest"]["model_replaced"])
        self.assertEqual(body["evaluation_snapshot"]["horizons_minutes"], [15, 30, 60])
        status, after = self.call("/api/model-status")
        self.assertEqual(status, 200)
        self.assertEqual(before["metrics"], after["metrics"])

    def test_backtest_policy_overrides_are_rejected_with_uncacheable_errors(self) -> None:
        for raw_body in (
            b'{"records": []}', b'{"include_modelled": true}',
            b'{"horizons_minutes": [1]}', b'{"limit": 20000}', b'{invalid json',
        ):
            with self.subTest(body=raw_body):
                request = urllib.request.Request(
                    f"{self.base_url}/api/routing-intelligence/congestion-backtest",
                    data=raw_body, headers={
                        "Content-Type": "application/json",
                        "X-CrossFlow-Admin-Token": "e2e-diagnostics-only",
                    }, method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=30)
                self.assertEqual(caught.exception.code, 422)
                self.assertIn("no-store", caught.exception.headers.get("Cache-Control", ""))
                caught.exception.close()

    def test_protected_diagnostics_serialize_actual_storage_and_model_scope(self) -> None:
        status, body = self.call(
            "/api/routing-intelligence/status",
            headers={"X-CrossFlow-Admin-Token": "e2e-diagnostics-only"},
        )
        self.assertEqual(status, 200, body)
        diagnostics = body["integration_status"]
        self.assertEqual(diagnostics["production_readiness"], "not_assessed")
        self.assertFalse(diagnostics["additional_remote_probes_performed"])
        self.assertEqual(
            diagnostics["persistence"]["local_routes"]["durability"], "process_memory",
        )
        self.assertFalse(diagnostics["persistence"]["shared_routes"]["configured"])
        self.assertEqual(diagnostics["model"]["observed_training_rows"], 0)
        self.assertEqual(diagnostics["providers"]["tomtom"]["current_response_source"], "not_sampled")
        self.assertNotIn("e2e-diagnostics-only", json.dumps(body))

    def test_corridors_match_the_shape_the_map_expects(self) -> None:
        """``fetchCorridors`` reads ``payload.corridors``; anything else falls
        back to the bundled offline list and silently hides live data."""
        status, body = self.call("/api/corridors")
        self.assertEqual(status, 200)
        self.assertIn("corridors", body)
        corridors = body["corridors"]
        self.assertIsInstance(corridors, list)
        self.assertGreater(len(corridors), 0)
        for field in ("id", "name", "distance_km", "base_time_mins"):
            self.assertIn(field, corridors[0])

    def test_route_locations_expose_coordinates_for_the_picker(self) -> None:
        """``fetchRouteLocations`` reads ``payload.locations``."""
        status, body = self.call("/api/route-locations")
        self.assertEqual(status, 200)
        self.assertIn("locations", body)
        locations = body["locations"]
        self.assertIsInstance(locations, list)
        self.assertGreater(len(locations), 0)
        for field in ("id", "name", "category", "lat", "lng"):
            self.assertIn(field, locations[0])

    def test_every_envelope_carries_the_provenance_badge_fields(self) -> None:
        """The header's source badge reads these off any enveloped response."""
        status, body = self.call("/api/corridors")
        self.assertEqual(status, 200)
        for field in ("generated_at", "data_source", "provenance"):
            self.assertIn(field, body)

    def test_cors_allows_the_vite_dev_origin(self) -> None:
        """Without this the browser blocks every call in local development."""
        request = urllib.request.Request(
            f"{self.base_url}/api/corridors",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            allowed = response.headers.get("Access-Control-Allow-Origin", "")
        self.assertIn(allowed, ("*", "http://localhost:5173"))

    def test_all_dashboard_features_serialize_their_public_contract(self) -> None:
        endpoints = {
            "/api/corridor-routes": ("routes", list),
            "/api/vehicle-profiles": ("vehicle_profiles", list),
            "/api/route-preferences": ("route_preferences", list),
            "/api/live-traffic": ("zones", list),
            "/api/historical-congestion?corridor_id=corridor-1&days=7": ("hourly_profile", list),
            "/api/model-status": ("metrics", dict),
            "/api/ferries": ("ferries", list),
            "/api/port-intelligence": ("ports", list),
            "/api/operations": ("alerts", list),
        }
        for path, (key, expected_type) in endpoints.items():
            with self.subTest(path=path):
                status, body = self.call(path)
                self.assertEqual(status, 200, body)
                self.assertIsInstance(body[key], expected_type)
                for provenance_field in ("generated_at", "data_source", "provenance"):
                    self.assertIn(provenance_field, body)

    def test_analytics_and_geocoding_reject_invalid_inputs(self) -> None:
        for path, expected in (
            ("/api/historical-congestion?corridor_id=unknown", 404),
            ("/api/historical-congestion?corridor_id=corridor-1&days=0", 422),
            ("/api/geocode?q=", 422),
            ("/api/reverse-geocode?lat=91&lng=104", 422),
        ):
            with self.subTest(path=path):
                status, body = self.call(path)
                self.assertEqual(status, expected, body)
                self.assertIn("detail", body)

    def test_machine_operations_are_protected_over_http(self) -> None:
        for path, method in (
            ("/api/retrain-model", "POST"),
            ("/api/route-learning/status", "GET"),
            ("/api/routing-intelligence/status", "GET"),
            ("/api/routing-intelligence/congestion-backtest", "POST"),
        ):
            with self.subTest(path=path):
                status, body = self.call(path, method=method)
                self.assertEqual(status, 403, body)


class DeployedAuthConfigurationTests(LiveServerTestCase):
    """The server as Vercel runs it, with Supabase auth switched on.

    Enabling auth must not close the driver's door. That regressed once
    already, and the failure only appears under the deployed configuration.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # An existing dispatch must survive the auth boundary independently
        # of planning permissions. Seed an isolated file, close it, then let
        # the real server reopen it under its Supabase configuration.
        cls.fixture_directory = tempfile.TemporaryDirectory(
            prefix="crossflow-auth-dispatch-",
        )
        cls.addClassCleanup(cls.fixture_directory.cleanup)
        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))
        from services.route_identity import route_id
        from services.route_store import RouteStore

        path = str(Path(cls.fixture_directory.name) / "routes.db")
        request = {"contract_fixture": "assigned_route"}
        route_key = route_id(request, route_kind="contract_fixture")
        store = RouteStore(path)
        try:
            code = store.route_code_for(route_key)
            cls.assigned_route = {
                "route_id": route_key,
                "route_code": code,
                "route_type": "ROAD_ROUTE",
                "route_geometry": [[104.0125, 1.1465], [104.0554, 1.1318]],
                "navigation": {"instructions": ["Follow the assigned road route"]},
                "route_data_source": "isolated_contract_fixture",
            }
            store.save(
                route_key, cls.assigned_route, request=request,
                route_kind="contract_fixture", created_by="operator-fixture",
            )
        finally:
            store.close()
        cls.extra_env = {
            "CROSSFLOW_AUTH_MODE": "supabase", "CROSSFLOW_ROUTE_DB": path,
        }
        super().setUpClass()

    def test_driver_retrieval_stays_open_when_auth_is_enabled(self) -> None:
        for key in (self.assigned_route["route_code"], self.assigned_route["route_id"]):
            with self.subTest(key=key):
                status, stored = self.call(f"/api/routes/{key}")
                self.assertEqual(status, 200, stored)
                for field in ("route_code", "route_id", "route_geometry", "navigation"):
                    self.assertEqual(stored[field], self.assigned_route[field])

    def test_history_demands_a_token_rather_than_reporting_misconfiguration(self) -> None:
        status, body = self.call("/api/routes")
        self.assertEqual(status, 401, body)

    def test_model_simulation_requires_an_operator_session_in_auth_mode(self) -> None:
        status, body = self.call("/api/analytics/simulation", method="POST", body={"weather": 0})
        self.assertEqual(status, 401, body)

    def test_history_never_honours_a_locally_minted_token(self) -> None:
        """A structurally valid JWS still has to survive Supabase.

        With no project credentials the server answers 503 rather than 401: it
        cannot verify the token, and saying "invalid" would be a claim it has
        no basis for. What matters either way is that nothing is handed back.
        """
        status, body = self.call(
            "/api/routes", headers={"Authorization": "Bearer forged.token.value"},
        )
        self.assertIn(status, (401, 503), body)
        self.assertNotIn("runs", body if isinstance(body, dict) else {})

    def test_every_planning_endpoint_requires_an_operator_session(self) -> None:
        requests = {
            "optimize-route": {
                "corridor_id": "corridor-2", "vehicle_type": "COMMUTER", "hour": 9,
            },
            "optimize-free-route": {
                "origin_lat": 1.1465, "origin_lng": 104.0125,
                "destination_lat": 1.1318, "destination_lng": 104.0554,
                "vehicle_type": "COMMUTER", "hour": 9,
            },
            "optimize-multi-stop-route": {
                "stops": [
                    {"lat": 1.1480, "lng": 104.0060},
                    {"lat": 1.1465, "lng": 104.0125},
                    {"lat": 1.1318, "lng": 104.0554},
                ],
                "vehicle_type": "COMMUTER", "hour": 9,
            },
        }
        for endpoint, payload in requests.items():
            for prefix in ("/api/", "/"):
                with self.subTest(path=prefix + endpoint):
                    request = urllib.request.Request(
                        f"{self.base_url}{prefix}{endpoint}",
                        data=json.dumps(payload).encode(), method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=15)
                    self.assertEqual(caught.exception.code, 401)
                    self.assertIn("no-store", caught.exception.headers.get("Cache-Control", ""))
                    caught.exception.close()

    def test_auth_status_tells_the_client_sign_in_is_possible(self) -> None:
        status, body = self.call("/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])


if __name__ == "__main__":
    unittest.main()
