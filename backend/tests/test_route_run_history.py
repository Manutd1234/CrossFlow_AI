"""API-boundary tests for route-run history and driver route retrieval.

The two endpoints deliberately sit on opposite sides of the auth line: a single
route is readable by anyone holding its dispatch code, while history spans
accounts and must never be. These tests pin that split, and in particular that
a non-administrator cannot widen its own scope.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response


PROJECT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_DIR / "backend"
for path in (str(BACKEND_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from auth import identity  # noqa: E402
from services import route_run_store, route_store  # noqa: E402
from services.route_identity import route_id  # noqa: E402


def user(user_id: str, role: str) -> identity.AuthenticatedUser:
    return identity.AuthenticatedUser(
        id=user_id,
        role=role,
        display_name=role.title(),
        access_token="token",
        expires_at=None,
    )


class RouteRunHistoryScopeTests(unittest.TestCase):
    """Who may read whose runs."""

    def call_history(self, caller: identity.AuthenticatedUser, mine: bool = False):
        """Invoke the endpoint with the shared table stubbed as reachable."""
        captured: dict[str, object] = {}

        def fake_recent(limit: int, *, created_by: str | None = None):
            captured["limit"] = limit
            captured["created_by"] = created_by
            return []

        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(identity, "require_user", return_value=caller), \
                patch.object(route_run_store, "configured", return_value=True), \
                patch.object(route_run_store, "available", return_value=True), \
                patch.object(route_run_store, "recent", fake_recent):
            payload = api_main.api_route_history(
                response=None, limit=10, mine=mine, authorization="Bearer token",
            )
        body = payload.get("data", payload)
        return body, captured

    def test_administrator_sees_every_account(self) -> None:
        body, captured = self.call_history(user("admin-1", "admin"))
        self.assertEqual(body["scope"], "all_accounts")
        self.assertIsNone(captured["created_by"])

    def test_administrator_can_narrow_to_own_runs(self) -> None:
        body, captured = self.call_history(user("admin-1", "admin"), mine=True)
        self.assertEqual(body["scope"], "own_account")
        self.assertEqual(captured["created_by"], "admin-1")

    def test_driver_is_confined_to_own_runs(self) -> None:
        body, captured = self.call_history(user("driver-1", "driver"))
        self.assertEqual(body["scope"], "own_account")
        self.assertEqual(captured["created_by"], "driver-1")

    def test_driver_cannot_widen_scope_by_asking_for_everything(self) -> None:
        """`mine=False` is a narrowing hint for admins, never a privilege grant."""
        _, captured = self.call_history(user("driver-1", "driver"), mine=False)
        self.assertEqual(captured["created_by"], "driver-1")

    def test_history_requires_authentication(self) -> None:
        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(
                    identity,
                    "require_user",
                    side_effect=identity.auth_error(401, "The access token is not valid."),
                ):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_route_history(
                    response=None, limit=10, mine=False, authorization=None,
                )
        self.assertEqual(caught.exception.status_code, 401)

    def test_missing_table_is_reported_rather_than_shown_as_no_runs(self) -> None:
        """An empty list from a reachable-but-broken store is a lie.

        The store fails soft, so a project whose table was never created looks
        exactly like one where nobody has planned anything. The response must
        name the migration instead.
        """
        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(identity, "require_user", return_value=user("a", "admin")), \
                patch.object(route_run_store, "configured", return_value=True), \
                patch.object(route_run_store, "recent", return_value=[]), \
                patch.object(route_run_store, "available", return_value=False), \
                patch.object(route_run_store, "failure_reason", return_value="http_404"):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_route_history(
                    response=None, limit=10, mine=False, authorization="Bearer token",
                )
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("route_runs.sql", caught.exception.detail)
        self.assertIn("http_404", caught.exception.detail)

    def test_a_genuinely_empty_history_still_succeeds(self) -> None:
        """No runs yet is a 200 with an empty list, not an error."""
        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(identity, "require_user", return_value=user("a", "admin")), \
                patch.object(route_run_store, "configured", return_value=True), \
                patch.object(route_run_store, "recent", return_value=[]), \
                patch.object(route_run_store, "available", return_value=True):
            payload = api_main.api_route_history(
                response=None, limit=10, mine=False, authorization="Bearer token",
            )
        body = payload.get("data", payload)
        self.assertEqual(body["runs"], [])

    def test_history_reports_unconfigured_backend_rather_than_empty(self) -> None:
        """An empty list would read as "no runs"; the cause must stay visible."""
        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(identity, "require_user", return_value=user("a", "admin")), \
                patch.object(route_run_store, "configured", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_route_history(
                    response=None, limit=10, mine=False, authorization="Bearer token",
                )
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("not configured", caught.exception.detail)


class SharedStoreTransportTests(unittest.TestCase):
    """The store's REST contract with supabase_server.

    A malformed path fails soft, exactly like an unreachable project, so the
    only symptom is history silently staying empty. That is indistinguishable
    from "no runs yet" in the UI, so it is pinned here instead.
    """

    def test_rest_path_satisfies_the_transport_validator(self) -> None:
        from services import supabase_server

        self.assertIsNotNone(
            supabase_server._REST_PATH.fullmatch(route_run_store.REST_PATH),
            f"{route_run_store.REST_PATH!r} would be rejected as invalid_rest_path",
        )

    def test_rest_path_targets_the_documented_table(self) -> None:
        self.assertEqual(
            route_run_store.REST_PATH, f"/rest/v1/{route_run_store.TABLE_NAME}",
        )

    def test_save_atomically_records_the_public_route_and_account_association(self) -> None:
        from services import supabase_server

        account = "11111111-1111-4111-8111-111111111111"
        with patch.object(route_run_store, "_config", return_value=object()), \
                patch.object(supabase_server, "request_json", return_value=None) as request:
            self.assertTrue(route_run_store.save(
                "a" * 64, "ABCDEFG", "optimize-route", {"weather": 0},
                {"route_code": "ABCDEFG"}, created_by=account,
            ))
        self.assertEqual(request.call_count, 1)
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["rest_path"], "/rest/v1/rpc/crossflow_save_route_run")
        self.assertEqual(kwargs["payload"]["p_run"]["created_by"], account)
        self.assertEqual(kwargs["payload"]["p_run"]["route_id"], "a" * 64)

    def test_history_filters_per_account_associations_or_deduplicates_admin_routes(self) -> None:
        account = "11111111-1111-4111-8111-111111111111"
        with patch.object(route_run_store, "_request", return_value=[]) as request:
            route_run_store.recent(10, created_by=account)
            scoped = request.call_args.kwargs
            self.assertEqual(scoped["rest_path"], "/rest/v1/crossflow_route_run_history")
            self.assertEqual(scoped["query"]["created_by"], f"eq.{account}")
            self.assertNotIn("account_rank", scoped["query"])
            route_run_store.recent(10)
            unscoped = request.call_args.kwargs
            self.assertEqual(unscoped["query"]["account_rank"], "eq.1")
            self.assertNotIn("created_by", unscoped["query"])

    def test_shared_history_paths_pass_the_server_transport_validator(self) -> None:
        from services import supabase_server

        for path in (route_run_store.SAVE_RPC_PATH, route_run_store.HISTORY_PATH):
            self.assertIsNotNone(supabase_server._REST_PATH.fullmatch(path))

    def test_save_reports_success_for_an_empty_204_body(self) -> None:
        """``return=minimal`` answers 204 with no body.

        Treating an absent payload as failure reported every successful write
        as a failure, which is only invisible because the caller ignores the
        result and the row lands anyway.
        """
        from services import supabase_server

        with patch.object(route_run_store, "_config", return_value=object()), \
                patch.object(supabase_server, "request_json", return_value=None):
            self.assertTrue(route_run_store.save(
                "a" * 64, "ABCDEFG", "optimize-route", {}, {},
            ))

    def test_save_reports_failure_when_the_transport_rejects_it(self) -> None:
        from services import supabase_server

        with patch.object(route_run_store, "_config", return_value=object()), \
                patch.object(
                    supabase_server, "request_json",
                    side_effect=supabase_server.SupabaseServerError("http_404"),
                ):
            self.assertFalse(route_run_store.save(
                "a" * 64, "ABCDEFG", "optimize-route", {}, {},
            ))
        self.assertEqual(route_run_store.failure_reason(), "http_404")

    def test_store_reports_unconfigured_rather_than_raising(self) -> None:
        """A missing Supabase project must never break route planning."""
        with patch.object(route_run_store, "_config", return_value=None):
            self.assertFalse(route_run_store.save(
                "a" * 64, "ABCDEFG", "optimize-route", {}, {},
            ))
            self.assertIsNone(route_run_store.get("a" * 64))
            self.assertEqual(route_run_store.recent(), [])

    def test_get_rejects_keys_that_are_neither_id_nor_code(self) -> None:
        for bad_key in ("", "short", "x" * 63, 12345):
            self.assertIsNone(route_run_store.get(bad_key))  # type: ignore[arg-type]

    def test_lookup_distinguishes_a_healthy_miss_from_store_failure(self) -> None:
        from services import supabase_server

        with patch.object(route_run_store, "_config", return_value=object()), \
                patch.object(supabase_server, "request_json", return_value=[]):
            self.assertEqual(route_run_store.get_lookup_result("ABCDEFG"), (None, True))
        with patch.object(route_run_store, "_config", return_value=object()), \
                patch.object(
                    supabase_server, "request_json",
                    side_effect=supabase_server.SupabaseServerError("network_error"),
                ):
            self.assertEqual(route_run_store.get_lookup_result("ABCDEFG"), (None, False))

    def test_lookup_result_does_not_depend_on_another_requests_diagnostic(self) -> None:
        expected = {"route_code": "ABCDEFG"}

        def concurrent_failure(**_kwargs):
            route_run_store._mark_failure("concurrent_request_failed")
            return [{"response": expected}]

        with patch.object(route_run_store, "_request", concurrent_failure):
            self.assertEqual(route_run_store.get_lookup_result("ABCDEFG"), (expected, True))
        with patch.object(route_run_store, "_request", return_value=[{"response": None}]):
            self.assertEqual(route_run_store.get_lookup_result("ABCDEFG"), (None, False))


class LocalRouteHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = route_store.RouteStore(":memory:")
        self.addCleanup(self.store.close)
        replacement = patch.object(api_main, "_ROUTE_STORE", self.store)
        replacement.start()
        self.addCleanup(replacement.stop)

    def seed(self, label: str, account: str | None, store=None) -> str:
        selected = store or self.store
        request = {"route": label}
        key = route_id(request)
        selected.save(
            key, {"route_id": key}, request=request, route_kind="optimize-route",
            created_by=account, origin_name=f"{label} origin",
            destination_name=f"{label} destination", vehicle_type="COMMUTER",
        )
        return key

    def history(self, caller, mine=False, **environment):
        response = Response()
        with patch.dict(os.environ, {
            "CROSSFLOW_LOCAL_ROUTE_HISTORY": "1", **environment,
        }, clear=True), patch.object(
            identity, "auth_enabled", return_value=True,
        ), patch.object(
            identity, "require_user", return_value=caller,
        ), patch.object(route_run_store, "configured", return_value=False):
            result = api_main.api_route_history(
                response=response, limit=20, mine=mine,
                authorization="Bearer verified-token",
            )
        self.assertIn("no-store", response.headers["Cache-Control"])
        return result

    def test_local_driver_history_excludes_other_accounts_and_guests(self) -> None:
        own = self.seed("own", "driver-1")
        self.seed("other", "driver-2")
        self.seed("guest", None)
        body = self.history(user("driver-1", "driver"))
        self.assertEqual(body["scope"], "own_account")
        self.assertEqual([run["route_id"] for run in body["runs"]], [own])
        self.assertEqual(body["history_storage"], {
            "source": "local_sqlite", "shared": False, "durable": False,
            "durability": "process_memory", "scope": "single_host",
        })

    def test_admin_history_can_be_narrowed_to_own_account(self) -> None:
        own = self.seed("own", "admin-1")
        self.seed("other", "driver-2")
        self.seed("guest", None)
        all_runs = self.history(user("admin-1", "admin"))
        self.assertEqual(all_runs["scope"], "all_accounts")
        self.assertEqual(len(all_runs["runs"]), 3)
        own_runs = self.history(user("admin-1", "admin"), mine=True)
        self.assertEqual([run["route_id"] for run in own_runs["runs"]], [own])

    def test_identical_routes_preserve_each_account_history_and_dedupe_admin(self) -> None:
        key = self.seed("shared path", "driver-1")
        self.seed("shared path", "driver-2")
        self.seed("shared path", None)
        self.seed("shared path", "driver-1")
        for account in ("driver-1", "driver-2"):
            runs = self.store.recent(created_by=account)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["route_id"], key)
            self.assertEqual(runs[0]["created_by"], account)
        self.assertEqual(len(self.store.recent()), 1)
        self.assertEqual(self.store.recent(created_by="' OR 1=1 --"), [])

    def test_local_history_survives_file_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "routes.db")
            first = route_store.RouteStore(path)
            key = self.seed("persisted", "driver-1", store=first)
            code = first.recent(created_by="driver-1")[0]["route_code"]
            first.close()
            reopened = route_store.RouteStore(path)
            try:
                self.assertEqual(reopened.get(code)["route_id"], key)
                self.assertEqual(reopened.recent(created_by="driver-1")[0]["route_id"], key)
            finally:
                reopened.close()

    def test_route_response_persists_verified_attribution_and_display_names(self) -> None:
        request = api_main.RouteRequest(
            corridor_id="corridor-1", vehicle_type="COMMUTER",
        )
        with patch.object(route_run_store, "save", return_value=False):
            planned = api_main._persist_route_response(
                request, "optimize-route", {
                    "route_type": "ROAD_ROUTE",
                    "requested_origin": {"name": "Batamindo Industrial Park"},
                    "requested_destination": {"name": "Batam Centre Ferry Terminal"},
                    "route_geometry": [[1.06, 104.03], [1.13, 104.05]],
                }, datetime.now(timezone.utc), created_by="driver-1",
            )
        runs = self.store.recent(created_by="driver-1")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["route_code"], planned["route_code"])
        self.assertEqual(runs[0]["origin_name"], "Batamindo Industrial Park")
        self.assertEqual(runs[0]["destination_name"], "Batam Centre Ferry Terminal")
        self.assertEqual(runs[0]["vehicle_type"], "COMMUTER")

    def test_local_history_never_enables_serverless_partial_history(self) -> None:
        for environment in ({"VERCEL": "1"}, {"AWS_LAMBDA_FUNCTION_NAME": "api"}):
            with self.subTest(environment=environment), self.assertRaises(HTTPException) as caught:
                self.history(user("admin-1", "admin"), **environment)
            self.assertEqual(caught.exception.status_code, 503)

    def test_configured_shared_store_failure_does_not_silently_fall_back(self) -> None:
        self.seed("local only", "admin-1")
        with patch.dict(os.environ, {"CROSSFLOW_LOCAL_ROUTE_HISTORY": "1"}), \
                patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(identity, "require_user", return_value=user("admin-1", "admin")), \
                patch.object(route_run_store, "configured", return_value=True), \
                patch.object(route_run_store, "recent", return_value=[]), \
                patch.object(route_run_store, "available", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_route_history(
                    response=None, limit=20, mine=False, authorization="Bearer token",
                )
        self.assertEqual(caught.exception.status_code, 503)


class DriverRouteRetrievalTests(unittest.TestCase):
    """A dispatch code is the capability, and grants exactly one route."""

    def planned_route_code(self) -> str:
        request = api_main.RouteRequest(
            corridor_id="corridor-1", vehicle_type="COMMUTER", weather=0, hour=8,
        )
        return api_main.api_optimize_route(request)["route_code"]

    def test_code_holder_may_read_without_any_credential(self) -> None:
        code = self.planned_route_code()
        stored = api_main.api_get_route(route_id=code, response=None)
        self.assertEqual(stored["route_code"], code)

    def test_unknown_code_is_not_found_rather_than_forbidden(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            api_main.api_get_route(route_id="2222222", response=None)
        self.assertEqual(caught.exception.status_code, 404)

    def test_shared_table_serves_a_code_this_instance_never_stored(self) -> None:
        """The per-instance SQLite file is empty on a fresh serverless worker."""
        envelope = {"route_code": "VQT4QX8", "route_id": "b" * 64}
        with patch.object(api_main._ROUTE_STORE, "get", return_value=None), \
                patch.object(route_run_store, "configured", return_value=True), \
                patch.object(route_run_store, "get_lookup_result", return_value=(envelope, True)) as shared:
            stored = api_main.api_get_route(route_id="VQT4QX8", response=None)
        shared.assert_called_once_with("VQT4QX8")
        self.assertEqual(stored["route_code"], "VQT4QX8")


if __name__ == "__main__":
    unittest.main()
