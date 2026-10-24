"""Integration diagnostics must be useful without leaking or probing services."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from services import historical_store, route_store, runtime_status, supabase_server, traffic_upstream  # noqa: E402
from services import shortcut_ingestion  # noqa: E402
from services.route_learning_store import RouteLearningStore  # noqa: E402


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = route_store.RouteStore(":memory:")
        self.addCleanup(self.routes.close)
        self.learning = RouteLearningStore(":memory:")
        self.addCleanup(self.learning.close)
        self.metrics = {
            "is_trained": True,
            "training_data_source": "synthetic_profile_generator",
            "validation_scope": "synthetic_holdout",
            "total_samples": 4000,
            "observed_training_rows": 0,
            "observed_fit_rows": 0,
            "calibration": {
                "interval_coverage_target": 0.8,
                "interval_coverage_observed": 0.6613,
            },
        }

    def snapshot(self, **overrides):
        return runtime_status.snapshot(**{
            "route_store": self.routes,
            "route_learning_store": self.learning,
            "graph_revision": "a" * 64,
            "graph_meta": {"schema_version": 2, "node_count": 100},
            "model_metrics": self.metrics,
            "intelligence_status": {
                "enabled": False, "configured": False,
                "implementation": "local_sqlite_and_process_review_queue",
            },
            **overrides,
        })

    def test_configured_keys_never_become_a_live_data_or_login_claim(self) -> None:
        environment = {
            "CROSSFLOW_AUTH_MODE": "supabase",
            "SUPABASE_URL": "https://test-project.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_not-for-diagnostics",
            "SUPABASE_SECRET_KEY": "sb_secret_not-for-diagnostics",
            "TOMTOM_API_KEY": "tomtom-not-for-diagnostics",
            "CROSSFLOW_ENABLE_GOOGLE_BENCHMARK": "true",
            "CROSSFLOW_GOOGLE_ROUTES_API_KEY": "google-not-for-diagnostics",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            supabase_server, "request_json", side_effect=AssertionError("no network"),
        ):
            result = self.snapshot()
        self.assertTrue(result["authentication"]["configured"])
        self.assertTrue(result["persistence"]["shared_routes"]["configured"])
        for provider in ("tomtom", "google_route_benchmark"):
            self.assertTrue(result["providers"][provider]["configured"])
            self.assertEqual(result["providers"][provider]["current_response_source"], "not_sampled")
        self.assertFalse(result["additional_remote_probes_performed"])
        self.assertEqual(result["production_readiness"], "not_assessed")
        serialized = json.dumps(result)
        for secret in environment.values():
            if "not-for-diagnostics" in secret or "supabase.co" in secret:
                self.assertNotIn(secret, serialized)
        for direct_key, expected_source in (
            ("", "public_crossflow_vercel_upstream"),
            ("direct-not-for-diagnostics", "direct_tomtom"),
        ):
            with self.subTest(source=expected_source), patch.dict(os.environ, {
                traffic_upstream.ENV_NAME: traffic_upstream.TRUSTED_URL,
                "TOMTOM_API_KEY": direct_key,
            }, clear=True), patch.object(
                traffic_upstream, "_read_public_snapshot", side_effect=AssertionError("metadata must not fetch"),
            ):
                relay = self.snapshot()["providers"]["tomtom"]
                self.assertTrue(relay["upstream_configured"])
                self.assertEqual(relay["configured_source"], expected_source)
                self.assertEqual(relay["current_response_source"], "not_sampled")
                self.assertEqual(relay["upstream_retention"], "process_memory_current_snapshot_only")

    def test_private_upstream_metadata_is_allowlisted_out_of_every_component(self) -> None:
        unsafe = {"path": "/private/customer.db", "credential": "private-key-value"}
        routes = SimpleNamespace(storage_metadata=lambda: {
            **unsafe, "source": "local_sqlite", "durable": False,
        })
        learning = SimpleNamespace(status=lambda _graph: {
            **unsafe, "storage_backend": "sqlite", "storage_warning": unsafe["path"],
        })
        with patch.object(historical_store, "get_storage_metadata", return_value=unsafe):
            result = self.snapshot(
                route_store=routes, route_learning_store=learning,
                model_metrics={**self.metrics, **unsafe, "calibration": unsafe},
                intelligence_status=unsafe,
            )
        serialized = json.dumps(result)
        self.assertNotIn(unsafe["path"], serialized)
        self.assertNotIn(unsafe["credential"], serialized)
        self.assertEqual(result["persistence"]["local_routes"]["source"], "local_sqlite")

    def test_one_failed_database_does_not_hide_other_integration_metadata(self) -> None:
        self.learning.close()
        result = self.snapshot()
        self.assertFalse(result["persistence"]["verified_traversals"]["metadata_available"])
        self.assertEqual(
            result["persistence"]["verified_traversals"]["failure"],
            "metadata_unavailable",
        )
        self.assertTrue(result["persistence"]["local_routes"]["metadata_available"])
        self.assertEqual(result["model"]["observed_training_rows"], 0)
        self.assertEqual(result["model"]["validation_scope"], "synthetic_holdout")
        self.assertEqual(result["model"]["calibration"]["interval_coverage_observed"], 0.6613)

    def test_memory_storage_and_disabled_auth_remain_explicit(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = self.snapshot()
        self.assertFalse(result["authentication"]["enabled"])
        self.assertFalse(result["authentication"]["configured"])
        self.assertFalse(result["persistence"]["local_routes"]["durable"])
        self.assertEqual(result["persistence"]["local_routes"]["durability"], "process_memory")
        self.assertFalse(result["providers"]["ferry_operations"]["live_operations_available"])

    def test_existing_status_endpoint_includes_snapshot_behind_the_admin_boundary(self) -> None:
        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "token"}), patch.object(
            api_main, "_shortcut_runtime",
            return_value=(shortcut_ingestion.SourcePolicy(), object()),
        ), patch.object(
            api_main.router, "_main_routing_core", return_value=(1, 2),
        ), patch.object(
            api_main.routing_intelligence_store, "shared_store_enabled", return_value=False,
        ):
            response = Response()
            result = api_main.api_routing_intelligence_status(response, "token")
            self.assertEqual(result["integration_status"]["contract_version"], 1)
            self.assertIn("no-store", response.headers["Cache-Control"])
            with patch.object(runtime_status, "snapshot") as capture:
                with self.assertRaises(HTTPException) as caught:
                    api_main.api_routing_intelligence_status(Response(), None)
            self.assertEqual(caught.exception.status_code, 403)
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
