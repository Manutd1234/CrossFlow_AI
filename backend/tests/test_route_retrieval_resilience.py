"""Driver retrieval must survive one failed storage backend.

Use a real closed SQLite connection to reproduce the local failure instead of
mocking the whole repository. The public dispatch-code lookup still needs to
consult shared storage, and failures must remain distinct from a healthy miss.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from services import route_run_store, route_store  # noqa: E402


class RouteRetrievalResilienceTests(unittest.TestCase):
    code = "ABCDEFG"

    def setUp(self) -> None:
        self.store = route_store.RouteStore(":memory:")
        self.addCleanup(self.store.close)
        replacement = patch.object(api_main, "_ROUTE_STORE", self.store)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_closed_local_database_still_serves_the_shared_route(self) -> None:
        self.store.close()
        stored = {"route_id": "a" * 64, "route_code": self.code}
        response = Response()
        with patch.object(route_run_store, "configured", return_value=True), patch.object(
            route_run_store, "get_lookup_result", return_value=(stored, True),
        ) as shared:
            result = api_main.api_get_route(self.code, response=response)
        self.assertEqual(result["route_code"], stored["route_code"])
        self.assertEqual(result["route_id"], stored["route_id"])
        self.assertTrue(result["dispatch_available"])
        self.assertEqual(result["route_storage"]["source"], "shared_supabase")
        shared.assert_called_once_with(self.code)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_failed_local_database_without_shared_route_is_retryable(self) -> None:
        self.store.close()
        with patch.object(route_run_store, "configured", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_get_route(self.code, response=Response())
        error = caught.exception
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.headers["Retry-After"], "5")
        self.assertIn("no-store", error.headers["Cache-Control"])
        self.assertNotIn("sqlite", error.detail.lower())

    def test_healthy_local_hit_does_not_depend_on_shared_store(self) -> None:
        stored = {"route_id": "a" * 64, "route_code": self.code}
        with patch.object(self.store, "get", return_value=stored), patch.object(
            route_run_store, "configured", side_effect=AssertionError("must not query"),
        ):
            result = api_main.api_get_route(self.code)
        self.assertEqual(result["route_code"], stored["route_code"])
        self.assertTrue(result["dispatch_available"])
        self.assertEqual(result["route_storage"]["source"], "local_sqlite")
        self.assertEqual(result["route_storage"]["scope"], "single_host")

    def test_true_missing_route_is_an_uncacheable_404(self) -> None:
        with patch.object(route_run_store, "configured", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_get_route(self.code, response=Response())
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])

    def test_unexpected_shared_read_failure_is_a_sanitized_503(self) -> None:
        with patch.object(route_run_store, "configured", return_value=True), patch.object(
            route_run_store, "get_lookup_result",
            side_effect=RuntimeError("secret backend URL"),
        ):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_get_route(self.code, response=Response())
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])
        self.assertNotIn("secret", caught.exception.detail)

    def test_shared_transport_outage_is_not_reported_as_missing(self) -> None:
        with patch.object(route_run_store, "configured", return_value=True), patch.object(
            route_run_store, "get_lookup_result", return_value=(None, False),
        ), patch.object(route_run_store, "available", return_value=True):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_get_route(self.code, response=Response())
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])

    def test_healthy_shared_miss_remains_404_despite_other_operation_failure(self) -> None:
        with patch.object(route_run_store, "configured", return_value=True), patch.object(
            route_run_store, "get_lookup_result", return_value=(None, True),
        ), patch.object(route_run_store, "available", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_get_route(self.code, response=Response())
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
