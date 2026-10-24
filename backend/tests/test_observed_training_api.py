"""Human read-only evaluation and machine-only model activation boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.requests import Request

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api
from services import observed_training_runtime as runtime
from services.traffic_observations import SpatialTrafficObservation

NOW = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)


def observation():
    return SpatialTrafficObservation.create(
        corridor_id="corridor-1", observed_at=NOW - timedelta(minutes=30),
        latitude=1.10, longitude=104.0, actual_speed_kph=25,
        free_flow_speed_kph=50, source="tomtom_live",
        upstream_event_id="observed-api-boundary", validation_now=NOW,
    )


class ObservedEvaluationRuntimeTests(unittest.TestCase):
    def test_cache_does_not_allow_callers_to_mutate_future_results(self):
        clock = [0.0]
        cache = runtime.ObservedEvaluationRuntime(monotonic=lambda: clock[0])
        build = Mock(return_value={"backtest": {"status": "insufficient"}})
        first = cache.evaluate("local", build)
        first["backtest"]["status"] = "forged"
        self.assertEqual(cache.evaluate("local", build)["backtest"]["status"], "insufficient")
        self.assertEqual(build.call_count, 1)
        clock[0] = 60.0
        cache.evaluate("local", build)
        self.assertEqual(build.call_count, 2)

    def test_store_change_and_invalidation_do_not_reuse_old_report(self):
        cache = runtime.ObservedEvaluationRuntime()
        build = Mock(return_value={"status": "ok"})
        cache.evaluate("local", build)
        cache.evaluate("shared", build)
        cache.invalidate()
        cache.evaluate("shared", build)
        self.assertEqual(build.call_count, 3)

    def test_failure_releases_slot_and_is_not_cached(self):
        cache = runtime.ObservedEvaluationRuntime()
        with self.assertRaises(ValueError):
            cache.evaluate("local", Mock(side_effect=ValueError("invalid")))
        self.assertEqual(cache.evaluate("local", lambda: {"ok": True}), {"ok": True})

    def test_concurrent_fit_is_rejected_without_running_second_builder(self):
        cache = runtime.ObservedEvaluationRuntime()
        entered, release = threading.Event(), threading.Event()

        def build():
            entered.set()
            self.assertTrue(release.wait(5))
            return {"ok": True}

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(cache.evaluate, "local", build)
            self.assertTrue(entered.wait(5))
            second = Mock()
            try:
                with self.assertRaises(runtime.EvaluationBusy):
                    cache.evaluate("local", second)
                second.assert_not_called()
            finally:
                release.set()
            self.assertTrue(first.result()["ok"])


class ObservedTrainingApiTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch.object(runtime, "RUNTIME", runtime.ObservedEvaluationRuntime()),
            patch.object(api.routing_intelligence_store, "shared_store_enabled", return_value=False),
            patch.object(api.clock, "now", return_value=NOW),
        ):
            context.start()
            self.addCleanup(context.stop)

    def test_denied_roles_never_read_or_fit(self):
        for status in (401, 403, 503):
            with self.subTest(status=status), patch.object(api.identity, "require_route_operator", side_effect=api.identity.auth_error(status, "Denied")), patch.object(api.historical_store, "get_spatial_training_dataset") as read:
                with self.assertRaises(HTTPException) as caught:
                    api.api_observed_analytics_backtest(Response(), None, "Bearer test")
                self.assertEqual(caught.exception.status_code, status)
                self.assertIn("no-store", caught.exception.headers["Cache-Control"])
                read.assert_not_called()

    def test_local_snapshot_has_fixed_bounds_no_model_mutation_and_private_cache(self):
        from services import observed_training
        report = {"status": "insufficient_observations", "model_replaced": False, "selection": {"excluded_non_observed_rows": 0}}
        with patch.object(api.identity, "require_route_operator") as guard, patch.object(api.historical_store, "get_spatial_training_dataset", return_value=[observation()]) as read, patch.object(observed_training, "evaluate_observed_training", return_value=report) as evaluate, patch.object(api.forecaster, "retrain_from_observations") as replace:
            response = Response()
            result = api.api_observed_analytics_backtest(response, None, "Bearer test")
        guard.assert_called_once_with("Bearer test")
        read.assert_called_once_with(limit=50000, now=NOW)
        self.assertEqual(evaluate.call_args.kwargs, {"now": NOW, "max_rows": 50000})
        replace.assert_not_called()
        self.assertFalse(result["backtest"]["model_replaced"])
        self.assertEqual(result["evaluation_snapshot"]["durability"], "local_sqlite")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_http_body_cannot_override_data_training_or_work_policy(self):
        for payload in ({"include_modelled": True}, {"records": []}, {"activate": True}, {"max_rows": 100000000}, {"horizons_minutes": [1]}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                api.CongestionBacktestRequest(**payload)
        request = Request({"type": "http", "path": "/api/analytics/backtest", "method": "POST", "headers": []})
        response = asyncio.run(api._private_backtest_validation_error(
            request, RequestValidationError([{"type": "extra_forbidden", "loc": ["body", "activate"], "msg": "Extra inputs are not permitted", "input": True}]),
        ))
        self.assertEqual(response.status_code, 422)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_shared_failure_is_not_replaced_with_local_or_private_error(self):
        with patch.object(api.identity, "require_route_operator"), patch.object(api.routing_intelligence_store, "shared_store_enabled", return_value=True), patch.object(api, "_shared_routing_store_required", side_effect=RuntimeError("private-key-and-path")), patch.object(api.historical_store, "get_spatial_training_dataset") as local:
            with self.assertRaises(HTTPException) as caught:
                api.api_observed_analytics_backtest(Response())
            self.assertEqual(caught.exception.status_code, 503)
            self.assertNotIn("private-key", caught.exception.detail)
            self.assertIn("no-store", caught.exception.headers["Cache-Control"])
            local.assert_not_called()

    def test_corrupt_snapshot_is_sanitized_without_fake_accuracy(self):
        from services import observed_training
        with patch.object(api.identity, "require_route_operator"), patch.object(api.historical_store, "get_spatial_training_dataset", return_value=[]), patch.object(observed_training, "evaluate_observed_training", side_effect=ValueError("private row identity")):
            with self.assertRaises(HTTPException) as caught:
                api.api_observed_analytics_backtest(Response())
            self.assertEqual(caught.exception.status_code, 409)
            self.assertNotIn("private", caught.exception.detail)

    def test_busy_evaluation_returns_retryable_uncacheable_429(self):
        with patch.object(api.identity, "require_route_operator"), patch.object(runtime.RUNTIME, "evaluate", side_effect=runtime.EvaluationBusy()):
            with self.assertRaises(HTTPException) as caught:
                api.api_observed_analytics_backtest(Response())
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(caught.exception.headers["Retry-After"], "5")
            self.assertIn("no-store", caught.exception.headers["Cache-Control"])

    def test_bad_store_iterator_cannot_remove_snapshot_bound(self):
        consumed = [0]

        def unbounded():
            while True:
                consumed[0] += 1
                yield observation()

        with patch.object(api.identity, "require_route_operator"), patch.object(api, "MAX_OBSERVED_ANALYTICS_ROWS", 3), patch.object(api.historical_store, "get_spatial_training_dataset", return_value=unbounded()):
            with self.assertRaises(HTTPException) as caught:
                api.api_observed_analytics_backtest(Response())
            self.assertEqual(consumed[0], 4)
            self.assertEqual(caught.exception.status_code, 503)

    def test_machine_guard_handles_unicode_and_never_exposes_diagnostics(self):
        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "secret"}):
            for supplied in (None, "wrong", "non-ascii-\u00e9"):
                with self.subTest(supplied=supplied), self.assertRaises(HTTPException) as caught:
                    api._require_admin(supplied)
                self.assertEqual(caught.exception.status_code, 403)
                self.assertIn("no-store", caught.exception.headers["Cache-Control"])

    def test_missing_configured_artifact_is_visible_without_creating_one(self):
        from tempfile import TemporaryDirectory
        old_status = api._OBSERVED_ARTIFACT_STATUS
        self.addCleanup(setattr, api, "_OBSERVED_ARTIFACT_STATUS", old_status)
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"CROSSFLOW_OBSERVED_MODEL_PATH": directory + "/missing.json"}):
            api._restore_observed_model_if_configured()
            result = api.api_model_status()["metrics"]["observed_artifact_status"]
        self.assertEqual(result["status"], "waiting_for_eligible_model")
        self.assertTrue(result["configured"])
        self.assertFalse(result["restored"])
        self.assertNotIn("path", result)

    def test_failed_restore_preserves_live_revision_and_reports_failure(self):
        from tempfile import TemporaryDirectory
        from services import observed_model_artifact
        old_status = api._OBSERVED_ARTIFACT_STATUS
        self.addCleanup(setattr, api, "_OBSERVED_ARTIFACT_STATUS", old_status)
        before = api.forecaster.model
        with TemporaryDirectory() as directory:
            artifact = Path(directory) / "corrupt.json"
            artifact.write_text("not-json")
            with patch.dict(os.environ, {"CROSSFLOW_OBSERVED_MODEL_PATH": str(artifact)}), patch.object(observed_model_artifact, "restore_observed_model", side_effect=ValueError("private artifact path")):
                api._restore_observed_model_if_configured()
        self.assertIs(api.forecaster.model, before)
        self.assertEqual(api._OBSERVED_ARTIFACT_STATUS["status"], "restore_failed_previous_model_retained")

    def test_successful_restore_has_matching_provenance_without_exposing_path(self):
        from tempfile import TemporaryDirectory
        from services import observed_model_artifact
        old_status = api._OBSERVED_ARTIFACT_STATUS
        self.addCleanup(setattr, api, "_OBSERVED_ARTIFACT_STATUS", old_status)
        with TemporaryDirectory() as directory:
            artifact = Path(directory) / "model.json"
            artifact.write_text("{}")
            with patch.dict(os.environ, {"CROSSFLOW_OBSERVED_MODEL_PATH": str(artifact)}), patch.object(observed_model_artifact, "restore_observed_model", return_value={"restored": True, "sha256": "a" * 64, "format": "crossflow_observed_rf_numeric_v1"}) as restore:
                api._restore_observed_model_if_configured()
            restore.assert_called_once_with(api.forecaster, artifact)
        self.assertEqual(api._OBSERVED_ARTIFACT_STATUS["status"], "restored")
        self.assertTrue(api._OBSERVED_ARTIFACT_STATUS["restored"])


if __name__ == "__main__":
    unittest.main()
