"""Planning must be authorized before expensive work or route publication.

The real identity resolver is used with a controlled Supabase transport. This
keeps role/profile checks in the acceptance boundary instead of mocking the
permission check that the endpoints are meant to enforce.
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response

PROJECT_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_DIR / "backend"
for path in (str(BACKEND_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from auth.tests.test_auth import (
    ADMIN_ID,
    DRIVER_ID,
    AuthTestCase,
    FakeSupabase,
    make_token,
)
from auth import identity
import main as api_main


class RouteOperatorBoundaryTests(AuthTestCase):
    def benchmark_request(self):
        return api_main.RouteBenchmarkRequest(
            origin_lat=1.1308, origin_lng=104.053,
            destination_lat=1.145, destination_lng=104.02,
        )

    def test_enabled_benchmark_denies_missing_or_driver_identity_before_provider_work(self):
        for authorization, fake, status in (
            (None, None, 401),
            (f"Bearer {make_token()}", FakeSupabase(), 403),
        ):
            with self.subTest(status=status), ExitStack() as stack:
                stack.enter_context(patch.object(api_main.google_routes_benchmark, "is_enabled", return_value=True))
                stack.enter_context(patch.object(identity.transport, "request_json", fake)) if fake else None
                key = stack.enter_context(patch.object(api_main.google_routes_benchmark, "get_api_key"))
                snap = stack.enter_context(patch.object(api_main.router, "snap_to_graph"))
                provider = stack.enter_context(patch.object(api_main.google_routes_benchmark, "benchmark_routes"))
                with self.assertRaises(HTTPException) as caught:
                    api_main.api_route_benchmark(self.benchmark_request(), Response(), authorization)
                self.assertEqual(caught.exception.status_code, status)
                self.assertIn("no-store", caught.exception.headers["Cache-Control"])
                key.assert_not_called()
                snap.assert_not_called()
                provider.assert_not_called()

    def test_verified_operator_can_request_ephemeral_benchmark(self):
        fake = FakeSupabase(
            user={"id": ADMIN_ID},
            profile_rows=[{"id": ADMIN_ID, "role": "admin", "display_name": "Operator"}],
        )
        response = Response()
        with ExitStack() as stack:
            stack.enter_context(patch.object(identity.transport, "request_json", fake))
            stack.enter_context(patch.object(api_main.google_routes_benchmark, "is_enabled", return_value=True))
            stack.enter_context(patch.object(api_main.google_routes_benchmark, "get_api_key", return_value="test-key"))
            stack.enter_context(patch.object(api_main.router, "snap_to_graph", side_effect=[("start", 0), ("end", 0)]))
            provider = stack.enter_context(patch.object(api_main.google_routes_benchmark, "benchmark_routes", return_value={"routes": []}))
            result = api_main.api_route_benchmark(
                self.benchmark_request(), response, f"Bearer {make_token(subject=ADMIN_ID)}",
            )
        provider.assert_called_once()
        self.assertFalse(result["provenance"]["external_route_content_persisted"])
        self.assertIn("no-store", response.headers["Cache-Control"])

    def cases(self):
        return (
            (
                "optimize-route",
                api_main.api_optimize_route,
                api_main.RouteRequest(
                    corridor_id="batam-centre-to-nongsa", vehicle_type="COMMUTER",
                ),
                "optimize_route",
            ),
            (
                "optimize-free-route",
                api_main.api_optimize_free_route,
                api_main.FreeRouteRequest(
                    origin_lat=1.1308, origin_lng=104.053,
                    destination_lat=1.145, destination_lng=104.02,
                ),
                "optimize_free_route",
            ),
            (
                "optimize-multi-stop-route",
                api_main.api_optimize_multi_stop_route,
                api_main.MultiStopRouteRequest(stops=[
                    {"lat": 1.1308, "lng": 104.053},
                    {"lat": 1.145, "lng": 104.02},
                    {"lat": 1.11, "lng": 104.06},
                ]),
                "optimize_multi_stop_route",
            ),
        )

    def mock_work(self, stack: ExitStack):
        work = {}
        for name, value in {
            "_route_schedule_authority": (None, {}),
            "_approved_override_snapshot_for_route": (None, "unavailable"),
            "_routing_intelligence_audit": None,
            "_persist_route_response": {"route_id": "a" * 64, "route_code": "7K3M9PQ"},
            "optimize_route": {"route_type": "ROAD_ROUTE"},
            "optimize_free_route": {"route_type": "ROAD_ROUTE"},
            "optimize_multi_stop_route": {"route_type": "MULTI_STOP_ROUTE"},
        }.items():
            work[name] = stack.enter_context(
                patch.object(api_main, name, return_value=value),
            )
        return work

    def assert_denied(self, authorization, status, fake=None):
        for name, handler, request, _solver in self.cases():
            with self.subTest(endpoint=name), ExitStack() as stack:
                work = self.mock_work(stack)
                transport = stack.enter_context(patch.object(
                    identity.transport, "request_json", fake,
                )) if fake else stack.enter_context(patch.object(
                    identity.transport, "request_json",
                    side_effect=AssertionError("invalid credentials must not reach Supabase"),
                ))
                with self.assertRaises(HTTPException) as caught:
                    handler(request, authorization=authorization)
                self.assertEqual(caught.exception.status_code, status)
                self.assertIn("no-store", caught.exception.headers["Cache-Control"])
                for call in work.values():
                    call.assert_not_called()
                if fake is None:
                    transport.assert_not_called()

    def test_missing_and_malformed_tokens_cannot_plan_or_publish(self):
        for authorization in (None, "", "Basic credential", "Bearer invalid"):
            with self.subTest(authorization=authorization):
                self.assert_denied(authorization, 401)

    def test_machine_ingestion_secret_does_not_grant_operator_access(self):
        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "machine-only-secret"}):
            self.assert_denied("Bearer machine-only-secret", 401)

    def test_driver_cannot_promote_itself_through_token_or_user_metadata(self):
        fake = FakeSupabase(user={
            "id": DRIVER_ID, "role": "admin", "user_metadata": {"role": "admin"},
        })
        token = make_token(role="admin", user_metadata={"role": "admin"})
        self.assert_denied(f"Bearer {token}", 403, fake)
        self.assertTrue(fake.calls)
        self.assertTrue(all(call["access_token"] == token for call in fake.calls))

    def test_remotely_rejected_token_cannot_plan(self):
        self.assert_denied(
            f"Bearer {make_token()}", 401, FakeSupabase(error="http_401"),
        )

    def test_authentication_outage_fails_closed_before_route_work(self):
        self.assert_denied(
            f"Bearer {make_token()}", 503, FakeSupabase(error="network_error"),
        )

    def test_verified_administrator_plans_and_attributes_each_route(self):
        fake = FakeSupabase(
            user={"id": ADMIN_ID},
            profile_rows=[{"id": ADMIN_ID, "role": "admin", "display_name": "Operator"}],
        )
        for name, handler, request, solver in self.cases():
            with self.subTest(endpoint=name), ExitStack() as stack:
                work = self.mock_work(stack)
                stack.enter_context(patch.object(identity.transport, "request_json", fake))
                result = handler(request, authorization=f"Bearer {make_token(subject=ADMIN_ID)}")
                self.assertEqual(result["route_code"], "7K3M9PQ")
                work[solver].assert_called_once()
                persistence = work["_persist_route_response"]
                persistence.assert_called_once()
                self.assertIs(persistence.call_args.args[0], request)
                self.assertEqual(persistence.call_args.args[1], name)
                self.assertEqual(persistence.call_args.kwargs["created_by"], ADMIN_ID)

    def test_role_demotion_blocks_next_plan_even_with_cached_verified_token(self):
        token = make_token(subject=ADMIN_ID)
        fake = FakeSupabase(
            user={"id": ADMIN_ID},
            profile_rows=[{"id": ADMIN_ID, "role": "admin", "display_name": "Operator"}],
        )
        with ExitStack() as stack:
            self.mock_work(stack)
            stack.enter_context(patch.object(identity.transport, "request_json", fake))
            api_main.api_optimize_route(self.cases()[0][2], authorization=f"Bearer {token}")
        fake.profile_rows[0]["role"] = "driver"
        self.assert_denied(f"Bearer {token}", 403, fake)
        self.assertEqual(sum(call["path"] == "/auth/v1/user" for call in fake.calls), 1)

    def test_explicit_public_mode_preserves_guest_planning_without_auth_requests(self):
        for mode in (None, "", "disabled", " DISABLED "):
            with self.subTest(mode=mode), patch.dict(os.environ, {}, clear=True):
                if mode is not None:
                    os.environ["CROSSFLOW_AUTH_MODE"] = mode
                for name, handler, request, solver in self.cases():
                    with self.subTest(endpoint=name), ExitStack() as stack:
                        work = self.mock_work(stack)
                        transport = stack.enter_context(patch.object(identity.transport, "request_json"))
                        handler(request, authorization=None)
                        work[solver].assert_called_once()
                        self.assertIsNone(work["_persist_route_response"].call_args.kwargs["created_by"])
                        transport.assert_not_called()

    def test_unknown_auth_mode_cannot_silently_enable_guest_planning(self):
        for mode in ("supabse", "true", "driver"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"CROSSFLOW_AUTH_MODE": mode}):
                self.assert_denied(None, 503)

    def test_both_route_prefixes_share_the_protected_handlers(self):
        registered = {
            route.path: route.endpoint for route in api_main.app.routes
            if getattr(route, "path", None) is not None
        }
        for name, handler, _request, _solver in self.cases():
            with self.subTest(endpoint=name):
                self.assertIs(registered[f"/api/{name}"], handler)
                self.assertIs(registered[f"/{name}"], handler)

    def test_observed_backtest_checks_real_identity_before_cache_or_snapshot(self):
        for authorization, fake, status in (
            (None, None, 401),
            ("Bearer machine-only-secret", None, 401),
            (f"Bearer {make_token(role='admin')}", FakeSupabase(), 403),
        ):
            with self.subTest(status=status), ExitStack() as stack:
                transport = stack.enter_context(patch.object(identity.transport, "request_json", fake)) if fake else stack.enter_context(patch.object(identity.transport, "request_json"))
                cached = stack.enter_context(patch.object(api_main.observed_training_runtime.RUNTIME, "evaluate", return_value={"backtest": {"status": "cached"}}))
                read = stack.enter_context(patch.object(api_main.historical_store, "get_spatial_training_dataset"))
                with self.assertRaises(HTTPException) as caught:
                    api_main.api_observed_analytics_backtest(Response(), None, authorization)
                self.assertEqual(caught.exception.status_code, status)
                cached.assert_not_called()
                read.assert_not_called()
                if fake is None:
                    transport.assert_not_called()

    def test_verified_admin_may_read_observed_evaluation_without_mutation(self):
        fake = FakeSupabase(
            user={"id": ADMIN_ID},
            profile_rows=[{"id": ADMIN_ID, "role": "admin", "display_name": "Operator"}],
        )
        cached_report = {"backtest": {"status": "insufficient_observations", "model_replaced": False}}
        with patch.object(identity.transport, "request_json", fake), \
                patch.object(api_main.observed_training_runtime.RUNTIME, "evaluate", return_value=cached_report) as evaluate, \
                patch.object(api_main.forecaster, "retrain_from_observations") as mutate:
            result = api_main.api_observed_analytics_backtest(Response(), None, f"Bearer {make_token(subject=ADMIN_ID)}")
        self.assertEqual(result, cached_report)
        evaluate.assert_called_once()
        mutate.assert_not_called()

    def test_a_human_access_token_cannot_activate_the_model_or_use_machine_backtest(self):
        token = make_token(subject=ADMIN_ID, role="admin")
        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "machine-only-secret"}), \
                patch.object(api_main.historical_store, "get_spatial_training_dataset") as read, \
                patch.object(identity.transport, "request_json") as user_transport:
            for operation in (
                lambda: api_main.api_retrain_model(token),
                lambda: api_main.api_congestion_backtest(Response(), None, token),
            ):
                with self.subTest(operation=operation), self.assertRaises(HTTPException) as caught:
                    operation()
                self.assertEqual(caught.exception.status_code, 403)
            read.assert_not_called()
            user_transport.assert_not_called()
